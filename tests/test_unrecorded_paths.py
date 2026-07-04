"""OpenAI responses.stream() passes through unrecorded with a one-time warning."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import select

import ledgerlm.wrapper
from conftest import Ledger
from ledgerlm.db.models import LlmEvent
from ledgerlm.providers.openai import OpenAIAdapter
from ledgerlm.recorder import Recorder
from ledgerlm.wrapper import _TransparentProxy


class _FakeResponsesStreamManager:
    """Stands in for the OpenAI SDK's responses.stream() return value."""


class _FakeResponses:
    def __init__(self) -> None:
        self.stream_calls: list[dict[str, Any]] = []
        self.manager = _FakeResponsesStreamManager()

    def stream(self, **kwargs: Any) -> _FakeResponsesStreamManager:
        self.stream_calls.append(kwargs)
        return self.manager


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


def test_responses_stream_passes_through_with_one_time_warning(
    ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    ledgerlm.wrapper.reset_unrecorded_warnings()
    inner = _FakeOpenAIClient()
    client = _TransparentProxy(inner, OpenAIAdapter(), Recorder(), ())

    with caplog.at_level(logging.WARNING, logger="ledgerlm"):
        first = client.responses.stream(model="gpt-5.4", input="hi")
        second = client.responses.stream(model="gpt-5.4", input="again")

    # Pass-through: the caller gets the SDK's own object, untouched
    assert first is inner.responses.manager
    assert second is inner.responses.manager
    assert len(inner.responses.stream_calls) == 2

    # Warn once, not per call
    warnings = [
        r
        for r in caplog.records
        if "responses.stream() calls are not recorded in v0" in r.getMessage()
    ]
    assert len(warnings) == 1

    # Unrecorded: no ledger rows
    with ledger.session_factory() as session:
        assert session.execute(select(LlmEvent)).scalars().all() == []
