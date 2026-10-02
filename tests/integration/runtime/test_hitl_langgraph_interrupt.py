"""真实 LangGraph ``interrupt`` / ``Command(resume=...)`` + 真实 PostgreSQL checkpoint。

为什么必须是真 LangGraph + 真 checkpointer
------------------------------------------
``core/hitl/gate.py`` 的模块 docstring 对 langgraph 1.2.12 / Python 3.10 断言了
**具体语义**。这些断言是整个审批恢复机制的地基，若随版本变化而失效，图会静默地
「不推进」——run 永远停在 WAITING_APPROVAL，且没有任何报错：

- ``interrupt()`` 内部依赖 ``get_config()``，深层 async 节点里必须显式设置
  ``var_child_runnable_config``，否则收集不到 interrupt；
- ``ainvoke(...)`` 在 interrupt 时**不抛异常**，而是在返回值注入 ``__interrupt__``；
- ``ainvoke(None, config)`` **无法**解除 interrupt（原样再次返回 ``__interrupt__``），
  恢复必须显式传 ``Command(resume=...)``；
- ``Command(resume=...)`` **必须**有 checkpointer，否则
  ``RuntimeError: Cannot use Command(resume=...) without checkpointer``。

最后一条是本文件用真实 PostgreSQL checkpoint（而非 MemorySaver）的直接原因：审批
恢复依赖 durable checkpoint 跨 worker / 跨进程续跑，MemorySaver 证明不了这一点，
而它恰恰是最容易让人误以为「已经验证过」的替代品。

沿用本目录约定：``TEST_DISTRIBUTED_DB_URL`` 未设置时整体 skip。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from core.hitl.approval_service import (
    DECISION_APPROVE,
    DECISION_REJECT,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalService,
)
from core.hitl.gate import INTERRUPT_KEY, run_approval_gate
from tools.hitl_staging_tools import (
    register_hitl_staging_tools,
    reset_staging_ledger,
    staging_call_count,
)
from tools.tool_registry import ToolRegistry

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def approvals(session_factory, monkeypatch):
    """审批 service 指向真实 PostgreSQL（默认单例在测试里指向本地 SQLite）。"""
    import core.hitl.approval_service as approval_mod

    svc = ApprovalService(session_factory=session_factory)
    monkeypatch.setattr(approval_mod, "_default_service", svc, raising=False)
    yield svc
    from sqlalchemy import delete

    from db.models import HumanApproval

    with contextlib.suppress(Exception):
        session = session_factory()
        try:
            session.execute(delete(HumanApproval))
            session.commit()
        finally:
            session.close()


@pytest.fixture
def container(session_factory, monkeypatch):
    """staging 工具 + 真实 PG 上的 side-effect ledger。"""
    import runtime.side_effects as se
    from runtime.side_effects import SideEffectStore

    monkeypatch.setattr(
        se, "_default_store", SideEffectStore(session_factory=session_factory), raising=False
    )
    reset_staging_ledger()
    registry = ToolRegistry()
    register_hitl_staging_tools(registry)
    holder = SimpleNamespace(tool_registry=registry)
    yield holder
    reset_staging_ledger()


@contextlib.contextmanager
def _run_ctx(run_id: str, thread_id: str):
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context(run_id, thread_id, "task-1")
    try:
        yield
    finally:
        reset_run_context(tokens)


class _GateState(TypedDict, total=False):
    """与生产 ``core.state.AgentState`` 同形状的**声明式** state。

    这里必须用 TypedDict 而**不能**用 ``StateGraph(dict)``。``dict`` 会让整个
    state 变成单一 channel，LangGraph 于是把 ``__interrupt__`` 当成 state 字段
    一起持久化并在下一轮合并回返回值——于是「已成功恢复并跑完」的 run，返回值里
    仍带着 ``__interrupt__``，executor 的 ``_awaiting_approval()`` 会误判为
    「还在等审批」，把一个已经完成的 run 重新挂成 WAITING_APPROVAL。

    生产用的是 ``AgentState``（TypedDict，total=False），``__interrupt__`` 不是
    其中的 key，所以不会泄漏。本类把这个前提**钉死**：一旦有人把生产图的状态
    声明改成裸 dict，本文件的断言会立刻失败。
    """

    session_id: str
    user_id: str | None
    pending_actions: list
    approval_results: list


def _build_gate_graph(checkpointer, container):
    """最小真图：唯一节点就是生产代码里的 ``run_approval_gate``。

    container 由闭包持有，**不放进 state**——state 会被 checkpoint 序列化，
    里面放 ToolRegistry 会直接 ``TypeError: not msgpack serializable``。
    生产实现（``core/graph_builder.py::_human_approval_gate_node``）同样是闭包捕获。
    """

    async def _gate(state: dict, config):
        result = await run_approval_gate(dict(state), config, container)
        out = dict(state)
        if result:
            out.update(result)
        return out

    builder = StateGraph(_GateState)
    builder.add_node("human_approval_gate", _gate)
    builder.add_edge(START, "human_approval_gate")
    builder.add_edge("human_approval_gate", END)
    return builder.compile(checkpointer=checkpointer)


def _state(thread_id: str, actions: list[dict]) -> dict:
    return {
        "session_id": thread_id,
        "user_id": "customer-1",
        "pending_actions": actions,
    }


def _pending(action: str = "staging_refund", **extra) -> dict:
    return {
        "tool": action,
        "arguments": {"order_id": f"O-{uuid.uuid4().hex[:8]}", "amount": 120, **extra},
        "agent": "order_agent",
        "risk_level": "high",
    }


def _scenario(pg_url, approvals, container, actions, drive):
    """在**一个** event loop 内跑完整个图交互（checkpointer 的连接池绑定 loop）。"""
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    async def _main():
        rt = await build_postgres_checkpointer(pg_url, min_size=1, max_size=2, setup_timeout=60.0)
        try:
            graph = _build_gate_graph(rt.checkpointer, container)
            return await drive(graph, rt)
        finally:
            await close_checkpoint_runtime(rt)

    return asyncio.run(_main())


class TestRealInterruptSemantics:
    def test_interrupt_pauses_graph_and_persists_checkpoint(self, pg_url, approvals, container):
        """真图上 interrupt 不抛异常，而是返回 ``__interrupt__``，且 checkpoint 已落库。"""
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                result = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
            # checkpoint 真的持久化了（durable，不是内存）
            assert await rt.checkpointer.aget_tuple(cfg) is not None
            return result

        result = _scenario(pg_url, approvals, container, None, _drive)

        assert isinstance(result, dict)
        assert result.get(INTERRUPT_KEY), f"真实图应挂起，实际 keys={sorted(result)}"
        payload = result[INTERRUPT_KEY][0].value
        assert payload["type"] == "human_approval_required"
        assert payload["action"] == "staging_refund"
        assert set(payload["allowed_decisions"]) == {"approve", "edit", "reject"}
        assert payload["approval_id"]

        # 副作用**尚未**发生：审批的意义就是拦在执行之前
        assert staging_call_count(payload["proposal"]["order_id"]) == 0
        stored = approvals.require(payload["approval_id"])
        assert stored["status"] == STATUS_PENDING
        assert stored["run_id"] == run_id

    def test_ainvoke_none_cannot_clear_the_interrupt(self, pg_url, approvals, container):
        """``ainvoke(None, config)`` 解除不了 interrupt。

        这条决定了 ``runtime/bootstrap.invoke_graph_with_resume`` 必须区分
        「崩溃恢复(ainvoke(None))」与「审批恢复(Command(resume=...))」两条路径。
        """
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                first = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
                second = await graph.ainvoke(None, cfg)
            return first, second

        first, second = _scenario(pg_url, approvals, container, None, _drive)

        assert first.get(INTERRUPT_KEY)
        assert second.get(INTERRUPT_KEY), "ainvoke(None) 不应解除 interrupt"
        order_id = first[INTERRUPT_KEY][0].value["proposal"]["order_id"]
        assert staging_call_count(order_id) == 0, "两次调用都不该执行副作用"

    def test_command_resume_executes_approved_action_exactly_once(
        self, pg_url, approvals, container
    ):
        """真实恢复路径：Command(resume=decision) -> 副作用恰好发生一次。"""
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        seen: dict = {}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                parked = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
            payload = parked[INTERRUPT_KEY][0].value
            seen["order_id"] = payload["proposal"]["order_id"]
            seen["approval_id"] = payload["approval_id"]

            approvals.decide(payload["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
            decision = approvals.consume_resume(run_id)
            assert decision is not None
            with _run_ctx(run_id, thread_id):
                return await graph.ainvoke(Command(resume=decision), cfg)

        resumed = _scenario(pg_url, approvals, container, None, _drive)

        assert not resumed.get(INTERRUPT_KEY), "恢复后不应再挂起"
        results = resumed.get("approval_results") or []
        assert results and results[0]["status"] == "executed"
        assert results[0]["tool"] == "staging_refund"
        assert staging_call_count(seen["order_id"]) == 1, "已批准的副作用应恰好发生一次"
        assert resumed.get("pending_actions") == []

    def test_command_resume_rejection_executes_nothing(self, pg_url, approvals, container):
        """拒绝：恢复必须发生（图不能永远挂着），但副作用一次都不发生。"""
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        seen: dict = {}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                parked = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
            payload = parked[INTERRUPT_KEY][0].value
            seen["order_id"] = payload["proposal"]["order_id"]
            approvals.decide(payload["approval_id"], reviewer_id="sup-1", decision=DECISION_REJECT)
            assert approvals.require(payload["approval_id"])["status"] == STATUS_REJECTED
            decision = approvals.consume_resume(run_id)
            assert decision is not None
            with _run_ctx(run_id, thread_id):
                return await graph.ainvoke(Command(resume=decision), cfg)

        resumed = _scenario(pg_url, approvals, container, None, _drive)

        results = resumed.get("approval_results") or []
        assert results and results[0]["status"] == "rejected"
        assert staging_call_count(seen["order_id"]) == 0, "被拒绝的副作用绝不能发生"

    def test_replayed_resume_does_not_duplicate_side_effect(self, pg_url, approvals, container):
        """同一个 resume 决策被重复投递，副作用仍只发生一次。

        防线是 approval_id 派生的 ``operation_key``（side-effect ledger），而不是
        「我们只会被调用一次」这种假设。
        """
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        seen: dict = {}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                parked = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
            payload = parked[INTERRUPT_KEY][0].value
            seen["order_id"] = payload["proposal"]["order_id"]
            approvals.decide(payload["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
            decision = approvals.consume_resume(run_id)
            with _run_ctx(run_id, thread_id):
                first = await graph.ainvoke(Command(resume=decision), cfg)
                # 模拟 worker 崩溃后同一决策被再次投递
                second = await graph.ainvoke(Command(resume=decision), cfg)
            return first, second

        first, second = _scenario(pg_url, approvals, container, None, _drive)

        assert first.get("approval_results")
        assert second is not None
        assert staging_call_count(seen["order_id"]) == 1, (
            f"重复恢复不得重复扣款，实际 {staging_call_count(seen['order_id'])} 次"
        )

    def test_completed_run_does_not_look_like_it_is_still_waiting(
        self, pg_url, approvals, container
    ):
        """回归：成功恢复并跑完之后，返回值**不得**再带 ``__interrupt__``。

        executor 用 ``_awaiting_approval(result)``（即 ``result["__interrupt__"]``）
        决定「标记 WAITING_APPROVAL」还是「标记成功」。若已完成的 run 仍带这个
        key，它会被重新挂成 WAITING_APPROVAL —— 审批已落地、副作用已执行，
        run 却永远不收敛。

        触发条件是把图状态声明成裸 ``dict``（``__interrupt__`` 变成 state channel
        并被 checkpoint 持久化后合并回来）。生产用 ``AgentState``（TypedDict）
        不会触发；本测试用同形状的 ``_GateState`` 把该前提钉死。
        """
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        seen: dict = {}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                parked = await graph.ainvoke(_state(thread_id, [_pending()]), cfg)
            payload = parked[INTERRUPT_KEY][0].value
            seen["order_id"] = payload["proposal"]["order_id"]
            approvals.decide(payload["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
            decision = approvals.consume_resume(run_id)
            with _run_ctx(run_id, thread_id):
                resumed = await graph.ainvoke(Command(resume=decision), cfg)
            state = await graph.aget_state(cfg)
            return resumed, state

        resumed, state = _scenario(pg_url, approvals, container, None, _drive)

        assert not resumed.get(INTERRUPT_KEY), (
            "已完成的 run 不应再带 __interrupt__，否则 executor 会把它误判为仍在等待审批并重新挂起"
        )
        assert state.next == (), "图必须已推进到终点"
        assert state.interrupts == ()
        assert staging_call_count(seen["order_id"]) == 1

    def test_gate_is_noop_without_pending_actions(self, pg_url, approvals, container):
        """无高风险动作时闸门是纯 no-op：普通问答不得有任何审批开销。"""
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}

        async def _drive(graph, rt):
            with _run_ctx(run_id, thread_id):
                return await graph.ainvoke(_state(thread_id, []), cfg)

        result = _scenario(pg_url, approvals, container, None, _drive)

        assert not result.get(INTERRUPT_KEY)
        assert approvals.list_by_run(run_id) == []

    def test_multiple_pending_actions_each_get_approval(self, pg_url, approvals, container):
        """多个高风险动作各自独立审批：一次挂一个，逐个决策。"""
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread_id}}
        first_order = f"O-a-{uuid.uuid4().hex[:6]}"
        second_order = f"O-b-{uuid.uuid4().hex[:6]}"

        async def _drive(graph, rt):
            approval_ids: list[str] = []
            results: list[dict] = []
            state = _state(
                thread_id,
                [
                    _pending("staging_refund", order_id=first_order),
                    _pending(
                        "staging_order_change",
                        order_id=second_order,
                        new_status="CANCELLED",
                    ),
                ],
            )
            with _run_ctx(run_id, thread_id):
                current = await graph.ainvoke(state, cfg)
            while current.get(INTERRUPT_KEY):
                payload = current[INTERRUPT_KEY][0].value
                approvals.decide(
                    payload["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE
                )
                approval_ids.append(payload["approval_id"])
                decision = approvals.consume_resume(run_id)
                assert decision is not None, "每个被 interrupt 的动作都必须能消费到决策"
                with _run_ctx(run_id, thread_id):
                    current = await graph.ainvoke(Command(resume=decision), cfg)
                results.extend(current.get("approval_results") or [])
            return approval_ids, results

        approval_ids, results = _scenario(pg_url, approvals, container, None, _drive)

        assert len(approval_ids) == 2, "两个高风险动作应各自产生一次审批"
        assert len(set(approval_ids)) == 2, "审批 ID 必须互不相同"
        assert len(approvals.list_by_run(run_id)) == 2
        assert {r["tool"] for r in results} == {"staging_refund", "staging_order_change"}
        assert staging_call_count(first_order) == 1
