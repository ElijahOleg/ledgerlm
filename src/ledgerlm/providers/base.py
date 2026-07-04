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


class StreamCollector(ABC):
    """Observes a stream's events as they pass to the caller; accumulates usage.

    Collectors never mutate events. ``observe`` returning True means the event
    is swallowed (used only for the OpenAI usage chunk LedgerLM itself
    injected — see D12); everything else reaches the caller untouched.
    """

    completed: bool = False

    @abstractmethod
    def observe(self, event: Any) -> bool:
        """Inspect one event; return True to swallow it."""

    @abstractmethod
    def is_content(self, event: Any) -> bool:
        """True if this event counts as content for first_token_ms."""

    @abstractmethod
    def usage(self) -> NormalizedUsage | None:
        """Normalized buckets from what has been observed so far, or None."""

    @abstractmethod
    def raw_usage(self) -> dict[str, Any]:
        """The provider's usage payload(s), verbatim."""

    def model(self) -> str | None:
        return None

    def request_id(self) -> str | None:
        return None


class ProviderAdapter(ABC):
    """One per provider. Stateless; the wrap() proxy consults it per call."""

    name: str
    intercept_paths: tuple[InterceptPath, ...]
    # Paths whose return value is a stream-manager (context manager yielding a
    # stream), e.g. Anthropic messages.stream(); handled via snapshot-at-exit.
    stream_manager_paths: tuple[InterceptPath, ...] = ()
    # Paths passed through UNRECORDED with a one-time warning per path — for
    # streaming surfaces v0 doesn't capture yet.
    unrecorded_paths: tuple[tuple[InterceptPath, str], ...] = ()

    def prepare_stream(
        self, kwargs: dict[str, Any], path: InterceptPath
    ) -> tuple[dict[str, Any], StreamCollector | None]:
        """Adjust request kwargs for capture and return a fresh collector.

        Returning None means this path's streams are passed through unrecorded.
        """
        return kwargs, None

    def stream_snapshot(self, stream: Any) -> tuple[Any | None, bool]:
        """(usage-bearing snapshot, completed) for stream-manager paths."""
        return None, False

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
