"""Anthropic Messages API adapter.

Anthropic's ``usage.input_tokens`` EXCLUDES ``cache_creation_input_tokens`` and
``cache_read_input_tokens`` — the three fields are already disjoint, so they
map directly with no arithmetic. Do not "fix" this; it is how the API works.
"""

from __future__ import annotations

from typing import Any

from ledgerlm.providers.base import (
    InterceptPath,
    NormalizedUsage,
    ProviderAdapter,
    StreamCollector,
    _int_or_none,
    canonical_hash,
    usage_to_dict,
)

CREATE_PATH: InterceptPath = ("messages", "create")
STREAM_HELPER_PATH: InterceptPath = ("messages", "stream")


class MessagesStreamCollector(StreamCollector):
    """Raw ``stream=True`` events: input-side usage arrives on ``message_start``
    (input_tokens already excludes the cache fields — disjoint, no arithmetic);
    output/cumulative usage on the final ``message_delta``. Nothing is
    injected or swallowed; every event reaches the caller untouched."""

    def __init__(self, adapter: AnthropicAdapter) -> None:
        self._adapter = adapter
        self._start_usage: Any | None = None
        self._delta_usage: Any | None = None
        self._model: str | None = None
        self._request_id: str | None = None

    def observe(self, event: Any) -> bool:
        event_type = getattr(event, "type", "")
        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                self._start_usage = getattr(message, "usage", None)
                model = getattr(message, "model", None)
                if model:
                    self._model = str(model)
                rid = getattr(message, "_request_id", None) or getattr(message, "id", None)
                if rid:
                    self._request_id = str(rid)
        elif event_type == "message_delta":
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._delta_usage = usage
        elif event_type == "message_stop":
            self.completed = True
        return False

    def is_content(self, event: Any) -> bool:
        return getattr(event, "type", "") in ("content_block_start", "content_block_delta")

    def usage(self) -> NormalizedUsage | None:
        if self._start_usage is None and self._delta_usage is None:
            return None

        # Prefer the final message_delta's cumulative fields where present;
        # fall back to message_start for the input side.
        def field(name: str) -> Any:
            value = getattr(self._delta_usage, name, None)
            if value is None:
                value = getattr(self._start_usage, name, None)
            return value

        return NormalizedUsage(
            input_tokens=int(field("input_tokens") or 0),
            output_tokens=int(field("output_tokens") or 0),
            cache_read_tokens=_int_or_none(field("cache_read_input_tokens")),
            cache_write_tokens=_int_or_none(field("cache_creation_input_tokens")),
        )

    def raw_usage(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self._start_usage is not None:
            raw["message_start"] = usage_to_dict(self._start_usage)
        if self._delta_usage is not None:
            raw["message_delta"] = usage_to_dict(self._delta_usage)
        return raw

    def model(self) -> str | None:
        return self._model

    def request_id(self) -> str | None:
        return self._request_id


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    intercept_paths: tuple[InterceptPath, ...] = (CREATE_PATH, STREAM_HELPER_PATH)
    stream_manager_paths: tuple[InterceptPath, ...] = (STREAM_HELPER_PATH,)

    def prepare_stream(
        self, kwargs: dict[str, Any], path: InterceptPath
    ) -> tuple[dict[str, Any], StreamCollector | None]:
        return kwargs, MessagesStreamCollector(self)

    def stream_snapshot(self, stream: Any) -> tuple[Any | None, bool]:
        """Usage from the ``messages.stream()`` helper's accumulated snapshot.

        The SDK accumulates a message snapshot no matter how the caller
        consumed the stream (events, text_stream, get_final_message). A
        populated stop_reason marks a completed stream.
        """
        try:
            snapshot = stream.current_message_snapshot
        except Exception:  # pragma: no cover - SDK raises before message_start
            return None, False
        usage = getattr(snapshot, "usage", None)
        completed = getattr(snapshot, "stop_reason", None) is not None
        return usage, completed

    def extract_usage(self, response: Any) -> Any | None:
        return getattr(response, "usage", None)

    def normalize_usage(self, usage: Any, path: InterceptPath) -> NormalizedUsage:
        return NormalizedUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=_int_or_none(getattr(usage, "cache_read_input_tokens", None)),
            cache_write_tokens=_int_or_none(getattr(usage, "cache_creation_input_tokens", None)),
        )

    def prompt_hash(self, kwargs: dict[str, Any], path: InterceptPath) -> str | None:
        messages = kwargs.get("messages")
        if messages is None:
            return None
        return canonical_hash({"system": kwargs.get("system"), "messages": messages})
