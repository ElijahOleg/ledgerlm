"""Settings via pydantic-settings; env prefix ``LEDGERLM_``."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DB_URL = "sqlite:///~/.ledgerlm/ledgerlm.db"

_SQLITE_PREFIX = "sqlite:///"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGERLM_")

    db_url: str = DEFAULT_DB_URL
    echo_sql: bool = False

    @property
    def resolved_db_url(self) -> str:
        """The db_url with ``~`` expanded; creates the parent dir for SQLite files."""
        url = self.db_url
        if url.startswith(_SQLITE_PREFIX) and not url.startswith("sqlite:///:memory:"):
            path = Path(url[len(_SQLITE_PREFIX) :]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"{_SQLITE_PREFIX}{path}"
        return url


def get_settings() -> Settings:
    """Read settings fresh from the environment (cheap; keeps tests simple)."""
    return Settings()
