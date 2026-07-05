# CLAUDE.md — LedgerLM build conventions

**Current phase: Phase 4 — in progress (wave 1 done: typer-0.12 CLI fix, py.typed, contract v1.3; recording path untouched). Remaining: dogfood, reconciliation, release.**
<!-- Update this marker at every gate. At a hard gate set it to "awaiting Gate N review". -->

Notes for Phase 4 (from Gate 4 review):
- `dev seed-demo` should grow repeated prompt-hash clusters so the optimizer's
  cache-candidates section populates in README screenshots.
- Polish list: quantize alert threshold display (`$4.00000000000` → `$4.00`).

## The contract

- `DESIGN.md` (v1.3) is the sole build contract (phases, gates, decision log).
  Approved changes are edits to that file, committed as `docs(design)` — never a PDF
  or sidecar amendment document (D16).
- One phase per session. Stop at hard gates and produce the gate report — the workflow
  lives in the `executing-a-phase` skill in `.claude/skills/`.
- The skill harness in `.claude/skills/` is contract, like DESIGN.md:
  - `executing-a-phase` — gate workflow, deviation protocol, report format
  - `building-provider-adapters` — the four-bucket normalization contract
  - `changing-the-schema` — SQLite+Postgres-portable migrations
  - `computing-costs` — Decimal-only money math, unpriced-NULL semantics

## Non-negotiables (DESIGN.md §3)

1. Cost only from provider-returned usage; never estimate from prompt text.
2. Unknown price → `cost_usd = NULL` ("unpriced"), warn once per (provider, model);
   never a fabricated $0. Every cost surface shows the unpriced-row count.
3. Every row stores `raw_usage` verbatim and the `price_snapshot` applied — each row
   is independently recomputable.
4. The recorder never raises into the host app; failures are logged and swallowed.
5. Local-first: no external services; dashboard (Phase 2) vendors assets, binds
   127.0.0.1.
6. SQLite (WAL + busy_timeout) default; schema stays Postgres-compatible; Alembic
   with `render_as_batch=True`.
7. `anthropic` / `openai` SDKs are optional extras with lazy imports; the core package
   runs with neither installed.
8. UTC in storage, always.
9. No prompt content stored — only `prompt_hash` (SHA-256 of canonical serialization).

## Conventions

- Small conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), tree green
  after each.
- Tests never touch the network; live-key tests are env-gated and skipped by default.
- No retry logic anywhere — the SDKs retry internally; one event per completed call.
- Python 3.11+, src layout, hatchling. `ruff check`, `ruff format`, `mypy src`
  (strict; targeted commented ignores OK at SDK boundaries), `pytest`.

## Phase roadmap (DESIGN.md §9)

- **Phase 0** — foundation skeleton · soft gate
- **Phase 1** — core ledger (wrap, tags, normalize, price, record, CLI) · HARD GATE 1
- **Phase 1.5** — streaming capture · HARD GATE 2
- **Phase 2** — local dashboard (FastAPI + Jinja2 + HTMX + Chart.js, vendored) · HARD GATE 3
- **Phase 3** — alerts, optimizer, CSV export · HARD GATE 4
- **Phase 4** — dogfood, reconciliation, release v0.1.0 · HARD GATE 5
