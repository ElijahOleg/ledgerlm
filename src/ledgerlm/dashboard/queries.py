"""Read-only aggregation for the dashboard. ALL aggregation SQL lives here.

Dialect isolation: tag-JSON queries use ``json_each`` on SQLite and the
``->>`` / ``json_object_keys`` operators on Postgres — nothing outside this
module may branch on dialect. Display-surface aggregation accepts the
dialect's numeric behavior (see .claude/skills/computing-costs); values that
get *stored* always go through the Decimal path in pricing.py.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from ledgerlm.db.models import LlmEvent, ModelPrice, utcnow

# Promoted, indexed dimensions the attribution page can group by directly.
PROMOTED_DIMENSIONS: dict[str, InstrumentedAttribute[str | None] | InstrumentedAttribute[str]] = {
    "provider": LlmEvent.provider,
    "model": LlmEvent.model,
    "project": LlmEvent.project,
    "feature": LlmEvent.feature,
    "env": LlmEvent.env,
    "run_id": LlmEvent.run_id,
    "customer": LlmEvent.customer,
}

# Display-layer price annotations for entries with a known expiry or
# introductory rate. The schema deliberately has no column for this (the DB
# holds only the rates applied); revisit if the list grows past a handful.
PRICE_NOTES: dict[tuple[str, str], str] = {
    (
        "anthropic",
        "claude-sonnet-5",
    ): "Introductory rate ends 2026-08-31; standard pricing (3.00 in / 15.00 out) from 2026-09-01",
}

STALE_AFTER_DAYS = 60


@dataclass(frozen=True)
class Filters:
    """The global filter set: date range plus provider/model/project."""

    since: dt.datetime | None = None
    until: dt.datetime | None = None
    provider: str | None = None
    model: str | None = None
    project: str | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        conds: list[ColumnElement[bool]] = []
        if self.since is not None:
            conds.append(LlmEvent.ts >= self.since)
        if self.until is not None:
            conds.append(LlmEvent.ts < self.until)
        if self.provider:
            conds.append(LlmEvent.provider == self.provider)
        if self.model:
            conds.append(LlmEvent.model == self.model)
        if self.project:
            conds.append(LlmEvent.project == self.project)
        return conds


@dataclass(frozen=True)
class Totals:
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal | None
    unpriced: int


@dataclass(frozen=True)
class GroupRow:
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal | None
    unpriced: int


@dataclass(frozen=True)
class DayPoint:
    day: str  # ISO date "YYYY-MM-DD"
    calls: int
    cost_usd: Decimal
    unpriced: int


@dataclass(frozen=True)
class TopCall:
    id: int
    ts: dt.datetime
    provider: str
    model: str
    status: str
    project: str | None
    feature: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: int
    prompt_hash: str | None
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceRow:
    provider: str
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None
    cache_write_per_mtok: Decimal | None
    last_verified: dt.date | None
    days_since_verified: int | None  # None = never verified (UNVERIFIED)
    stale: bool
    note: str | None


def _dec(value: Any) -> Decimal | None:
    """Dialect numeric → Decimal at the display edge (never stored)."""
    return None if value is None else Decimal(str(value))


# Shared measure columns; every surface that shows cost also gets the
# unpriced count (computing-costs rule 5) and cache buckets beside in/out.
def _measures() -> tuple[Any, ...]:
    return (
        func.count(),
        func.coalesce(func.sum(LlmEvent.input_tokens), 0),
        func.coalesce(func.sum(LlmEvent.output_tokens), 0),
        func.coalesce(func.sum(LlmEvent.cache_read_tokens), 0),
        func.coalesce(func.sum(LlmEvent.cache_write_tokens), 0),
        func.sum(LlmEvent.cost_usd),
        func.sum(case((LlmEvent.cost_usd.is_(None), 1), else_=0)),
    )


def overview_totals(session: Session, filters: Filters) -> Totals:
    errors = func.sum(case((LlmEvent.status == "error", 1), else_=0))
    row = session.execute(select(*_measures(), errors).where(*filters.conditions())).one()
    calls, tok_in, tok_out, cache_r, cache_w, cost, unpriced, error_count = row
    return Totals(
        calls=int(calls),
        errors=int(error_count or 0),
        input_tokens=int(tok_in),
        output_tokens=int(tok_out),
        cache_read_tokens=int(cache_r),
        cache_write_tokens=int(cache_w),
        cost_usd=_dec(cost),
        unpriced=int(unpriced or 0),
    )


def spend_by_day(session: Session, filters: Filters) -> list[DayPoint]:
    day = func.date(LlmEvent.ts)
    rows = session.execute(
        select(
            day,
            func.count(),
            func.sum(LlmEvent.cost_usd),
            func.sum(case((LlmEvent.cost_usd.is_(None), 1), else_=0)),
        )
        .where(*filters.conditions())
        .group_by(day)
        .order_by(day)
    ).all()
    return [
        DayPoint(
            day=str(d),
            calls=int(calls),
            cost_usd=_dec(cost) or Decimal(0),
            unpriced=int(unpriced or 0),
        )
        for d, calls, cost, unpriced in rows
    ]


def _group_rows(rows: list[Any]) -> list[GroupRow]:
    return [
        GroupRow(
            key="(none)" if key is None else str(key),
            calls=int(calls),
            input_tokens=int(tok_in),
            output_tokens=int(tok_out),
            cache_read_tokens=int(cache_r),
            cache_write_tokens=int(cache_w),
            cost_usd=_dec(cost),
            unpriced=int(unpriced or 0),
        )
        for key, calls, tok_in, tok_out, cache_r, cache_w, cost, unpriced in rows
    ]


def group_totals(session: Session, filters: Filters, dimension: str) -> list[GroupRow]:
    """Group by one of the promoted columns; rows ordered by cost desc."""
    col = PROMOTED_DIMENSIONS[dimension]
    rows = session.execute(
        select(col, *_measures())
        .where(*filters.conditions())
        .group_by(col)
        .order_by(func.sum(LlmEvent.cost_usd).desc().nulls_last())
    ).all()
    return _group_rows(list(rows))


def group_totals_by_tag(session: Session, filters: Filters, tag_key: str) -> list[GroupRow]:
    """Group by an arbitrary key inside the tags JSON column.

    SQLite: FROM llm_events, json_each(llm_events.tags) WHERE json_each.key = :key
    Postgres: GROUP BY tags ->> :key
    Both keep the key fully parameterized (no path-string interpolation).
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        je = func.json_each(LlmEvent.tags).table_valued("key", "value", joins_implicitly=True)
        value: ColumnElement[Any] = je.c.value
        stmt = (
            select(value, *_measures())
            .select_from(LlmEvent)
            .where(je.c.key == tag_key, *filters.conditions())
            .group_by(value)
        )
    else:
        value = LlmEvent.tags.op("->>")(tag_key)
        stmt = (
            select(value, *_measures())
            .where(value.is_not(None), *filters.conditions())
            .group_by(value)
        )
    rows = session.execute(stmt.order_by(func.sum(LlmEvent.cost_usd).desc().nulls_last())).all()
    return _group_rows(list(rows))


