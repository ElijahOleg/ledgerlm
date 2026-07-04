"""Deterministic SDK-shaped mock client. Ships in the package so smoke tests
and demos exercise the full wrap → normalize → price → record path offline."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ledgerlm.providers.base import (
    InterceptPath,
    NormalizedUsage,
    ProviderAdapter,
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
class _MockMessages:
    client: MockLLMClient

    def create(self, **kwargs: Any) -> MockResponse | Iterator[str]:
        self.client.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter(self.client.response_text.split())
        return MockResponse(
            model=str(kwargs.get("model", self.client.model)),
            content=self.client.response_text,
            usage=self.client.usage,
        )


@dataclass
class MockLLMClient:
    """SDK-shaped: ``client.messages.create(model=..., messages=[...])``.

    Usage is configurable across all four buckets so tests can hand-compute
    exact costs, including cache buckets.
    """

    model: str = DEFAULT_MOCK_MODEL
    response_text: str = "mock response"
    usage: MockUsage = field(default_factory=MockUsage)
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def messages(self) -> _MockMessages:
        return _MockMessages(client=self)


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
