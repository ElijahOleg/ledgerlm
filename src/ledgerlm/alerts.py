"""Alert evaluation and webhook delivery (Phase 3).

Config lives in ``ledgerlm.toml`` (D11); only firings are persisted, for
cooldown dedupe and a delivery audit trail. Evaluation is read-only over
llm_events. Spend sums are computed in Decimal — never float — because
``observed`` and ``threshold`` are *stored* to alert_firings
(see .claude/skills/computing-costs). Unpriced rows are excluded from every
sum and their count travels with the payload: a window full of unpriced rows
must not look like a quiet one.

The only network call LedgerLM ever makes is the user-configured webhook
POST here (DESIGN.md §3.5). Delivery failures are recorded
(``delivered=False``), never raised.

Both evaluation surfaces — ``ledgerlm alerts check`` and the optional
dashboard background tick — go through :func:`check_alerts`.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ledgerlm.db.models import AlertFiring, LlmEvent, utcnow

logger = logging.getLogger("ledgerlm")

DEFAULT_CONFIG_PATH = Path("ledgerlm.toml")
WEBHOOK_SECRET_HEADER = "X-LedgerLM-Secret"
WEBHOOK_TIMEOUT_SECONDS = 10.0
TOP_CONTRIBUTORS_LIMIT = 5

RULE_BUDGET = "budget"
RULE_SPIKE = "spike"

_KNOWN_KEYS = {
    "daily_budget_usd",
    "spike_multiplier",
    "baseline_days",
    "min_spend_floor_usd",
    "webhook_url",
    "webhook_secret",
    "cooldown_minutes",
}


class AlertConfigError(ValueError):
    """ledgerlm.toml is missing, unreadable, or has no valid [alerts] table."""


@dataclass(frozen=True)
class AlertConfig:
    daily_budget_usd: Decimal | None = None
    spike_multiplier: Decimal = Decimal("2.0")
    baseline_days: int = 7
    min_spend_floor_usd: Decimal = Decimal("1.00")
    webhook_url: str | None = None
    webhook_secret: str | None = None
    cooldown_minutes: int = 360


def _decimal_key(table: dict[str, object], key: str) -> Decimal | None:
    value = table.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise AlertConfigError(f"[alerts] {key} must be a number, got {value!r}") from exc


def load_config(path: Path | None = None) -> AlertConfig:
    """Parse the ``[alerts]`` table of ledgerlm.toml into an AlertConfig."""
    path = path or DEFAULT_CONFIG_PATH
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise AlertConfigError(f"alert config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AlertConfigError(f"invalid TOML in {path}: {exc}") from exc
    alerts = raw.get("alerts")
    if not isinstance(alerts, dict):
        raise AlertConfigError(f"no [alerts] table in {path}")
    unknown = sorted(set(alerts) - _KNOWN_KEYS)
    if unknown:
        logger.warning("ledgerlm: ignoring unknown [alerts] keys in %s: %s", path, unknown)
    defaults = AlertConfig()
    return AlertConfig(
        daily_budget_usd=_decimal_key(alerts, "daily_budget_usd"),
        spike_multiplier=_decimal_key(alerts, "spike_multiplier") or defaults.spike_multiplier,
        baseline_days=int(alerts.get("baseline_days", defaults.baseline_days)),
        min_spend_floor_usd=_decimal_key(alerts, "min_spend_floor_usd")
        or defaults.min_spend_floor_usd,
        webhook_url=alerts.get("webhook_url") or None,
        webhook_secret=alerts.get("webhook_secret") or None,
        cooldown_minutes=int(alerts.get("cooldown_minutes", defaults.cooldown_minutes)),
    )


@dataclass(frozen=True)
class Evaluation:
    """One rule's verdict for one window (before cooldown is considered)."""

    rule: str
    window_start: dt.datetime
    window_end: dt.datetime
    observed: Decimal
    threshold: Decimal
    unpriced: int
    fired: bool


