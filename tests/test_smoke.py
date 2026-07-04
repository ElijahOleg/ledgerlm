"""End-to-end smoke tests over the mock provider: wrap → tags → call → row."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from sqlalchemy import select

import ledgerlm
from conftest import Ledger
from ledgerlm.db.models import LlmEvent
from ledgerlm.providers.mock import MockLLMClient, MockUsage
from ledgerlm.recorder import Recorder

MESSAGES = [{"role": "user", "content": "Summarize the ledger design."}]


def test_smoke_full_path(ledger: Ledger) -> None:
    client = ledgerlm.wrap(
        MockLLMClient(
            usage=MockUsage(
                input_tokens=150_000,
                output_tokens=20_000,
                cache_read_tokens=200_000,
                cache_write_tokens=50_000,
            )
        )
    )

    with ledgerlm.tags(project="blog-net", env="prod", team="core"):  # noqa: SIM117 — nesting is the point
        with ledgerlm.tags(feature="summarize", run_id="run-42", team="infra"):
            resp = client.messages.create(model="mock-model", max_tokens=512, messages=MESSAGES)

    # The SDK response comes back unmodified
    assert resp.content == "mock response"
    assert resp.usage.input_tokens == 150_000

    with ledger.session_factory() as session:
        events = session.execute(select(LlmEvent)).scalars().all()
    assert len(events) == 1  # exactly one persisted row
    event = events[0]

    # Hand-computed oracle against the seeded mock-model rates
    # (input $3/M, cache read $0.30/M, cache write $3.75/M, output $15/M):
    #   150,000 uncached in @ $3/M     = $0.450000
    #   200,000 cache reads @ $0.30/M  = $0.060000
    #    50,000 cache writes @ $3.75/M = $0.187500
    #    20,000 out @ $15/M            = $0.300000   → total $0.997500
    assert event.cost_usd == Decimal("0.9975000000")
    # Snapshot records the rate applied to every nonzero bucket (string form may
    # carry dialect scale, e.g. "3.000000" — compare as Decimals)
    snapshot = {k: Decimal(v) for k, v in event.price_snapshot.items()}
    assert snapshot == {
        "input_per_mtok": Decimal("3.00"),
        "cache_read_per_mtok": Decimal("0.30"),
        "cache_write_per_mtok": Decimal("3.75"),
        "output_per_mtok": Decimal("15.00"),
    }

    assert event.provider == "mock"
    assert event.model == "mock-model"
    assert event.status == "ok"
    assert event.latency_ms >= 0
    assert event.input_tokens == 150_000
    assert event.cache_read_tokens == 200_000
    assert event.cache_write_tokens == 50_000
    assert event.output_tokens == 20_000
    assert event.raw_usage["input_tokens"] == 150_000

    # Inner scope overrides outer per-key; non-reserved keys land in tags JSON
    assert event.project == "blog-net"
    assert event.feature == "summarize"
    assert event.env == "prod"
    assert event.run_id == "run-42"
    assert event.customer is None
    assert event.tags == {"team": "infra"}


def test_identical_prompts_yield_identical_prompt_hash(ledger: Ledger) -> None:
    client = ledgerlm.wrap(MockLLMClient())
    client.messages.create(model="mock-model", messages=MESSAGES)
    client.messages.create(model="mock-model", messages=MESSAGES)
    client.messages.create(model="mock-model", messages=[{"role": "user", "content": "other"}])

    with ledger.session_factory() as session:
        hashes = (
            session.execute(select(LlmEvent.prompt_hash).order_by(LlmEvent.id)).scalars().all()
        )
    assert len(hashes) == 3
    assert hashes[0] == hashes[1]
    assert hashes[2] != hashes[0]
    assert all(h and len(h) == 64 for h in hashes)  # sha256 hex; content never stored


def test_error_call_records_error_event_and_reraises(ledger: Ledger) -> None:
    class ExplodingClient(MockLLMClient):
        @property
        def messages(self):  # type: ignore[override]
            class _M:
                def create(self, **kwargs: object) -> None:
                    raise RuntimeError("provider exploded")

            return _M()

    client = ledgerlm.wrap(ExplodingClient())
    try:
        client.messages.create(model="mock-model", messages=MESSAGES)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    with ledger.session_factory() as session:
        event = session.execute(select(LlmEvent)).scalar_one()
    assert event.status == "error"
    assert event.error_type == "RuntimeError"
    assert event.cost_usd is None  # usage unknown — never a fabricated $0
    assert event.input_tokens == 0
    assert event.raw_usage == {}


def test_recorder_never_raises_into_host_app(
    ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken DB session must not break the wrapped call (DESIGN.md §3.4)."""

    def broken_session_factory() -> None:
        raise ConnectionError("database is on fire")

    client = ledgerlm.wrap(
        MockLLMClient(),
        recorder=Recorder(session_factory=broken_session_factory),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="ledgerlm"):
        resp = client.messages.create(model="mock-model", messages=MESSAGES)

    assert resp.content == "mock response"  # the caller still gets the response
    assert any("failed to record" in r.getMessage() for r in caplog.records)


def test_non_intercepted_attributes_pass_through(ledger: Ledger) -> None:
    inner = MockLLMClient()
    client = ledgerlm.wrap(inner)
    assert client.model == "mock-model"
    assert client.calls is inner.calls
