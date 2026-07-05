"""CSV export (Phase 3). Read-only; data leaves the ledger losslessly.

Money exits as exact Decimal strings (never float-formatted); unpriced rows
export with an EMPTY cost_usd cell — empty means "unknown", which must stay
distinguishable from a real $0 forever (computing-costs rule 3). JSON columns
(raw_usage, price_snapshot, tags) are serialized verbatim so every exported
row remains independently recomputable, like the ledger row it came from.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from decimal import Decimal
from typing import IO, Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ledgerlm.db.models import LlmEvent

EVENT_COLUMNS = (
    "id",
    "ts",
    "provider",
    "model",
    "status",
    "error_type",
    "latency_ms",
    "first_token_ms",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "prompt_hash",
    "project",
    "feature",
    "env",
    "run_id",
    "customer",
    "tags",
    "provider_request_id",
    "raw_usage",
    "price_snapshot",
    "created_at",
)

SUMMARY_DIMENSIONS = ("provider", "model", "project", "feature")


def _cell(value: Any) -> str:
    """One CSV cell: None -> empty, Decimal -> exact string, datetime -> ISO,
    dict -> JSON. Spreadsheets open it; a reader round-trips it."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def export_events(session: Session, since: dt.datetime | None, out: IO[str]) -> tuple[int, int]:
    """Write one CSV row per llm_events row in the window; (rows, unpriced)."""
    writer = csv.writer(out)
    writer.writerow(EVENT_COLUMNS)
    stmt = select(LlmEvent).order_by(LlmEvent.ts)
    if since is not None:
        stmt = stmt.where(LlmEvent.ts >= since)
    rows = 0
    unpriced = 0
    for event in session.execute(stmt).scalars():
        writer.writerow(_cell(getattr(event, column)) for column in EVENT_COLUMNS)
        rows += 1
        if event.cost_usd is None:
            unpriced += 1
    return rows, unpriced


def export_summary(
    session: Session, since: dt.datetime | None, by: str | None, out: IO[str]
) -> tuple[int, int]:
    """Write grouped totals (same shape as `ledgerlm summary`); (rows, unpriced).

    Cost totals exclude unpriced rows; the unpriced count is its own column on
    every row — a cost surface never ships without it.
    """
    label = by or "scope"
    measures = (
        func.count(),
        func.coalesce(func.sum(LlmEvent.input_tokens), 0),
        func.coalesce(func.sum(LlmEvent.output_tokens), 0),
        func.coalesce(func.sum(LlmEvent.cache_read_tokens), 0),
        func.coalesce(func.sum(LlmEvent.cache_write_tokens), 0),
        func.sum(LlmEvent.cost_usd),
        func.sum(case((LlmEvent.cost_usd.is_(None), 1), else_=0)),
    )
    conditions = [] if since is None else [LlmEvent.ts >= since]
    if by is None:
        result = [("(all)", *session.execute(select(*measures).where(*conditions)).one())]
    else:
        col = getattr(LlmEvent, by)
        result = [
            tuple(row)
            for row in session.execute(
                select(func.coalesce(col, "(none)"), *measures)
                .where(*conditions)
                .group_by(col)
                .order_by(func.sum(LlmEvent.cost_usd).desc())
            ).all()
        ]

    writer = csv.writer(out)
    writer.writerow(
        (
            label,
            "calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "unpriced",
        )
    )
    total_unpriced = 0
    for key, calls, t_in, t_out, t_cr, t_cw, cost, unpriced in result:
        # Display-surface aggregation: the dialect's SUM is accepted here, but
        # the cell is still rendered from Decimal, not float formatting.
        cost_cell = "" if cost is None else str(Decimal(str(cost)))
        writer.writerow((key, calls, t_in, t_out, t_cr, t_cw, cost_cell, int(unpriced or 0)))
        total_unpriced += int(unpriced or 0)
    return len(result), total_unpriced
