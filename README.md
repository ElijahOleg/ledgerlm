# LedgerLM

Local-first cost ledger for LLM API calls. Wrap your Anthropic or OpenAI client, tag
your calls with project/feature context, and every call is recorded — tokens, USD cost,
latency, attribution — into a local SQLite ledger you can query and trust. No proxy, no
account, no data leaving your machine.

Costs are computed only from provider-returned usage against a local price table, with
cache-aware math (uncached input / cache reads / cache writes / output priced
separately). Unknown prices are recorded as *unpriced* (never a fabricated $0), and
every row stores the raw usage and the exact rates applied, so it is independently
auditable.

## Install

```bash
pip install -e ".[anthropic]"   # or [openai], [all]; core works with neither SDK
ledgerlm init                   # creates ~/.ledgerlm/ledgerlm.db and seeds prices
```

## Quickstart

```python
import ledgerlm
from anthropic import Anthropic

client = ledgerlm.wrap(Anthropic())   # also AsyncAnthropic, OpenAI, AsyncOpenAI

with ledgerlm.tags(project="blog-net", feature="summarize"):
    resp = client.messages.create(
        model="claude-sonnet-5", max_tokens=512,
        messages=[{"role": "user", "content": "Summarize this post..."}],
    )
# resp is the unmodified SDK response; one llm_events row was recorded
```

## Reporting

```console
$ ledgerlm summary --by project
project   calls  tokens_in  tokens_out  cost_usd  unpriced
blog-net  3      360000     24000       $1.8000   0
research  1      120000     8000        -         1

unpriced rows in window: 1 (cost totals exclude unpriced rows; fix with `ledgerlm prices set` + `prices backfill`)
```

Other commands: `ledgerlm summary --since 24h --by model`, `ledgerlm prices list`,
`ledgerlm prices set PROVIDER MODEL --input X --output Y`, `ledgerlm prices backfill`.

Config via env (`LEDGERLM_` prefix): `LEDGERLM_DB_URL` overrides the default shared
per-user ledger at `~/.ledgerlm/ledgerlm.db`.

## Status

Phase 1 (core ledger) complete. Streaming capture, a local dashboard, and alerts are
planned — see `DESIGN.md` for the roadmap.
