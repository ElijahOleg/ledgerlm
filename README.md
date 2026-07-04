# LedgerLM

Local-first cost ledger for LLM API calls. Wrap your Anthropic or OpenAI client, tag
your calls with project/feature context, and every call is recorded — tokens, USD cost,
latency, attribution — into a local SQLite ledger you can query and trust. No proxy, no
account, no data leaving your machine.

**Under construction** — Phase 1 (core ledger) in progress.
