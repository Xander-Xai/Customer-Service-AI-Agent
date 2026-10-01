"""分布式 runtime 单元测试辅助（不被 pytest 收集）。

提供隔离的 SQLite session factory、fake runtime、可控 dispatcher 与
prometheus counter 读取，避免测试触碰真实数据库/Redis。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base


def make_sqlite_session_factory() -> tuple[Callable[[], Any], Any, str]:
    """返回 (session_factory, engine, db_path)，表结构用 Base.metadata 创建。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="csai-runtime-test-")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return session_factory, engine, path


def dispose(session_factory_engine: Any, path: str) -> None:
    try:
        session_factory_engine.dispose()
    finally:
        if os.path.exists(path):
            os.remove(path)


class FakeRuntime:
    """最小 AgentRuntime 替身：只暴露 ``run`` 协程。"""

    def __init__(self, fn: Callable[..., Any]):
        self._fn = fn
        self.calls: list[dict[str, Any]] = []

    async def run(self, *, thread_id: str, query: str, user_id: str | None = None):
        self.calls.append({"thread_id": thread_id, "query": query, "user_id": user_id})
        result = self._fn(thread_id=thread_id, query=query, user_id=user_id)
        if hasattr(result, "__await__"):
            return await result
        return result


def runtime_provider(runtime: FakeRuntime):
    async def _provider():
        return runtime

    return _provider


class RecordingDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, float | None]] = []

    async def __call__(self, run_id: str, countdown: float | None = None):
        self.calls.append((run_id, countdown))
        return f"task-{len(self.calls)}"


def counter_value(name: str) -> float:
    """读取 prometheus counter 总值（未安装 prometheus 时返回 0）。"""
    try:
        from prometheus_client import REGISTRY
    except Exception:
        return 0.0
    total = 0.0
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name:
                total += sample.value
    return total
