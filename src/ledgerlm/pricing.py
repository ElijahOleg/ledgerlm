"""Price lookup and cost computation. Decimal end-to-end; unpriced means NULL.

Iron rules (see .claude/skills/computing-costs): never compute in float; if any
rate needed for a nonzero bucket is missing, the whole row is unpriced (NULL,
never $0), with one warning per (provider, model); whenever a cost is written,
the rates applied are written beside it as ``price_snapshot``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerlm.db.models import ModelPrice
from ledgerlm.providers.base import NormalizedUsage

logger = logging.getLogger("ledgerlm")

MTOK = Decimal(1_000_000)
COST_QUANTUM = Decimal("0.0000000001")  # 10 decimal places, matching Numeric(18,10)


@dataclass(frozen=True)
class Rates:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None
    cache_write_per_mtok: Decimal | None


_warned_lock = threading.Lock()
_warned: set[tuple[str, str]] = set()


def _warn_once(provider: str, model: str, reason: str) -> None:
    with _warned_lock:
        if (provider, model) in _warned:
            return
        _warned.add((provider, model))
    logger.warning(
        "ledgerlm: %s for provider=%r model=%r — recording unpriced (cost_usd=NULL). "
        "Fix with: ledgerlm prices set %s %s --input X --output Y; then: ledgerlm prices backfill",
        reason,
        provider,
        model,
        provider,
        model,
    )


def reset_warned_models() -> None:
    """Clear the warn-once cache (tests)."""
    with _warned_lock:
        _warned.clear()


def get_rates(session: Session, provider: str, model: str) -> Rates | None:
    row = session.execute(
        select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model)
    ).scalar_one_or_none()
    if row is None:
        return None
    return Rates(
        input_per_mtok=Decimal(row.input_per_mtok),
        output_per_mtok=Decimal(row.output_per_mtok),
        cache_read_per_mtok=None
        if row.cache_read_per_mtok is None
        else Decimal(row.cache_read_per_mtok),
        cache_write_per_mtok=None
        if row.cache_write_per_mtok is None
        else Decimal(row.cache_write_per_mtok),
    )


def compute_cost(usage: NormalizedUsage, rates: Rates) -> tuple[Decimal, dict[str, str]] | None:
    """(cost_usd, price_snapshot) or None when a nonzero bucket has no rate.

    The snapshot records the per-Mtok rate applied to every nonzero bucket, as
    strings, so the row stays independently recomputable forever.
    """
    buckets: list[tuple[str, int, Decimal | None]] = [
        ("input_per_mtok", usage.input_tokens, rates.input_per_mtok),
        ("cache_read_per_mtok", usage.cache_read_tokens or 0, rates.cache_read_per_mtok),
        ("cache_write_per_mtok", usage.cache_write_tokens or 0, rates.cache_write_per_mtok),
        ("output_per_mtok", usage.output_tokens, rates.output_per_mtok),
    ]
    total = Decimal(0)
    snapshot: dict[str, str] = {}
    for rate_name, tokens, rate in buckets:
        if tokens == 0:
            continue
        if rate is None:
            return None
        total += Decimal(tokens) * rate / MTOK
        snapshot[rate_name] = str(rate)
    return total.quantize(COST_QUANTUM), snapshot


def price_usage(
    session: Session, provider: str, model: str, usage: NormalizedUsage
) -> tuple[Decimal | None, dict[str, str] | None]:
    """Full pricing path: look up rates, compute, warn once on any gap."""
    rates = get_rates(session, provider, model)
    if rates is None:
        _warn_once(provider, model, "no price entry")
        return None, None
    result = compute_cost(usage, rates)
    if result is None:
        _warn_once(provider, model, "missing rate for a nonzero token bucket")
        return None, None
    return result
