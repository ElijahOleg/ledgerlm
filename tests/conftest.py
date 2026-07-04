from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ledgerlm.cli import app
from ledgerlm.db.session import (
    create_db_engine,
    create_session_factory,
    reset_default_session_factory,
)
from ledgerlm.pricing import reset_warned_models


@dataclass
class Ledger:
    runner: CliRunner
    session_factory: sessionmaker[Session]
    db_path: Path


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """A fresh migrated + seeded ledger in a temp SQLite file.

    Runs the real `ledgerlm init` (alembic upgrade head + price seed), so every
    test exercises the actual migration path. No network anywhere.
    """
    db_path = tmp_path / "ledger.db"
    monkeypatch.setenv("LEDGERLM_DB_URL", f"sqlite:///{db_path}")
    reset_default_session_factory()
    reset_warned_models()
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "seeded" in result.output
    return Ledger(
        runner=runner,
        session_factory=create_session_factory(create_db_engine()),
        db_path=db_path,
    )
