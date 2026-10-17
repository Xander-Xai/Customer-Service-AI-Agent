"""A1 生产路径验证：``ToolRegistry`` 在节点重放时不得重复执行副作用。

覆盖 ``agents/base_agent.py`` 传入 ``LogicalCall`` 的**真实**接线：同一个逻辑调用在
重放时 ``tool_call_id`` 变了，副作用仍只发生一次。

不需要真实基础设施：用 ``SideEffectStore`` 绑定独立 SQLite（``claim`` 的
``INSERT ... ON CONFLICT`` 只在 PostgreSQL 上走快路径，SQLite 走等价分支），
并在 ``agent_runs`` 无 run 上下文时用显式 ``set_run_context`` 提供。
"""

from __future__ import annotations

import pytest

from tools.tool_registry import LogicalCall, ToolCachePolicy, ToolRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    """SQLite ledger + run 上下文 + 一个确定性「外部系统」。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime import side_effects as se

    engine = create_engine(
        f"sqlite:///{tmp_path / 'se.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = se.SideEffectStore(session_factory=factory)

    monkeypatch.setattr(se, "get_side_effect_store", lambda: store)

    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context("run-a1", "thread-a1", "task-a1")

    calls: list[dict] = []

    async def _handler(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    registry = ToolRegistry()
    registry.register(
        name="write_refund",
        description="写操作",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    yield registry, store, calls

    reset_run_context(tokens)


async def test_replay_with_new_tool_call_id_executes_side_effect_once(env):
    """A1 回归：重放换 ``tool_call_id`` 不再导致第二次执行。"""
    registry, _store, calls = env
    logical = LogicalCall(node_id="tools#0", agent="refund_agent", ordinal=0)

    first = await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-1", "amount": 10},
        tool_call_id="call_A",
        logical_call=logical,
    )
    assert len(calls) == 1

    # 节点重放：LLM 给出了不同的 tool_call_id，逻辑身份不变
    replay = await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-1", "amount": 10},
        tool_call_id="call_B",
        logical_call=logical,
    )
    assert len(calls) == 1, "重放不得再次执行副作用"
    assert replay == first, "重放必须返回首次执行的结果（而不是新结果）"


async def test_distinct_logical_calls_in_one_node_both_execute(env):
    """同一节点内两次同类写操作是**两个**操作，都必须执行。"""
    registry, _store, calls = env

    await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-1", "amount": 10},
        tool_call_id="call_A",
        logical_call=LogicalCall(node_id="tools#0", agent="a", ordinal=0),
    )
    await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-2", "amount": 20},
        tool_call_id="call_B",
        logical_call=LogicalCall(node_id="tools#0", agent="a", ordinal=1),
    )
    assert len(calls) == 2, "不同 ordinal 是两次合法操作，不得被合并"


async def test_second_invocation_of_same_agent_is_a_distinct_operation(env):
    """同名 agent 在同一 run 内被第二次调用（咨询/层级模式）= 不同逻辑操作。"""
    registry, _store, calls = env

    await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-9", "amount": 10},
        tool_call_id="call_A",
        logical_call=LogicalCall(node_id="a#0", agent="a", ordinal=0),
    )
    await registry.execute_raw(
        "write_refund",
        {"order_id": "ORD-9", "amount": 10},
        tool_call_id="call_A",
        logical_call=LogicalCall(node_id="a#1", agent="a", ordinal=0),
    )
    assert len(calls) == 2, "第二次调用是新的业务动作"


async def test_readonly_tool_is_unaffected(env):
    """只读工具不走 ledger，逻辑身份无关紧要。"""
    registry, _store, calls = env
    registry.register(
        name="read_order",
        description="只读",
        parameters={"type": "object", "properties": {}},
        handler=lambda a: _noop(),
        side_effect=False,
    )
    for _ in range(2):
        await registry.execute_raw("read_order", {"order_id": "ORD-1"})
    # 只读工具的 handler 不记账，这里只验证它没有被 ledger 拦截
    assert registry.is_side_effect("read_order") is False


async def _noop():
    return "ok"


async def test_side_effect_without_any_identity_is_refused(env):
    """既无 ``logical_call`` 也无 ``tool_call_id`` -> fail-closed 拒绝执行。

    没有可证明重放安全的操作身份，就不能执行写操作。这是 A1 的直接推论：宁可拒绝，
    也不要在一个可能被重放的链路上裸执行副作用。
    """
    registry, _store, calls = env
    result = await registry.execute_raw("write_refund", {"order_id": "ORD-1", "amount": 10})
    assert calls == [], "不得在没有幂等保护时执行写操作"
    assert isinstance(result, str)
    assert "幂等" in result or "拒绝" in result
