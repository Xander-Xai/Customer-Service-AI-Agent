"""LangGraph Checkpoint 后端（memory / postgres）配置与生命周期测试。

覆盖任务要求的 6 个用例：
  1. test_postgres_checkpointer_config
  2. test_production_rejects_memory_checkpointer
  3. test_thread_checkpoint_survives_graph_rebuild
  4. test_different_threads_are_isolated
  5. test_checkpointer_close
  6. test_postgres_unavailable_production_fail_fast

这些是本地单元测试（IMPLEMENTED / LOCALLY VERIFIED），不是生产验证。
真正跨进程持久化由 tests/integration/test_checkpoint_postgres.py（需真实 PG）覆盖。
"""

from typing import TypedDict

import pytest

import core.config as config
from core.checkpointer import (
    BACKEND_MEMORY,
    BACKEND_POSTGRES,
    STATUS_HEALTHY,
    CheckpointRuntime,
    build_memory_checkpointer,
    close_checkpoint_runtime,
    probe_checkpoint_runtime,
)
from core.container import ServiceContainer


class _State(TypedDict, total=False):
    value: int


def _build_tiny_graph(checkpointer):
    """最小 StateGraph：节点把 value 自增 1。"""
    from langgraph.graph import StateGraph

    graph = StateGraph(_State)
    graph.add_node("inc", lambda s: {"value": s.get("value", 0) + 1})
    graph.set_entry_point("inc")
    graph.set_finish_point("inc")
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 1. 配置解析 / DSN 安全转换
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_checkpointer_config():
    """backend 解析与 SQLAlchemy→libpq DSN 安全转换。"""
    # 空值：开发 memory / 生产 postgres
    assert config.resolve_checkpoint_backend("", dev_mode=True) == "memory"
    assert config.resolve_checkpoint_backend("", dev_mode=False) == "postgres"
    # 显式值
    assert config.resolve_checkpoint_backend("memory", dev_mode=False) == "memory"
    assert config.resolve_checkpoint_backend("postgres", dev_mode=True) == "postgres"
    # 非法值 fail closed
    with pytest.raises(config.ConfigurationError):
        config.resolve_checkpoint_backend("redis", dev_mode=True)

    # DSN 转换：只接受 postgres 协议，剥离 SQLAlchemy driver 后缀
    assert (
        config.derive_checkpoint_database_url(
            "", database_url="postgresql+psycopg2://u:p@h:5432/db"
        )
        == "postgresql://u:p@h:5432/db"
    )
    assert (
        config.derive_checkpoint_database_url("postgres://u:p@h/db")
        == "postgresql://u:p@h/db"
    )
    # 显式 URL 优先
    assert (
        config.derive_checkpoint_database_url(
            "postgresql://explicit@h/db", database_url="postgresql://other@h/db"
        )
        == "postgresql://explicit@h/db"
    )
    # 非 postgres 协议一律拒绝（不做字符串乱替换）
    assert config.derive_checkpoint_database_url("", database_url="sqlite:///x.db") is None
    assert config.derive_checkpoint_database_url("", database_url="mysql://h/db") is None
    assert config.derive_checkpoint_database_url("", database_url="not-a-dsn") is None


# ---------------------------------------------------------------------------
# 2. 生产禁止 MemorySaver
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_rejects_memory_checkpointer():
    """生产（dev_mode=False）显式或自动 memory 都必须被校验拒绝。"""
    problems = config.validate_checkpoint_settings(
        "memory", explicit_url="", database_url="postgresql://u:p@h/db", dev_mode=False
    )
    assert problems and "postgres" in problems[0]

    # 自动解析在生产就是 postgres，此时没有 URL 也应报错
    auto = config.resolve_checkpoint_backend("", dev_mode=False)
    problems = config.validate_checkpoint_settings(
        auto, explicit_url="", database_url="", dev_mode=False
    )
    assert problems

    # 开发环境允许 memory
    assert config.validate_checkpoint_settings("memory", dev_mode=True) == []
    # 生产 postgres + 合法 URL 通过
    assert (
        config.validate_checkpoint_settings(
            "postgres",
            explicit_url="",
            database_url="postgresql://u:p@h:5432/db",
            dev_mode=False,
        )
        == []
    )


