"""Typer CLI: init, summary, prices list/set/backfill."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from sqlalchemy import func, select

import ledgerlm.db
from ledgerlm.config import get_settings
from ledgerlm.db.models import LlmEvent, ModelPrice, utcnow
from ledgerlm.db.session import create_db_engine, create_session_factory
from ledgerlm.pricing import Rates, compute_cost
from ledgerlm.providers.base import NormalizedUsage

app = typer.Typer(
    help="LedgerLM: local-first cost ledger for LLM API calls.", no_args_is_help=True
)
prices_app = typer.Typer(help="Manage the model price table.", no_args_is_help=True)
app.add_typer(prices_app, name="prices")


def _session_factory() -> Any:
    return create_session_factory(create_db_engine())


def _seed_path() -> Path | None:
    packaged = Path(ledgerlm.db.__file__).parents[1] / "data" / "prices_seed.yaml"
    if packaged.exists():
        return packaged
    repo_root = Path(ledgerlm.db.__file__).parents[3] / "data" / "prices_seed.yaml"
    if repo_root.exists():
        return repo_root
    return None


@app.command()
def init() -> None:
    """Create/upgrade the ledger schema; seed prices if the table is empty."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(ledgerlm.db.__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.resolved_db_url)
    command.upgrade(cfg, "head")
    typer.echo(f"schema up to date: {settings.resolved_db_url}")

    with _session_factory()() as session:
        existing = session.execute(select(func.count()).select_from(ModelPrice)).scalar_one()
        if existing:
            typer.echo(f"model_prices already has {existing} entries; seed not loaded")
            return
        path = _seed_path()
        if path is None:
            typer.echo("prices_seed.yaml not found; no prices seeded")
            return
        entries = yaml.safe_load(path.read_text())["prices"]
        for entry in entries:
            session.add(
                ModelPrice(
                    provider=entry["provider"],
                    model=entry["model"],
                    input_per_mtok=Decimal(str(entry["input_per_mtok"])),
                    output_per_mtok=Decimal(str(entry["output_per_mtok"])),
                    cache_read_per_mtok=_opt_decimal(entry.get("cache_read_per_mtok")),
                    cache_write_per_mtok=_opt_decimal(entry.get("cache_write_per_mtok")),
                    last_verified=entry.get("last_verified"),
                )
            )
        session.commit()
        typer.echo(f"seeded {len(entries)} prices from {path}")


def _opt_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


_SINCE_RE = re.compile(r"^(\d+)([dh])$")

GROUP_COLUMNS = {
    "provider": LlmEvent.provider,
    "model": LlmEvent.model,
    "project": LlmEvent.project,
    "feature": LlmEvent.feature,
}


def _parse_since(since: str) -> dt.datetime:
    match = _SINCE_RE.match(since.strip())
    if not match:
        raise typer.BadParameter(f"invalid --since {since!r}; use forms like 7d, 24h, 30d")
    amount, unit = int(match.group(1)), match.group(2)
    delta = dt.timedelta(days=amount) if unit == "d" else dt.timedelta(hours=amount)
    return utcnow() - delta


@app.command()
def summary(
    since: Annotated[str, typer.Option(help="Window: e.g. 7d, 24h, 30d")] = "7d",
    by: Annotated[
        str | None, typer.Option(help="Group by: provider|model|project|feature")
    ] = None,
) -> None:
    """Calls, tokens in/out, and cost for the window — plus the unpriced-row count."""
    if by is not None and by not in GROUP_COLUMNS:
        raise typer.BadParameter(f"invalid --by {by!r}; choose from {sorted(GROUP_COLUMNS)}")
    cutoff = _parse_since(since)

    from sqlalchemy import case

    # Display-surface aggregation (dialect numeric behavior accepted here);
    # stored values always go through the Decimal path in pricing.py.
    measures = (
        func.count().label("calls"),
        func.coalesce(func.sum(LlmEvent.input_tokens), 0).label("tokens_in"),
        func.coalesce(func.sum(LlmEvent.output_tokens), 0).label("tokens_out"),
        func.sum(LlmEvent.cost_usd).label("cost_usd"),
        func.sum(case((LlmEvent.cost_usd.is_(None), 1), else_=0)).label("unpriced"),
    )

    with _session_factory()() as session:
        if by is None:
            row = session.execute(select(*measures).where(LlmEvent.ts >= cutoff)).one()
            rows = [("(all)", *row)]
            label = "scope"
        else:
            col = GROUP_COLUMNS[by]
            result = session.execute(
                select(func.coalesce(col, "(none)"), *measures)
                .where(LlmEvent.ts >= cutoff)
                .group_by(col)
                .order_by(func.sum(LlmEvent.cost_usd).desc())
            ).all()
            rows = [tuple(r) for r in result]
            label = by

    header = (label, "calls", "tokens_in", "tokens_out", "cost_usd", "unpriced")
    table = [
        (
            str(name),
            str(calls),
            str(tokens_in),
            str(tokens_out),
            "-" if cost is None else f"${Decimal(str(cost)):,.4f}",
            str(unpriced_count or 0),
        )
        for name, calls, tokens_in, tokens_out, cost, unpriced_count in rows
    ]
    widths = [
        max(len(header[i]), *(len(r[i]) for r in table)) if table else len(header[i])
        for i in range(6)
    ]
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    for r in table:
        typer.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    total_unpriced = sum(int(r[5]) for r in table)
    typer.echo(
        f"\nunpriced rows in window: {total_unpriced} "
        "(cost totals exclude unpriced rows; fix with `ledgerlm prices set` + `prices backfill`)"
    )


