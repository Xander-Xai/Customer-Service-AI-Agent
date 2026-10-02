"""分布式并发原语（API 执行边界用）。

本模块提供 **per-thread 分布式锁** 的 API 层封装，保证同一个
``thread_id`` 在同一时刻只有一个 LangGraph Run 修改状态；不同 thread 仍并行。

实现复用 ``runtime.thread_lock`` 的 Redis/内存原语（owner token + TTL + Lua
compare-and-delete），避免维护两套锁实现。API 快路径（REST/SSE/WS/multimodal）
统一经 ``api/app.py::_run_graph`` 调用本模块。

后端选择：
  - 生产（``DEV_MODE=false``）：按 ``AGENT_RUN_THREAD_LOCK_BACKEND``（默认 redis），
    Redis 不可用时 **fail closed**（抛 ``ThreadLockUnavailableError``），绝不静默并发；
  - 开发/测试：默认使用进程内锁（不依赖 Redis），显式 ``memory`` 亦可。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from core.logger import get_logger
from runtime.errors import ThreadLockBackendError
from runtime.thread_lock import (
    ThreadLock,
    get_thread_lock_manager,
)

logger = get_logger("core.concurrency")

KEY_PREFIX = "agent:thread-lock:"


class ThreadBusyError(RuntimeError):
    """同一 thread 已有正在执行的 Run，未在 acquire timeout 内获得锁。"""

    code = "THREAD_BUSY"

    def __init__(self, thread_id: str, timeout: float):
        self.thread_id = thread_id
        self.timeout = timeout
        super().__init__(f"thread {thread_id} 正在执行中，{timeout:.1f}s 内未获得执行锁")


class ThreadLockUnavailableError(RuntimeError):
    """锁后端不可用，且当前环境要求 fail closed（生产）。"""

    code = "THREAD_LOCK_UNAVAILABLE"


def _new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


_api_manager: ThreadLock | None = None


def get_api_lock_manager() -> ThreadLock | None:
    """返回 API 执行边界使用的锁管理器（进程内单例）。

    - 未启用 -> None（不加锁）；
    - 否则复用 ``runtime.thread_lock.get_thread_lock_manager()`` 的**同一个单例**，
      使 API 快路径与 worker 路径共享锁状态。两者若各持一个实例，在后端降级为
      memory 的 DEV/TEST 下会成为两套互不相干的锁空间，跨路径并发写同一 thread
      无法被发现。

    单例很重要：每次调用都新建 InMemoryThreadLock 会导致进程内完全无互斥。
    """
    global _api_manager
    from core.config import AGENT_RUN_THREAD_LOCK_ENABLED

    if not AGENT_RUN_THREAD_LOCK_ENABLED:
        return None
    if _api_manager is None:
        _api_manager = get_thread_lock_manager()
    return _api_manager


async def shutdown_api_lock_manager() -> None:
    global _api_manager
    manager = _api_manager
    _api_manager = None
    if manager is not None:
        with contextlib.suppress(Exception):
            await manager.close()


def reset_api_lock_manager_for_tests() -> None:
    global _api_manager
    _api_manager = None
    from runtime.thread_lock import reset_thread_lock_manager_for_tests

    reset_thread_lock_manager_for_tests()


@asynccontextmanager
async def thread_lock(
    thread_id: str,
    *,
    owner: str | None = None,
    manager: ThreadLock | None = None,
    ttl_seconds: float | None = None,
    acquire_timeout: float | None = None,
) -> AsyncIterator[str | None]:
    """获取 per-thread 锁并在退出时安全释放。

    - 未启用锁（``AGENT_RUN_THREAD_LOCK_ENABLED=false``）：直接 yield ``None``；
    - ``manager=None`` 表示**解析进程默认 manager**，不等于关闭——要关闭只有配置
      开关一条路径，避免调用方误以为已禁用而实际仍被串行化；
    - 获取失败（超时）：抛 :class:`ThreadBusyError`（调用方映射 409/THREAD_BUSY）；
    - 后端不可用：生产抛 :class:`ThreadLockUnavailableError`；开发记录告警并放行。
    """
    from core.config import (
        AGENT_RUN_THREAD_LOCK_ACQUIRE_TIMEOUT_SECONDS,
        AGENT_RUN_THREAD_LOCK_TTL_SECONDS,
        DEV_MODE,
    )

    mgr = manager if manager is not None else get_api_lock_manager()
    if mgr is None:
        yield None
        return

    ttl = ttl_seconds if ttl_seconds is not None else AGENT_RUN_THREAD_LOCK_TTL_SECONDS
    timeout = (
        acquire_timeout
        if acquire_timeout is not None
        else AGENT_RUN_THREAD_LOCK_ACQUIRE_TIMEOUT_SECONDS
    )
    owner_token = owner or _new_owner()

    from runtime import metrics as run_metrics

    started = time.perf_counter()
    deadline = started + max(0.0, timeout)
    acquired = False
    while True:
        try:
            acquired = await mgr.acquire(thread_id, owner_token, ttl)
        except ThreadLockBackendError as e:
            if not DEV_MODE:
                raise ThreadLockUnavailableError(
                    f"thread lock backend unavailable: {type(e).__name__}"
                ) from e
            logger.warning(
                "thread lock 后端不可用，开发环境放行（不保证跨进程互斥）: thread=%s err=%s",
                thread_id,
                type(e).__name__,
            )
            break
        if acquired:
            break
        if time.perf_counter() >= deadline:
            run_metrics.record_lock_contention()
            raise ThreadBusyError(thread_id, timeout)
        await asyncio.sleep(min(0.05, max(0.0, deadline - time.perf_counter())))

    if not acquired:
        # 开发环境后端不可用：不加锁执行
        yield None
        return

    run_metrics.observe_lock_wait(time.perf_counter() - started)
    run_metrics.record_lock_acquire()
    logger.debug("thread lock acquired thread=%s owner=%s", thread_id, owner_token)
    try:
        yield owner_token
    finally:
        with contextlib.suppress(Exception):
            released = await mgr.release(thread_id, owner_token)
            if not released:
                logger.warning(
                    "thread lock release 未命中 owner（可能已过期）: thread=%s", thread_id
                )


def thread_busy_payload(exc: ThreadBusyError) -> dict[str, Any]:
    """统一的 THREAD_BUSY 错误体（REST/SSE/WS 共用）。"""
    return {
        "error": "THREAD_BUSY",
        "code": exc.code,
        "thread_id": exc.thread_id,
        "message": "该会话正在处理上一条请求，请稍后重试",
    }


__all__ = [
    "KEY_PREFIX",
    "ThreadBusyError",
    "ThreadLockUnavailableError",
    "get_api_lock_manager",
    "reset_api_lock_manager_for_tests",
    "shutdown_api_lock_manager",
    "thread_lock",
    "thread_busy_payload",
]
