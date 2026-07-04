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
    canonical_hash,
)

CHAT_PATH: InterceptPath = ("chat", "completions", "create")
RESPONSES_PATH: InterceptPath = ("responses", "create")


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    intercept_paths: tuple[InterceptPath, ...] = (CHAT_PATH, RESPONSES_PATH)

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
