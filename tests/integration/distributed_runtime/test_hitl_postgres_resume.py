"""HITL + PostgreSQL Checkpointer：Agent 暂停 -> 进程重启 -> 审批 -> resume。

仅在提供 TEST_POSTGRES_CHECKPOINT_URL 时运行：
    TEST_POSTGRES_CHECKPOINT_URL=postgresql://... pytest \\
        tests/integration/distributed_runtime/test_hitl_postgres_resume.py -q

证明 checkpoint 落在 PostgreSQL，新进程（全新 saver + 连接池）能恢复并完成审批。
"""

from __future__ import annotations

import os
import uuid
from typing import TypedDict

import pytest
from langgraph.graph import StateGraph
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.hitl import approval_service as apmod
from core.hitl.approval_service import ApprovalService
from core.hitl.gate import run_approval_gate
from db.models import Base
from runtime.context import reset_run_context, set_run_context
from runtime.side_effects import SideEffectStore
from tools.tool_registry import ToolRegistry

CHECKPOINT_URL = os.getenv("TEST_POSTGRES_CHECKPOINT_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not CHECKPOINT_URL,
    reason="TEST_POSTGRES_CHECKPOINT_URL 未设置；需要真实 PostgreSQL 才能运行",
)


class _State(TypedDict, total=False):
    session_id: str
    pending_actions: list
    approval_results: list


class _Container:
    def __init__(self, registry):
        self.tool_registry = registry


def _make_container():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    registry = ToolRegistry(side_effect_store=SideEffectStore(sf))
    return registry, sf


def _build_graph(checkpointer, container, calls):
    async def propose(state):
        return {
            "pending_actions": [
                {
                    "tool": "refund",
                    "arguments": {"order": "O1"},
                    "risk_level": "high",
                    "agent": "billing",
                }
            ]
        }

    async def refund(args):
        calls["refund"] = calls.get("refund", 0) + 1
        return {"refunded": args.get("order")}

    container.tool_registry.register(
        name="refund", description="refund", parameters={}, handler=refund,
        side_effect=True, risk_level="high",
    )

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


@pytest.mark.integration
async def test_postgres_checkpoint_survives_restart_then_resume():
    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    registry, sf = _make_container()
    approval_service = ApprovalService(sf)
    previous = apmod._default_service
    apmod._default_service = approval_service

    container = _Container(registry)
    calls: dict = {}
    thread_id = f"hitl-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}
    tokens = set_run_context(f"run-{uuid.uuid4().hex}", thread_id)
    try:
        worker1 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
        try:
            app1 = _build_graph(worker1.checkpointer, container, calls)
            result = await app1.ainvoke({"session_id": thread_id}, config=config)
            assert result.get("__interrupt__")
            assert calls.get("refund", 0) == 0
        finally:
            await close_checkpoint_runtime(worker1)

        # "进程重启"：全新 saver + 连接池
        worker2 = await build_postgres_checkpointer(CHECKPOINT_URL, setup_timeout=10.0)
        try:
            pending = approval_service.list_pending()[0]
            aid = pending["approval_id"]
            run_id = pending["run_id"]
            approval_service.decide(aid, reviewer_id="sup-1", decision="approve")
            decision = approval_service.get_resume_decision(run_id)
            app2 = _build_graph(worker2.checkpointer, container, calls)
            resumed = await app2.ainvoke(Command(resume=decision), config=config)
            assert calls["refund"] == 1
            assert resumed["approval_results"][0]["status"] == "executed"
        finally:
            await close_checkpoint_runtime(worker2)
    finally:
        reset_run_context(tokens)
        apmod._default_service = previous
