"""tags(): nesting overrides and isolation across concurrent asyncio tasks."""

from __future__ import annotations

import asyncio

from ledgerlm.tagging import current_tags, split_tags, tags


def test_nesting_inner_overrides_outer_per_key() -> None:
    with tags(project="outer", env="prod", team="core"):
        assert current_tags() == {"project": "outer", "env": "prod", "team": "core"}
        with tags(project="inner", run_id="r1"):
            assert current_tags() == {
                "project": "inner",
                "env": "prod",
                "team": "core",
                "run_id": "r1",
            }
        # inner scope restored on exit
        assert current_tags() == {"project": "outer", "env": "prod", "team": "core"}
    assert current_tags() == {}


def test_split_reserved_keys_from_extras() -> None:
    reserved, extras = split_tags(
        {"project": "p", "feature": "f", "env": "e", "run_id": 42, "customer": "c", "x": [1]}
    )
    assert reserved == {
        "project": "p",
        "feature": "f",
        "env": "e",
        "run_id": "42",
        "customer": "c",
    }
    assert extras == {"x": [1]}


async def test_isolation_across_concurrent_async_tasks() -> None:
    seen: dict[str, dict[str, object]] = {}

    async def worker(name: str) -> None:
        with tags(project=name, task=name):
            await asyncio.sleep(0.01)  # force interleaving
            seen[name] = current_tags()
            with tags(feature=f"{name}-inner"):
                await asyncio.sleep(0.01)
                seen[f"{name}-inner"] = current_tags()

    await asyncio.gather(worker("a"), worker("b"), worker("c"))

    for name in ("a", "b", "c"):
        assert seen[name] == {"project": name, "task": name}
        assert seen[f"{name}-inner"] == {
            "project": name,
            "task": name,
            "feature": f"{name}-inner",
        }


async def test_tags_do_not_leak_into_sibling_task_started_outside_scope() -> None:
    started = asyncio.Event()
    result: dict[str, object] = {}

    async def outside_observer() -> None:
        await started.wait()
        result["outside"] = current_tags()

    async def tagged() -> None:
        with tags(project="secret"):
            started.set()
            await asyncio.sleep(0.01)

    await asyncio.gather(outside_observer(), tagged())
    assert result["outside"] == {}
