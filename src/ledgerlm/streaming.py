"""Recording wrappers for streamed calls.

Caller-visible behavior must be byte-identical to the unwrapped SDK: events
pass through untouched (except a usage-only chunk LedgerLM itself injected,
which is swallowed — D12), and exactly one ledger event is recorded per
stream: "ok" on completion, "error" on exception, or "stream_abandoned" when
the caller closes/exits before the stream ends. Recording failures never
reach the caller.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from ledgerlm.providers.base import StreamCollector

logger = logging.getLogger("ledgerlm")

# Callback signature shared by all wrappers:
#   finish(status, error_type, latency_ms, first_token_ms)
FinishFn = Any


class _RecordingBase:
    def __init__(self, inner: Any, collector: StreamCollector, finish: FinishFn, start: float):
        self._inner = inner
        self._collector = collector
        self._finish_cb = finish
        self._start = start
        self._first_token_ms: int | None = None
        self._finished = False
        self._event_iter: Any = None

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def _finish(self, status: str, error_type: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._finish_cb(status, error_type, self._elapsed_ms(), self._first_token_ms)
        except Exception:
            logger.exception("ledgerlm: failed to record streamed LLM event")

    def _handle(self, event: Any) -> bool:
        """Observe one event; returns True if it must be swallowed."""
        swallow = self._collector.observe(event)
        if not swallow and self._first_token_ms is None and self._collector.is_content(event):
            self._first_token_ms = self._elapsed_ms()
        return swallow

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RecordingStream(_RecordingBase):
    """Wraps a sync stream (iterator, usually also a context manager)."""

    def __iter__(self) -> RecordingStream:
        return self

    def __next__(self) -> Any:
        while True:
            try:
                event = next(self._inner_iter())
            except StopIteration:
                self._finish("ok", None)
                raise
            except BaseException as exc:
                self._finish("error", type(exc).__name__)
                raise
            if not self._handle(event):
                return event

    def _inner_iter(self) -> Any:
        if self._event_iter is None:
            self._event_iter = iter(self._inner)
        return self._event_iter

    def close(self) -> None:
        self._finish("error", "stream_abandoned")
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> RecordingStream:
        enter = getattr(self._inner, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self._finish("error", exc_type.__name__)
        else:
            self._finish("error", "stream_abandoned")
        exit_ = getattr(self._inner, "__exit__", None)
        if callable(exit_):
            exit_(exc_type, exc, tb)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - GC-time best effort
            self._finish("error", "stream_abandoned")


class AsyncRecordingStream(_RecordingBase):
    """Wraps an async stream (async iterator, usually also an async CM)."""

    def __aiter__(self) -> AsyncRecordingStream:
        return self

    async def __anext__(self) -> Any:
        while True:
            try:
                event = await self._inner_aiter().__anext__()
            except StopAsyncIteration:
                self._finish("ok", None)
                raise
            except BaseException as exc:
                self._finish("error", type(exc).__name__)
                raise
            if not self._handle(event):
                return event

    def _inner_aiter(self) -> Any:
        if self._event_iter is None:
            self._event_iter = self._inner.__aiter__()
        return self._event_iter

    async def aclose(self) -> None:
        self._finish("error", "stream_abandoned")
        aclose = getattr(self._inner, "aclose", None) or getattr(self._inner, "close", None)
        if callable(aclose):
            result = aclose()
            if hasattr(result, "__await__"):
                await result

    async def __aenter__(self) -> AsyncRecordingStream:
        aenter = getattr(self._inner, "__aenter__", None)
        if callable(aenter):
            await aenter()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self._finish("error", exc_type.__name__)
        else:
            self._finish("error", "stream_abandoned")
        aexit = getattr(self._inner, "__aexit__", None)
        if callable(aexit):
            await aexit(exc_type, exc, tb)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - GC-time best effort
            self._finish("error", "stream_abandoned")


class RecordingStreamManager:
    """Wraps a stream-manager helper (e.g. Anthropic ``messages.stream()``).

    The caller receives the REAL stream object — every helper feature
    (text_stream, get_final_message, ...) is untouched. Recording happens at
    context exit from the SDK's accumulated snapshot, which is populated no
    matter how the caller consumed the stream. ``first_token_ms`` is not
    measurable on this path and stays NULL.
    """

    def __init__(self, inner: Any, finish_from_stream: Any):
        self._inner = inner
        # finish_from_stream(stream, status_hint_error_type, latency_ms)
        self._finish_cb = finish_from_stream
        self._start = time.perf_counter()
        self._stream: Any = None
        self._finished = False

    def _finish(self, error_type: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        latency_ms = int((time.perf_counter() - self._start) * 1000)
        try:
            self._finish_cb(self._stream, error_type, latency_ms)
        except Exception:
            logger.exception("ledgerlm: failed to record streamed LLM event")

    def __enter__(self) -> Any:
        self._stream = self._inner.__enter__()
        return self._stream

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._finish(exc_type.__name__ if exc_type is not None else None)
        return self._inner.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> Any:
        self._stream = await self._inner.__aenter__()
        return self._stream

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        self._finish(exc_type.__name__ if exc_type is not None else None)
        return await self._inner.__aexit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
