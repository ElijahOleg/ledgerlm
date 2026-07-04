"""Programmatic Alembic upgrade (D15): works from any directory with only the
installed package — migration scripts ship as package data."""

from __future__ import annotations

import threading
from pathlib import Path

# Concurrent in-process alembic runs over one SQLite file crash the sqlite3
# C extension — serialize them. Cross-process racers are protected by
# SQLite's own file locking: the loser errors and the caller's write retry
# proceeds against the winner's schema (D20).
_migrate_lock = threading.Lock()


def upgrade_to_head(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    with _migrate_lock:
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")
