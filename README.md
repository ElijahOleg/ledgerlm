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
project   calls  tokens_in  tokens_out  cache_read  cache_write  cost_usd  unpriced
blog-net  3      360000     24000       80000       12000        $1.8600   0
research  1      120000     8000        0           0            -         1

unpriced rows in window: 1 (cost totals exclude unpriced rows; fix with `ledgerlm prices set` + `prices backfill`)
```

Other commands: `ledgerlm summary --since 24h --by model`, `ledgerlm prices list`,
`ledgerlm prices set PROVIDER MODEL --input X --output Y`, `ledgerlm prices backfill`.

Config via env (`LEDGERLM_` prefix): `LEDGERLM_DB_URL` overrides the default shared
per-user ledger at `~/.ledgerlm/ledgerlm.db`.

## Dashboard

```bash
ledgerlm dashboard              # http://127.0.0.1:8642
```

A fully offline, read-only view of the ledger: spend over time, breakdowns by
provider/model/project/feature (and any custom tag key), the most expensive calls, and
the price table with staleness hints. All assets are vendored — the dashboard makes
zero non-localhost requests. It binds 127.0.0.1 and has no auth: that is the v0
security model; don't bind it to a public interface.

Want data to look at first? `ledgerlm dev seed-demo` fills a ledger with ~100k
synthetic events (refuses to touch a non-empty ledger without `--force`).

Docker: `docker compose up` serves the dashboard with the ledger in `./data/` —
**Linux hosts only**. SQLite WAL over Docker Desktop bind mounts (macOS/Windows) is
unreliable; on those platforms run `ledgerlm dashboard` on the host instead.

## Status

Core ledger, streaming capture, and the local dashboard are built (Phase 2 awaiting
gate review). Alerts, the optimizer report, and CSV export are planned — see
`DESIGN.md` for the roadmap.
