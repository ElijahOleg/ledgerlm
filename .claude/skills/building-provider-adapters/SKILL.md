---
name: building-provider-adapters
description: Use when creating or modifying anything in src/ledgerlm/providers/ (anthropic, openai, mock, or a new provider), or when touching usage normalization, token buckets, prompt_hash, wrap() interception, or streaming capture. Encodes the normalization contract — four disjoint token buckets with provider-specific cached-token semantics — that keeps LedgerLM's cost numbers trustworthy. Consult even for "small" adapter tweaks: bucket mistakes don't crash anything, they silently corrupt every downstream dollar figure.
---

# Building provider adapters

Every dollar LedgerLM ever reports flows through an adapter's normalization. A bucket
error produces no exception and no failing behavior — it just quietly makes all
downstream numbers wrong, which is the one unforgivable failure for this product.
Treat this file as the contract that prevents it.

## The normalization contract

An adapter maps a provider's raw usage into four **disjoint** buckets:

- `input_tokens` — uncached input only
- `cache_read_tokens`
- `cache_write_tokens`
- `output_tokens`

Disjoint means the buckets sum to the total billed tokens: nothing counted twice,
nothing dropped. Independently of normalization, store the provider's usage object
verbatim in `raw_usage` — a normalizer bug must never destroy information, because
verbatim raw usage makes any row repairable later.

## Provider-specific semantics — do not "fix" these; they are how the APIs work

- **OpenAI Chat Completions:** `usage.prompt_tokens` INCLUDES
  `prompt_tokens_details.cached_tokens`. Uncached input = `prompt_tokens −
  cached_tokens`. `completion_tokens` → output.
- **OpenAI Responses API:** `usage.input_tokens` INCLUDES
  `input_tokens_details.cached_tokens` — same subtraction.
- **Anthropic Messages:** `usage.input_tokens` EXCLUDES
  `cache_creation_input_tokens` and `cache_read_input_tokens`; the three fields are
  already disjoint — map directly, no arithmetic.
- Absent or None fields count as 0 for buckets; `raw_usage` still stores whatever
  arrived, including fields this code doesn't recognize yet.

## Adding or changing an adapter — checklist

1. Implement the base interface in `providers/<name>.py`. Keep the SDK import lazy:
   importing the module must succeed with the SDK absent; raise a helpful
   `pip install "ledgerlm[<name>]"` error only on first use.
2. Register client detection in `wrap()` — sync and async client classes both.
3. Compute `prompt_hash`: SHA-256 over a canonical JSON serialization of the ordered
   system + message content. Never store or log the content itself, anywhere.
4. Capture `provider_request_id` where the SDK exposes it.
5. Add fixtures under `tests/fixtures/<provider>/` — realistic captured usage
   payloads (structure real, content redacted) covering at least: no-cache,
   cache-read-heavy, cache-write, an error response, and (if streaming) the full
   stream event sequence.
6. Write normalizer unit tests against every fixture, asserting all four buckets
   exactly.
7. Add seed prices for the provider's mainstream models, or leave them intentionally
   unpriced and say so in the gate report.
8. Never estimate tokens from text. If usage is absent (some error paths), record
   the event with absent usage and the appropriate status — do not reconstruct.

## Streaming rules (Phase 1.5 onward)

- Caller-visible stream behavior must be **byte-identical** to the unwrapped SDK.
  When injecting `stream_options={"include_usage": True}` for OpenAI, swallow the
  final usage-only chunk before the caller sees it; when the caller set it
  themselves, record from it and pass it through.
- Anthropic: input-side usage from `message_start`; output/cumulative usage from the
  final `message_delta`. Wrap both the raw `stream=True` iterator and the
  `messages.stream()` helper.
- Abandoned stream (closed/exited before completion): record `status="error"`,
  `error_type="stream_abandoned"`, with whatever usage is known. A partial row is
  honest; a silent gap in the ledger is not.
- Record `first_token_ms` (first content event) and `latency_ms` (completion).

## Failure posture

The whole instrumentation path inherits DESIGN.md principle 4: adapter exceptions
must never escape into the host app. Degrade to an error-status event or, at absolute
worst, a logged skip — never a raised exception, never a blocked or altered call.
