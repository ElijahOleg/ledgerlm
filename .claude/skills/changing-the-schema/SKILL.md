---
name: changing-the-schema
description: Use for ANY change to database models (src/ledgerlm/db/models.py) or Alembic migrations — new columns, tables, indexes, or type changes, however small. Covers the SQLite+Postgres portability rules, batch-mode constraints, the autogenerate-then-hand-review procedure, and how to test migrations from both empty and populated databases before committing.
---

# Changing the schema

The ledger is append-mostly historical data the user trusts. A careless migration or
a dialect-specific column type doesn't fail loudly — it quietly breaks either the
SQLite default or the Postgres upgrade path, or worse, mangles history. Schema work
therefore follows a fixed procedure.

## Rules

- Every schema change must work on **SQLite and Postgres**. Use SQLAlchemy's generic
  types: `JSON` (not JSONB directly), `Numeric` for rates and costs, `DateTime`
  following the project's existing UTC convention in models.py — match the existing
  style exactly rather than introducing a second one.
- Alembic runs with `render_as_batch=True` (SQLite's ALTER limits). Give every
  constraint and index an explicit name via the project's `naming_convention` so
  batch mode can find and recreate them.
- Columns holding cost or usage data are load-bearing (see the computing-costs
  skill): any change to their type or semantics requires a deviation entry at the
  gate, not just a migration.
- SQLite pragmas (WAL, busy_timeout) belong in engine/session setup, never in
  migrations.
- Timestamps are UTC, always (DESIGN.md §3.8).

## Procedure

1. Edit `models.py` (SQLAlchemy 2.0 `Mapped[]` style, matching existing patterns).
2. `alembic revision --autogenerate -m "<imperative summary>"`.
3. **Hand-review the generated migration.** Autogenerate misses server defaults,
   mangles SQLite alters, and can emit dialect-specific DDL — this review is the
   entire point of the procedure. Fix by hand.
4. Test the migration:
   - fresh temp DB: `upgrade` from empty to head succeeds;
   - a populated fixture DB: `upgrade` succeeds and existing rows survive
     (spot-check one);
   - downgrade is best-effort in v0 — implement when cheap; otherwise raise
     `NotImplementedError` with a comment. A missing downgrade is acceptable; a
     silently wrong one is not.
5. Run the full test suite.
6. Never edit a committed migration. If a shipped migration is wrong, ship a new
   revision that corrects it.

## Adding indexes

Justify every index with the query that needs it (which dashboard page or CLI read).
Order composite index columns equality-first, range-last, matching that query. An
index not listed in DESIGN.md §6 is a small deviation — cheap, but still recorded in
the gate report.
