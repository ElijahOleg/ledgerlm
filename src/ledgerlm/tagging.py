"""Attribution tag scopes: contextvars-based, nestable, async-safe."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

RESERVED_TAG_KEYS = ("project", "feature", "env", "run_id", "customer")

_current: ContextVar[dict[str, Any]] = ContextVar("ledgerlm_tags", default={})  # noqa: B039 - never mutated, only replaced


@contextmanager
def tags(**kwargs: Any) -> Iterator[None]:
    """Attach attribution tags to every call recorded inside the scope.

    Reserved keys (project, feature, env, run_id, customer) map to ledger
    columns; any other kwargs land in the ``tags`` JSON column. Scopes nest;
    inner scopes override outer ones per key. Safe across asyncio tasks.
    """
    token = _current.set({**_current.get(), **kwargs})
    try:
        yield
    finally:
        _current.reset(token)


def current_tags() -> dict[str, Any]:
    """A copy of the tags in effect for the current context."""
    return dict(_current.get())


def split_tags(all_tags: dict[str, Any]) -> tuple[dict[str, str | None], dict[str, Any]]:
    """Split into (reserved column values, JSON extras)."""
    reserved: dict[str, str | None] = {}
    extras: dict[str, Any] = {}
    for key, value in all_tags.items():
        if key in RESERVED_TAG_KEYS:
            reserved[key] = None if value is None else str(value)
        else:
            extras[key] = value
    return reserved, extras
