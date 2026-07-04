"""Adapter contract: normalization into four disjoint token buckets.

The buckets must sum to the total billed tokens — nothing counted twice,
nothing dropped (see .claude/skills/building-provider-adapters). Adapters
never import their SDK: they work over duck-typed response/usage objects, so
the core package runs with neither SDK installed.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

InterceptPath = tuple[str, ...]


@dataclass(frozen=True)
class NormalizedUsage:
    """Four disjoint buckets. ``None`` means the provider did not report the bucket."""

    input_tokens: int = 0  # uncached input only
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


def canonical_hash(payload: Any) -> str:
    """SHA-256 over a canonical JSON serialization. Content is hashed, never stored."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def usage_to_dict(usage: Any) -> dict[str, Any]:
    """Provider usage object → JSON-safe dict, verbatim, unknown fields included."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    dump = getattr(usage, "model_dump", None)  # pydantic models (both SDKs)
    if callable(dump):
        result = dump(mode="json")
        if isinstance(result, dict):
            return result
    if hasattr(usage, "__dict__"):
        return {k: v for k, v in vars(usage).items() if not k.startswith("_")}
    return {"repr": repr(usage)}


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


class ProviderAdapter(ABC):
    """One per provider. Stateless; the wrap() proxy consults it per call."""

    name: str
    intercept_paths: tuple[InterceptPath, ...]

    @abstractmethod
    def extract_usage(self, response: Any) -> Any | None:
        """The provider's usage object from a response, or None."""

    @abstractmethod
    def normalize_usage(self, usage: Any, path: InterceptPath) -> NormalizedUsage:
        """Map raw usage into the four disjoint buckets."""

    @abstractmethod
    def prompt_hash(self, kwargs: dict[str, Any], path: InterceptPath) -> str | None:
        """SHA-256 over canonical ordered system + message content, or None."""

    def model_name(self, kwargs: dict[str, Any], response: Any | None) -> str:
        model = getattr(response, "model", None) or kwargs.get("model")
        return str(model) if model else "unknown"

    def request_id(self, response: Any) -> str | None:
        rid = getattr(response, "_request_id", None)
        return str(rid) if rid else None
