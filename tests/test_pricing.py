"""Unpriced-NULL semantics, warn-once, backfill, and Decimal cost math."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from sqlalchemy import select

import ledgerlm
from conftest import Ledger
from ledgerlm.cli import app
from ledgerlm.db.models import LlmEvent
from ledgerlm.pricing import Rates, compute_cost
from ledgerlm.providers.base import NormalizedUsage
from ledgerlm.providers.mock import MockLLMClient, MockUsage

MESSAGES = [{"role": "user", "content": "hi"}]


def test_unknown_model_records_unpriced_with_one_warning(
    ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    client = ledgerlm.wrap(MockLLMClient(usage=MockUsage(input_tokens=100, output_tokens=50)))

    with caplog.at_level(logging.WARNING, logger="ledgerlm"):
        client.messages.create(model="unknown-model", messages=MESSAGES)
        client.messages.create(model="unknown-model", messages=MESSAGES)

    warnings = [r for r in caplog.records if "unpriced" in r.getMessage()]
    assert len(warnings) == 1  # once per (provider, model), not per call

    with ledger.session_factory() as session:
        events = session.execute(select(LlmEvent)).scalars().all()
    assert len(events) == 2
    for event in events:
        assert event.cost_usd is None  # NULL, never a fabricated $0
        assert event.price_snapshot is None
        assert event.input_tokens == 100  # full token counts still recorded
        assert event.output_tokens == 50


def test_prices_set_then_backfill_fills_cost_and_snapshot(ledger: Ledger) -> None:
    client = ledgerlm.wrap(MockLLMClient(usage=MockUsage(input_tokens=100, output_tokens=50)))
    client.messages.create(model="unknown-model", messages=MESSAGES)

    result = ledger.runner.invoke(
        app, ["prices", "set", "mock", "unknown-model", "--input", "2.00", "--output", "4.00"]
    )
    assert result.exit_code == 0, result.output

    result = ledger.runner.invoke(app, ["prices", "backfill"])
    assert result.exit_code == 0, result.output
    assert "backfilled 1 rows" in result.output

    with ledger.session_factory() as session:
        event = session.execute(select(LlmEvent)).scalar_one()
    # 100 in @ $2/M = $0.0002; 50 out @ $4/M = $0.0002 → total $0.0004
    assert event.cost_usd == Decimal("0.0004000000")
    assert event.price_snapshot is not None
    assert Decimal(event.price_snapshot["input_per_mtok"]) == Decimal("2.00")
    assert Decimal(event.price_snapshot["output_per_mtok"]) == Decimal("4.00")


def test_backfill_never_prices_rows_without_usage(ledger: Ledger) -> None:
    with ledger.session_factory() as session:
        session.add(
            LlmEvent(
                provider="mock",
                model="mock-model",
                status="error",
                error_type="TimeoutError",
                latency_ms=1000,
                input_tokens=0,
                output_tokens=0,
                raw_usage={},  # no usage captured — cost must stay NULL forever
                tags={},
            )
        )
        session.commit()

    result = ledger.runner.invoke(app, ["prices", "backfill"])
    assert result.exit_code == 0, result.output
    assert "backfilled 0 rows" in result.output

    with ledger.session_factory() as session:
        event = session.execute(select(LlmEvent)).scalar_one()
    assert event.cost_usd is None


def test_missing_rate_for_nonzero_bucket_unprices_whole_row() -> None:
    rates = Rates(
        input_per_mtok=Decimal("3"),
        output_per_mtok=Decimal("15"),
        cache_read_per_mtok=None,  # missing
        cache_write_per_mtok=None,
    )
    usage = NormalizedUsage(input_tokens=100, output_tokens=10, cache_read_tokens=500)
    assert compute_cost(usage, rates) is None

    # ...but a ZERO bucket with a missing rate prices fine:
    usage_no_cache = NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = compute_cost(usage_no_cache, rates)
    assert result is not None
    cost, snapshot = result
    # 1,000,000 in @ $3/M = $3; 1,000,000 out @ $15/M = $15 → total $18
    assert cost == Decimal("18.0000000000")
    assert set(snapshot) == {"input_per_mtok", "output_per_mtok"}


def test_summary_always_shows_unpriced_count(ledger: Ledger) -> None:
    priced = ledgerlm.wrap(MockLLMClient())
    with ledgerlm.tags(project="blog-net"):
        priced.messages.create(model="mock-model", messages=MESSAGES)
        priced.messages.create(model="unknown-model", messages=MESSAGES)

    result = ledger.runner.invoke(app, ["summary", "--by", "project"])
    assert result.exit_code == 0, result.output
    assert "unpriced" in result.output
    assert "unpriced rows in window: 1" in result.output
    assert "blog-net" in result.output
