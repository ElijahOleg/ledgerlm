"""Event persistence. The recorder NEVER raises into the host app (P4): any
failure here is logged and swallowed; the wrapped call's result is always
returned to the caller regardless.

D17 keeps never-raise from decaying into silent data loss: a first write
against a schema-less SQLite ledger auto-initializes it (programmatic
migration, one retry; SQLite only — never Postgres), and persistent failures
emit rate-limited REPEATING warnings carrying a cumulative dropped-event
count instead of a single swallowed log line.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ledgerlm.db.models import LlmEvent
from ledgerlm.db.session import get_default_session_factory
from ledgerlm.pricing import price_usage
from ledgerlm.providers.base import NormalizedUsage
from ledgerlm.tagging import current_tags, split_tags

logger = logging.getLogger("ledgerlm")


@dataclass(frozen=True)
class CallEvent:
    """A normalized, provider-agnostic completed call. The recorder does not
    know or care who produced it (the wrap() proxy is merely its first producer)."""

    provider: str
    model: str
    status: str  # "ok" | "error"
    latency_ms: int
    usage: NormalizedUsage | None  # None when the provider returned no usage
    raw_usage: dict[str, Any]
    error_type: str | None = None
    first_token_ms: int | None = None  # streaming only
    prompt_hash: str | None = None
    provider_request_id: str | None = None


class Recorder:
    # Minimum seconds between dropped-event warnings; tests may lower it.
    warn_interval_s: float = 60.0

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._dropped = 0
        self._last_warned: float | None = None
        self._auto_init_attempted = False

    def record(self, event: CallEvent) -> None:
        try:
            self._record(event)
            return
        except OperationalError as exc:
            if self._try_auto_init(exc):
                # Retry REGARDLESS of the migration attempt's outcome (D20):
                # losing a concurrent-initialization race means another
                # process/recorder created the schema — the write must go
                # against the winner's schema, never be dropped.
                try:
                    self._record(event)
                    return
                except Exception:
                    logger.debug("ledgerlm: write retry after auto-init failed", exc_info=True)
        except Exception:
            logger.debug("ledgerlm: event write failed", exc_info=True)
        self._note_dropped(event)

    def _try_auto_init(self, exc: OperationalError) -> bool:
        """Attempt to migrate a schema-less SQLite ledger; True if attempted.

        Returns whether an attempt was made (once per recorder), NOT whether
        the migration itself succeeded — a concurrent initializer may have
        won the race, in which case our alembic run fails but the schema
        exists and the caller's retry succeeds.
        """
        if self._auto_init_attempted or "no such table" not in str(exc):
            return False
        self._auto_init_attempted = True
        url = self._sqlite_url()
        if url is None:
            return False  # not SQLite (or unknown) — never auto-migrate Postgres
        try:
            from ledgerlm.db.migrate import upgrade_to_head

            upgrade_to_head(url)
        except Exception:
            logger.warning(
                "ledgerlm: auto-initialization of %s did not complete (possibly lost a "
                "concurrent-init race); retrying the write anyway",
                url,
                exc_info=True,
            )
            return True
        logger.warning("ledgerlm: initialized empty ledger schema at %s", url)
        return True

    def _sqlite_url(self) -> str | None:
        factory = self._session_factory
        if factory is None:
            from ledgerlm.config import get_settings

            url = get_settings().resolved_db_url
            return url if url.startswith("sqlite") else None
        engine = getattr(factory, "kw", {}).get("bind")
        if engine is None or getattr(engine, "dialect", None) is None:
            return None
        if engine.dialect.name != "sqlite":
            return None
        return str(engine.url.render_as_string(hide_password=False))

    def _note_dropped(self, event: CallEvent) -> None:
        """Rate-limited repeating warning with a cumulative dropped count (D17)."""
        with self._lock:
            self._dropped += 1
            dropped = self._dropped
            now = time.monotonic()
            should_warn = (
                self._last_warned is None or now - self._last_warned >= self.warn_interval_s
            )
            if should_warn:
                self._last_warned = now
        if should_warn:
            logger.warning(
                "ledgerlm: failed to record LLM event (provider=%s model=%s) — %d event(s) "
                "dropped so far by this recorder; the wrapped calls themselves were unaffected",
                event.provider,
                event.model,
                dropped,
            )

    def _record(self, event: CallEvent) -> None:
        factory = self._session_factory or get_default_session_factory()
        reserved, extras = split_tags(current_tags())
        usage = event.usage
        with factory() as session:
            if usage is None:
                # No usage from the provider: tokens unknown. cost stays NULL —
                # never reconstructed, never a fabricated $0.
                cost, snapshot = None, None
                usage = NormalizedUsage()
            else:
                cost, snapshot = price_usage(session, event.provider, event.model, usage)
            session.add(
                LlmEvent(
                    provider=event.provider,
                    model=event.model,
                    status=event.status,
                    error_type=event.error_type,
                    latency_ms=event.latency_ms,
                    first_token_ms=event.first_token_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    raw_usage=event.raw_usage,
                    price_snapshot=snapshot,
                    cost_usd=cost,
                    prompt_hash=event.prompt_hash,
                    project=reserved.get("project"),
                    feature=reserved.get("feature"),
                    env=reserved.get("env"),
                    run_id=reserved.get("run_id"),
                    customer=reserved.get("customer"),
                    tags=extras,
                    provider_request_id=event.provider_request_id,
                )
            )
            session.commit()
