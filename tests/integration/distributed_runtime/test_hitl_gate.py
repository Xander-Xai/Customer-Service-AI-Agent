"""HITL 风险闸门集成测试：真实 LangGraph interrupt / Command(resume)。

覆盖：high_risk_interrupts / low_risk_does_not_interrupt / approval_resumes /
rejection_does_not_execute_tool / restart_then_resume /
duplicate_approval_is_idempotent。

使用 MemorySaver（进程内持久化）；跨进程 PostgreSQL 见 gated 测试。
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.hitl import approval_service as apmod
from core.hitl.approval_service import ApprovalService
from core.hitl.gate import run_approval_gate, should_propose_approval
from db.models import Base
from runtime.context import reset_run_context, set_run_context
from tools.tool_registry import ToolRegistry


class _State(TypedDict, total=False):
    session_id: str
    pending_actions: list
    approval_results: list


class _Container:
    def __init__(self, registry):
        self.tool_registry = registry


@pytest.fixture()
def approval_service():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = ApprovalService(session_factory)
    previous = apmod._default_service
    apmod._default_service = service
    yield service
    apmod._default_service = previous


def _registry(calls: dict, *, risk="high"):
    from runtime.side_effects import SideEffectStore

    # 每个测试独立 side-effect store，避免跨测试幂等命中
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    registry = ToolRegistry(side_effect_store=SideEffectStore(session_factory))

    async def refund(args):
        calls["refund"] = calls.get("refund", 0) + 1
        return {"refunded": args.get("order")}

    registry.register(
        name="refund", description="refund", parameters={}, handler=refund,
        side_effect=True, risk_level=risk,
    )
    return registry


def _build_graph(checkpointer, container, *, pending):
    async def propose(state):
        if pending:
            return {"pending_actions": list(pending)}
        return {"pending_actions": []}

    async def gate(state, config):
        patch = await run_approval_gate(state, config, container)
        if patch:
            state.update(patch)
        return state

    graph = StateGraph(_State)
    graph.add_node("propose", propose)
    graph.add_node("gate", gate)
    graph.set_entry_point("propose")
    graph.add_edge("propose", "gate")
    graph.set_finish_point("gate")
    return graph.compile(checkpointer=checkpointer)


_PENDING = [
    {"tool": "refund", "arguments": {"order": "O1"}, "risk_level": "high", "agent": "billing"}
]


@pytest.mark.integration
async def test_high_risk_interrupts(approval_service):
    calls: dict = {}
    container = _Container(_registry(calls))
    app = _build_graph(MemorySaver(), container, pending=_PENDING)
    config = {"configurable": {"thread_id": "th-high"}}
    tokens = set_run_context("run-high", "th-high")
    try:
        result = await app.ainvoke({"session_id": "th-high"}, config=config)
        assert result.get("__interrupt__")
        assert calls.get("refund", 0) == 0  # 未执行
        pending = approval_service.list_pending()
        assert len(pending) == 1 and pending[0]["action"] == "refund"
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
async def test_low_risk_does_not_interrupt(approval_service):
    calls: dict = {}
    registry = _registry(calls, risk="low")
    container = _Container(registry)
    # 无 pending_actions -> gate 直接通过
    app = _build_graph(MemorySaver(), container, pending=[])
    config = {"configurable": {"thread_id": "th-low"}}
    tokens = set_run_context("run-low", "th-low")
    try:
        result = await app.ainvoke({"session_id": "th-low"}, config=config)
        assert not result.get("__interrupt__")
        assert approval_service.list_pending() == []
        # 低风险工具不触发审批
        assert should_propose_approval("query_order", {}, registry) is False
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
async def test_approval_resumes(approval_service):
    calls: dict = {}
    container = _Container(_registry(calls))
    app = _build_graph(MemorySaver(), container, pending=_PENDING)
    config = {"configurable": {"thread_id": "th-resume"}}
    tokens = set_run_context("run-resume", "th-resume")
    try:
        await app.ainvoke({"session_id": "th-resume"}, config=config)
        pending = approval_service.list_pending()
        aid = pending[0]["approval_id"]
        approval_service.decide(aid, reviewer_id="sup-1", decision="approve")
        decision = approval_service.get_resume_decision("run-resume")
        result = await app.ainvoke(Command(resume=decision), config=config)
        assert calls["refund"] == 1
        assert result["approval_results"][0]["status"] == "executed"
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
async def test_rejection_does_not_execute_tool(approval_service):
    calls: dict = {}
    container = _Container(_registry(calls))
    app = _build_graph(MemorySaver(), container, pending=_PENDING)
    config = {"configurable": {"thread_id": "th-reject"}}
    tokens = set_run_context("run-reject", "th-reject")
    try:
        await app.ainvoke({"session_id": "th-reject"}, config=config)
        aid = approval_service.list_pending()[0]["approval_id"]
        approval_service.decide(aid, reviewer_id="sup-1", decision="reject", reason="policy")
        decision = approval_service.get_resume_decision("run-reject")
        result = await app.ainvoke(Command(resume=decision), config=config)
        assert calls.get("refund", 0) == 0  # 拒绝不执行
        assert result["approval_results"][0]["status"] == "rejected"
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
async def test_restart_then_resume(approval_service):
    """Agent 暂停 -> 重启（新 graph 实例，共享 checkpoint）-> 审批 -> resume。"""
    calls: dict = {}
    container = _Container(_registry(calls))
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "th-restart"}}
    tokens = set_run_context("run-restart", "th-restart")
    try:
        app1 = _build_graph(saver, container, pending=_PENDING)
        await app1.ainvoke({"session_id": "th-restart"}, config=config)
        aid = approval_service.list_pending()[0]["approval_id"]
        approval_service.decide(aid, reviewer_id="sup-1", decision="approve")

        # "重启"：新 graph 实例，共享同一 checkpointer（真实跨进程用 PostgreSQL）
        app2 = _build_graph(saver, container, pending=_PENDING)
        decision = approval_service.get_resume_decision("run-restart")
        result = await app2.ainvoke(Command(resume=decision), config=config)
        assert calls["refund"] == 1
        assert result["approval_results"][0]["status"] == "executed"
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
async def test_duplicate_approval_is_idempotent(approval_service):
    calls: dict = {}
    container = _Container(_registry(calls))
    app = _build_graph(MemorySaver(), container, pending=_PENDING)
    config = {"configurable": {"thread_id": "th-dup"}}
    tokens = set_run_context("run-dup", "th-dup")
    try:
        await app.ainvoke({"session_id": "th-dup"}, config=config)
        aid = approval_service.list_pending()[0]["approval_id"]
        _, newly1 = approval_service.decide(aid, reviewer_id="sup-1", decision="approve")
        _, newly2 = approval_service.decide(aid, reviewer_id="sup-2", decision="approve")
        assert newly1 is True and newly2 is False  # 重复审批不重复生效
        decision = approval_service.get_resume_decision("run-dup")
        await app.ainvoke(Command(resume=decision), config=config)
        assert calls["refund"] == 1  # 只执行一次
    finally:
        reset_run_context(tokens)


@pytest.mark.integration
def test_should_propose_requires_run_context(approval_service):
    calls: dict = {}
    registry = _registry(calls, risk="high")
    # 无 run 上下文（legacy /api/chat）-> 不进入 HITL 审批
    assert should_propose_approval("refund", {}, registry) is False
    tokens = set_run_context("run-ctx", "th-ctx")
    try:
        assert should_propose_approval("refund", {}, registry) is True
    finally:
        reset_run_context(tokens)
