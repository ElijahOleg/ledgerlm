"""Deterministic SDK-shaped mock client. Ships in the package so smoke tests
and demos exercise the full wrap → normalize → price → record path offline,
for both regular and streamed calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ledgerlm.providers.base import (
    InterceptPath,
    NormalizedUsage,
    ProviderAdapter,
    StreamCollector,
    _int_or_none,
    canonical_hash,
)

DEFAULT_MOCK_MODEL = "mock-model"


@dataclass(frozen=True)
class MockUsage:
    """Already shaped as the four disjoint buckets."""

    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass
class MockResponse:
    model: str
    content: str
    usage: MockUsage
    id: str = "mock-msg-1"
    _request_id: str | None = "mock-req-1"


@dataclass
class MockStreamEvent:
    """Anthropic-shaped stream event: message_start carries input-side usage,
    the final message_delta carries output usage."""

    type: str
    message: Any = None  # on message_start
    text: str = ""  # on content_block_delta
    usage: Any = None  # on message_delta


class MockStream:
    """Deterministic event stream; iterator + context manager, closeable."""

    def __init__(self, events: list[MockStreamEvent]) -> None:
        self._events = iter(events)
        self.closed = False

    def __iter__(self) -> MockStream:
        return self

    def __next__(self) -> MockStreamEvent:
        return next(self._events)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> MockStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class MockAsyncStream:
    """Async twin of MockStream."""

    def __init__(self, events: list[MockStreamEvent]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> MockAsyncStream:
        return self

    async def __anext__(self) -> MockStreamEvent:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> MockAsyncStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _stream_events(model: str, text: str, usage: MockUsage) -> list[MockStreamEvent]:
    input_side = MockUsage(
        input_tokens=usage.input_tokens,
        output_tokens=0,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    events = [
        MockStreamEvent(
            type="message_start",
            message=MockResponse(model=model, content="", usage=input_side),
        )
    ]
    events.extend(MockStreamEvent(type="content_block_delta", text=word) for word in text.split())
    events.append(
        MockStreamEvent(type="message_delta", usage=MockUsage(0, usage.output_tokens, None, None))
    )
    events.append(MockStreamEvent(type="message_stop"))
    return events


@dataclass
class _MockMessages:
    client: MockLLMClient

    def create(self, **kwargs: Any) -> MockResponse | MockStream:
        self.client.calls.append(kwargs)
        model = str(kwargs.get("model", self.client.model))
        if kwargs.get("stream"):
            return MockStream(_stream_events(model, self.client.response_text, self.client.usage))
        return MockResponse(
            model=model, content=self.client.response_text, usage=self.client.usage
        )


@dataclass
class MockLLMClient:
    """SDK-shaped: ``client.messages.create(model=..., messages=[...])``.

    Usage is configurable across all four buckets so tests can hand-compute
    exact costs, including cache buckets. ``stream=True`` yields a
    deterministic Anthropic-shaped event stream.
    """

    model: str = DEFAULT_MOCK_MODEL
    response_text: str = "mock response"
    usage: MockUsage = field(default_factory=MockUsage)
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def messages(self) -> _MockMessages:
        return _MockMessages(client=self)


class MockStreamCollector(StreamCollector):
    def __init__(self, adapter: MockAdapter) -> None:
        self._adapter = adapter
        self._input: MockUsage | None = None
        self._output: MockUsage | None = None
        self._model: str | None = None

    def observe(self, event: Any) -> bool:
        event_type = getattr(event, "type", "")
        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                self._input = getattr(message, "usage", None)
                model = getattr(message, "model", None)
                if model:
                    self._model = str(model)
        elif event_type == "message_delta":
            self._output = getattr(event, "usage", None)
        elif event_type == "message_stop":
            self.completed = True
        return False

    def is_content(self, event: Any) -> bool:
        return getattr(event, "type", "") == "content_block_delta"

    def usage(self) -> NormalizedUsage | None:
        if self._input is None and self._output is None:
            return None
        return NormalizedUsage(
            input_tokens=int(getattr(self._input, "input_tokens", 0) or 0),
            output_tokens=int(getattr(self._output, "output_tokens", 0) or 0),
            cache_read_tokens=_int_or_none(getattr(self._input, "cache_read_tokens", None)),
            cache_write_tokens=_int_or_none(getattr(self._input, "cache_write_tokens", None)),
        )

    def raw_usage(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self._input is not None:
            raw["message_start"] = {
                "input_tokens": self._input.input_tokens,
                "cache_read_tokens": self._input.cache_read_tokens,
                "cache_write_tokens": self._input.cache_write_tokens,
            }
        if self._output is not None:
            raw["message_delta"] = {"output_tokens": self._output.output_tokens}
        return raw

    def model(self) -> str | None:
        return self._model


class MockAdapter(ProviderAdapter):
    name = "mock"
    intercept_paths: tuple[InterceptPath, ...] = (("messages", "create"),)

    def extract_usage(self, response: Any) -> Any | None:
        return getattr(response, "usage", None)

    def normalize_usage(self, usage: Any, path: InterceptPath) -> NormalizedUsage:
        return NormalizedUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=_int_or_none(getattr(usage, "cache_read_tokens", None)),
            cache_write_tokens=_int_or_none(getattr(usage, "cache_write_tokens", None)),
        )

    def prompt_hash(self, kwargs: dict[str, Any], path: InterceptPath) -> str | None:
        messages = kwargs.get("messages")
        if messages is None:
            return None
        return canonical_hash({"system": kwargs.get("system"), "messages": messages})

    def prepare_stream(
        self, kwargs: dict[str, Any], path: InterceptPath
    ) -> tuple[dict[str, Any], StreamCollector | None]:
        return kwargs, MockStreamCollector(self)


__all__ = [
    "DEFAULT_MOCK_MODEL",
    "MockAdapter",
    "MockAsyncStream",
    "MockLLMClient",
    "MockResponse",
    "MockStream",
    "MockStreamCollector",
    "MockStreamEvent",
    "MockUsage",
]
