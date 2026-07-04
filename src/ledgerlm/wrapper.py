"""wrap(): a transparent proxy over an SDK client.

Only known call paths are intercepted (Anthropic ``messages.create`` and
``messages.stream``; OpenAI ``chat.completions.create`` and
``responses.create``; the mock client); every other attribute and method
passes through untouched. Sync and async clients are both supported — the
wrapper mirrors whichever it is given. Streamed calls are recorded via
stream wrappers that keep the caller-visible stream byte-identical. No retry
logic anywhere: the SDKs retry internally; one event per completed call.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from typing import Any

from ledgerlm.providers.base import (
    InterceptPath,
    ProviderAdapter,
    StreamCollector,
    usage_to_dict,
)
from ledgerlm.providers.mock import MockAdapter, MockLLMClient
from ledgerlm.recorder import CallEvent, Recorder
from ledgerlm.streaming import AsyncRecordingStream, RecordingStream, RecordingStreamManager

logger = logging.getLogger("ledgerlm")

_warned_paths_lock = threading.Lock()
_warned_paths: set[InterceptPath] = set()


def _warn_unrecorded_once(path: InterceptPath, message: str) -> None:
    with _warned_paths_lock:
        if path in _warned_paths:
            return
        _warned_paths.add(path)
    logger.warning("ledgerlm: %s", message)


def reset_unrecorded_warnings() -> None:
    """Clear the warn-once cache (tests)."""
    with _warned_paths_lock:
        _warned_paths.clear()


def _detect_adapter(client: Any) -> ProviderAdapter:
    if isinstance(client, MockLLMClient):
        return MockAdapter()
    module_root = type(client).__module__.split(".")[0]
    if module_root == "anthropic":
        from ledgerlm.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter()
    if module_root == "openai":
        from ledgerlm.providers.openai import OpenAIAdapter

        return OpenAIAdapter()
    raise TypeError(
        f"ledgerlm.wrap() does not recognize {type(client).__qualname__}; supported clients: "
        "anthropic.(Async)Anthropic, openai.(Async)OpenAI, ledgerlm MockLLMClient"
    )


class _TransparentProxy:
    """Passes everything through; intercepts exactly the adapter's known paths."""

    def __init__(
        self, target: Any, adapter: ProviderAdapter, recorder: Recorder, path: InterceptPath
    ) -> None:
        self._target = target
        self._adapter = adapter
        self._recorder = recorder
        self._path = path

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        path = (*self._path, name)
        matching = [p for p in self._adapter.intercept_paths if p[: len(path)] == path]
        if not matching:
            return attr
        if any(len(p) == len(path) for p in matching):
            return _instrument(attr, self._adapter, self._recorder, path)
        return _TransparentProxy(attr, self._adapter, self._recorder, path)

    def __repr__(self) -> str:
        return f"<ledgerlm wrapped {self._target!r}>"


def _record_safely(
    adapter: ProviderAdapter,
    recorder: Recorder,
    path: InterceptPath,
    kwargs: dict[str, Any],
    latency_ms: int,
    response: Any | None,
    exc: BaseException | None,
) -> None:
    """Build + persist a non-streamed event. Never lets a failure reach the caller."""
    try:
        usage_obj = adapter.extract_usage(response) if response is not None else None
        event = CallEvent(
            provider=adapter.name,
            model=adapter.model_name(kwargs, response),
            status="ok" if exc is None else "error",
            error_type=None if exc is None else type(exc).__name__,
            latency_ms=latency_ms,
            usage=None if usage_obj is None else adapter.normalize_usage(usage_obj, path),
            raw_usage=usage_to_dict(usage_obj),
            prompt_hash=adapter.prompt_hash(kwargs, path),
            provider_request_id=adapter.request_id(response) if response is not None else None,
        )
        recorder.record(event)
    except Exception:
        logger.exception("ledgerlm: failed to record LLM event (call result was unaffected)")


def _stream_finisher(
    adapter: ProviderAdapter,
    recorder: Recorder,
    path: InterceptPath,
    kwargs: dict[str, Any],
    collector: StreamCollector,
) -> Any:
    """finish(status, error_type, latency_ms, first_token_ms) for RecordingStream."""

    def finish(
        status: str, error_type: str | None, latency_ms: int, first_token_ms: int | None
    ) -> None:
        try:
            event = CallEvent(
                provider=adapter.name,
                model=collector.model() or str(kwargs.get("model", "unknown")),
                status=status,
                error_type=error_type,
                latency_ms=latency_ms,
                first_token_ms=first_token_ms,
                usage=collector.usage(),
                raw_usage=collector.raw_usage(),
                prompt_hash=adapter.prompt_hash(kwargs, path),
                provider_request_id=collector.request_id(),
            )
            recorder.record(event)
        except Exception:
            logger.exception("ledgerlm: failed to record streamed LLM event")

    return finish


