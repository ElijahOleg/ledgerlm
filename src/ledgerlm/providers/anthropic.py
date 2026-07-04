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
    _int_or_none,
    canonical_hash,
)


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    intercept_paths: tuple[InterceptPath, ...] = (("messages", "create"),)

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
