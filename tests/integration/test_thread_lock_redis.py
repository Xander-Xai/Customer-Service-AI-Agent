"""Redis thread lock 集成测试（真实 Redis）。

仅在提供 TEST_REDIS_URL 时运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/integration/test_thread_lock_redis.py -q

验证 Redis Lua compare-and-delete / compare-and-expire 的 owner 语义与 TTL 恢复。
"""

import asyncio
import os
import uuid

import pytest

REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="TEST_REDIS_URL 未设置；需要真实 Redis 才能运行",
)


async def test_redis_lock_owner_semantics_and_expiry():
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    lock = RedisThreadLock(client, key_prefix=f"agent:test-lock:{uuid.uuid4().hex}:")
    thread = "t1"
    try:
        assert await lock.acquire(thread, "owner-1", 60) is True
        # 已被占用
        assert await lock.acquire(thread, "owner-2", 60) is False
        # 非 owner 不能删除
        assert await lock.release(thread, "owner-2") is False
        assert await lock.is_locked(thread) is True
        # owner 可删除
        assert await lock.release(thread, "owner-1") is True
        assert await lock.is_locked(thread) is False

        # TTL 过期后可被其他 owner 获取
        assert await lock.acquire(thread, "owner-3", 1) is True
        await asyncio.sleep(1.3)
        assert await lock.acquire(thread, "owner-4", 60) is True
        assert await lock.release(thread, "owner-4") is True
    finally:
        await lock.release(thread, "owner-1")
        await lock.release(thread, "owner-4")
        await client.aclose()
