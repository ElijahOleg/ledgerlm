"""Live-key streaming integration tests — ALWAYS env-gated, skipped by default.

Run explicitly with:
    LEDGERLM_LIVE_TESTS=1 ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \\
        pytest tests/test_live_integration.py

These are the only tests allowed to touch the network, and only when the
gate variable is set. CI never sets it.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

import ledgerlm
from conftest import Ledger
from ledgerlm.db.models import LlmEvent

LIVE = os.environ.get("LEDGERLM_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE, reason="live tests are env-gated; set LEDGERLM_LIVE_TESTS=1 to run"
)


def _one_streamed_event(ledger: Ledger) -> LlmEvent:
    with ledger.session_factory() as session:
        return session.execute(select(LlmEvent)).scalar_one()


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_anthropic_raw_stream_records_usage(ledger: Ledger) -> None:
    from anthropic import Anthropic

    client = ledgerlm.wrap(Anthropic())
    stream = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=32,
        messages=[{"role": "user", "content": "Say hi in three words."}],
        stream=True,
    )
    events = list(stream)
    assert events, "stream produced no events"

    event = _one_streamed_event(ledger)
    assert event.provider == "anthropic"
    assert event.status == "ok"
    assert event.input_tokens > 0
    assert event.output_tokens > 0
    assert event.first_token_ms is not None


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_anthropic_stream_helper_records_usage(ledger: Ledger) -> None:
    from anthropic import Anthropic

    client = ledgerlm.wrap(Anthropic())
    with client.messages.stream(
        model="claude-haiku-4-5",
        max_tokens=32,
        messages=[{"role": "user", "content": "Say hi in three words."}],
    ) as stream:
        text = "".join(stream.text_stream)
    assert text

    event = _one_streamed_event(ledger)
    assert event.status == "ok"
    assert event.output_tokens > 0


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
def test_openai_chat_stream_swallow_and_record(ledger: Ledger) -> None:
    from openai import OpenAI

    client = ledgerlm.wrap(OpenAI())
    stream = client.chat.completions.create(
        model="gpt-5.4-nano",
        messages=[{"role": "user", "content": "Say hi in three words."}],
        stream=True,  # include_usage injected by LedgerLM; final chunk swallowed
    )
    chunks = list(stream)
    assert chunks
    assert all(chunk.choices for chunk in chunks), "caller saw an injected usage-only chunk"

    event = _one_streamed_event(ledger)
    assert event.provider == "openai"
    assert event.status == "ok"
    assert event.output_tokens > 0
