"""Arithmetic-only optimizer report (Phase 3). Read-only; nothing is stored.

Three analyses over the ledger: what-if repricing of identical token buckets
at other models' current rates, token-heavy calls above their feature's p95
input size, and repeated-prompt groups that never hit cache. All claims are
arithmetic (DESIGN.md §3.9): every surface rendering this report carries
:data:`DISCLAIMER` verbatim — candidates for experiments, never verdicts.

Money rules (see .claude/skills/computing-costs): candidate repricing runs
through the same Decimal ``compute_cost`` path as the recorder; groups are
repriced over their PRICED rows only, so "current" and "what-if" cover the
same calls, with each group's unpriced-row count reported beside its cost.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import Session

from ledgerlm.db.models import LlmEvent, ModelPrice
from ledgerlm.pricing import MTOK, Rates, compute_cost
from ledgerlm.providers.base import NormalizedUsage

DISCLAIMER = (
    "Repricing is arithmetic on identical tokens — it is not a claim that output "
    "quality would match; treat these as candidates for experiments."
)

CACHE_SAVINGS_ASSUMPTION = (
    "Savings estimate: repeat input tokens (total minus one call's worth) at the "
    "cache-read rate instead of the uncached input rate; the first call's "
    "cache-write premium is not modeled."
)

PCT_QUANTUM = Decimal("0.1")
_SAVINGS_QUANTUM = Decimal("0.0000000001")  # matches pricing.COST_QUANTUM

DEFAULT_GROUP_LIMIT = 10
DEFAULT_CALL_LIMIT = 20
DEFAULT_MIN_REPEATS = 3
CANDIDATES_PER_GROUP = 3


@dataclass(frozen=True)
class OptimizerFilters:
    """Event-selection filters shared by all three analyses."""

    since: dt.datetime | None = None
    provider: str | None = None
    model: str | None = None
    project: str | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        conds: list[ColumnElement[bool]] = []
        if self.since is not None:
            conds.append(LlmEvent.ts >= self.since)
        if self.provider:
            conds.append(LlmEvent.provider == self.provider)
        if self.model:
            conds.append(LlmEvent.model == self.model)
        if self.project:
            conds.append(LlmEvent.project == self.project)
        return conds


@dataclass(frozen=True)
class RepricingCandidate:
    provider: str
    model: str
    cost_usd: Decimal  # the group's identical buckets at this model's current rates
    delta_pct: Decimal  # negative = cheaper


@dataclass(frozen=True)
class RepricingGroup:
    project: str | None
    feature: str | None
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    current_cost: Decimal  # sum over the group's priced rows
    unpriced: int  # rows excluded from both current and what-if figures
    candidates: list[RepricingCandidate]


@dataclass(frozen=True)
class TokenHeavyCall:
    id: int
    ts: dt.datetime
    project: str | None
    feature: str | None
    provider: str
    model: str
    input_tokens: int
    feature_p95: int
    cost_usd: Decimal | None  # None = unpriced


@dataclass(frozen=True)
class CacheCandidate:
    provider: str
    model: str
    prompt_hash: str
    repeats: int
    input_tokens_total: int
    repeat_input_tokens: int  # total minus one call's worth (the largest)
    est_savings_usd: Decimal | None  # None = model has no input or cache-read rate


@dataclass(frozen=True)
class OptimizerReport:
    disclaimer: str
    window_unpriced: int  # unpriced rows in the window — shown on every surface
    repricing: list[RepricingGroup]
    token_heavy: list[TokenHeavyCall]
    cache_candidates: list[CacheCandidate]


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _rate_map(session: Session) -> dict[tuple[str, str], Rates]:
    rows = session.execute(select(ModelPrice)).scalars().all()
    return {
        (p.provider, p.model): Rates(
            input_per_mtok=Decimal(p.input_per_mtok),
            output_per_mtok=Decimal(p.output_per_mtok),
            cache_read_per_mtok=None
            if p.cache_read_per_mtok is None
            else Decimal(p.cache_read_per_mtok),
            cache_write_per_mtok=None
            if p.cache_write_per_mtok is None
            else Decimal(p.cache_write_per_mtok),
        )
        for p in rows
    }


def whatif_repricing(
    session: Session,
    filters: OptimizerFilters,
    limit: int = DEFAULT_GROUP_LIMIT,
) -> list[RepricingGroup]:
    """(a) Each (project, feature, model) group's exact buckets at other models' rates."""
    group_cols = (LlmEvent.project, LlmEvent.feature, LlmEvent.provider, LlmEvent.model)

    priced = session.execute(
        select(
            *group_cols,
            func.count(),
            func.coalesce(func.sum(LlmEvent.input_tokens), 0),
            func.coalesce(func.sum(LlmEvent.output_tokens), 0),
            func.coalesce(func.sum(LlmEvent.cache_read_tokens), 0),
            func.coalesce(func.sum(LlmEvent.cache_write_tokens), 0),
            func.sum(LlmEvent.cost_usd),
        )
        .where(LlmEvent.cost_usd.is_not(None), *filters.conditions())
        .group_by(*group_cols)
        .order_by(func.sum(LlmEvent.cost_usd).desc())
        .limit(limit)
    ).all()

    unpriced_counts = {
        (row[0], row[1], row[2], row[3]): int(row[4])
        for row in session.execute(
            select(*group_cols, func.count())
            .where(LlmEvent.cost_usd.is_(None), *filters.conditions())
            .group_by(*group_cols)
        ).all()
    }

    rates = _rate_map(session)
    groups: list[RepricingGroup] = []
    for project, feature, provider, model, calls, t_in, t_out, t_cr, t_cw, cost in priced:
        current = _dec(cost)
        usage = NormalizedUsage(
            input_tokens=int(t_in),
            output_tokens=int(t_out),
            cache_read_tokens=int(t_cr) or None,
            cache_write_tokens=int(t_cw) or None,
        )
        candidates: list[RepricingCandidate] = []
        if current > 0:
            for (cand_provider, cand_model), cand_rates in rates.items():
                if (cand_provider, cand_model) == (provider, model):
                    continue
                priced_result = compute_cost(usage, cand_rates)
                if priced_result is None:  # a nonzero bucket has no rate: not comparable
                    continue
                cand_cost, _snapshot = priced_result
                if cand_cost >= current:
                    continue
                delta = ((cand_cost - current) / current * 100).quantize(PCT_QUANTUM)
                candidates.append(
                    RepricingCandidate(
                        provider=cand_provider,
                        model=cand_model,
                        cost_usd=cand_cost,
                        delta_pct=delta,
                    )
                )
            candidates.sort(key=lambda c: c.cost_usd)
        groups.append(
            RepricingGroup(
                project=project,
                feature=feature,
                provider=provider,
                model=model,
                calls=int(calls),
                input_tokens=int(t_in),
                output_tokens=int(t_out),
                cache_read_tokens=int(t_cr),
                cache_write_tokens=int(t_cw),
                current_cost=current,
                unpriced=unpriced_counts.get((project, feature, provider, model), 0),
                candidates=candidates[:CANDIDATES_PER_GROUP],
            )
        )
    return groups


def _p95_nearest_rank(sorted_values: list[int]) -> int:
    """Nearest-rank p95: the value at ceil(0.95 * n) in the sorted list."""
    index = max(0, math.ceil(0.95 * len(sorted_values)) - 1)
    return sorted_values[index]


def token_heavy_calls(
    session: Session,
    filters: OptimizerFilters,
    limit: int = DEFAULT_CALL_LIMIT,
) -> list[TokenHeavyCall]:
    """(b) Calls above the p95 input-token count within their feature."""
    per_feature: dict[str | None, list[int]] = {}
    for feature, tokens in session.execute(
        select(LlmEvent.feature, LlmEvent.input_tokens).where(*filters.conditions())
    ).all():
        per_feature.setdefault(feature, []).append(int(tokens))

    p95s = {feature: _p95_nearest_rank(sorted(values)) for feature, values in per_feature.items()}
    if not p95s:
        return []

    above = [
        and_(
            LlmEvent.feature.is_(None) if feature is None else LlmEvent.feature == feature,
            LlmEvent.input_tokens > p95,
        )
        for feature, p95 in p95s.items()
    ]
    events = (
        session.execute(
            select(LlmEvent)
            .where(or_(*above), *filters.conditions())
            .order_by(LlmEvent.input_tokens.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        TokenHeavyCall(
            id=e.id,
            ts=e.ts,
            project=e.project,
            feature=e.feature,
            provider=e.provider,
            model=e.model,
            input_tokens=e.input_tokens,
            feature_p95=p95s[e.feature],
            cost_usd=None if e.cost_usd is None else Decimal(e.cost_usd),
        )
        for e in events
    ]


def cache_candidates(
    session: Session,
    filters: OptimizerFilters,
    min_repeats: int = DEFAULT_MIN_REPEATS,
    limit: int = DEFAULT_CALL_LIMIT,
) -> list[CacheCandidate]:
    """(c) prompt_hash groups repeated >= min_repeats with zero cache reads."""
    cache_reads = func.coalesce(func.sum(func.coalesce(LlmEvent.cache_read_tokens, 0)), 0)
    rows = session.execute(
        select(
            LlmEvent.provider,
            LlmEvent.model,
            LlmEvent.prompt_hash,
            func.count(),
            func.sum(LlmEvent.input_tokens),
            func.max(LlmEvent.input_tokens),
        )
        .where(LlmEvent.prompt_hash.is_not(None), *filters.conditions())
        .group_by(LlmEvent.provider, LlmEvent.model, LlmEvent.prompt_hash)
        .having(func.count() >= min_repeats, cache_reads == 0)
        .order_by(func.sum(LlmEvent.input_tokens).desc())
        .limit(limit)
    ).all()

    rates = _rate_map(session)
    out: list[CacheCandidate] = []
    for provider, model, prompt_hash, repeats, total_in, max_in in rows:
        repeat_tokens = int(total_in) - int(max_in)  # conservative: drop the largest call
        model_rates = rates.get((provider, model))
        savings: Decimal | None = None
        if model_rates is not None and model_rates.cache_read_per_mtok is not None:
            per_tok_delta = model_rates.input_per_mtok - model_rates.cache_read_per_mtok
            savings = (Decimal(repeat_tokens) * per_tok_delta / MTOK).quantize(_SAVINGS_QUANTUM)
        out.append(
            CacheCandidate(
                provider=provider,
                model=model,
                prompt_hash=prompt_hash,
                repeats=int(repeats),
                input_tokens_total=int(total_in),
                repeat_input_tokens=repeat_tokens,
                est_savings_usd=savings,
            )
        )
    return out


def build_report(
    session: Session,
    filters: OptimizerFilters,
    group_limit: int = DEFAULT_GROUP_LIMIT,
    call_limit: int = DEFAULT_CALL_LIMIT,
    min_repeats: int = DEFAULT_MIN_REPEATS,
) -> OptimizerReport:
    window_unpriced = session.execute(
        select(func.sum(case((LlmEvent.cost_usd.is_(None), 1), else_=0))).where(
            *filters.conditions()
        )
    ).scalar_one()
    return OptimizerReport(
        disclaimer=DISCLAIMER,
        window_unpriced=int(window_unpriced or 0),
        repricing=whatif_repricing(session, filters, limit=group_limit),
        token_heavy=token_heavy_calls(session, filters, limit=call_limit),
        cache_candidates=cache_candidates(
            session, filters, min_repeats=min_repeats, limit=call_limit
        ),
    )
