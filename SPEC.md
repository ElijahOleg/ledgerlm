# LedgerLM — Phase 1 spec (core ledger)

You are building Phase 1 of LedgerLM from scratch in this empty repo: a local-first
Python library that instruments Anthropic and OpenAI SDK calls and records each call's
tokens, USD cost, latency, and attribution tags into a local SQLite ledger.

## Context
Built by a Python developer to track and cut LLM spend across a multi-agent system;
will be released publicly later. Future phases — do NOT build them now, but don't
preclude them: 1.5 = streaming usage capture; 2 = local dashboard (FastAPI + Jinja2 +
HTMX + Chart.js, all assets vendored, no CDNs, binds 127.0.0.1); 3 = alerts (budget +
spike → webhook), optimizer report, CSV export.

## Non-negotiables
- Cost is computed only from provider-returned usage. Never estimate from prompt text.
- Recording must never raise into the host app. Any recorder/DB failure is logged and
  swallowed; the wrapped call's result is always returned to the caller.
- Unknown (provider, model) pricing → warn once per model, record the event with
  cost_usd = NULL ("unpriced") and full token counts. Never write a fabricated $0.
- Every event stores the provider's raw usage object verbatim (raw_usage JSON) and the
  per-Mtok rates applied (price_snapshot JSON), so every row is independently
  auditable and recomputable.
- All timestamps stored in UTC.
- SQLite is the default store; schema must remain Postgres-compatible. Enable WAL mode
  and a busy_timeout on SQLite connections. Configure Alembic with render_as_batch=True.
- The anthropic and openai SDKs are optional extras with lazy imports. The core package
  (mock provider, CLI, DB) installs and runs with neither present.

## Public API (design target)
```python
import ledgerlm
from anthropic import Anthropic

client = ledgerlm.wrap(Anthropic())   # also AsyncAnthropic, OpenAI, AsyncOpenAI

with ledgerlm.tags(project="blog-net", feature="summarize", run_id=run_id):
    resp = client.messages.create(model="...", max_tokens=512, messages=[...])
# resp is the unmodified SDK response; exactly one llm_events row was recorded
```
- wrap() returns a transparent proxy. Intercept only known call paths — Anthropic
  `messages.create`; OpenAI `chat.completions.create` and `responses.create` — and pass
  every other attribute/method through untouched.
- Detect sync vs async clients and mirror them; both are Phase 1.
- Streaming calls: pass through unbroken and unrecorded, with a one-time warning that
  streaming capture lands in Phase 1.5.
- ledgerlm.tags(...) is a contextvars-based, nestable, async-safe context manager.
  Reserved keys project/feature/env/run_id/customer map to columns; any other kwargs
  land in the tags JSON column.
- No retry logic anywhere — the SDKs retry internally. One event per completed call,
  status "ok" or "error" (on errors: exception type + latency; usage if present).

## Usage normalization (correctness-critical)
Normalize provider usage into four disjoint buckets: input_tokens (uncached),
cache_read_tokens, cache_write_tokens, output_tokens.
- OpenAI chat completions: prompt_tokens INCLUDES prompt_tokens_details.cached_tokens —
  subtract to get uncached input; completion_tokens → output. The Responses API's
  input_tokens / input_tokens_details.cached_tokens have the same subset semantics.
- Anthropic: input_tokens EXCLUDES cache_creation_input_tokens and
  cache_read_input_tokens (already disjoint) — map directly.
Unit-test both normalizers against realistic usage payload fixtures.

## Cost computation
Decimal arithmetic only: cost_usd = Σ over nonzero buckets of
(tokens / 1_000_000 × per-Mtok rate), quantized to 10 decimal places. If any rate
required for a nonzero bucket is missing from model_prices, the whole row is unpriced
(NULL) with one warning per (provider, model).

## Data model (SQLAlchemy 2.0, Mapped[] typing)
llm_events:
  id PK; ts DateTime(UTC) indexed; provider str; model str indexed;
  status str "ok"|"error"; error_type str NULL; latency_ms int;
  input_tokens int; output_tokens int; cache_read_tokens int NULL;
  cache_write_tokens int NULL; raw_usage JSON; price_snapshot JSON NULL;
  cost_usd Numeric(18,10) NULL; project str NULL (ix); feature str NULL (ix);
  env str NULL; run_id str NULL (ix); customer str NULL;
  tags JSON NOT NULL default {}; provider_request_id str NULL; created_at.
  Composite indexes: (project, ts), (model, ts).
