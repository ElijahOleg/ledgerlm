"""CSV export round-trips through a reader; money exits as exact strings."""

from __future__ import annotations

import csv
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import Ledger

from ledgerlm.cli import app
from ledgerlm.db.models import LlmEvent, utcnow


def add_event(
    factory: sessionmaker[Session],
    *,
    cost: Decimal | None,
    project: str = "p1",
    cache_read: int | None = None,
    cache_write: int | None = None,
    status: str = "ok",
) -> None:
    with factory() as session:
        session.add(
            LlmEvent(
                ts=utcnow() - dt.timedelta(hours=2),
                provider="mock",
                model="mock-model",
                status=status,
                error_type=None if status == "ok" else "RateLimitError",
                latency_ms=321,
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                raw_usage={"input_tokens": 1000, "output_tokens": 200},
                price_snapshot=None if cost is None else {"input_per_mtok": "3.00"},
                cost_usd=cost,
                project=project,
                feature="feat",
                tags={"team": "core", "demo": "true"},
                prompt_hash="ab" * 32,
            )
        )
        session.commit()


def test_export_events_round_trips(ledger: Ledger, tmp_path: Path) -> None:
    add_event(
        ledger.session_factory,
        cost=Decimal("6.5250000000"),
        cache_read=500_000,
        cache_write=100_000,
    )
    add_event(ledger.session_factory, cost=None)  # unpriced
    add_event(ledger.session_factory, cost=Decimal("0.0000000000"))  # real $0 != unknown

    out = tmp_path / "events.csv"
    result = ledger.runner.invoke(app, ["export", "events", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "exported 3 events rows (1 unpriced)" in result.output

    with out.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3
    priced, unpriced, zero = rows
    # Money round-trips exactly; empty cell = unknown, distinct from a real 0.
    assert Decimal(priced["cost_usd"]) == Decimal("6.525")
    assert unpriced["cost_usd"] == ""
    assert Decimal(zero["cost_usd"]) == Decimal("0")
    # All four token buckets survive.
    assert priced["input_tokens"] == "1000"
    assert priced["cache_read_tokens"] == "500000"
    assert priced["cache_write_tokens"] == "100000"
    # JSON columns parse back verbatim; the row stays recomputable.
    assert json.loads(priced["raw_usage"]) == {"input_tokens": 1000, "output_tokens": 200}
    assert json.loads(priced["tags"]) == {"team": "core", "demo": "true"}
    assert json.loads(priced["price_snapshot"]) == {"input_per_mtok": "3.00"}
    assert unpriced["price_snapshot"] == ""
    # Timestamps parse back as ISO 8601.
    assert dt.datetime.fromisoformat(priced["ts"]).tzinfo is not None


def test_export_summary_by_project(ledger: Ledger, tmp_path: Path) -> None:
    add_event(ledger.session_factory, cost=Decimal("1.25"), project="alpha")
    add_event(ledger.session_factory, cost=Decimal("2.50"), project="alpha")
    add_event(ledger.session_factory, cost=None, project="beta")  # fully unpriced group

    out = tmp_path / "summary.csv"
    result = ledger.runner.invoke(app, ["export", "summary", "--by", "project", "--out", str(out)])
    assert result.exit_code == 0, result.output

    with out.open(newline="") as fh:
        rows = {row["project"]: row for row in csv.DictReader(fh)}
    assert set(rows) == {"alpha", "beta"}
    # 1.25 + 2.50 = 3.75, hand-computed.
    assert Decimal(rows["alpha"]["cost_usd"]) == Decimal("3.75")
    assert rows["alpha"]["unpriced"] == "0"
    assert rows["alpha"]["calls"] == "2"
    assert rows["alpha"]["cache_read_tokens"] == "0"
    # A group with no priced rows exports an EMPTY total, never a fabricated 0.
    assert rows["beta"]["cost_usd"] == ""
    assert rows["beta"]["unpriced"] == "1"


def test_export_to_stdout_and_bad_args(ledger: Ledger) -> None:
    add_event(ledger.session_factory, cost=Decimal("1.00"))
    result = ledger.runner.invoke(app, ["export", "events"])
    assert result.exit_code == 0
    assert result.output.startswith("id,ts,provider,model,")

    assert ledger.runner.invoke(app, ["export", "nope"]).exit_code != 0
    assert ledger.runner.invoke(app, ["export", "events", "--format", "tsv"]).exit_code != 0
    assert ledger.runner.invoke(app, ["export", "events", "--by", "project"]).exit_code != 0
    assert ledger.runner.invoke(app, ["export", "summary", "--by", "nope"]).exit_code != 0