@dataclass(frozen=True)
class RuleOutcome:
    evaluation: Evaluation
    suppressed_by_cooldown: bool
    firing: AlertFiring | None  # persisted row; None unless a new firing happened
    redelivered: bool = False  # a previously undelivered firing was delivered this check (D25)


def _window_spend(session: Session, start: dt.datetime, end: dt.datetime) -> tuple[Decimal, int]:
    """(priced spend summed in Decimal, unpriced-row count) for [start, end)."""
    costs = (
        session.execute(select(LlmEvent.cost_usd).where(LlmEvent.ts >= start, LlmEvent.ts < end))
        .scalars()
        .all()
    )
    total = sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))
    unpriced = sum(1 for c in costs if c is None)
    return total, unpriced


def evaluate_rules(
    session: Session, config: AlertConfig, now: dt.datetime | None = None
) -> list[Evaluation]:
    """Evaluate both rules against the ledger; pure read, no persistence."""
    now = now or utcnow()
    evaluations: list[Evaluation] = []

    if config.daily_budget_usd is not None:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        observed, unpriced = _window_spend(session, day_start, now)
        evaluations.append(
            Evaluation(
                rule=RULE_BUDGET,
                window_start=day_start,
                window_end=now,
                observed=observed,
                threshold=config.daily_budget_usd,
                unpriced=unpriced,
                fired=observed >= config.daily_budget_usd,
            )
        )

    if config.baseline_days >= 1:
        window_start = now - dt.timedelta(hours=24)
        observed, unpriced = _window_spend(session, window_start, now)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_totals: list[Decimal] = []
        for age in range(1, config.baseline_days + 1):
            day = today - dt.timedelta(days=age)
            day_total, _ = _window_spend(session, day, day + dt.timedelta(days=1))
            daily_totals.append(day_total)  # empty days count as 0 — honest baseline
        baseline_median = Decimal(statistics.median(daily_totals))
        threshold = config.spike_multiplier * baseline_median
        evaluations.append(
            Evaluation(
                rule=RULE_SPIKE,
                window_start=window_start,
                window_end=now,
                observed=observed,
                threshold=threshold,
                unpriced=unpriced,
                fired=observed >= threshold and observed >= config.min_spend_floor_usd,
            )
        )

    return evaluations


