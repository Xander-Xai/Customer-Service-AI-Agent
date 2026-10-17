"""A/B 复现：LLM 工具循环的 ``ordinal`` 作用域。

Issue #12 有两部分，本文件盯住 **Part B**：

``_logical_ordinals`` 过去在 ``for round_num in range(max_tool_rounds)`` **循环内部**
初始化（``agents/base_agent.py``）。于是一个节点里：

- Round 0 发出 ``staging_refund(order=A)`` -> ``ordinal=0``
- Round 1 再次发出 ``staging_refund(order=B)`` -> ``ordinal=0``（字典被重新初始化）

两轮里同工具的调用拿到**同一个** ``operation_key``：

- 参数相同时 -> 第二次 ``CLAIM_SUCCEEDED`` -> 第二次写操作**被静默吞掉**；
- 参数不同时 -> ``CLAIM_CONFLICT`` -> ``PermanentError`` -> 合法的第二笔写被永久拒绝。

两者都违反 ``build_replay_stable_operation_key`` docstring 明确拒绝的
「两次各退款 100 元被当成同一笔」。

本用例用确定性 scripted LLM 驱动**真实** ``_process_with_tools`` 路径（不经图），
两轮各发一次同名同参工具调用，断言 handler **执行两次**。修复前只执行一次。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


class _TwoRoundLLM:
    """确定性 LLM：连续两轮发出同一个写工具调用，然后给出最终回答。

    刻意不接受 base_url / API key，结构上不可能出网。
    """

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self._turn = 0

    async def async_invoke(self, messages, timeout=None, tools=None):
        self._turn += 1
        if self._turn <= 2:
            return type(
                "_R",
                (),
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tc-{self._turn}",
                            "name": self._tool_name,
                            "arguments": json.dumps(self._arguments, ensure_ascii=False),
                        }
                    ],
                },
            )()
        return type("_R", (), {"content": "已为您处理。", "tool_calls": None})()

    async def async_invoke_stream(self, messages, timeout=None):
        content = "已为您处理。"
        yield content


class _ScriptedLLM:
    """确定性 LLM：按给定顺序逐轮返回工具调用序列，``None`` 表示最终回答。

    用于模拟「节点重放时调用序列发生变化」：一次执行发 ``[refund]``，另一次发
    ``[query, refund]``。
    """

    def __init__(self, turns: list[list[dict[str, Any]] | None]) -> None:
        self._turns = list(turns)
        self._calls = 0

    async def async_invoke(self, messages, timeout=None, tools=None):
        idx = self._calls
        self._calls += 1
        tcs = self._turns[idx] if idx < len(self._turns) else None
        if tcs is None:
            return type("_R", (), {"content": "已为您处理。", "tool_calls": None})()
        return type("_R", (), {"content": "", "tool_calls": tcs})()

    async def async_invoke_stream(self, messages, timeout=None):
        content = "已为您处理。"
        yield content


def _tc(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """独立 SQLite ledger + run 上下文 + 记录型「外部系统」。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime import side_effects as se

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ordinal.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    store = se.SideEffectStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(se, "get_side_effect_store", lambda: store)

    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context("run-ordinal", "thread-ordinal", "task-ordinal")
    try:
        yield store
    finally:
        reset_run_context(tokens)


def _make_agent(llm):
    from agents.general_agent import GeneralAgent
    from tools.tool_registry import ToolCachePolicy, ToolRegistry

    calls: list[dict] = []

    async def _handler(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    async def _read_handler(arguments: dict) -> str:
        return "order-ok"

    registry = ToolRegistry()
    registry.register(
        name="staging_refund",
        description="写操作",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )
    registry.register(
        name="query_order",
        description="只读查询",
        parameters={"type": "object", "properties": {}},
        handler=_read_handler,
        side_effect=False,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    agent = GeneralAgent(llm=llm)
    agent.set_tool_registry(registry)
    return agent, calls


async def test_inserted_read_call_does_not_shift_write_slot(ledger):
    """插入一种**只读**工具不得改变某写工具的槽位（计数器按工具名分槽）。

    这是对「重放时多出一条只读调用」这一常见漂移的回归：``_logical_ordinals`` 按
    工具名分槽，``query_order`` 有自己的计数，故 ``[refund]`` 与
    ``[query_order, refund]`` 里 refund 都落在 ``ordinal=0`` -> 同一键 -> 去重。
    """
    arguments = {"order_id": "ORD-READ-SHIFT", "amount": 100}

    agent1, calls = _make_agent(_ScriptedLLM([[_tc("a1", "staging_refund", arguments)], None]))
    state = {
        "customer_query": "给 ORD-READ-SHIFT 退款 100 元",
        "session_id": "ordinal-session-a",
        "user_id": "ordinal-user",
        "query_type": "billing",
        "agents_used": [],
    }
    await agent1._process_with_tools(state, "你是客服。")
    assert len(calls) == 1

    agent2, calls2 = _make_agent(
        _ScriptedLLM(
            [
                [
                    _tc("b1", "query_order", {"order_id": "ORD-READ-SHIFT"}),
                    _tc("b2", "staging_refund", arguments),
                ],
                None,
            ]
        )
    )
    state2 = dict(state, session_id="ordinal-session-b")
    await agent2._process_with_tools(state2, "你是客服。")
    assert len(calls2) == 0, "插入只读调用不得把同一笔写推到另一个槽位"


async def test_same_tool_across_two_rounds_executes_twice(ledger):
    """Part B 回归：同一节点内两轮各发一次同名同参写操作 = 两笔操作，必须都执行。

    修复前：``_logical_ordinals`` 在轮次循环内初始化，两轮都拿到 ``ordinal=0`` ->
    同一个 ``operation_key`` -> 第二次被 ``CLAIM_SUCCEEDED`` 静默吞掉，handler 只
    调用一次。修复后计数器跨轮单调递增，两次拿到不同 ordinal。
    """
    arguments = {"order_id": "ORD-DUP", "amount": 100}
    agent, calls = _make_agent(_TwoRoundLLM("staging_refund", arguments))

    state = {
        "customer_query": "给 ORD-DUP 退款 100 元",
        "session_id": "ordinal-session",
        "user_id": "ordinal-user",
        "query_type": "billing",
        "agents_used": [],
    }
    await agent._process_with_tools(state, "你是客服。")

    assert len(calls) == 2, (
        "两轮各一次合法写操作必须各自执行；被静默合并说明 ordinal 计数器在轮次间"
        "被重置（Issue #12 Part B）"
    )
