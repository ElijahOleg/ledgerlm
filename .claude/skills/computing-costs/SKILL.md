---
name: computing-costs
description: Use when touching anything that computes, stores, aggregates, backfills, or displays dollar costs or token counts — pricing.py, recorder cost fields, prices backfill, summary/dashboard aggregation, exports, or optimizer repricing. LedgerLM's entire value is that these numbers are trustworthy; this skill holds the Decimal-only rules, unpriced-NULL semantics, price-snapshot requirements, the independent-oracle test rule, and the optimizer's mandatory honesty language.
---

# Computing costs

This product makes exactly one promise: the numbers are right. Every rule in this
file exists to defend that promise, and most violations produce no error — only
wrong dollars. Assume any shortcut here is a silent-corruption bug until proven
otherwise.

## Iron rules

1. **Decimal end-to-end in computation.** Rates load as `Decimal`; cost = the sum
   over nonzero buckets of `tokens × rate / 1_000_000`, quantized to 10 decimal
   places for storage. Never compute in float; never cast to Decimal late.
2. **Usage comes only from provider responses.** No token estimation from text or
   character counts, ever — including "helpful" fallbacks on error paths.
3. **Unpriced means NULL, never 0.** If any rate needed for a nonzero bucket is
   missing, the entire row is unpriced; warn once per (provider, model). $0 is a
   real value (a free tier is conceivable); NULL is "unknown". They must stay
   distinguishable forever.
4. **A row's cost is recomputable from the row alone.** `price_snapshot` is written
   whenever `cost_usd` is written — including by `prices backfill`, which snapshots
   the rates it actually applied at backfill time.
5. **Every surface that shows cost shows the unpriced count beside it** — CLI
   summary, every dashboard page, optimizer output, exports. Totals exclude NULL
   rows and must say so.

## Aggregation

DB-level `SUM`/`GROUP BY` is acceptable for display surfaces (accept the dialect's
numeric behavior there), but any value that gets **stored** goes through the Decimal
path. Keep aggregation SQL in `queries.py` behind tests so display math has an
audit point.

## The independent-oracle test rule

Any change to cost computation, backfill, or repricing requires at least one test
whose expected value is **hand-computed and written down**, with the arithmetic shown
in a comment. Asserting the code against values produced by calling the same code
proves nothing. At least one oracle must exercise cache buckets.

Example shape:

```python
# 2,000,000 uncached in @ $3/M = $6.000000
# 1,000,000 cache reads @ $0.30/M = $0.300000
#   500,000 out @ $15/M = $7.500000            → total $13.800000
assert event.cost_usd == Decimal("13.8000000000")
```

## Optimizer honesty language

Repricing outputs are arithmetic on identical tokens — nothing more. Every optimizer
surface carries this disclaimer verbatim:

> Repricing is arithmetic on identical tokens — it is not a claim that output
> quality would match; treat these as candidates for experiments.

Never generate copy implying a cheaper model "would have sufficed" or "would work
just as well". Flag candidates; never issue verdicts.

## When prices change

Historical rows never shift — snapshots guarantee it. New or corrected rates affect
only new rows and future backfills. If a rate is discovered to have been *wrong*
historically, the remedy is an explicit, documented recompute the user runs
deliberately — never an automatic rewrite of history. If that need arises, raise it
at the gate as a deviation/parking-lot item rather than improvising it.
