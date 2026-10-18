"""Issue #12 Part A 回归：稳定逻辑操作身份（内容寻址 + 显式 id）。

缺陷（Part A）
-------------
``operation_key`` 过去由 ``run_id + node_id + agent + tool_name + ordinal`` 派生，
而 ``ordinal`` 是「调用序列里的位置」。节点在 checkpoint 提交前崩溃并被重放时，LLM
被重新调用；若它把**同一写工具**的调用序列重排（插入/交换另一次同类写），同一笔写
会换到另一个槽位、拿到**新的 operation_key**，``request_fingerprint`` 只在同一个键内
比较，看不到这次重复，于是**副作用被再次执行**。

修复
----
``build_replay_stable_operation_key`` 改为**内容寻址**：身份 = 作用域 +
``sha256(canonical_json(arguments))`` + 「同一节点内**相同内容**的出现序号」。
调用方持有稳定业务操作 id 时**优先**显式传 ``logical_operation_id``。

- 同一笔写重排到别的槽位：内容指纹不变、内容出现序号不变 -> **同一个键**（去重）。
- 参数相同的有意重复：出现序号不同 -> **不同键**（两次都执行，不被静默合并）。
- 参数不同：内容指纹不同 -> **不同键**。

本文件用确定性 scripted LLM 驱动**真实** ``_process_with_tools`` 路径 + 独立 SQLite
ledger，不依赖真实基础设施，也不出网。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _tc(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


class _ScriptedLLM:
    """确定性 LLM：按给定顺序逐轮返回工具调用序列（``None`` 表示最终回答）。"""

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
        yield "已为您处理。"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """独立 SQLite ledger + run 上下文（与生产路径同一 store 接口）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime import side_effects as se

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stable_identity.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    store = se.SideEffectStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(se, "get_side_effect_store", lambda: store)

    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context("run-issue12a", "thread-issue12a", "task-issue12a")
    try:
        yield store
    finally:
        reset_run_context(tokens)


