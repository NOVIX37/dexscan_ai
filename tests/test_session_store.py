"""
Playbook reference: Unified Developer Playbook, Part VIII Step 1 - Unit
Testing Requirements.

get/set round-trip, cache_put/cache_get round-trip, and cache_get on an
unknown key returns None rather than raising. Pure stdlib + this project's
own modules only - no pydantic/aiogram dependency, so this file can run
even before those packages are installed.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.types import SessionContext
from state.session_store import SessionStore


def test_get_returns_none_for_unknown_user() -> None:
    store = SessionStore()
    assert store.get(user_id=12345) is None


def test_set_then_get_round_trips() -> None:
    store = SessionStore()
    ctx = SessionContext(user_id=1, payload={"fsm_state": "awaiting_address", "chain": "sol"})

    store.set(1, ctx)
    fetched = store.get(1)

    assert fetched is not None
    assert fetched.user_id == 1
    assert fetched.payload == {"fsm_state": "awaiting_address", "chain": "sol"}


def test_set_replaces_existing_context_for_same_user() -> None:
    store = SessionStore()
    store.set(1, SessionContext(user_id=1, payload={"chain": "sol"}))
    store.set(1, SessionContext(user_id=1, payload={"chain": "eth"}))

    fetched = store.get(1)
    assert fetched is not None
    assert fetched.payload == {"chain": "eth"}


def test_cache_put_get_round_trips() -> None:
    store = SessionStore()
    payload = {"address": "So11111111111111111111111111111111111111112", "chain": "sol"}

    key = store.cache_put(payload)

    assert isinstance(key, str) and len(key) > 0
    assert store.cache_get(key) == payload


def test_cache_put_returns_unique_keys_across_calls() -> None:
    store = SessionStore()
    key_a = store.cache_put({"n": 1})
    key_b = store.cache_put({"n": 2})

    assert key_a != key_b
    assert store.cache_get(key_a) == {"n": 1}
    assert store.cache_get(key_b) == {"n": 2}


def test_cache_get_unknown_key_returns_none_not_exception() -> None:
    store = SessionStore()
    assert store.cache_get("does-not-exist") is None


def test_cache_put_can_add_result_to_user_history() -> None:
    store = SessionStore()

    result_id = store.cache_put({"score": 42}, user_id=7)
    unassociated_id = store.cache_put({"score": 10})

    assert store.get_user_history(7) == [result_id]
    assert unassociated_id not in store.get_user_history(7)
    assert store.get_user_history(8) == []


def test_user_history_is_newest_first_bounded_and_returns_a_copy() -> None:
    store = SessionStore()
    result_ids = [f"result-{index}" for index in range(55)]

    for result_id in result_ids:
        store.add_to_user_history(7, result_id)

    expected = list(reversed(result_ids[-50:]))
    assert store.get_user_history(7, limit=100) == expected
    assert store.get_user_history(7, limit=3) == expected[:3]
    assert store.get_user_history(7, limit=0) == []

    returned = store.get_user_history(7)
    returned.clear()
    assert store.get_user_history(7) == expected


def test_cache_batch_round_trips_copies_and_unknown_batch_returns_none() -> None:
    store = SessionStore()
    result_ids = ["result-a", "result-b"]
    meta = {"total": 2, "source": "trending"}

    batch_id = store.cache_put_batch(7, result_ids, meta)
    result_ids.append("result-c")
    meta["total"] = 3

    assert store.cache_get_batch(batch_id) == (
        ["result-a", "result-b"],
        {"total": 2, "source": "trending"},
    )
    assert store.cache_get_batch("unknown-batch") is None

    cached = store.cache_get_batch(batch_id)
    assert cached is not None
    cached[0].clear()
    cached[1].clear()
    assert store.cache_get_batch(batch_id) == (
        ["result-a", "result-b"],
        {"total": 2, "source": "trending"},
    )


@pytest.mark.asyncio
async def test_scan_task_registry_tracks_source_and_pop_removes_task() -> None:
    store = SessionStore()
    task = asyncio.create_task(asyncio.sleep(60))
    store.set_scan_task(7, task, source="auto_watch")

    assert store.get_scan_task(7) is task
    assert store.get_scan_task_source(7) == "auto_watch"
    assert store.pop_scan_task(7) is task
    assert store.get_scan_task(7) is None
    assert store.get_scan_task_source(7) is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_stores_are_independent_between_instances() -> None:
    """Guards against an accidental module-level mutable default - each
    SessionStore() must start empty, since Step 2's dispatcher tests will
    construct fresh stores per test."""
    store_a = SessionStore()
    store_a.set(1, SessionContext(user_id=1, payload={"chain": "sol"}))
    store_a.add_to_user_history(1, "result-a")

    store_b = SessionStore()
    assert store_b.get(1) is None
    assert store_b.get_user_history(1) == []
