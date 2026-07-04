"""wrap(): a transparent proxy over an SDK client.

Only known call paths are intercepted (Anthropic ``messages.create``; OpenAI
``chat.completions.create`` and ``responses.create``; the mock client); every
other attribute and method passes through untouched. Sync and async clients
are both supported — the wrapper mirrors whichever it is given. No retry
logic anywhere: the SDKs retry internally; one event per completed call.
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from typing import Any

from ledgerlm.providers.base import InterceptPath, ProviderAdapter, usage_to_dict
from ledgerlm.providers.mock import MockAdapter, MockLLMClient
from ledgerlm.recorder import CallEvent, Recorder

logger = logging.getLogger("ledgerlm")

_stream_warn_lock = threading.Lock()
_stream_warned = False


def _warn_streaming_once() -> None:
    global _stream_warned
    with _stream_warn_lock:
        if _stream_warned:
            return
        _stream_warned = True
    logger.warning(
        "ledgerlm: streaming calls are passed through unrecorded — "
        "streaming capture lands in Phase 1.5"
    )


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
    """Build + persist the event. Never lets any failure reach the caller."""
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


def _instrument(fn: Any, adapter: ProviderAdapter, recorder: Recorder, path: InterceptPath) -> Any:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                _warn_streaming_once()
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
            _warn_streaming_once()
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

    The returned proxy is transparent: the SDK response is returned unmodified
    and recording failures never raise into the host app. ``recorder`` is an
    injection point for tests; by default events go to the configured ledger.
    """
    adapter = _detect_adapter(client)
    return _TransparentProxy(client, adapter, recorder or Recorder(), ())