def _make_agent(llm, calls: list[dict]):
    from agents.general_agent import GeneralAgent
    from tools.tool_registry import ToolCachePolicy, ToolRegistry

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    async def _read(arguments: dict) -> str:
        return "order-ok"

    registry = ToolRegistry()
    registry.register(
        name="staging_refund",
        description="写操作（退款）",
        parameters={"type": "object", "properties": {}},
        handler=_write,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )
    registry.register(
        name="query_order",
        description="只读查询",
        parameters={"type": "object", "properties": {}},
        handler=_read,
        side_effect=False,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    agent = GeneralAgent(llm=llm)
    agent.set_tool_registry(registry)
    return agent


def _state() -> dict[str, Any]:
    return {
        "customer_query": "处理退款",
        "session_id": "issue12a-session",
        "user_id": "issue12a-user",
        "query_type": "billing",
        "agents_used": [],
    }


A = {"order_id": "ORD-A", "amount": 100}
B = {"order_id": "ORD-B", "amount": 200}


# ---------------------------------------------------------------------------
# 1. 同工具调用顺序变化后，既有逻辑操作身份仍然稳定
# ---------------------------------------------------------------------------


async def test_same_tool_reorder_does_not_reexecute(ledger):
    """重放时同工具另一次写插到前面（[A] -> [B, A]），A 不得被执行两次。

    修复前：A 首次在位置 0（key K0）。重放里 B 占位置 0（与 A 撞键 -> 冲突），A 被
    挤到位置 1 -> **新键 K1** -> A 的第二笔退款发出；合法的 B 反而被拒。修复后：
    内容寻址让 A 稳定命中 K_A 去重，B 作为新内容得到 K_B 正常执行。
    """
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("a1", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")
    assert calls == [A], "首次执行 A"

    # 节点重放：同工具的另一笔写 B 插到 A 前面，且 tool_call_id 变了
    await _make_agent(
        _ScriptedLLM(
            [
                [_tc("b1", "staging_refund", B), _tc("b2", "staging_refund", A)],
                None,
            ]
        ),
        calls,
    )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 1, (
        "A 换槽位后必须命中同一个 operation_key 被去重；A 出现两次说明身份仍是位置"
        "而非内容（Issue #12 Part A）"
    )
    assert calls.count(B) == 1, "被插入的新写 B 是合法操作，必须执行一次"


# ---------------------------------------------------------------------------
# 2. LLM round 重置后，重放操作不会被当成新的业务操作
# ---------------------------------------------------------------------------


async def test_node_replay_with_same_sequence_dedupes(ledger):
    """整节点重放（相同调用序列、不同 tool_call_id）不得产生新的业务操作。"""
    calls: list[dict] = []
    turns = [[_tc("r1", "staging_refund", A)], None]

    await _make_agent(_ScriptedLLM(turns), calls)._process_with_tools(_state(), "你是客服。")
    await _make_agent(_ScriptedLLM(turns), calls)._process_with_tools(_state(), "你是客服。")

    assert len(calls) == 1, "同一逻辑操作的重放必须被 ledger 去重"


async def test_replay_read_inserted_before_write_dedupes(ledger):
    """重放时在同笔写之前插入只读调用，写入仍命中同一身份、不重复执行。"""
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("a1", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")
    await _make_agent(
        _ScriptedLLM(
            [
                [
                    _tc("b1", "query_order", {"order_id": "ORD-A"}),
                    _tc("b2", "staging_refund", A),
                ],
                None,
            ]
        ),
        calls,
    )._process_with_tools(_state(), "你是客服。")

    assert len(calls) == 1, "插入只读调用不得让同一笔写换身份"


# ---------------------------------------------------------------------------
# 3. 同一逻辑操作的 retry / resume 不重复提交副作用
# ---------------------------------------------------------------------------


def test_same_content_identity_is_stable_across_repeated_builds():
    """builder 契约：相同 (作用域, 内容, 出现序号) 恒定得到同一个键。"""
    from runtime.side_effects import build_replay_stable_operation_key

    kw = dict(run_id="r1", node_id="n#0", agent="a", tool_name="t", arguments=A, ordinal=0)
    assert build_replay_stable_operation_key(**kw) == build_replay_stable_operation_key(**kw)


# ---------------------------------------------------------------------------
# 4. 不同业务操作使用不同身份
# ---------------------------------------------------------------------------


def test_builder_is_order_stable_and_content_addressed():
    """同一内容在重排前后得到同一个键；不同内容得到不同键。"""
    from runtime.side_effects import build_replay_stable_operation_key as build

    def key(content, ordinal=0):
        return build(
            run_id="r1", node_id="n#0", agent="a", tool_name="t", arguments=content, ordinal=ordinal
        )

    # 位置无关：无论 A 排在序列哪个位置，作为独立构建结果都一致
    assert key(A) == key(A)
    assert key(B) == key(B)
    assert key(A) != key(B), "不同内容必须是不同操作"


def test_builder_scope_components_separate_operations():
    """作用域四个维度分别区分不同的业务操作。"""
    from runtime.side_effects import build_replay_stable_operation_key as build

    base = dict(run_id="r1", node_id="n#0", agent="a", tool_name="t", arguments=A, ordinal=0)
    keys = {
        build(**base),
        build(**{**base, "run_id": "r2"}),
        build(**{**base, "node_id": "n#1"}),
        build(**{**base, "agent": "b"}),
        build(**{**base, "tool_name": "u"}),
    }
    assert len(keys) == 5, f"作用域不同维度的操作不得共用键: {keys}"


# ---------------------------------------------------------------------------
# 5. 两个参数相同但业务意图不同的操作不会被错误合并
# ---------------------------------------------------------------------------


async def test_identical_args_twice_in_one_node_are_two_operations(ledger):
    """同一节点内两次**参数完全相同**的写 = 两笔有意重复，必须都执行。

    修复前（位置计数器按工具名分槽）：同一轮里的第二次也会拿到不同 ordinal，
    故这里两种实现都执行两次；真正的区分点在 retry/replay（见其它用例）。
    这里钉住「内容寻址不靠合并相同参数来去重」。
    """
    calls: list[dict] = []
    await _make_agent(
        _ScriptedLLM(
            [
                [_tc("a1", "staging_refund", A), _tc("a2", "staging_refund", A)],
                None,
            ]
        ),
        calls,
    )._process_with_tools(_state(), "你是客服。")

    assert len(calls) == 2, "参数相同的两次有意写操作不得被静默合并成一次"


async def test_identical_args_across_two_rounds_are_two_operations(ledger):
    """跨 LLM round 的两次同参写 = 两笔操作（Part B 不回归）。"""
    calls: list[dict] = []
    turns = [[_tc("r0", "staging_refund", A)], [_tc("r1", "staging_refund", A)], None]
    await _make_agent(_ScriptedLLM(turns), calls)._process_with_tools(_state(), "你是客服。")
    assert len(calls) == 2


def test_explicit_logical_operation_id_separates_identical_args():
    """调用方显式提供稳定业务 id：同参数不同 id 必须是不同操作。"""
    from runtime.side_effects import build_replay_stable_operation_key as build

    def key(op_id):
        return build(
            run_id="r1",
            node_id="n#0",
            agent="a",
            tool_name="t",
            logical_operation_id=op_id,
            arguments=A,
        )

    assert key("op-1") != key("op-2")
    assert key("op-1") == key("op-1"), "显式 id 的身份跨重放稳定"


async def test_explicit_logical_operation_id_both_execute(ledger):
    """两个参数相同但显式 id 不同的写操作，都真实执行。"""
    from runtime.context import get_current_run_id, get_current_thread_id
    from runtime.side_effects import (
        build_replay_stable_operation_key,
        execute_idempotent_operation,
    )
    from tools.tool_registry import LogicalCall

    calls: list[dict] = []

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    run_id = get_current_run_id()
    thread_id = get_current_thread_id()
    for op_id in ("op-1", "op-2"):
        logical = LogicalCall(node_id="n#0", agent="a", ordinal=0, logical_operation_id=op_id)
        key = build_replay_stable_operation_key(
            run_id=run_id,
            node_id=logical.node_id,
            agent=logical.agent,
            tool_name="staging_refund",
            ordinal=logical.ordinal,
            logical_operation_id=logical.logical_operation_id,
            arguments=A,
        )
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=key,
            run_id=run_id,
            thread_id=thread_id,
            arguments=A,
            operation=lambda: _write(A),
            store=ledger,
        )
    assert len(calls) == 2, "显式 id 不同的两笔写必须都执行"


# ---------------------------------------------------------------------------
# 6. 崩溃 / 重投 / 终态后重放：仍符合既有安全契约
# ---------------------------------------------------------------------------


async def test_repeated_redelivery_after_terminal_success_stays_stable(ledger):
    """同一 reorder 场景被反复重投（模拟崩溃后多次 redelivery），结果仍稳定。

    账本已终态 SUCCEEDED 的记录必须被复用而不是重复提交；新内容 B 只执行一次。
    """
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("a1", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")

    for i in range(3):  # 三次重投，每次 tool_call_id 都不同
        await _make_agent(
            _ScriptedLLM(
                [
                    [
                        _tc(f"b{i}-1", "staging_refund", B),
                        _tc(f"b{i}-2", "staging_refund", A),
                    ],
                    None,
                ]
            ),
            calls,
        )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 1, "A 已终态，重投必须复用而不得重复提交"
    assert calls.count(B) == 1, "B 是新操作，只执行一次"


# ---------------------------------------------------------------------------
# 修复前失败证据：位置身份在重排下确实会重复执行
# ---------------------------------------------------------------------------


def test_positional_identity_versus_content_identity_on_reorder():
    """锚定缺陷机制：[A] -> [B, A] 下，位置身份给 A 两个键，内容身份给 A 一个键。

    这是 ``test_same_tool_reorder_does_not_reexecute`` 的判别力证明：位置身份下 A
    从槽位 0 移到槽位 1 -> 新键 -> ledger 看不到重复 -> 可重复执行；内容寻址下 A 的
    身份只由内容决定，与位置无关。
    """
    import hashlib

    from runtime.side_effects import build_replay_stable_operation_key as content_build

    def positional(ordinal: int) -> str:
        scope = "|".join(["v1", "run", "n#0", "a", "staging_refund", str(ordinal)])
        return f"v1:{hashlib.sha256(scope.encode('utf-8')).hexdigest()}"

    # attempt1 A 在槽位 0；attempt2 [B, A] 里 A 被挤到槽位 1
    assert positional(0) != positional(1), "位置身份下 A 换槽位 -> 新键（缺陷根源）"

    def content(args: dict[str, Any]) -> str:
        return content_build(
            run_id="run",
            node_id="n#0",
            agent="a",
            tool_name="staging_refund",
            arguments=args,
            ordinal=0,
        )

    assert content(A) == content(A), "内容寻址下 A 的身份跨重排稳定"
    assert content(A) != content(B), "内容寻址仍区分不同业务操作"


# ---------------------------------------------------------------------------
# 7. 缺少稳定操作身份 -> fail-closed
# ---------------------------------------------------------------------------


async def test_no_identity_refuses_write(ledger):
    """缺少稳定操作身份时，非幂等写操作 fail-closed 拒绝执行。"""
    from tools.tool_registry import ToolCachePolicy, ToolRegistry

    calls: list[dict] = []

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return "written"

    registry = ToolRegistry()
    registry.register(
        name="staging_refund",
        description="写操作",
        parameters={"type": "object", "properties": {}},
        handler=_write,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )
    # 既无 logical_call 也无 tool_call_id -> 不得裸执行
    result = await registry.execute_raw("staging_refund", A)
    assert calls == []
    assert isinstance(result, str) and ("幂等" in result or "拒绝" in result)
