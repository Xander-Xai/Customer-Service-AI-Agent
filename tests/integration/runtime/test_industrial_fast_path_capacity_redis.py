"""Real Redis fast-path leases; isolated keys in the runtime-e2e lane."""

import asyncio
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("TEST_REDIS_URL"), reason="requires real Redis"),
]


@pytest.fixture
def capacity(monkeypatch):
    from core.concurrency import fast_path_admission as admission

    monkeypatch.setattr("core.config.REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setattr("core.config.JWT_SECRET", "fixture")
    monkeypatch.setattr("core.config.AGENT_RUN_THREAD_LOCK_TTL_SECONDS", 300.0)
    monkeypatch.setenv("CSAI_FAST_PATH_MAX_ACTIVE", "2")
    monkeypatch.setenv("CSAI_FAST_PATH_MAX_ACTIVE_PER_USER", "1")
    monkeypatch.setenv("CSAI_FAST_PATH_DEADLINE_SECONDS", "10")
    prefix = f"fixture-{uuid.uuid4().hex}:"
    original = admission.capacity_keys

    def keys(user_id, secret):
        first, second = original(user_id, secret)
        return prefix + first, prefix + second

    monkeypatch.setattr(admission, "capacity_keys", keys)
    return admission


async def test_fast_path_shared_user_and_global_capacity(capacity):
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def hold():
        await entered.put(True)
        await release.wait()
        return "completed"

    async def unexpected():
        pytest.fail("Rejected graph must not execute")

    first = asyncio.create_task(capacity.execute_admitted("owner-a", hold))
    second = None
    try:
        await asyncio.wait_for(entered.get(), 3)
        with pytest.raises(capacity.FastPathAdmissionError) as user:
            await capacity.execute_admitted("owner-a", unexpected)
        assert user.value.code == "FAST_PATH_USER_BUSY"
        second = asyncio.create_task(capacity.execute_admitted("owner-b", hold))
        await asyncio.wait_for(entered.get(), 3)
        with pytest.raises(capacity.FastPathAdmissionError) as global_limit:
            await capacity.execute_admitted("owner-c", unexpected)
        assert global_limit.value.code == "FAST_PATH_GLOBAL_BUSY"
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    assert await capacity.execute_admitted("owner-a", lambda: asyncio.sleep(0, result="ok")) == "ok"


async def test_fast_path_cancel_releases_reservation(capacity):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def hold():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(capacity.execute_admitted("owner", hold))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert await capacity.execute_admitted("owner", lambda: asyncio.sleep(0, result="ok")) == "ok"


async def test_fast_path_lost_lease_refuses_success(capacity):
    import redis.asyncio as redis

    async def lose():
        keys = capacity.capacity_keys("owner", "fixture")
        async with redis.from_url(os.environ["TEST_REDIS_URL"]) as client:
            await client.delete(*keys)
        return "must-not-publish"

    with pytest.raises(capacity.FastPathAdmissionError) as error:
        await capacity.execute_admitted("owner", lose)
    assert error.value.code == "FAST_PATH_LEASE_LOST"


async def test_fast_path_deadline_cancels_operation(capacity, monkeypatch):
    monkeypatch.setenv("CSAI_FAST_PATH_DEADLINE_SECONDS", "1")
    cleaned = asyncio.Event()

    async def hold():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(capacity.FastPathAdmissionError) as error:
        await capacity.execute_admitted("owner", hold)
    assert error.value.code == "FAST_PATH_DEADLINE"
    assert cleaned.is_set()
    assert await capacity.execute_admitted("owner", lambda: asyncio.sleep(0, result="ok")) == "ok"