# ---------------------------------------------------------------------------
# 3/4. thread 语义：重建后恢复、不同 thread 隔离
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_thread_checkpoint_survives_graph_rebuild():
    """同一 thread_id 在 graph 重建后仍能读取到持久化的 checkpoint。"""
    runtime = build_memory_checkpointer()
    config_a = {"configurable": {"thread_id": "thread-A"}}

    graph_before = _build_tiny_graph(runtime.checkpointer)
    result = await graph_before.ainvoke({"value": 0}, config=config_a)
    assert result["value"] == 1

    # 模拟 graph rebuild（不共享 graph 对象，只共享 checkpointer）
    graph_after = _build_tiny_graph(runtime.checkpointer)
    state = await graph_after.aget_state(config_a)
    assert state.values["value"] == 1


@pytest.mark.unit
async def test_different_threads_are_isolated():
    """不同 thread_id 的 checkpoint 互不串状态。"""
    runtime = build_memory_checkpointer()
    graph = _build_tiny_graph(runtime.checkpointer)
    config_a = {"configurable": {"thread_id": "thread-A"}}
    config_b = {"configurable": {"thread_id": "thread-B"}}

    await graph.ainvoke({"value": 0}, config=config_a)
    state_b = await graph.aget_state(config_b)
    assert state_b.values == {}

    result_b = await graph.ainvoke({"value": 100}, config=config_b)
    assert result_b["value"] == 101

    state_a = await graph.aget_state(config_a)
    assert state_a.values["value"] == 1


# ---------------------------------------------------------------------------
# 5. 生命周期 close
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self):
        self.closed = False

    async def close(self, timeout: float = 5.0):
        self.closed = True


@pytest.mark.unit
async def test_checkpointer_close():
    """关闭释放连接池并清空容器引用（幂等）。"""
    fake_pool = _FakePool()
    runtime = CheckpointRuntime(
        backend=BACKEND_POSTGRES, checkpointer=object(), pool=fake_pool
    )
    await close_checkpoint_runtime(runtime)
    assert fake_pool.closed is True

    container = ServiceContainer()
    container._checkpoint_runtime = runtime
    container.checkpointer = runtime.checkpointer
    await container._close_checkpointer()
    assert container._checkpoint_runtime is None
    assert container.checkpointer is None
    # 再次关闭不抛异常
    await container._close_checkpointer()

    # memory 后端 close 为 no-op
    mem = build_memory_checkpointer()
    await close_checkpoint_runtime(mem)


@pytest.mark.unit
async def test_checkpointer_health_probe():
    """健康探针只返回 backend/status，不泄露连接串。"""
    mem = build_memory_checkpointer()
    info = await probe_checkpoint_runtime(mem)
    assert info == {"backend": BACKEND_MEMORY, "status": STATUS_HEALTHY}

    disabled = await probe_checkpoint_runtime(None)
    assert disabled["backend"] == "disabled"
    assert "postgresql://" not in str(disabled)


# ---------------------------------------------------------------------------
# 6. 生产 postgres 不可用 fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_postgres_unavailable_production_fail_fast(monkeypatch):
    """生产环境 postgres 连接失败必须抛 ConfigurationError，不得回退 MemorySaver。"""
    monkeypatch.setattr(config, "LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT", 0.5)
    container = ServiceContainer()
    with pytest.raises(config.ConfigurationError):
        await container._init_checkpointer(
            backend="postgres",
            database_url="postgresql://nouser:nopass@127.0.0.1:1/nodb",
            dev_mode=False,
        )
    assert container.checkpointer is None
    assert container._checkpoint_runtime is None
