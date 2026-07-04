# DESIGN.md amendments

The build contract is `DESIGN.md.pdf`. The PDF cannot be edited in-repo, so
gate-approved changes to it are recorded here, in gate order, per §12. Read this
file together with the PDF; where they differ, the amendment governs.

## Gate 1 (approved 2026-07-03)

### §13 Decision log — appended rows

| # | Decision | Rationale (compressed) |
|---|---|---|
| D14 | cache_write_per_mtok holds Anthropic's 5-minute write rate; 1-hour writes (2x input) are not separately priced in v0 | Two write tiers exist but the schema has one column; raw_usage preserves full payloads for later recomputation. Revisit if 1h caching is adopted or usage payloads expose the split. |
| D15 | ledgerlm init migrates via Alembic's programmatic API; migration scripts ship as package data (verified with a non-editable install) | init must work from any directory with only the installed package; alembic.ini remains for developer use. |

### §9 Phase 2 — edits

- **Prices page bullet gains:** "flag entries with a known expiry or introductory
  rate (e.g. Sonnet 5 intro pricing ends 2026-08-31)".
- **Added to the Pages item:** "wherever token counts are displayed (summary CLI
  included), show cache read/write columns alongside in/out — cache-heavy
  workloads must not look smaller than they bill."

### Repo hygiene

- SPEC.md deleted; DESIGN.md is the sole contract.
