"""分布式 Runtime 真实基础设施测试的共用 fixture。

只用**真实** PostgreSQL + 真实 Redis（不 mock）。环境变量未设置时整体 skip，
绝不让"没跑"看起来像"通过"。

注意：本目录**故意不放** ``__init__.py``。放了之后 pytest 会把
``tests/integration`` 插入 ``sys.path``，其中本目录名 ``runtime`` 会**遮蔽**应用的
``runtime`` 包（``runtime.errors`` / ``runtime.executor`` 全部 ImportError）。
因此共享能力一律以 fixture 暴露，不通过 import 跨文件引用。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable, Iterator

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def pg_url() -> str:
    if not DB_URL:
        pytest.skip(INFRA_REASON)
    return DB_URL


@pytest.fixture(scope="session")
def redis_url() -> str:
    if not REDIS_URL:
        pytest.skip(INFRA_REASON)
    return REDIS_URL


@pytest.fixture(scope="session")
def pg_engine(pg_url: str):
    """真实 PostgreSQL engine；确保 runtime 相关表存在（模型元数据，非 migration）。"""
    from sqlalchemy import create_engine

    engine = create_engine(pg_url, pool_pre_ping=True)
    from db.models import AgentDeadLetter, AgentRun, HumanApproval, ToolSideEffect

    for table in (AgentRun, AgentDeadLetter, ToolSideEffect, HumanApproval):
        table.__table__.create(engine, checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture
def redis_client(redis_url: str) -> Iterator:
    """真实 Redis client。"""
    import redis as redis_lib

    client = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def unique() -> Callable[[str], str]:
    """返回唯一标识工厂（避免不同用例互相踩 thread_id / run_id）。"""

    def _make(prefix: str = "rt") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    return _make


@pytest.fixture
def run_service(pg_engine):
    """绑定到真实 PostgreSQL 的 RunService。"""
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService

    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    return RunService(repository=repo)


@pytest.fixture(autouse=True)
def _close_shared_event_publisher():
    """收尾关闭 executor 持有的共享事件 publisher。

    每个用例各自 ``asyncio.run``（各自新 event loop），publisher 的 Redis 连接绑定在
    上一个已关闭的 loop 上，若不显式关闭会在解释器 GC 时打印 "Event loop is closed"。
    """
    yield
    import asyncio

    from runtime import executor

    publisher = getattr(executor, "_shared_publisher", None)
    if publisher is not None:
        executor._shared_publisher = None
        with contextlib.suppress(Exception):
            asyncio.run(publisher.close())


@pytest.fixture
def thread_lock_off(monkeypatch):
    """关闭 per-thread 锁。

    用于只验证状态机/重试语义的用例：那些用例不需要 Redis 锁，且 ``asyncio.run``
    每轮新建 event loop 会让跨轮复用的 Redis 连接在 GC 时报 "Event loop is closed"。
    """
    import core.config as config

    monkeypatch.setattr(config, "AGENT_RUN_THREAD_LOCK_ENABLED", False)
    from runtime import thread_lock

    thread_lock.reset_thread_lock_manager_for_tests()
    yield
    thread_lock.reset_thread_lock_manager_for_tests()


@pytest.fixture
def side_effect_store(pg_engine):
    """绑定到真实 PostgreSQL 的 side-effect ledger。"""
    from runtime.side_effects import SideEffectStore

    return SideEffectStore(session_factory=_session_factory(pg_engine))