def _governing_firing(
    session: Session, rule: str, now: dt.datetime, cooldown_minutes: int
) -> AlertFiring | None:
    """The rule's most recent firing if it is still inside its cooldown window."""
    firing = session.execute(
        select(AlertFiring)
        .where(AlertFiring.rule == rule)
        .order_by(AlertFiring.fired_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if firing is None:
        return None
    fired_at = firing.fired_at
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=dt.UTC)
    if now - fired_at < dt.timedelta(minutes=cooldown_minutes):
        return firing
    return None


def top_contributors(
    session: Session,
    start: dt.datetime,
    end: dt.datetime,
    limit: int = TOP_CONTRIBUTORS_LIMIT,
) -> list[dict[str, object]]:
    """Top (project, model) groups by priced spend in the window."""
    rows = session.execute(
        select(
            LlmEvent.project,
            LlmEvent.model,
            func.sum(LlmEvent.cost_usd),
            func.count(),
        )
        .where(LlmEvent.ts >= start, LlmEvent.ts < end, LlmEvent.cost_usd.is_not(None))
        .group_by(LlmEvent.project, LlmEvent.model)
        .order_by(func.sum(LlmEvent.cost_usd).desc())
        .limit(limit)
    ).all()
    return [
        {
            "project": project,
            "model": model,
            "cost_usd": str(Decimal(str(cost))),
            "calls": int(calls),
        }
        for project, model, cost, calls in rows
    ]


def build_payload(session: Session, evaluation: Evaluation) -> dict[str, object]:
    """The webhook JSON body. Money as strings — exact, never float."""
    return {
        "rule": evaluation.rule,
        "window": {
            "start": evaluation.window_start.isoformat(),
            "end": evaluation.window_end.isoformat(),
        },
        "observed": str(evaluation.observed),
        "threshold": str(evaluation.threshold),
        "unpriced_rows": evaluation.unpriced,
        "top_contributors": top_contributors(
            session, evaluation.window_start, evaluation.window_end
        ),
    }


def deliver_webhook(config: AlertConfig, payload: dict[str, object]) -> tuple[bool, int | None]:
    """POST the firing; (delivered, http_status). Failures are logged, never raised."""
    if not config.webhook_url:
        return False, None
    import httpx

    headers = {}
    if config.webhook_secret:
        headers[WEBHOOK_SECRET_HEADER] = config.webhook_secret
    try:
        response = httpx.post(
            config.webhook_url,
            json=payload,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("ledgerlm: alert webhook delivery failed: %s", exc)
        return False, None
    if not response.is_success:
        logger.warning("ledgerlm: alert webhook returned HTTP %s", response.status_code)
    return response.is_success, response.status_code


def _retry_undelivered(session: Session, config: AlertConfig, governing: AlertFiring) -> bool:
    """Reattempt a governing firing's webhook once (D25); update the row in place.

    Cooldown suppresses NEW firings, not delivery of the one that already
    fired — a webhook outage must not silently eat the alert that mattered.
    Never a new row; the payload is rebuilt from the stored firing.
    """
    _total, unpriced = _window_spend(session, governing.window_start, governing.window_end)
    payload = build_payload(
        session,
        Evaluation(
            rule=governing.rule,
            window_start=governing.window_start,
            window_end=governing.window_end,
            observed=Decimal(governing.observed),
            threshold=Decimal(governing.threshold),
            unpriced=unpriced,
            fired=True,
        ),
    )
    delivered, status = deliver_webhook(config, payload)
    governing.delivered = delivered
    governing.response_status = status
    session.commit()
    return delivered


def check_alerts(
    session: Session, config: AlertConfig, now: dt.datetime | None = None
) -> list[RuleOutcome]:
    """Evaluate, dedupe against cooldown, persist new firings, deliver.

    The single evaluation path behind both ``ledgerlm alerts check`` and the
    dashboard background tick. A firing is persisted whether or not delivery
    succeeds — cooldown is about alert noise, not webhook health; an
    undelivered firing is retried once per subsequent check until it lands
    (D25). Exit-code/outcome semantics are unaffected by redelivery.
    """
    now = now or utcnow()
    outcomes: list[RuleOutcome] = []
    for evaluation in evaluate_rules(session, config, now):
        governing = _governing_firing(session, evaluation.rule, now, config.cooldown_minutes)
        redelivered = False
        if governing is not None and not governing.delivered and config.webhook_url:
            redelivered = _retry_undelivered(session, config, governing)
        if not evaluation.fired:
            outcomes.append(
                RuleOutcome(
                    evaluation,
                    suppressed_by_cooldown=False,
                    firing=None,
                    redelivered=redelivered,
                )
            )
            continue
        if governing is not None:
            outcomes.append(
                RuleOutcome(
                    evaluation,
                    suppressed_by_cooldown=True,
                    firing=None,
                    redelivered=redelivered,
                )
            )
            continue
        payload = build_payload(session, evaluation)
        delivered, status = deliver_webhook(config, payload)
        firing = AlertFiring(
            rule=evaluation.rule,
            window_start=evaluation.window_start,
            window_end=evaluation.window_end,
            observed=evaluation.observed,
            threshold=evaluation.threshold,
            fired_at=now,
            delivered=delivered,
            response_status=status,
        )
        session.add(firing)
        session.commit()
        outcomes.append(RuleOutcome(evaluation, suppressed_by_cooldown=False, firing=firing))
    return outcomes
