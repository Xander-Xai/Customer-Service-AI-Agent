"""Redis thread lock 跨进程语义集成测试（真实 Redis）。

两个独立 client 共享同一 Redis，模拟两个 worker 进程：
  - 同 thread 互斥（第二个 owner 拿不到）；
  - 不同 thread 可并发；
  - 非 owner 不能释放 / 续期；
  - TTL 过期后允许接管。

默认跳过。运行：``TEST_REDIS_URL=redis://localhost:6379 pytest tests/integration/test_thread_lock_redis.py``。
"""

from __future__ import annotations

import os
import uuid

import pytest

REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL 未设置；需要真实 Redis"),
]


@pytest.mark.timeout(60)
async def test_redis_thread_lock_cross_process_semantics():
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    client_a = aioredis.from_url(REDIS_URL, decode_responses=True)
    client_b = aioredis.from_url(REDIS_URL, decode_responses=True)
    lock_a = RedisThreadLock(client_a)
    lock_b = RedisThreadLock(client_b)

    tag = uuid.uuid4().hex[:8]
    t1 = f"it-lock-{tag}-1"
    t2 = f"it-lock-{tag}-2"
    try:
        assert await lock_a.acquire(t1, "worker-a", ttl_seconds=30) is True
        # 另一个 client（进程）拿不到同一 thread
        assert await lock_b.acquire(t1, "worker-b", ttl_seconds=30) is False
        # 不同 thread 可并发
        assert await lock_b.acquire(t2, "worker-b", ttl_seconds=30) is True

        # 非 owner 不能释放 / 续期
        assert await lock_b.release(t1, "worker-b") is False
        assert await lock_b.refresh(t1, "worker-b", ttl_seconds=30) is False

        # owner 释放后可重新获取
        assert await lock_a.release(t1, "worker-a") is True
        assert await lock_b.acquire(t1, "worker-b", ttl_seconds=30) is True
    finally:
        await lock_a.release(t1, "worker-a")
        await lock_b.release(t1, "worker-b")
        await lock_b.release(t2, "worker-b")
        await client_a.aclose()
        await client_b.aclose()


@pytest.mark.timeout(60)
async def test_redis_thread_lock_ttl_allows_takeover():
    import redis.asyncio as aioredis

    from runtime.thread_lock import RedisThreadLock

    client_a = aioredis.from_url(REDIS_URL, decode_responses=True)
    client_b = aioredis.from_url(REDIS_URL, decode_responses=True)
    lock_a = RedisThreadLock(client_a)
    lock_b = RedisThreadLock(client_b)
    thread = f"it-lock-ttl-{uuid.uuid4().hex[:8]}"
    try:
        assert await lock_a.acquire(thread, "worker-a", ttl_seconds=1) is True
        import asyncio

        await asyncio.sleep(1.5)
        # TTL 过期（模拟持有者崩溃）后其他 worker 可接管
        assert await lock_b.acquire(thread, "worker-b", ttl_seconds=30) is True
    finally:
        await lock_b.release(thread, "worker-b")
        await client_a.aclose()
        await client_b.aclose()


@pytest.mark.timeout(60)
async def test_api_boundary_thread_lock_over_redis():
    """API 执行边界 context manager 在真实 Redis 上的互斥 + THREAD_BUSY 语义。"""
    import asyncio

    import redis.asyncio as aioredis

    from core.concurrency.distributed_lock import ThreadBusyError, thread_lock
    from runtime.thread_lock import RedisThreadLock

    client_a = aioredis.from_url(REDIS_URL, decode_responses=True)
    client_b = aioredis.from_url(REDIS_URL, decode_responses=True)
    mgr_a = RedisThreadLock(client_a)
    mgr_b = RedisThreadLock(client_b)
    thread = f"it-api-lock-{uuid.uuid4().hex[:8]}"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with thread_lock(thread, manager=mgr_a, ttl_seconds=30, acquire_timeout=5):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        # 第二个“进程”在锁被持有时拿不到 -> THREAD_BUSY
        with pytest.raises(ThreadBusyError):
            async with thread_lock(thread, manager=mgr_b, ttl_seconds=30, acquire_timeout=0.3):
                pass
        release.set()
        await asyncio.wait_for(task, timeout=5)
        # 释放后可重新获取
        async with thread_lock(thread, manager=mgr_b, ttl_seconds=30, acquire_timeout=2):
            pass
    finally:
        release.set()
        with __import__("contextlib").suppress(Exception):
            await task
        await mgr_a.release(thread, "x")
        await client_a.aclose()
        await client_b.aclose()
