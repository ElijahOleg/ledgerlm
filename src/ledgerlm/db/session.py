"""Engine and session factory. SQLite gets WAL mode and a busy_timeout."""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ledgerlm.config import Settings, get_settings

SQLITE_BUSY_TIMEOUT_MS = 5000


def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


def create_db_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    engine = create_engine(settings.resolved_db_url, echo=settings.echo_sql)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


_lock = threading.Lock()
_default_factory: sessionmaker[Session] | None = None


def get_default_session_factory() -> sessionmaker[Session]:
    """Lazily built process-wide session factory against the configured ledger."""
    global _default_factory
    with _lock:
        if _default_factory is None:
            _default_factory = create_session_factory(create_db_engine())
        return _default_factory


def reset_default_session_factory() -> None:
    """Drop the cached factory (used by tests and after config changes)."""
    global _default_factory
    with _lock:
        _default_factory = None
