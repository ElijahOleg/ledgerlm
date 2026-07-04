"""Phase 1.5: streamed calls recorded as faithfully as non-streamed ones."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

import ledgerlm
from conftest import Ledger
from ledgerlm.db.models import LlmEvent
from ledgerlm.providers.mock import (
    MockAsyncStream,
    MockLLMClient,
    MockUsage,
    _stream_events,
)
from ledgerlm.providers.openai import CHAT_PATH, OpenAIAdapter
from ledgerlm.recorder import Recorder
from ledgerlm.streaming import RecordingStream, RecordingStreamManager
from ledgerlm.wrapper import _stream_finisher

MESSAGES = [{"role": "user", "content": "stream me"}]
USAGE = MockUsage(
    input_tokens=150_000,
    output_tokens=20_000,
    cache_read_tokens=200_000,
    cache_write_tokens=50_000,
)


def _events(ledger: Ledger) -> list[LlmEvent]:
    with ledger.session_factory() as session:
        return list(session.execute(select(LlmEvent).order_by(LlmEvent.id)).scalars().all())


class TestMockStreamedVsNonStreamed:
    def test_identical_usage_and_cost(self, ledger: Ledger) -> None:
        client = ledgerlm.wrap(MockLLMClient(usage=USAGE))
        with ledgerlm.tags(project="stream-parity"):
            plain = client.messages.create(model="mock-model", messages=MESSAGES)
            stream = client.messages.create(model="mock-model", messages=MESSAGES, stream=True)
            consumed = list(stream)

        # Caller-visible stream: message_start, N content deltas, message_delta, message_stop
        assert [e.type for e in consumed[:1]] == ["message_start"]
        assert consumed[-1].type == "message_stop"
        assert plain.usage.input_tokens == 150_000

        events = _events(ledger)
        assert len(events) == 2
        non_streamed, streamed = events
        assert streamed.status == "ok"
        # Identical normalized usage...
        for column in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            assert getattr(streamed, column) == getattr(non_streamed, column), column
        # ...and identical cost (hand math: 150k@$3/M + 200k@$0.30/M + 50k@$3.75/M
        # + 20k@$15/M = 0.45 + 0.06 + 0.1875 + 0.30 = $0.9975)
        assert streamed.cost_usd == non_streamed.cost_usd
        assert str(streamed.cost_usd) == "0.9975000000"
        # first_token_ms recorded for streams only
        assert non_streamed.first_token_ms is None
        assert streamed.first_token_ms is not None
        assert 0 <= streamed.first_token_ms <= streamed.latency_ms
        assert streamed.prompt_hash == non_streamed.prompt_hash

    def test_abandoned_stream_records_partial_row(self, ledger: Ledger) -> None:
        client = ledgerlm.wrap(MockLLMClient(usage=USAGE))
        stream = client.messages.create(model="mock-model", messages=MESSAGES, stream=True)
        next(stream)  # message_start
        next(stream)  # first content
        stream.close()  # abandoned before completion

        (event,) = _events(ledger)
        assert event.status == "error"
        assert event.error_type == "stream_abandoned"
        # Whatever usage is known: input side from message_start; no output seen
        assert event.input_tokens == 150_000
        assert event.cache_read_tokens == 200_000
        assert event.output_tokens == 0
        assert "message_delta" not in event.raw_usage

    def test_with_block_exit_before_completion_is_abandoned(self, ledger: Ledger) -> None:
        client = ledgerlm.wrap(MockLLMClient(usage=USAGE))
        with client.messages.create(model="mock-model", messages=MESSAGES, stream=True) as stream:
            next(stream)

        (event,) = _events(ledger)
        assert event.error_type == "stream_abandoned"

    def test_fully_consumed_with_block_records_ok_once(self, ledger: Ledger) -> None:
        client = ledgerlm.wrap(MockLLMClient(usage=USAGE))
        with client.messages.create(model="mock-model", messages=MESSAGES, stream=True) as stream:
            list(stream)

        (event,) = _events(ledger)  # exactly one row despite exit after exhaustion
        assert event.status == "ok"


class AsyncStreamMockClient(MockLLMClient):
    """Async-SDK-shaped mock: create is a coroutine; stream=True yields an async stream."""

    @property
    def messages(self) -> Any:
        outer = self

        class _M:
            async def create(self, **kwargs: Any) -> Any:
                outer.calls.append(kwargs)
                model = str(kwargs.get("model", outer.model))
                assert kwargs.get("stream")
                return MockAsyncStream(_stream_events(model, outer.response_text, outer.usage))

        return _M()


async def test_async_streaming_recorded(ledger: Ledger) -> None:
    client = ledgerlm.wrap(AsyncStreamMockClient(usage=USAGE))
    with ledgerlm.tags(project="async-stream"):
        stream = await client.messages.create(model="mock-model", messages=MESSAGES, stream=True)
        events = [e async for e in stream]

    assert events[-1].type == "message_stop"
    (event,) = _events(ledger)
    assert event.status == "ok"
    assert event.project == "async-stream"
    assert str(event.cost_usd) == "0.9975000000"
    assert event.first_token_ms is not None


async def test_async_abandoned_via_aclose(ledger: Ledger) -> None:
    client = ledgerlm.wrap(AsyncStreamMockClient(usage=USAGE))
    stream = await client.messages.create(model="mock-model", messages=MESSAGES, stream=True)
    await stream.__anext__()
    await stream.aclose()

    (event,) = _events(ledger)
    assert event.error_type == "stream_abandoned"
    assert event.input_tokens == 150_000


def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _chat_chunks(with_usage_chunk: bool) -> list[SimpleNamespace]:
    chunks = [
        ns(model="gpt-5.4", choices=[ns(delta=ns(content="Hel"))], usage=None),
        ns(model="gpt-5.4", choices=[ns(delta=ns(content="lo"))], usage=None),
    ]
    if with_usage_chunk:
        chunks.append(
            ns(
                model="gpt-5.4",
                choices=[],
                usage=ns(
                    prompt_tokens=1000,
                    completion_tokens=200,
                    prompt_tokens_details=ns(cached_tokens=600),
                ),
            )
        )
    return chunks


class _Sink:
    """Captures the finish callback's arguments in place of a real recorder."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def record(self, event: Any) -> None:
        self.calls.append(event)


