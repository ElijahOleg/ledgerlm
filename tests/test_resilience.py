"""D17: never-raise must not decay into silent data loss."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select

import ledgerlm
from ledgerlm.db.models import LlmEvent
from ledgerlm.db.session import (
    create_db_engine,
    create_session_factory,
    reset_default_session_factory,
)
from ledgerlm.pricing import reset_warned_models
from ledgerlm.providers.mock import MockLLMClient
from ledgerlm.recorder import Recorder

MESSAGES = [{"role": "user", "content": "hi"}]


def test_uninitialized_sqlite_ledger_is_auto_initialized_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A call against a ledger that was never `ledgerlm init`ed still lands."""
    db_path = tmp_path / "never-inited.db"
    monkeypatch.setenv("LEDGERLM_DB_URL", f"sqlite:///{db_path}")
    reset_default_session_factory()
    reset_warned_models()

    client = ledgerlm.wrap(MockLLMClient())
    with (
        caplog.at_level(logging.WARNING, logger="ledgerlm"),
        ledgerlm.tags(project="auto-init"),
    ):
        resp = client.messages.create(model="mock-model", messages=MESSAGES)

    assert resp.content == "mock response"
    assert any("initialized empty ledger schema" in r.getMessage() for r in caplog.records)

    factory = create_session_factory(create_db_engine())
    with factory() as session:
        event = session.execute(select(LlmEvent)).scalar_one()
    assert event.project == "auto-init"
    assert event.input_tokens == 100
    # Schema was created but prices were never seeded — unpriced, never $0
    assert event.cost_usd is None


def test_auto_init_never_touches_non_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGERLM_DB_URL", "postgresql://nobody@localhost:1/nope")
    reset_default_session_factory()
    recorder = Recorder()
    assert recorder._sqlite_url() is None  # Postgres is never auto-migrated


def test_persistent_failure_repeats_warnings_with_cumulative_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_session_factory() -> None:
        raise ConnectionError("disk is gone")

    recorder = Recorder(session_factory=broken_session_factory)  # type: ignore[arg-type]
    recorder.warn_interval_s = 0.0  # fire on every drop for the test
    client = ledgerlm.wrap(MockLLMClient(), recorder=recorder)

    with caplog.at_level(logging.WARNING, logger="ledgerlm"):
        for _ in range(3):
            resp = client.messages.create(model="mock-model", messages=MESSAGES)
            assert resp.content == "mock response"  # every call still succeeds

    drops = [r.getMessage() for r in caplog.records if "dropped so far" in r.getMessage()]
    assert len(drops) == 3  # warnings REPEAT — not warn-once
    assert "1 event(s) dropped" in drops[0]
    assert "2 event(s) dropped" in drops[1]
    assert "3 event(s) dropped" in drops[2]


def test_warning_rate_limit_suppresses_intermediate_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_session_factory() -> None:
        raise ConnectionError("still gone")

    recorder = Recorder(session_factory=broken_session_factory)  # type: ignore[arg-type]
    recorder.warn_interval_s = 3600.0  # only the first drop within the window warns
    client = ledgerlm.wrap(MockLLMClient(), recorder=recorder)

    with caplog.at_level(logging.WARNING, logger="ledgerlm"):
        for _ in range(5):
            client.messages.create(model="mock-model", messages=MESSAGES)

    drops = [r for r in caplog.records if "dropped so far" in r.getMessage()]
    assert len(drops) == 1
    # ...but the count keeps accumulating for the next warning
    assert recorder._dropped == 5
