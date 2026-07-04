"""Dashboard tests: query-layer aggregation against a hand-built fixture ledger,
then route smoke tests via FastAPI TestClient. No network anywhere."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from conftest import Ledger
from ledgerlm.dashboard import queries
from ledgerlm.db.models import LlmEvent

UTC = dt.UTC

# Fixture ledger — six events, hand-auditable.
#
#  id  ts        prov       project  feature    in    out   cr    cw    cost  tags
#  e1  base      anthropic  blog     summarize  1000  500   0     0     0.05  team=core
#  e2  base      anthropic  blog     summarize  2000  1000  4000  1000  0.11  team=core
#  e3  base+1d   openai     blog     rank       5000  200   0     0     0.02  team=infra
#  e4  base+1d   openai     shop     rank       500   100   0     0     NULL (unpriced)
#  e5  base+1d   mock       shop     misc       10    5     0     0     NULL, status=error
#  e6  base-40d  anthropic  blog     summarize  100   50    0     0     1.00 (outside 30d)
#
# All-time totals: calls=6, in=8610, out=1855, cache_read=4000, cache_write=1000,
#                  cost = 0.05+0.11+0.02+1.00 = 1.18, unpriced=2, errors=1

BASE = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _event(**kw: object) -> LlmEvent:
    defaults: dict[str, object] = {
        "ts": BASE,
        "provider": "anthropic",
        "model": "claude-fable-5",
        "status": "ok",
        "latency_ms": 100,
        "input_tokens": 0,
        "output_tokens": 0,
        "raw_usage": {"input_tokens": 1},
        "tags": {},
    }
    defaults.update(kw)
    return LlmEvent(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def fixture_ledger(ledger: Ledger) -> Ledger:
    with ledger.session_factory() as session:
        session.add_all(
            [
                _event(
                    project="blog",
                    feature="summarize",
                    input_tokens=1000,
                    output_tokens=500,
                    cost_usd=Decimal("0.05"),
                    tags={"team": "core"},
                ),
                _event(
                    project="blog",
                    feature="summarize",
                    input_tokens=2000,
                    output_tokens=1000,
                    cache_read_tokens=4000,
                    cache_write_tokens=1000,
                    cost_usd=Decimal("0.11"),
                    tags={"team": "core"},
                ),
                _event(
                    ts=BASE + dt.timedelta(days=1),
                    provider="openai",
                    model="gpt-5.4",
                    project="blog",
                    feature="rank",
                    input_tokens=5000,
                    output_tokens=200,
                    cost_usd=Decimal("0.02"),
                    tags={"team": "infra"},
                ),
                _event(
                    ts=BASE + dt.timedelta(days=1),
                    provider="openai",
                    model="gpt-5.4",
                    project="shop",
                    feature="rank",
                    input_tokens=500,
                    output_tokens=100,
                    cost_usd=None,
                ),
                _event(
                    ts=BASE + dt.timedelta(days=1),
                    provider="mock",
                    model="mystery-model",
                    project="shop",
                    feature="misc",
                    status="error",
                    error_type="BoomError",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=None,
                ),
                _event(
                    ts=BASE - dt.timedelta(days=40),
                    project="blog",
                    feature="summarize",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=Decimal("1.00"),
                ),
            ]
        )
        session.commit()
    return ledger


def test_overview_totals_all_time(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        totals = queries.overview_totals(session, queries.Filters())
    assert totals.calls == 6
    assert totals.errors == 1
    # 1000 + 2000 + 5000 + 500 + 10 + 100
    assert totals.input_tokens == 8610
    # 500 + 1000 + 200 + 100 + 5 + 50
    assert totals.output_tokens == 1855
    assert totals.cache_read_tokens == 4000
    assert totals.cache_write_tokens == 1000
    # 0.05 + 0.11 + 0.02 + 1.00 (NULLs excluded)
    assert totals.cost_usd == Decimal("1.18")
    assert totals.unpriced == 2


def test_overview_totals_windowed_and_filtered(fixture_ledger: Ledger) -> None:
    since = BASE - dt.timedelta(days=30)
    with fixture_ledger.session_factory() as session:
        windowed = queries.overview_totals(session, queries.Filters(since=since))
        blog_only = queries.overview_totals(session, queries.Filters(since=since, project="blog"))
        openai_only = queries.overview_totals(
            session, queries.Filters(since=since, provider="openai")
        )
    # e6 (base-40d, $1.00) falls outside the window: 0.05+0.11+0.02 = 0.18
    assert windowed.calls == 5
    assert windowed.cost_usd == Decimal("0.18")
    assert windowed.unpriced == 2
    assert blog_only.calls == 3
    assert blog_only.cost_usd == Decimal("0.18")
    assert blog_only.unpriced == 0
    assert openai_only.calls == 2
    assert openai_only.cost_usd == Decimal("0.02")
    assert openai_only.unpriced == 1


def test_spend_by_day(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        points = queries.spend_by_day(session, queries.Filters(since=BASE - dt.timedelta(days=30)))
    assert [p.day for p in points] == ["2026-07-01", "2026-07-02"]
    d1, d2 = points
    assert d1.calls == 2 and d1.cost_usd == Decimal("0.16") and d1.unpriced == 0
    assert d2.calls == 3 and d2.cost_usd == Decimal("0.02") and d2.unpriced == 2


def test_group_totals_by_project(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        rows = queries.group_totals(session, queries.Filters(), "project")
    by_key = {r.key: r for r in rows}
    assert set(by_key) == {"blog", "shop"}
    # blog: e1+e2+e3+e6 → cost 0.05+0.11+0.02+1.00, cache 4000/1000
    blog = by_key["blog"]
    assert blog.calls == 4
    assert blog.cost_usd == Decimal("1.18")
    assert blog.cache_read_tokens == 4000
    assert blog.cache_write_tokens == 1000
    assert blog.unpriced == 0
    # shop: e4+e5, both unpriced
    shop = by_key["shop"]
    assert shop.calls == 2
    assert shop.cost_usd is None
    assert shop.unpriced == 2
    # ordered by cost desc, NULL-cost groups last
    assert rows[0].key == "blog"
    assert rows[-1].key == "shop"


def test_group_totals_by_tag_key(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        rows = queries.group_totals_by_tag(session, queries.Filters(), "team")
    by_key = {r.key: r for r in rows}
    # Only e1/e2 (team=core) and e3 (team=infra) carry the tag; others drop out.
    assert set(by_key) == {"core", "infra"}
    core = by_key["core"]
    assert core.calls == 2
    assert core.cost_usd == Decimal("0.16")  # 0.05 + 0.11
    assert core.cache_read_tokens == 4000
    infra = by_key["infra"]
    assert infra.calls == 1
    assert infra.cost_usd == Decimal("0.02")


def test_tag_keys(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        assert queries.tag_keys(session) == ["team"]


def test_top_calls_order_and_content(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        calls = queries.top_calls(session, queries.Filters(), limit=3)
    assert [c.cost_usd for c in calls] == [
        Decimal("1.0000000000"),
        Decimal("0.1100000000"),
        Decimal("0.0500000000"),
    ]
    top = calls[1]  # e2: the cache-heavy call
    assert top.cache_read_tokens == 4000
    assert top.cache_write_tokens == 1000
    assert top.tags == {"team": "core"}

    with fixture_ledger.session_factory() as session:
        all_calls = queries.top_calls(session, queries.Filters(), limit=10)
    # unpriced rows sort after every priced row
    assert [c.cost_usd is None for c in all_calls[-2:]] == [True, True]


def test_filter_options(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        opts = queries.filter_options(session)
    assert opts["providers"] == ["anthropic", "mock", "openai"]
    assert opts["projects"] == ["blog", "shop"]
    assert "claude-fable-5" in opts["models"] and "mystery-model" in opts["models"]


def test_prices_staleness_and_notes(fixture_ledger: Ledger) -> None:
    with fixture_ledger.session_factory() as session:
        rows = queries.prices(session)
    assert rows, "seed prices expected"
    sonnet5 = next(r for r in rows if r.model == "claude-sonnet-5")
    assert sonnet5.note is not None and "2026-08-31" in sonnet5.note
    verified = [r for r in rows if r.last_verified is not None]
    assert verified and all(r.days_since_verified is not None for r in verified)


# ---- Route smoke tests (FastAPI TestClient; in-process, no network) ----


@pytest.fixture
def client(fixture_ledger: Ledger) -> object:
    from fastapi.testclient import TestClient

    from ledgerlm.dashboard.app import create_app

    return TestClient(create_app(fixture_ledger.session_factory))


def test_overview_page_renders_key_numbers(client: object) -> None:
    resp = client.get("/", params={"since": "all"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    # headline spend 1.18, 6 calls, cache buckets, unpriced tile + banner
    assert "$1.1800" in html
    assert "4,000" in html and "1,000" in html  # cache read / write tiles
    assert "unpriced" in html
    assert "2 unpriced rows" in html  # persistent banner (ledger-wide count)
    # zero external requests: every src/href on the page is a local path
    import re

    for url in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert not url.startswith(("http://", "https://", "//")), url


def test_overview_htmx_request_returns_partial(client: object) -> None:
    resp = client.get("/", params={"since": "all"}, headers={"HX-Request": "true"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "<html" not in resp.text  # fragment only
    assert "$1.1800" in resp.text


def test_attribution_page_promoted_and_tag_dimensions(client: object) -> None:
    resp = client.get("/attribution", params={"since": "all", "by": "project"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "blog" in resp.text and "shop" in resp.text

    resp = client.get("/attribution", params={"since": "all", "by": "tag:team"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "core" in resp.text and "infra" in resp.text
    assert "$0.1600" in resp.text  # team=core: 0.05 + 0.11


def test_top_calls_page(client: object) -> None:
    resp = client.get("/top-calls", params={"since": "all"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    html = resp.text
    assert "$1.0000" in html  # most expensive call
    assert "team=core" in html
    assert "badge err" in html  # the error row's status badge


def test_prices_page(client: object) -> None:
    resp = client.get("/prices")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert "claude-sonnet-5" in resp.text
    assert "2026-08-31" in resp.text  # intro-pricing note surfaced


def test_api_spend_by_day(client: object) -> None:
    resp = client.get("/api/spend-by-day", params={"since": "all"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    data = resp.json()
    assert data["labels"][-2:] == ["2026-07-01", "2026-07-02"]
    assert data["values"][-2:] == [0.16, 0.02]
    assert data["unpriced"][-2:] == [0, 2]


def test_api_spend_by_group_validates_dimension(client: object) -> None:
    resp = client.get("/api/spend-by-group", params={"by": "model", "since": "all"})  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert set(resp.json()["labels"]) == {"claude-fable-5", "gpt-5.4", "mystery-model"}

    resp = client.get("/api/spend-by-group", params={"by": "evil; drop table"})  # type: ignore[attr-defined]
    assert resp.status_code == 400
