"""Alert rules, cooldown dedupe, and webhook delivery — httpx always mocked."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import Ledger

from ledgerlm.alerts import (
    AlertConfig,
    AlertConfigError,
    check_alerts,
    evaluate_rules,
    load_config,
)
from ledgerlm.cli import app
from ledgerlm.db.models import AlertFiring, LlmEvent, utcnow


def add_event(
    factory: sessionmaker[Session],
    ts: dt.datetime,
    cost: Decimal | None,
    project: str = "blog-net",
    model: str = "mock-model",
) -> None:
    with factory() as session:
        session.add(
            LlmEvent(
                ts=ts,
                provider="mock",
                model=model,
                status="ok",
                latency_ms=100,
                input_tokens=100,
                output_tokens=50,
                raw_usage={"input_tokens": 100, "output_tokens": 50},
                cost_usd=cost,
                project=project,
                tags={},
            )
        )
        session.commit()


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


@pytest.fixture
def webhook_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> Any:
        calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def spike_config(**overrides: Any) -> AlertConfig:
    defaults: dict[str, Any] = {
        "webhook_url": "https://example.invalid/hook",
        "spike_multiplier": Decimal("2.0"),
        "baseline_days": 7,
        "min_spend_floor_usd": Decimal("1.00"),
        "cooldown_minutes": 360,
    }
    defaults.update(overrides)
    return AlertConfig(**defaults)


def seed_spike(factory: sessionmaker[Session], now: dt.datetime) -> None:
    """Baseline: $1/day for the 7 prior days (median 1). Trailing 24h: $5."""
    for age in range(1, 8):
        add_event(factory, now - dt.timedelta(hours=24 * age + 12), Decimal("1.00"))
    for _ in range(5):
        add_event(factory, now - dt.timedelta(minutes=5), Decimal("1.00"))


def test_spike_fires_exactly_once_per_cooldown_window(
    ledger: Ledger, webhook_calls: list[dict[str, Any]]
) -> None:
    now = utcnow()
    seed_spike(ledger.session_factory, now)
    config = spike_config()

    with ledger.session_factory() as session:
        outcomes = check_alerts(session, config, now=now)
        assert [o.evaluation.rule for o in outcomes] == ["spike"]
        assert outcomes[0].firing is not None
        assert outcomes[0].firing.delivered is True
        assert outcomes[0].firing.response_status == 200
        assert len(webhook_calls) == 1

        # Inside the cooldown window: still over threshold, but no new firing
        # and no second webhook.
        again = check_alerts(session, config, now=now + dt.timedelta(minutes=359))
        assert again[0].suppressed_by_cooldown is True
        assert again[0].firing is None
        assert len(webhook_calls) == 1

        # Past the cooldown: fires again.
        later = now + dt.timedelta(minutes=361)
        add_event(ledger.session_factory, later - dt.timedelta(minutes=1), Decimal("5.00"))
        third = check_alerts(session, config, now=later)
        assert third[0].firing is not None
        assert len(webhook_calls) == 2


def test_budget_rule_boundaries(ledger: Ledger) -> None:
    now = dt.datetime(2026, 7, 4, 18, 0, tzinfo=dt.UTC)
    config = AlertConfig(daily_budget_usd=Decimal("10.00"), baseline_days=0)
    add_event(ledger.session_factory, now - dt.timedelta(hours=2), Decimal("9.99"))
    # Yesterday's spend must not count toward today's budget.
    add_event(ledger.session_factory, now - dt.timedelta(hours=20), Decimal("50.00"))

    with ledger.session_factory() as session:
        (ev,) = evaluate_rules(session, config, now=now)
        assert ev.rule == "budget"
        assert ev.observed == Decimal("9.99")
        assert ev.fired is False

        add_event(ledger.session_factory, now - dt.timedelta(hours=1), Decimal("0.01"))
        (ev,) = evaluate_rules(session, config, now=now)
        assert ev.observed == Decimal("10.00")  # >= threshold: boundary fires
        assert ev.fired is True


def test_spike_floor_suppresses_noise(ledger: Ledger) -> None:
    """Empty baseline → threshold 0, but spend under the floor must not fire."""
    now = utcnow()
    add_event(ledger.session_factory, now - dt.timedelta(minutes=10), Decimal("0.50"))
    with ledger.session_factory() as session:
        (ev,) = evaluate_rules(session, spike_config(), now=now)
        assert ev.rule == "spike"
        assert ev.threshold == Decimal("0")
        assert ev.fired is False


def test_webhook_payload_shape_and_secret(
    ledger: Ledger, webhook_calls: list[dict[str, Any]]
) -> None:
    now = utcnow()
    seed_spike(ledger.session_factory, now)
    # An unpriced row inside the window must be counted, not silently dropped.
    add_event(ledger.session_factory, now - dt.timedelta(minutes=3), None, project="research")
    add_event(
        ledger.session_factory,
        now - dt.timedelta(minutes=2),
        Decimal("3.00"),
        project="shop-bot",
        model="pricey-model",
    )

    config = spike_config(webhook_secret="s3cr3t")
    with ledger.session_factory() as session:
        check_alerts(session, config, now=now)

    (call,) = webhook_calls
    assert call["headers"]["X-LedgerLM-Secret"] == "s3cr3t"
    payload = call["json"]
    assert payload["rule"] == "spike"
    assert set(payload["window"]) == {"start", "end"}
    # Money travels as exact strings, never JSON floats.
    assert isinstance(payload["observed"], str)
    assert Decimal(payload["observed"]) == Decimal("8.00")
    assert payload["unpriced_rows"] == 1
    contributors = payload["top_contributors"]
    assert 1 <= len(contributors) <= 5
    assert contributors[0]["project"] == "blog-net"  # $5 beats $3
    assert Decimal(contributors[0]["cost_usd"]) == Decimal("5.00")


def test_delivery_failure_is_recorded_not_raised(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_post(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", failing_post)
    now = utcnow()
    seed_spike(ledger.session_factory, now)
    with ledger.session_factory() as session:
        (outcome,) = check_alerts(session, spike_config(), now=now)
        assert outcome.firing is not None
        assert outcome.firing.delivered is False
        assert outcome.firing.response_status is None


def test_undelivered_firing_redelivered_once_no_new_row(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D25: cooldown suppresses new firings, not delivery of an existing one."""

    def listener_down(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", listener_down)
    now = utcnow()
    seed_spike(ledger.session_factory, now)
    config = spike_config()

    with ledger.session_factory() as session:
        (first,) = check_alerts(session, config, now=now)
        assert first.firing is not None
        assert first.firing.delivered is False  # persisted undelivered

        received: list[dict[str, Any]] = []

        def listener_up(
            url: str, json: Any = None, headers: Any = None, timeout: Any = None
        ) -> Any:
            received.append(json)
            return _FakeResponse(200)

        monkeypatch.setattr(httpx, "post", listener_up)

        # Next check, inside cooldown: redelivers the existing firing exactly
        # once, updates the row in place, and exit-code semantics are
        # unchanged (still no NEW firing).
        (second,) = check_alerts(session, config, now=now + dt.timedelta(minutes=5))
        assert second.suppressed_by_cooldown is True
        assert second.firing is None
        assert second.redelivered is True
        assert len(received) == 1
        assert received[0]["rule"] == "spike"

        firings = session.query(AlertFiring).all()
        assert len(firings) == 1  # updated in place, never a new row
        assert firings[0].delivered is True
        assert firings[0].response_status == 200

        # Once delivered, later checks do not deliver again.
        (third,) = check_alerts(session, config, now=now + dt.timedelta(minutes=10))
        assert third.redelivered is False
        assert len(received) == 1


def test_cli_alerts_check_exit_codes(
    ledger: Ledger, tmp_path: Path, webhook_calls: list[dict[str, Any]]
) -> None:
    config_path = tmp_path / "ledgerlm.toml"
    config_path.write_text(
        '[alerts]\nwebhook_url = "https://example.invalid/hook"\n'
        "spike_multiplier = 2.0\nbaseline_days = 7\nmin_spend_floor_usd = 1.0\n"
        "cooldown_minutes = 360\n"
    )
    seed_spike(ledger.session_factory, utcnow())

    result = ledger.runner.invoke(app, ["alerts", "check", "--config", str(config_path)])
    assert result.exit_code == 1, result.output  # new firing
    assert "spike" in result.output and "FIRED" in result.output
    assert "unpriced" in result.output

    result = ledger.runner.invoke(app, ["alerts", "check", "--config", str(config_path)])
    assert result.exit_code == 0, result.output  # cooldown: no new firing
    assert "cooldown" in result.output
    assert len(webhook_calls) == 1

    missing = ledger.runner.invoke(app, ["alerts", "check", "--config", str(tmp_path / "nope")])
    assert missing.exit_code == 2


def test_load_config_parses_and_validates(tmp_path: Path) -> None:
    path = tmp_path / "ledgerlm.toml"
    path.write_text(
        "[alerts]\ndaily_budget_usd = 25.5\nspike_multiplier = 3.0\nbaseline_days = 3\n"
        'webhook_secret = "shh"\n'
    )
    config = load_config(path)
    assert config.daily_budget_usd == Decimal("25.5")
    assert config.spike_multiplier == Decimal("3.0")
    assert config.baseline_days == 3
    assert config.webhook_secret == "shh"
    # Unset keys keep their DESIGN.md defaults.
    assert config.min_spend_floor_usd == Decimal("1.00")
    assert config.cooldown_minutes == 360

    with pytest.raises(AlertConfigError):
        load_config(tmp_path / "missing.toml")
    (tmp_path / "empty.toml").write_text("[other]\n")
    with pytest.raises(AlertConfigError):
        load_config(tmp_path / "empty.toml")


def test_dashboard_background_tick_persists_firing(ledger: Ledger, tmp_path: Path) -> None:
    """The dashboard tick runs the same check_alerts path (no webhook needed)."""
    import time

    from fastapi.testclient import TestClient

    from ledgerlm.dashboard.app import create_app

    config_path = tmp_path / "ledgerlm.toml"
    config_path.write_text("[alerts]\nspike_multiplier = 2.0\nbaseline_days = 7\n")
    seed_spike(ledger.session_factory, utcnow())

    app_ = create_app(ledger.session_factory, alerts_every=3600, alerts_config=config_path)
    with TestClient(app_):  # lifespan starts the loop; first tick is immediate
        deadline = time.monotonic() + 5
        firings: list[AlertFiring] = []
        while time.monotonic() < deadline and not firings:
            with ledger.session_factory() as session:
                firings = list(session.query(AlertFiring).all())
            time.sleep(0.05)
    assert len(firings) == 1
    assert firings[0].rule == "spike"
    assert firings[0].delivered is False  # no webhook configured
