"""The wrap() proxy mirrors async clients: awaited calls record one row each."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

import ledgerlm
from conftest import Ledger
from ledgerlm.db.models import LlmEvent
from ledgerlm.providers.mock import MockLLMClient, MockResponse


class AsyncMockLLMClient(MockLLMClient):
    """Mock whose messages.create is a coroutine, like AsyncAnthropic/AsyncOpenAI."""

    @property
    def messages(self) -> Any:
        outer = self

        class _AsyncMessages:
            async def create(self, **kwargs: Any) -> MockResponse:
                outer.calls.append(kwargs)
                return MockResponse(
                    model=str(kwargs.get("model", outer.model)),
                    content=outer.response_text,
                    usage=outer.usage,
                )

        return _AsyncMessages()


async def test_async_call_is_awaited_and_recorded(ledger: Ledger) -> None:
    client = ledgerlm.wrap(AsyncMockLLMClient())
    with ledgerlm.tags(project="async-net"):
        resp = await client.messages.create(
            model="mock-model", messages=[{"role": "user", "content": "hi"}]
        )
    assert resp.content == "mock response"

    with ledger.session_factory() as session:
        event = session.execute(select(LlmEvent)).scalar_one()
    assert event.project == "async-net"
    assert event.status == "ok"
    assert event.cost_usd is not None
