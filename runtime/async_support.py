"""Celery worker 的进程内持久事件循环。

Celery prefork 任务函数是同步的，而 runtime/graph 是异步的。这里为每个 worker
进程维护一个可复用的事件循环，避免每个任务 ``asyncio.run`` 反复创建/销毁 loop
（async 客户端如 httpx/Qdrant/checkpoint pool 会绑定到 loop）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def get_worker_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """在当前 worker 进程的持久 loop 上运行协程。"""
    return get_worker_loop().run_until_complete(coro)


def shutdown_worker_loop() -> None:
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None