def tag_keys(session: Session) -> list[str]:
    """Distinct keys present in the tags JSON column, for the group-by selector."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        je = func.json_each(LlmEvent.tags).table_valued("key", joins_implicitly=True)
        stmt = select(je.c.key).select_from(LlmEvent).distinct()
    else:
        stmt = select(func.json_object_keys(LlmEvent.tags)).distinct()
    return sorted(str(k) for (k,) in session.execute(stmt).all())


def top_calls(session: Session, filters: Filters, limit: int = 50) -> list[TopCall]:
    """The N most expensive calls in the window; unpriced rows sort last."""
    events = (
        session.execute(
            select(LlmEvent)
            .where(*filters.conditions())
            .order_by(LlmEvent.cost_usd.desc().nulls_last(), LlmEvent.ts.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        TopCall(
            id=e.id,
            ts=e.ts,
            provider=e.provider,
            model=e.model,
            status=e.status,
            project=e.project,
            feature=e.feature,
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            cache_read_tokens=e.cache_read_tokens,
            cache_write_tokens=e.cache_write_tokens,
            cost_usd=None if e.cost_usd is None else Decimal(e.cost_usd),
            latency_ms=e.latency_ms,
            prompt_hash=e.prompt_hash,
            tags=e.tags or {},
        )
        for e in events
    ]


def filter_options(session: Session) -> dict[str, list[str]]:
    """Distinct non-null values for the filter dropdowns."""
    out: dict[str, list[str]] = {}
    for name, col in (
        ("providers", LlmEvent.provider),
        ("models", LlmEvent.model),
        ("projects", LlmEvent.project),
    ):
        values = session.execute(select(col).where(col.is_not(None)).distinct()).scalars().all()
        out[name] = sorted(str(v) for v in values)
    return out


def prices(session: Session) -> list[PriceRow]:
    """model_prices with staleness hints and known-expiry notes."""
    today = utcnow().date()
    rows = (
        session.execute(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model))
        .scalars()
        .all()
    )
    out: list[PriceRow] = []
    for p in rows:
        days = None if p.last_verified is None else (today - p.last_verified).days
        out.append(
            PriceRow(
                provider=p.provider,
                model=p.model,
                input_per_mtok=Decimal(p.input_per_mtok),
                output_per_mtok=Decimal(p.output_per_mtok),
                cache_read_per_mtok=None
                if p.cache_read_per_mtok is None
                else Decimal(p.cache_read_per_mtok),
                cache_write_per_mtok=None
                if p.cache_write_per_mtok is None
                else Decimal(p.cache_write_per_mtok),
                last_verified=p.last_verified,
                days_since_verified=days,
                stale=days is None or days > STALE_AFTER_DAYS,
                note=PRICE_NOTES.get((p.provider, p.model)),
            )
        )
    return out
