"""SQLAlchemy 2.0 models. Schema must stay SQLite- and Postgres-compatible."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store naive UTC in the DB; hand back timezone-aware UTC datetimes.

    Portable across SQLite and Postgres without relying on either dialect's
    timezone handling. Naive datetimes are rejected on write.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime; timestamps must be aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class LlmEvent(Base):
    __tablename__ = "llm_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), index=True, default=utcnow)
    provider: Mapped[str] = mapped_column(String())
    model: Mapped[str] = mapped_column(String(), index=True)
    status: Mapped[str] = mapped_column(String())  # "ok" | "error"
    error_type: Mapped[str | None] = mapped_column(String())
    latency_ms: Mapped[int]
    input_tokens: Mapped[int]  # normalized: UNCACHED input only
    output_tokens: Mapped[int]
    cache_read_tokens: Mapped[int | None]
    cache_write_tokens: Mapped[int | None]
    raw_usage: Mapped[dict[str, Any]] = mapped_column(JSON)  # provider usage, verbatim
    price_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))  # NULL = unpriced
    prompt_hash: Mapped[str | None] = mapped_column(String(), index=True)
    project: Mapped[str | None] = mapped_column(String(), index=True)
    feature: Mapped[str | None] = mapped_column(String(), index=True)
    env: Mapped[str | None] = mapped_column(String())
    run_id: Mapped[str | None] = mapped_column(String(), index=True)
    customer: Mapped[str | None] = mapped_column(String())
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    provider_request_id: Mapped[str | None] = mapped_column(String())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        Index("ix_llm_events_project_ts", "project", "ts"),
        Index("ix_llm_events_model_ts", "model", "ts"),
    )


class ModelPrice(Base):
    __tablename__ = "model_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String())
    model: Mapped[str] = mapped_column(String())
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    cache_read_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cache_write_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str] = mapped_column(String(), default="USD", server_default=text("'USD'"))
    last_verified: Mapped[date | None]
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("provider", "model"),)
