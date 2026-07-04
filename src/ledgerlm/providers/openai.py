"""OpenAI Chat Completions + Responses API adapter.

Both APIs report cached tokens as a SUBSET of the total input figure:
``prompt_tokens`` INCLUDES ``prompt_tokens_details.cached_tokens`` (chat), and
``input_tokens`` INCLUDES ``input_tokens_details.cached_tokens`` (responses).
Uncached input is therefore total minus cached. Do not "fix" this; it is how
the API works.
"""

from __future__ import annotations

from typing import Any

from ledgerlm.providers.base import (
    InterceptPath,
    NormalizedUsage,
    ProviderAdapter,
    StreamCollector,
    canonical_hash,
    usage_to_dict,
)

CHAT_PATH: InterceptPath = ("chat", "completions", "create")
RESPONSES_PATH: InterceptPath = ("responses", "create")


class ChatStreamCollector(StreamCollector):
    """Chat Completions streaming: usage arrives in a final usage-only chunk
    (empty ``choices``) when ``stream_options.include_usage`` is set. If
    LedgerLM injected that option, the chunk is swallowed so the caller-visible
    stream is byte-identical to what they wrote code against (D12)."""

    def __init__(self, adapter: OpenAIAdapter, injected: bool) -> None:
        self._adapter = adapter
        self._injected = injected
        self._usage: Any | None = None
        self._model: str | None = None

    def observe(self, event: Any) -> bool:
        model = getattr(event, "model", None)
        if model:
            self._model = str(model)
        usage = getattr(event, "usage", None)
        if usage is not None:
            self._usage = usage
            self.completed = True
            if self._injected and not getattr(event, "choices", None):
                return True  # the chunk we asked for — caller never opted in
        return False

    def is_content(self, event: Any) -> bool:
        return bool(getattr(event, "choices", None))

    def usage(self) -> NormalizedUsage | None:
        if self._usage is None:
            return None
        return self._adapter.normalize_usage(self._usage, CHAT_PATH)

    def raw_usage(self) -> dict[str, Any]:
        return usage_to_dict(self._usage)

    def model(self) -> str | None:
        return self._model


class ResponsesStreamCollector(StreamCollector):
    """Responses API streaming: usage rides on the terminal completed event's
    ``response`` object. Nothing is injected or swallowed."""

    def __init__(self, adapter: OpenAIAdapter) -> None:
        self._adapter = adapter
        self._usage: Any | None = None
        self._model: str | None = None
        self._request_id: str | None = None

    def observe(self, event: Any) -> bool:
        response = getattr(event, "response", None)
        if response is not None:
            model = getattr(response, "model", None)
            if model:
                self._model = str(model)
            usage = getattr(response, "usage", None)
            if usage is not None:
                self._usage = usage
            if getattr(event, "type", "") == "response.completed":
                self.completed = True
        return False

    def is_content(self, event: Any) -> bool:
        return "delta" in str(getattr(event, "type", ""))

    def usage(self) -> NormalizedUsage | None:
        if self._usage is None:
            return None
        return self._adapter.normalize_usage(self._usage, RESPONSES_PATH)

    def raw_usage(self) -> dict[str, Any]:
        return usage_to_dict(self._usage)

    def model(self) -> str | None:
        return self._model


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    intercept_paths: tuple[InterceptPath, ...] = (CHAT_PATH, RESPONSES_PATH)

    def prepare_stream(
        self, kwargs: dict[str, Any], path: InterceptPath
    ) -> tuple[dict[str, Any], StreamCollector | None]:
        if path == RESPONSES_PATH:
            return kwargs, ResponsesStreamCollector(self)
        options = dict(kwargs.get("stream_options") or {})
        caller_opted_in = options.get("include_usage") is True
        if not caller_opted_in:
            options["include_usage"] = True
            kwargs = {**kwargs, "stream_options": options}
        return kwargs, ChatStreamCollector(self, injected=not caller_opted_in)

    def extract_usage(self, response: Any) -> Any | None:
        return getattr(response, "usage", None)

    def normalize_usage(self, usage: Any, path: InterceptPath) -> NormalizedUsage:
        if path == RESPONSES_PATH:
            total_input = int(getattr(usage, "input_tokens", 0) or 0)
            details = getattr(usage, "input_tokens_details", None)
            output = int(getattr(usage, "output_tokens", 0) or 0)
        else:
            total_input = int(getattr(usage, "prompt_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            output = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        return NormalizedUsage(
            input_tokens=total_input - cached,
            output_tokens=output,
            cache_read_tokens=cached if details is not None else None,
            cache_write_tokens=None,  # OpenAI does not report or bill cache writes
        )

    def prompt_hash(self, kwargs: dict[str, Any], path: InterceptPath) -> str | None:
        if path == RESPONSES_PATH:
            if "input" not in kwargs:
                return None
            return canonical_hash(
                {"instructions": kwargs.get("instructions"), "input": kwargs["input"]}
            )
        messages = kwargs.get("messages")
        if messages is None:
            return None
        return canonical_hash({"messages": messages})