class TestOpenAIChatSwallowing:
    def _run(self, caller_opted_in: bool) -> tuple[list[Any], Any]:
        adapter = OpenAIAdapter()
        kwargs: dict[str, Any] = {"model": "gpt-5.4", "messages": MESSAGES, "stream": True}
        if caller_opted_in:
            kwargs["stream_options"] = {"include_usage": True}
        prepared, collector = adapter.prepare_stream(dict(kwargs), CHAT_PATH)
        assert prepared["stream_options"] == {"include_usage": True}
        assert collector is not None
        sink = _Sink()
        recorder = Recorder()
        recorder.record = sink.record  # type: ignore[method-assign]
        stream = RecordingStream(
            iter(_chat_chunks(with_usage_chunk=True)),
            collector,
            _stream_finisher(adapter, recorder, CHAT_PATH, kwargs, collector),
            start=0.0,
        )
        yielded = list(stream)
        (event,) = sink.calls
        return yielded, event

    def test_injected_usage_chunk_is_swallowed(self) -> None:
        yielded, event = self._run(caller_opted_in=False)
        # Caller never sees a chunk with empty choices it didn't opt into
        assert len(yielded) == 2
        assert all(chunk.choices for chunk in yielded)
        # ...but the usage was recorded, with the cached-token subtraction applied
        assert event.usage.input_tokens == 400  # 1000 - 600 cached
        assert event.usage.cache_read_tokens == 600
        assert event.usage.output_tokens == 200
        assert event.status == "ok"

    def test_caller_opted_in_chunk_passes_through(self) -> None:
        yielded, event = self._run(caller_opted_in=True)
        assert len(yielded) == 3  # usage chunk visible, exactly as the SDK sends it
        assert yielded[-1].usage is not None
        assert event.usage.input_tokens == 400


