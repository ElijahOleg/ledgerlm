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
dev_app = typer.Typer(help="Development helpers (synthetic data).", no_args_is_help=True)
app.add_typer(dev_app, name="dev")


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
    from ledgerlm.db.migrate import upgrade_to_head

    settings = get_settings()
    upgrade_to_head(settings.resolved_db_url)
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
    # Cache buckets always show beside in/out: cache-heavy workloads must not
    # look smaller than they bill.
    measures = (
        func.count().label("calls"),
        func.coalesce(func.sum(LlmEvent.input_tokens), 0).label("tokens_in"),
        func.coalesce(func.sum(LlmEvent.output_tokens), 0).label("tokens_out"),
        func.coalesce(func.sum(LlmEvent.cache_read_tokens), 0).label("cache_read"),
        func.coalesce(func.sum(LlmEvent.cache_write_tokens), 0).label("cache_write"),
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

    header = (
        label,
        "calls",
        "tokens_in",
        "tokens_out",
        "cache_read",
        "cache_write",
        "cost_usd",
        "unpriced",
    )
    table = [
        (
            str(row[0]),  # group label
            str(row[1]),  # calls
            str(row[2]),  # tokens_in
            str(row[3]),  # tokens_out
            str(row[4]),  # cache_read
            str(row[5]),  # cache_write
            "-" if row[6] is None else f"${Decimal(str(row[6])):,.4f}",
            str(row[7] or 0),  # unpriced
        )
        for row in rows
    ]
    widths = [
        max(len(header[i]), *(len(r[i]) for r in table)) if table else len(header[i])
        for i in range(len(header))
    ]
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    for r in table:
        typer.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    total_unpriced = sum(int(r[7]) for r in table)
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


@app.command()
def dashboard(
    host: Annotated[
        str, typer.Option(help="Bind address. 127.0.0.1 = this machine only (v0 has no auth).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8642,
) -> None:
    """Serve the local read-only dashboard (fully offline; assets vendored)."""
    import uvicorn

    from ledgerlm.dashboard.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo(
            f"WARNING: binding {host} exposes the dashboard beyond this machine; "
            "v0 has no authentication.",
            err=True,
        )
    typer.echo(f"LedgerLM dashboard: http://{host}:{port}  (ledger: {get_settings().db_url})")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@dev_app.command("seed-demo")
def seed_demo(
    rows: Annotated[int, typer.Option(help="Approximate number of events to generate")] = 100_000,
    days: Annotated[int, typer.Option(help="Spread events over this many days")] = 90,
    seed: Annotated[int, typer.Option(help="RNG seed (deterministic output)")] = 1,
    force: Annotated[
        bool, typer.Option("--force", help="Seed even if the ledger already has events")
    ] = False,
) -> None:
    """Fill the configured ledger with synthetic events for development,
    screenshots, and performance checks.

    Several projects/features/models, cache-heavy calls, unpriced rows
    (an unknown model), and a few errors. Costs go through the real Decimal
    pricing path against the seeded price table; the unknown model stays
    NULL — never a fabricated $0.
    """
    import random

    from sqlalchemy import insert

    from ledgerlm.pricing import compute_cost, get_rates

    existing_check = select(func.count()).select_from(LlmEvent)
    with _session_factory()() as session:
        existing = session.execute(existing_check).scalar_one()
        if existing and not force:
            typer.echo(
                f"ledger already has {existing} events; refusing to add demo data "
                "(use --force to seed anyway, or point LEDGERLM_DB_URL at a scratch file)"
            )
            raise typer.Exit(code=1)

        rng = random.Random(seed)
        projects = {
            "blog-net": ["summarize", "outline", "seo-rank", "publish"],
            "shop-bot": ["classify", "reply", "escalate"],
            "research": ["extract", "cluster"],
        }
        # (provider, model, cache-eligible, error-rate); one model has no
        # price entry so a realistic slice of rows lands unpriced.
        models = [
            ("anthropic", "claude-fable-5", True, 0.01),
            ("anthropic", "claude-sonnet-5", True, 0.01),
            ("anthropic", "claude-haiku-4-5", False, 0.005),
            ("openai", "gpt-5.4", True, 0.02),
            ("openai", "gpt-5.4-mini", False, 0.01),
            ("openai", "gpt-experimental-preview", False, 0.02),  # unpriced
        ]
        rates_cache = {
            (provider, model): get_rates(session, provider, model)
            for provider, model, _, _ in models
        }

        now = utcnow()
        batch: list[dict[str, Any]] = []
        inserted = 0
        unpriced = 0
        errors = 0
        for _ in range(rows):
            provider, model, cacheable, error_rate = rng.choice(models)
            project = rng.choice(list(projects))
            feature = rng.choice(projects[project])
            ts = now - dt.timedelta(days=rng.uniform(0, days), seconds=rng.uniform(0, 3600))
            is_error = rng.random() < error_rate
            input_tokens = rng.randint(200, 12_000)
            output_tokens = 0 if is_error else rng.randint(50, 4_000)
            cache_read = rng.randint(1_000, 60_000) if cacheable and rng.random() < 0.55 else 0
            cache_write = rng.randint(500, 20_000) if cacheable and rng.random() < 0.25 else 0
            usage = NormalizedUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read or None,
                cache_write_tokens=cache_write or None,
            )
            rates = rates_cache[(provider, model)]
            cost: Decimal | None = None
            snapshot: dict[str, str] | None = None
            if rates is not None and not is_error:
                priced = compute_cost(usage, rates)
                if priced is not None:
                    cost, snapshot = priced
            if cost is None and not is_error:
                unpriced += 1
            if is_error:
                errors += 1
            batch.append(
                {
                    "ts": ts,
                    "provider": provider,
                    "model": model,
                    "status": "error" if is_error else "ok",
                    "error_type": "RateLimitError" if is_error else None,
                    "latency_ms": rng.randint(180, 14_000),
                    "first_token_ms": rng.randint(90, 1_500) if rng.random() < 0.4 else None,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read or None,
                    "cache_write_tokens": cache_write or None,
                    "raw_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    "price_snapshot": snapshot,
                    "cost_usd": cost,
                    "prompt_hash": f"{rng.getrandbits(256):064x}",
                    "project": project,
                    "feature": feature,
                    "env": rng.choice(["prod", "prod", "prod", "dev"]),
                    "run_id": f"run-{rng.randint(1, 400):04d}",
                    "customer": rng.choice([None, "acme", "globex", "initech"]),
                    "tags": {"team": rng.choice(["core", "infra", "growth"]), "demo": "true"},
                    "provider_request_id": None,
                    "created_at": ts,
                }
            )
            if len(batch) >= 5_000:
                session.execute(insert(LlmEvent), batch)
                inserted += len(batch)
                batch = []
                typer.echo(f"  ...{inserted} rows", err=True)
        if batch:
            session.execute(insert(LlmEvent), batch)
            inserted += len(batch)
        session.commit()
    typer.echo(
        f"seeded {inserted} demo events over {days} days "
        f"({unpriced} unpriced, {errors} errors) — try `ledgerlm dashboard`"
    )


if __name__ == "__main__":
    app()