model_prices:
  id PK; (provider, model) UNIQUE; input_per_mtok Numeric; output_per_mtok Numeric;
  cache_read_per_mtok Numeric NULL; cache_write_per_mtok Numeric NULL;
  currency str default "USD"; last_verified date NULL; updated_at.
One initial Alembic migration creating both tables.

## Pricing seed workflow
- data/prices_seed.yaml ships entries for current mainstream Anthropic + OpenAI models.
  If you have web access, verify rates against the official pricing pages and set
  last_verified; otherwise write clearly marked UNVERIFIED rates with
  last_verified: null. A short verified list beats a long guessed one.
- `ledgerlm init` loads the seed into model_prices only if the table is empty; the DB
  is authoritative thereafter.

## Config
pydantic-settings, env prefix LEDGERLM_. Keys: db_url (default
sqlite:///~/.ledgerlm/ledgerlm.db — expanduser, create parent dir), echo_sql (default
false). One shared per-user ledger by default so multiple apps attribute into the same
DB; override per project via env.

## CLI (Typer, entry point `ledgerlm`)
- init             create/upgrade schema (alembic upgrade head); seed prices if empty
- summary [--since 7d|24h|30d] [--by provider|model|project|feature]
                   calls, tokens in/out, cost — ALWAYS show the count of unpriced rows
- prices list
- prices set PROVIDER MODEL --input X --output Y [--cache-read Z --cache-write W]
- prices backfill  recompute unpriced rows that now have full rates (current rates;
                   write price_snapshot accordingly)

## Repo layout (src layout, hatchling build backend)
pyproject.toml  README.md  alembic.ini  data/prices_seed.yaml
src/ledgerlm/
  __init__.py            # public: wrap, tags, __version__
  config.py  tagging.py  pricing.py  recorder.py
  providers/ base.py anthropic.py openai.py mock.py
  db/ models.py session.py migrations/
  cli.py
tests/ conftest.py test_normalizers.py test_pricing.py test_tags.py test_smoke.py

## Tooling & quality bar
Python 3.11+. Deps: sqlalchemy>=2, alembic, typer, pydantic-settings, pyyaml.
Extras: [anthropic], [openai], [all], [dev] (pytest, pytest-asyncio, ruff, mypy).
Ruff for lint + format. Type hints throughout; mypy on src with reasonable strictness
(targeted ignores with comments are fine at SDK boundaries). Tests never touch the
network: real provider modules get unit tests over normalization fixtures; the mock
provider drives everything end-to-end.

## Mock provider (ships in the package, not just tests)
ledgerlm.providers.mock.MockLLMClient: SDK-shaped, deterministic, with configurable
usage across all four buckets and canned responses; wrap() recognizes it as provider
"mock". The seed includes prices for a "mock" model so smoke tests exercise real cost
math end to end.

## Required tests (pytest)
1. Smoke: wrap MockLLMClient → nested tags scopes → call → exactly one persisted row;
   assert exact Decimal cost against a hand-computed value including cache buckets;
   assert tag columns and JSON extras.
2. Unpriced path: unknown model → warning + NULL cost + tokens present; then
   prices set → prices backfill → cost filled, snapshot written.
3. Normalizers: OpenAI cached-token subtraction; Anthropic disjoint mapping.
4. tags(): nesting overrides; isolation across concurrent async tasks.
5. Never-raise: recorder given a broken DB session still returns the model response;
   the failure is logged.

## Process
- Small, conventional git commits as you go.
- Write CLAUDE.md capturing conventions, the non-negotiables, and the phase roadmap;
  point it at this SPEC.md.
- README: install, a ~10-line quickstart, example `ledgerlm summary` output.

## Phase 1 exit criteria — then STOP for review
- `pip install -e ".[dev]"` clean; `ruff check` and `pytest` both green.
- Demo (script or README snippet): mock client makes a few tagged calls →
  `ledgerlm summary --by project` prints sensible totals including the unpriced count.
- Stop. Summarize what you built, any decisions made beyond this spec, and any
  deviations. Do not start Phase 1.5 or Phase 2.
