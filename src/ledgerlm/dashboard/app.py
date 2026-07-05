"""FastAPI app factory for the read-only local dashboard.

Routing + rendering only — every SQL statement lives in queries.py. Pages are
server-rendered Jinja2; filter changes are HTMX partial swaps (the same route
returns just the content fragment when the HX-Request header is present);
charts fetch same-origin JSON fragments. All assets are vendored under
static/ — the dashboard makes zero non-localhost requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from ledgerlm.dashboard import queries
from ledgerlm.db.models import utcnow

_PKG = Path(__file__).parent

logger = logging.getLogger("ledgerlm")

SINCE_PRESETS: tuple[str, ...] = ("24h", "7d", "30d", "90d", "all")
DEFAULT_SINCE = "30d"
_SINCE_DELTAS: dict[str, dt.timedelta | None] = {
    "24h": dt.timedelta(hours=24),
    "7d": dt.timedelta(days=7),
    "30d": dt.timedelta(days=30),
    "90d": dt.timedelta(days=90),
    "all": None,
}
TOP_CALL_LIMITS = (25, 50, 100, 250)


def _usd(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.4f}"


def _num(value: int | None) -> str:
    return "0" if value is None else f"{value:,}"


def _hash8(value: str | None) -> str:
    return "—" if not value else value[:8]


def _rate(value: Decimal | None) -> str:
    """Per-Mtok rate without trailing zeros: 10.000000 → 10, 0.075000 → 0.075."""
    if value is None:
        return "—"
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _read_params(request: Request) -> dict[str, str]:
    """The filter state, normalized; unknown values fall back to defaults."""
    q = request.query_params
    since = q.get("since", DEFAULT_SINCE)
    if since not in _SINCE_DELTAS:
        since = DEFAULT_SINCE
    return {
        "since": since,
        "provider": q.get("provider", ""),
        "model": q.get("model", ""),
        "project": q.get("project", ""),
    }


def _filters(params: dict[str, str]) -> queries.Filters:
    delta = _SINCE_DELTAS[params["since"]]
    return queries.Filters(
        since=None if delta is None else utcnow() - delta,
        provider=params["provider"] or None,
        model=params["model"] or None,
        project=params["project"] or None,
    )


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    alerts_every: int = 0,
    alerts_config: Path | None = None,
) -> FastAPI:
    def get_factory() -> sessionmaker[Session]:
        if session_factory is not None:
            return session_factory
        from ledgerlm.db.session import get_default_session_factory

        return get_default_session_factory()

    def alerts_tick_once() -> None:
        """One alert evaluation — the same code path as `ledgerlm alerts check`."""
        from ledgerlm.alerts import check_alerts, load_config

        config = load_config(alerts_config)
        with get_factory()() as session:
            for outcome in check_alerts(session, config):
                if outcome.firing is not None:
                    ev = outcome.evaluation
                    logger.warning(
                        "ledgerlm: alert %s fired (observed $%s vs threshold $%s)",
                        ev.rule,
                        ev.observed,
                        ev.threshold,
                    )

    async def alerts_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(alerts_tick_once)
            except Exception as exc:  # the tick must never kill the dashboard
                logger.warning("ledgerlm: background alerts tick failed: %s", exc)
            await asyncio.sleep(alerts_every)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(alerts_loop()) if alerts_every > 0 else None
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="LedgerLM dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_PKG / "static"), name="static")

    templates = Jinja2Templates(directory=_PKG / "templates")
    templates.env.filters["usd"] = _usd
    templates.env.filters["num"] = _num
    templates.env.filters["hash8"] = _hash8
    templates.env.filters["rate"] = _rate

    def base_context(request: Request, session: Session, page: str) -> dict[str, Any]:
        params = _read_params(request)
        return {
            "request": request,
            "page": page,
            "page_title": page.replace("-", " "),
            "params": params,
            "qs": urlencode(params),
            "since_presets": SINCE_PRESETS,
            "options": queries.filter_options(session),
            "banner_unpriced": queries.overview_totals(session, queries.Filters()).unpriced,
        }

    def render(
        request: Request, ctx: dict[str, Any], page_template: str, partial_template: str
    ) -> HTMLResponse:
        """Full page normally; just the content fragment for HTMX swaps."""
        name = partial_template if request.headers.get("hx-request") else page_template
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        with get_factory()() as session:
            ctx = base_context(request, session, "overview")
            filters = _filters(ctx["params"])
            ctx["totals"] = queries.overview_totals(session, filters)
            ctx["by_provider"] = queries.group_totals(session, filters, "provider")
            ctx["by_model"] = queries.group_totals(session, filters, "model")
        return render(request, ctx, "overview.html", "_overview.html")

    @app.get("/attribution", response_class=HTMLResponse)
    def attribution(request: Request) -> HTMLResponse:
        with get_factory()() as session:
            ctx = base_context(request, session, "attribution")
            by = request.query_params.get("by", "project")
            tag_dims = [f"tag:{k}" for k in queries.tag_keys(session)]
            by_options = list(queries.PROMOTED_DIMENSIONS) + tag_dims
            if by not in by_options:
                by = "project"
            filters = _filters(ctx["params"])
            if by.startswith("tag:"):
                rows = queries.group_totals_by_tag(session, filters, by.removeprefix("tag:"))
            else:
                rows = queries.group_totals(session, filters, by)
            ctx["params"]["by"] = by
            ctx["qs_with_by"] = urlencode({"by": by, **ctx["params"]})
            ctx["by_options"] = by_options
            ctx["show_by"] = True
            ctx["rows"] = rows
        return render(request, ctx, "attribution.html", "_attribution.html")

    @app.get("/top-calls", response_class=HTMLResponse)
    def top_calls(request: Request) -> HTMLResponse:
        with get_factory()() as session:
            ctx = base_context(request, session, "top-calls")
            try:
                limit = int(request.query_params.get("limit", "50"))
            except ValueError:
                limit = 50
            limit = max(1, min(limit, TOP_CALL_LIMITS[-1]))
            ctx["params"]["limit"] = str(limit)
            ctx["show_limit"] = True
            ctx["calls"] = queries.top_calls(session, _filters(ctx["params"]), limit=limit)
        return render(request, ctx, "top_calls.html", "_top_calls.html")

    @app.get("/optimizer", response_class=HTMLResponse)
    def optimizer_page(request: Request) -> HTMLResponse:
        from ledgerlm import optimizer

        with get_factory()() as session:
            ctx = base_context(request, session, "optimizer")
            params = ctx["params"]
            filters = optimizer.OptimizerFilters(
                since=_filters(params).since,
                provider=params["provider"] or None,
                model=params["model"] or None,
                project=params["project"] or None,
            )
            ctx["report"] = optimizer.build_report(session, filters)
            ctx["cache_savings_assumption"] = optimizer.CACHE_SAVINGS_ASSUMPTION
        return render(request, ctx, "optimizer.html", "_optimizer.html")

    @app.get("/prices", response_class=HTMLResponse)
    def prices_page(request: Request) -> HTMLResponse:
        with get_factory()() as session:
            ctx = base_context(request, session, "prices")
            ctx["price_rows"] = queries.prices(session)
        return templates.TemplateResponse(request, "prices.html", ctx)

    # JSON fragments backing the charts (and preserving the SPA option, D7).
    # Chart payloads are a display surface: floats are acceptable here; stored
    # values never pass through these endpoints.

    @app.get("/api/spend-by-day")
    def api_spend_by_day(request: Request) -> JSONResponse:
        with get_factory()() as session:
            points = queries.spend_by_day(session, _filters(_read_params(request)))
        return JSONResponse(
            {
                "labels": [p.day for p in points],
                "values": [float(p.cost_usd) for p in points],
                "unpriced": [p.unpriced for p in points],
            }
        )

    @app.get("/api/spend-by-group")
    def api_spend_by_group(request: Request, by: str = "provider") -> JSONResponse:
        with get_factory()() as session:
            filters = _filters(_read_params(request))
            if by.startswith("tag:"):
                rows = queries.group_totals_by_tag(session, filters, by.removeprefix("tag:"))
            elif by in queries.PROMOTED_DIMENSIONS:
                rows = queries.group_totals(session, filters, by)
            else:
                raise HTTPException(status_code=400, detail=f"unknown dimension {by!r}")
        rows = rows[:20]  # charts cap at 20 groups; the table below shows all
        return JSONResponse(
            {
                "labels": [r.key for r in rows],
                "values": [0.0 if r.cost_usd is None else float(r.cost_usd) for r in rows],
                "unpriced": [r.unpriced for r in rows],
            }
        )

    return app