@prices_app.command("list")
def prices_list() -> None:
    """Show the model price table."""
    with _session_factory()() as session:
        rows = (
            session.execute(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model))
            .scalars()
            .all()
        )
    if not rows:
        typer.echo("no prices configured; run `ledgerlm init` to load the seed")
        return
    header = ("provider", "model", "input", "output", "cache_read", "cache_write", "verified")
    table = [
        (
            p.provider,
            p.model,
            str(p.input_per_mtok),
            str(p.output_per_mtok),
            "-" if p.cache_read_per_mtok is None else str(p.cache_read_per_mtok),
            "-" if p.cache_write_per_mtok is None else str(p.cache_write_per_mtok),
            "-" if p.last_verified is None else p.last_verified.isoformat(),
        )
        for p in rows
    ]
    widths = [max(len(header[i]), *(len(r[i]) for r in table)) for i in range(7)]
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    for r in table:
        typer.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))


def _decimal_arg(value: str, name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"{name} must be a decimal number, got {value!r}") from exc


@prices_app.command("set")
def prices_set(
    provider: str,
    model: str,
    input: Annotated[str, typer.Option("--input", help="USD per Mtok, uncached input")],
    output: Annotated[str, typer.Option("--output", help="USD per Mtok, output")],
    cache_read: Annotated[str | None, typer.Option("--cache-read")] = None,
    cache_write: Annotated[str | None, typer.Option("--cache-write")] = None,
) -> None:
    """Insert or update the rates for (PROVIDER, MODEL)."""
    with _session_factory()() as session:
        row = session.execute(
            select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model)
        ).scalar_one_or_none()
        if row is None:
            row = ModelPrice(
                provider=provider,
                model=model,
                input_per_mtok=Decimal(0),
                output_per_mtok=Decimal(0),
            )
            session.add(row)
        row.input_per_mtok = _decimal_arg(input, "--input")
        row.output_per_mtok = _decimal_arg(output, "--output")
        row.cache_read_per_mtok = (
            None if cache_read is None else _decimal_arg(cache_read, "--cache-read")
        )
        row.cache_write_per_mtok = (
            None if cache_write is None else _decimal_arg(cache_write, "--cache-write")
        )
        session.commit()
    typer.echo(f"price set for {provider}/{model}")


@prices_app.command("backfill")
def prices_backfill() -> None:
    """Recompute unpriced rows that now have full rates, at CURRENT rates.

    The price_snapshot written reflects the rates applied at backfill time.
    Rows with no captured usage (empty raw_usage) are never backfilled —
    unknown usage stays NULL, never a fabricated $0.
    """
    with _session_factory()() as session:
        events = (
            session.execute(select(LlmEvent).where(LlmEvent.cost_usd.is_(None))).scalars().all()
        )
        rates_cache: dict[tuple[str, str], Rates | None] = {}
        filled = 0
        skipped = 0
        for event in events:
            if not event.raw_usage:
                skipped += 1
                continue
            key = (event.provider, event.model)
            if key not in rates_cache:
                from ledgerlm.pricing import get_rates

                rates_cache[key] = get_rates(session, *key)
            rates = rates_cache[key]
            if rates is None:
                skipped += 1
                continue
            usage = NormalizedUsage(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_tokens,
                cache_write_tokens=event.cache_write_tokens,
            )
            result = compute_cost(usage, rates)
            if result is None:
                skipped += 1
                continue
            event.cost_usd, event.price_snapshot = result
            filled += 1
        session.commit()
    typer.echo(f"backfilled {filled} rows; {skipped} still unpriced")


if __name__ == "__main__":
    app()