def _manager_finisher(
    adapter: ProviderAdapter,
    recorder: Recorder,
    path: InterceptPath,
    kwargs: dict[str, Any],
) -> Any:
    """finish(stream, error_type, latency_ms) for RecordingStreamManager."""

    def finish(stream: Any, error_type: str | None, latency_ms: int) -> None:
        try:
            usage_obj, completed = (
                adapter.stream_snapshot(stream) if stream is not None else (None, False)
            )
            if error_type is None and not completed:
                error_type = "stream_abandoned"
            event = CallEvent(
                provider=adapter.name,
                model=str(kwargs.get("model", "unknown")),
                status="ok" if error_type is None else "error",
                error_type=error_type,
                latency_ms=latency_ms,
                usage=None if usage_obj is None else adapter.normalize_usage(usage_obj, path),
                raw_usage=usage_to_dict(usage_obj),
                prompt_hash=adapter.prompt_hash(kwargs, path),
            )
            recorder.record(event)
        except Exception:
            logger.exception("ledgerlm: failed to record streamed LLM event")

    return finish


def _instrument(fn: Any, adapter: ProviderAdapter, recorder: Recorder, path: InterceptPath) -> Any:
    for unrecorded_path, message in adapter.unrecorded_paths:
        if path == unrecorded_path:

            @functools.wraps(fn)
            def passthrough_wrapped(*args: Any, **kwargs: Any) -> Any:
                _warn_unrecorded_once(path, message)  # noqa: B023 - loop exits via return
                return fn(*args, **kwargs)

            return passthrough_wrapped

    if path in adapter.stream_manager_paths:

        @functools.wraps(fn)
        def manager_wrapped(*args: Any, **kwargs: Any) -> Any:
            manager = fn(*args, **kwargs)
            return RecordingStreamManager(
                manager, _manager_finisher(adapter, recorder, path, kwargs)
            )

        return manager_wrapped

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                prepared, collector = adapter.prepare_stream(dict(kwargs), path)
                if collector is not None:
                    start = time.perf_counter()
                    inner = await fn(*args, **prepared)
                    return AsyncRecordingStream(
                        inner,
                        collector,
                        _stream_finisher(adapter, recorder, path, kwargs, collector),
                        start,
                    )
                return await fn(*args, **kwargs)
            start = time.perf_counter()
            try:
                response = await fn(*args, **kwargs)
            except BaseException as exc:
                latency = int((time.perf_counter() - start) * 1000)
                _record_safely(adapter, recorder, path, kwargs, latency, None, exc)
                raise
            latency = int((time.perf_counter() - start) * 1000)
            _record_safely(adapter, recorder, path, kwargs, latency, response, None)
            return response

        return async_wrapped

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            prepared, collector = adapter.prepare_stream(dict(kwargs), path)
            if collector is not None:
                start = time.perf_counter()
                inner = fn(*args, **prepared)
                if hasattr(inner, "__aiter__"):  # async client whose create() isn't a coroutine fn
                    return AsyncRecordingStream(
                        inner,
                        collector,
                        _stream_finisher(adapter, recorder, path, kwargs, collector),
                        start,
                    )
                return RecordingStream(
                    inner,
                    collector,
                    _stream_finisher(adapter, recorder, path, kwargs, collector),
                    start,
                )
            return fn(*args, **kwargs)
        start = time.perf_counter()
        try:
            response = fn(*args, **kwargs)
        except BaseException as exc:
            latency = int((time.perf_counter() - start) * 1000)
            _record_safely(adapter, recorder, path, kwargs, latency, None, exc)
            raise
        latency = int((time.perf_counter() - start) * 1000)
        _record_safely(adapter, recorder, path, kwargs, latency, response, None)
        return response

    return wrapped


def wrap(client: Any, *, recorder: Recorder | None = None) -> Any:
    """Wrap an SDK client so completed calls are recorded to the ledger.

    The returned proxy is transparent: SDK responses and streams come back
    with caller-visible behavior unchanged, and recording failures never
    raise into the host app. ``recorder`` is an injection point for tests;
    by default events go to the configured ledger.
    """
    adapter = _detect_adapter(client)
    return _TransparentProxy(client, adapter, recorder or Recorder(), ())
