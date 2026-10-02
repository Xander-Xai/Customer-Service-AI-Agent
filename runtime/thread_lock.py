"""Thread 级分布式锁：保证同一 thread 的多个 Run 串行执行。

不同 thread 可并发；同一 thread 默认串行。

安全释放语义：
  - owner token（每次获取生成唯一 owner）；
  - TTL（lease timeout，防止持有者崩溃后死锁）；
  - 原子 compare-and-delete（Redis Lua），只能 owner 解锁；
  - 禁止 ``SETNX ... DEL key``（会误删其他 worker 重新获得的锁）。

后端：
  - ``redis``（生产，跨进程/副本）；
  - ``memory``（开发/测试，仅单进程有效）。

范围：仅要求单 Redis 服务上的跨进程互斥，不实现 Redlock 集群算法。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Protocol

from core.logger import get_logger

from .errors import ThreadLockBackendError

logger = get_logger("runtime.thread_lock")

# Redis 官方安全锁语义：比较 owner 后再删除 / 续期
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_REFRESH_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
)

KEY_PREFIX = "agent:thread-lock:"


class ThreadLock(Protocol):
    async def acquire(self, thread_id: str, owner: str, ttl_seconds: float) -> bool: ...
    async def release(self, thread_id: str, owner: str) -> bool: ...
    async def refresh(self, thread_id: str, owner: str, ttl_seconds: float) -> bool: ...
    async def is_locked(self, thread_id: str) -> bool: ...
    async def close(self) -> None: ...


class InMemoryThreadLock:
    """进程内锁（开发/测试）。语义与 Redis 版一致：owner + TTL + 原子比较。

    每实例一份登记表；因此**同一进程内只能有一个实例**才具备互斥意义，manager
    获取函数（``get_thread_lock_manager`` / ``core.concurrency``）共享同一单例。
    """

    def __init__(self, clock=time.monotonic):
        self._locks: dict[str, tuple[str, float]] = {}
        self._guard = asyncio.Lock()
        self._clock = clock

    async def acquire(self, thread_id: str, owner: str, ttl_seconds: float) -> bool:
        async with self._guard:
            existing = self._locks.get(thread_id)
            if existing is not None and existing[1] > self._clock():
                return False
            self._locks[thread_id] = (owner, self._clock() + ttl_seconds)
            return True

    async def release(self, thread_id: str, owner: str) -> bool:
        async with self._guard:
            existing = self._locks.get(thread_id)
            if existing is None or existing[0] != owner:
                return False  # 只能 owner 解锁
            del self._locks[thread_id]
            return True

    async def refresh(self, thread_id: str, owner: str, ttl_seconds: float) -> bool:
        async with self._guard:
            existing = self._locks.get(thread_id)
            if existing is None or existing[0] != owner:
                return False
            self._locks[thread_id] = (owner, self._clock() + ttl_seconds)
            return True

    async def is_locked(self, thread_id: str) -> bool:
        async with self._guard:
            existing = self._locks.get(thread_id)
            return existing is not None and existing[1] > self._clock()

    async def expire(self, thread_id: str) -> None:
        """测试辅助：强制使锁过期。"""
        async with self._guard:
            self._locks.pop(thread_id, None)

    async def close(self) -> None:
        self._locks.clear()


class RedisThreadLock:
    """Redis 分布式锁（生产）。"""

    def __init__(self, client: Any, key_prefix: str = KEY_PREFIX):
        self._client = client
        self._prefix = key_prefix

    def _key(self, thread_id: str) -> str:
        return f"{self._prefix}{thread_id}"

    async def acquire(self, thread_id: str, owner: str, ttl_seconds: float) -> bool:
        try:
            result = await self._client.set(
                self._key(thread_id), owner, nx=True, px=max(1, int(ttl_seconds * 1000))
            )
            return bool(result)
        except Exception as e:
            raise ThreadLockBackendError(f"thread lock acquire 失败: {type(e).__name__}") from e

    async def release(self, thread_id: str, owner: str) -> bool:
        try:
            result = await self._client.eval(_RELEASE_LUA, 1, self._key(thread_id), owner)
            return bool(result)
        except Exception as e:
            raise ThreadLockBackendError(f"thread lock release 失败: {type(e).__name__}") from e

    async def refresh(self, thread_id: str, owner: str, ttl_seconds: float) -> bool:
        try:
            result = await self._client.eval(
                _REFRESH_LUA, 1, self._key(thread_id), owner, max(1, int(ttl_seconds * 1000))
            )
            return bool(result)
        except Exception as e:
            raise ThreadLockBackendError(f"thread lock refresh 失败: {type(e).__name__}") from e

    async def is_locked(self, thread_id: str) -> bool:
        try:
            return bool(await self._client.exists(self._key(thread_id)))
        except Exception as e:
            raise ThreadLockBackendError(f"thread lock exists 失败: {type(e).__name__}") from e

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


_manager: ThreadLock | None = None


def build_thread_lock(backend: str, redis_url: str) -> ThreadLock:
    backend = (backend or "redis").strip().lower()
    if backend == "memory":
        return InMemoryThreadLock()
    if backend == "redis":
        import redis.asyncio as aioredis

        client = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        return RedisThreadLock(client)
    raise ValueError(f"未知 thread lock backend: {backend!r}（仅支持 redis | memory）")


def get_thread_lock_manager() -> ThreadLock:
    """进程内唯一的 thread lock manager 单例（worker 与 API 执行边界共用）。

    必须是**同一个实例**：API 快路径（``core.concurrency.get_api_lock_manager``）与
    worker 路径（``runtime.executor``）都经由此处获取锁，否则进程内会出现两套互不
    相干的锁状态，"同一 key 前缀" 的一致性没有任何实际意义。

    DEV_MODE 下 ``redis`` 后端降级为进程内锁：开发/测试不应强依赖 Redis，生产
    （``DEV_MODE=false``）始终使用配置的后端。
    """
    global _manager
    if _manager is not None:
        return _manager

    from core.config import AGENT_RUN_THREAD_LOCK_BACKEND, DEV_MODE, REDIS_URL

    backend = AGENT_RUN_THREAD_LOCK_BACKEND
    if DEV_MODE and backend == "redis":
        backend = "memory"
    _manager = build_thread_lock(backend, REDIS_URL)
    return _manager


async def shutdown_thread_lock_manager() -> None:
    global _manager
    manager = _manager
    _manager = None
    if manager is not None:
        await manager.close()


def reset_thread_lock_manager_for_tests() -> None:
    global _manager
    _manager = None
