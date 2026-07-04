"""Normalizer unit tests over realistic usage payload fixtures.

The fixtures mirror the real SDKs' usage objects structurally (attribute
access, same field names); content is synthetic. See
.claude/skills/building-provider-adapters for the bucket contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ledgerlm.providers.anthropic import AnthropicAdapter
from ledgerlm.providers.openai import CHAT_PATH, RESPONSES_PATH, OpenAIAdapter


def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class TestOpenAIChatCompletions:
    adapter = OpenAIAdapter()

    def test_cached_tokens_are_subtracted_from_prompt_tokens(self) -> None:
        # prompt_tokens INCLUDES cached_tokens — uncached input = 1000 - 600
        usage = ns(
            prompt_tokens=1000,
            completion_tokens=200,
            total_tokens=1200,
            prompt_tokens_details=ns(cached_tokens=600, audio_tokens=0),
            completion_tokens_details=ns(reasoning_tokens=0),
        )
        u = self.adapter.normalize_usage(usage, CHAT_PATH)
        assert u.input_tokens == 400
        assert u.cache_read_tokens == 600
        assert u.output_tokens == 200
        assert u.cache_write_tokens is None  # OpenAI does not bill cache writes

    def test_no_details_means_all_input_uncached(self) -> None:
        usage = ns(prompt_tokens=350, completion_tokens=42, prompt_tokens_details=None)
        u = self.adapter.normalize_usage(usage, CHAT_PATH)
        assert u.input_tokens == 350
        assert u.cache_read_tokens is None
        assert u.output_tokens == 42

    def test_zero_cached_tokens(self) -> None:
        usage = ns(
            prompt_tokens=100,
            completion_tokens=10,
            prompt_tokens_details=ns(cached_tokens=0),
        )
        u = self.adapter.normalize_usage(usage, CHAT_PATH)
        assert u.input_tokens == 100
        assert u.cache_read_tokens == 0


class TestOpenAIResponses:
    adapter = OpenAIAdapter()

    def test_cached_tokens_are_subtracted_from_input_tokens(self) -> None:
        # Responses API: input_tokens INCLUDES input_tokens_details.cached_tokens
        usage = ns(
            input_tokens=5000,
            output_tokens=800,
            total_tokens=5800,
            input_tokens_details=ns(cached_tokens=1500),
            output_tokens_details=ns(reasoning_tokens=120),
        )
        u = self.adapter.normalize_usage(usage, RESPONSES_PATH)
        assert u.input_tokens == 3500
        assert u.cache_read_tokens == 1500
        assert u.output_tokens == 800
        assert u.cache_write_tokens is None

    def test_buckets_sum_to_total_billed_input(self) -> None:
        usage = ns(
            input_tokens=5000,
            output_tokens=800,
            input_tokens_details=ns(cached_tokens=1500),
        )
        u = self.adapter.normalize_usage(usage, RESPONSES_PATH)
        assert u.input_tokens + (u.cache_read_tokens or 0) == 5000


class TestAnthropic:
    adapter = AnthropicAdapter()

    def test_disjoint_fields_map_directly_no_arithmetic(self) -> None:
        # Anthropic input_tokens EXCLUDES the cache fields — already disjoint
        usage = ns(
            input_tokens=1200,
            output_tokens=250,
            cache_creation_input_tokens=300,
            cache_read_input_tokens=4500,
        )
        u = self.adapter.normalize_usage(usage, ("messages", "create"))
        assert u.input_tokens == 1200  # NOT reduced by cache fields
        assert u.cache_write_tokens == 300
        assert u.cache_read_tokens == 4500
        assert u.output_tokens == 250

    def test_absent_cache_fields_stay_none(self) -> None:
        usage = ns(input_tokens=80, output_tokens=15)
        u = self.adapter.normalize_usage(usage, ("messages", "create"))
        assert u.input_tokens == 80
        assert u.cache_read_tokens is None
        assert u.cache_write_tokens is None

    def test_none_cache_fields_stay_none(self) -> None:
        usage = ns(
            input_tokens=80,
            output_tokens=15,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        )
        u = self.adapter.normalize_usage(usage, ("messages", "create"))
        assert u.cache_read_tokens is None
        assert u.cache_write_tokens is None
