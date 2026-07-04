"""Programmatic Alembic upgrade (D15): works from any directory with only the
installed package — migration scripts ship as package data."""

from __future__ import annotations

from pathlib import Path


def upgrade_to_head(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
