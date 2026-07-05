"""Optimizer analyses against hand-computed oracles (cache buckets included)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import Ledger

from ledgerlm.cli import app
from ledgerlm.db.models import LlmEvent, utcnow
from ledgerlm.optimizer import (
    DISCLAIMER,
    OptimizerFilters,
    build_report,
    cache_candidates,
    token_heavy_calls,
    whatif_repricing,
)


def add_event(
    factory: sessionmaker[Session],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cost: Decimal | None,
    project: str = "p1",
    feature: str = "f1",
    provider: str = "mock",
    model: str = "mock-model",
    prompt_hash: str | None = None,
) -> None:
    with factory() as session:
        session.add(
            LlmEvent(
                ts=utcnow() - dt.timedelta(hours=1),
                provider=provider,
                model=model,
                status="ok",
                latency_ms=100,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                raw_usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
                cost_usd=cost,
                project=project,
                feature=feature,
                prompt_hash=prompt_hash,
                tags={},
            )
        )
        session.commit()


def seed_repricing_group(factory: sessionmaker[Session]) -> None:
    """One mock/mock-model group (rates 3.00 in / 15.00 out / 0.30 cr / 3.75 cw).

    call 1: 1,000,000 in @ 3.00/M = 3.000000
            200,000 out @ 15.00/M = 3.000000
            500,000 cache reads @ 0.30/M = 0.150000
            100,000 cache writes @ 3.75/M = 0.375000     -> 6.525
    call 2: 500,000 in @ 3.00/M = 1.50; 100,000 out @ 15.00/M = 1.50 -> 3.000
    Group buckets: in 1,500,000 / out 300,000 / cr 500,000 / cw 100,000
    Current cost: 9.525. Plus one UNPRICED row excluded from all buckets.
    """
    add_event(
        factory,
        input_tokens=1_000_000,
        output_tokens=200_000,
        cache_read=500_000,
        cache_write=100_000,
        cost=Decimal("6.525"),
    )
    add_event(factory, input_tokens=500_000, output_tokens=100_000, cost=Decimal("3.0"))
    add_event(factory, input_tokens=10, output_tokens=5, cost=None)  # unpriced


def test_whatif_repricing_hand_computed(ledger: Ledger) -> None:
    seed_repricing_group(ledger.session_factory)
    # A second, cheaper group so ordering by spend is observable.
    add_event(
        ledger.session_factory,
        input_tokens=100_000,
        output_tokens=10_000,
        cost=Decimal("0.45"),
        project="p2",
        feature="f2",
    )

    with ledger.session_factory() as session:
        groups = whatif_repricing(session, OptimizerFilters())

    assert [g.project for g in groups] == ["p1", "p2"]  # spend desc
    g = groups[0]
    assert g.calls == 2  # the unpriced row is excluded...
    assert g.unpriced == 1  # ...and reported
    assert (g.input_tokens, g.output_tokens) == (1_500_000, 300_000)
    assert (g.cache_read_tokens, g.cache_write_tokens) == (500_000, 100_000)
    assert g.current_cost == Decimal("9.525")

    by_model = {(c.provider, c.model): c for c in g.candidates}
    # claude-haiku-4-5 (1.00 / 5.00 / 0.10 / 1.25):
    #   1,500,000 in @ 1.00/M  = 1.500000
    #     300,000 out @ 5.00/M = 1.500000
    #     500,000 cr @ 0.10/M  = 0.050000
    #     100,000 cw @ 1.25/M  = 0.125000            -> total 3.175
    haiku = by_model[("anthropic", "claude-haiku-4-5")]
    assert haiku.cost_usd == Decimal("3.175")
    # (3.175 - 9.525) / 9.525 * 100 = -66.666...% -> -66.7%
    assert haiku.delta_pct == Decimal("-66.7")
    # claude-sonnet-5 (2.00 / 10.00 / 0.20 / 2.50):
    #   3.000000 + 3.000000 + 0.100000 + 0.250000    -> total 6.35 (-33.3%)
    sonnet = by_model[("anthropic", "claude-sonnet-5")]
    assert sonnet.cost_usd == Decimal("6.35")
    assert sonnet.delta_pct == Decimal("-33.3")
    # OpenAI models have no cache-write rate; this group HAS cache-write
    # tokens, so none of them may appear (unpriceable bucket, never $0).
    assert not any(provider == "openai" for provider, _ in by_model)
    # Same-rate and dearer models are not "candidates" either.
    assert ("anthropic", "claude-sonnet-4-6") not in by_model
    assert ("anthropic", "claude-fable-5") not in by_model


def test_token_heavy_calls_above_feature_p95(ledger: Ledger) -> None:
    # feature "fh": twenty calls at 100 input tokens, one at 10,000.
    # n=21 -> nearest-rank p95 index = ceil(0.95*21)-1 = 19 -> value 100.
    # Only the 10,000-token call is strictly above.
    for _ in range(20):
        add_event(
            ledger.session_factory,
            input_tokens=100,
            output_tokens=10,
            cost=Decimal("0.001"),
            feature="fh",
        )
    add_event(
        ledger.session_factory,
        input_tokens=10_000,
        output_tokens=10,
        cost=Decimal("0.05"),
        feature="fh",
    )

    with ledger.session_factory() as session:
        calls = token_heavy_calls(session, OptimizerFilters())

    assert len(calls) == 1
    assert calls[0].input_tokens == 10_000
    assert calls[0].feature == "fh"
    assert calls[0].feature_p95 == 100


def test_cache_candidates_hand_computed(ledger: Ledger) -> None:
    factory = ledger.session_factory
    # h1: three identical prompts, zero cache reads. 200,000 input each.
    #   repeat tokens = 600,000 - 200,000 = 400,000
    #   savings = 400,000 * (3.00 - 0.30)/M = 0.4 * 2.70 = 1.08
    for _ in range(3):
        add_event(
            factory,
            input_tokens=200_000,
            output_tokens=1_000,
            cost=Decimal("0.615"),
            prompt_hash="h1" * 32,
        )
    # h2: repeated but already cache-reading — not a candidate.
    for _ in range(3):
        add_event(
            factory,
            input_tokens=1_000,
            output_tokens=100,
            cache_read=150_000,
            cost=Decimal("0.05"),
            prompt_hash="h2" * 32,
        )
    # h3: only two repeats — below the threshold.
    for _ in range(2):
        add_event(
            factory,
            input_tokens=50_000,
            output_tokens=100,
            cost=Decimal("0.15"),
            prompt_hash="h3" * 32,
        )
    # h4: unknown model — candidate surfaces, savings honestly n/a (never $0).
    for _ in range(3):
        add_event(
            factory,
            input_tokens=80_000,
            output_tokens=100,
            cost=None,
            model="unknown-model",
            prompt_hash="h4" * 32,
        )

    with ledger.session_factory() as session:
        candidates = cache_candidates(session, OptimizerFilters(), min_repeats=3)

    by_hash = {c.prompt_hash[:2]: c for c in candidates}
    assert set(by_hash) == {"h1", "h4"}
    h1 = by_hash["h1"]
    assert h1.repeats == 3
    assert h1.repeat_input_tokens == 400_000
    assert h1.est_savings_usd == Decimal("1.08")
    h4 = by_hash["h4"]
    assert h4.est_savings_usd is None


def test_cli_optimize_renders_with_disclaimer(ledger: Ledger) -> None:
    seed_repricing_group(ledger.session_factory)
    result = ledger.runner.invoke(app, ["optimize", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert DISCLAIMER in result.output
    assert "What-if repricing" in result.output
    assert "unpriced rows in window: 1" in result.output
    assert "identical tokens on anthropic/claude-haiku-4-5 = $3.1750 (-66.7%)" in result.output
    assert "Cache candidates" in result.output


def test_dashboard_optimizer_page_renders_with_disclaimer(ledger: Ledger) -> None:
    seed_repricing_group(ledger.session_factory)
    from ledgerlm.dashboard.app import create_app

    client = TestClient(create_app(ledger.session_factory))
    response = client.get("/optimizer")
    assert response.status_code == 200
    assert DISCLAIMER in response.text
    assert "$9.5250" in response.text  # current group cost
    assert "$3.1750" in response.text  # haiku candidate, hand-computed above
    assert "-66.7%" in response.text


def test_report_window_unpriced_count(ledger: Ledger) -> None:
    seed_repricing_group(ledger.session_factory)
    with ledger.session_factory() as session:
        report = build_report(session, OptimizerFilters())
    assert report.window_unpriced == 1
    assert report.disclaimer == DISCLAIMER