def test_responses_stream_usage_from_terminal_event() -> None:
    from ledgerlm.providers.openai import RESPONSES_PATH

    adapter = OpenAIAdapter()
    kwargs: dict[str, Any] = {"model": "gpt-5.4", "input": "hi", "stream": True}
    prepared, collector = adapter.prepare_stream(dict(kwargs), RESPONSES_PATH)
    assert "stream_options" not in prepared  # nothing injected on this path
    assert collector is not None
    events = [
        ns(type="response.created", response=ns(model="gpt-5.4", usage=None)),
        ns(type="response.output_text.delta", delta="Hel"),
        ns(type="response.output_text.delta", delta="lo"),
        ns(
            type="response.completed",
            response=ns(
                model="gpt-5.4",
                usage=ns(
                    input_tokens=5000,
                    output_tokens=800,
                    input_tokens_details=ns(cached_tokens=1500),
                ),
            ),
        ),
    ]
    sink = _Sink()
    recorder = Recorder()
    recorder.record = sink.record  # type: ignore[method-assign]
    stream = RecordingStream(
        iter(events),
        collector,
        _stream_finisher(adapter, recorder, RESPONSES_PATH, kwargs, collector),
        start=0.0,
    )
    yielded = list(stream)
    assert len(yielded) == 4  # everything passes through
    (event,) = sink.calls
    assert event.usage.input_tokens == 3500  # 5000 - 1500 cached
    assert event.usage.cache_read_tokens == 1500
    assert event.usage.output_tokens == 800


def test_anthropic_raw_stream_collector_fixture_events() -> None:
    from ledgerlm.providers.anthropic import CREATE_PATH, AnthropicAdapter

    adapter = AnthropicAdapter()
    _, collector = adapter.prepare_stream(
        {"model": "claude-sonnet-5", "stream": True}, CREATE_PATH
    )
    assert collector is not None
    fixture = [
        ns(
            type="message_start",
            message=ns(
                model="claude-sonnet-5",
                id="msg_1",
                usage=ns(
                    input_tokens=1200,
                    output_tokens=1,
                    cache_creation_input_tokens=300,
                    cache_read_input_tokens=4500,
                ),
            ),
        ),
        ns(type="content_block_start", index=0),
        ns(type="content_block_delta", index=0, delta=ns(text="Hi")),
        ns(type="content_block_stop", index=0),
        ns(type="message_delta", usage=ns(output_tokens=250)),
        ns(type="message_stop"),
    ]
    for event in fixture:
        assert collector.observe(event) is False  # nothing swallowed on this path
    assert collector.completed
    usage = collector.usage()
    assert usage is not None
    assert usage.input_tokens == 1200  # disjoint — NOT reduced by cache fields
    assert usage.cache_write_tokens == 300
    assert usage.cache_read_tokens == 4500
    assert usage.output_tokens == 250  # from the final message_delta
    raw = collector.raw_usage()
    assert "message_start" in raw and "message_delta" in raw


class _FakeMessageStream:
    """Duck-typed stand-in for anthropic's MessageStream snapshot surface."""

    def __init__(self, usage: Any, stop_reason: str | None) -> None:
        self.current_message_snapshot = ns(usage=usage, stop_reason=stop_reason)


class _FakeManager:
    def __init__(self, stream: _FakeMessageStream) -> None:
        self._stream = stream

    def __enter__(self) -> _FakeMessageStream:
        return self._stream

    def __exit__(self, *exc: Any) -> None:
        return None


class TestAnthropicStreamHelperSnapshot:
    def _finish_events(self, stop_reason: str | None) -> list[Any]:
        from ledgerlm.providers.anthropic import STREAM_HELPER_PATH, AnthropicAdapter
        from ledgerlm.wrapper import _manager_finisher

        adapter = AnthropicAdapter()
        sink = _Sink()
        recorder = Recorder()
        recorder.record = sink.record  # type: ignore[method-assign]
        usage = ns(
            input_tokens=1200,
            output_tokens=250,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        )
        manager = RecordingStreamManager(
            _FakeManager(_FakeMessageStream(usage, stop_reason)),
            _manager_finisher(adapter, recorder, STREAM_HELPER_PATH, {"model": "claude-sonnet-5"}),
        )
        with manager as stream:
            assert isinstance(stream, _FakeMessageStream)  # caller gets the REAL stream
        return sink.calls

    def test_completed_helper_records_ok(self) -> None:
        (event,) = self._finish_events(stop_reason="end_turn")
        assert event.status == "ok"
        assert event.usage.input_tokens == 1200
        assert event.usage.output_tokens == 250
        assert event.first_token_ms is None  # not measurable on the helper path

    def test_abandoned_helper_records_stream_abandoned(self) -> None:
        (event,) = self._finish_events(stop_reason=None)
        assert event.status == "error"
        assert event.error_type == "stream_abandoned"
