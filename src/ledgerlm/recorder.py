"""Event persistence. The recorder NEVER raises into the host app (DESIGN.md §3.4):
any failure here is logged and swallowed; the wrapped call's result is always
returned to the caller regardless."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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
    prompt_hash: str | None = None
    provider_request_id: str | None = None


class Recorder:
    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory

    def record(self, event: CallEvent) -> None:
        try:
            self._record(event)
        except Exception:
            logger.exception(
                "ledgerlm: failed to record LLM event (the wrapped call itself succeeded "
                "and its response was returned to the caller)"
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
