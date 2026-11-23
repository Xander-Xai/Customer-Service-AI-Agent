"""Real compiled graph handoff tests: create ticket and suspend bot while claimed."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.handoff_service import HandoffService
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory
from tests.unit.test_hitl_real_graph_gate import _drain_graph

pytestmark = pytest.mark.unit


class _Mode:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def execute(self, agents, state, context):
        self.calls += 1
        return {
            "response": self.response,
            "mode": "sequential",
            "agents_used": ["general_agent"],
            "tool_executions": [],
            "pending_actions": [],
        }


class _NullCache:
    def get(self, query, metadata=None):
        return None

    def put(self, query, response, metadata=None):
        return None


async def test_graph_creates_and_then_respects_human_takeover(monkeypatch):
    from core.container import ServiceContainer

    factory, engine, path = make_sqlite_session_factory()
    svc = HandoffService(session_factory=factory)
    monkeypatch.setattr("core.config.HUMAN_HANDOFF_ENABLED", True)
    monkeypatch.setattr("core.config.EVAL_RETRY_ENABLED", False)
    monkeypatch.setattr("core.handoff_service.get_handoff_service", lambda: svc)

    container = ServiceContainer()
    await container.initialize()
    mode = _Mode("我无法自行完成处理，请转人工客服。")
    container.orchestrator._modes["sequential"] = mode
    container.orchestrator.select_mode_name = lambda *args: "sequential"
    container.orchestrator.build_context = lambda *args: (
        "sequential",
        {"primary_agent": "general_agent"},
    )
    container.cache = _NullCache()
    container.response_agent.cache = _NullCache()

    thread = f"handoff-e2e-{uuid4().hex}"
    try:
        first = await _drain_graph(container, thread_id=thread)
        assert first.get("handoff_ticket_id")
        assert first["handoff_status"] == "OPEN"
        assert first["resolution_status"] == "escalated"
        assert mode.calls == 1

        ticket = svc.by_session(
            thread, actor_id="hitl-e2e-user", role="customer"
        )
        assert ticket["ticket_id"] == first["handoff_ticket_id"]
        assert len(svc.messages(ticket["ticket_id"], actor_id="hitl-e2e-user", role="customer")) == 1

        svc.claim(ticket["ticket_id"], actor_id="human-1", role="agent")
        second = await _drain_graph(container, thread_id=thread)
        assert second["handoff_ticket_id"] == first["handoff_ticket_id"]
        assert second["handoff_status"] == "CLAIMED"
        assert second["resolution_status"] == "escalated"
        assert mode.calls == 1, "Human takeover must prevent further autonomous runs"
    finally:
        await container.close()
        dispose(engine, path)


async def test_handoff_disabled_leaves_graph_without_ticket(monkeypatch):
    from core.container import ServiceContainer

    monkeypatch.setattr("core.config.HUMAN_HANDOFF_ENABLED", False)
    monkeypatch.setattr("core.config.EVAL_RETRY_ENABLED", False)
    container = ServiceContainer()
    await container.initialize()
    mode = _Mode("请转人工客服。")
    container.orchestrator._modes["sequential"] = mode
    container.orchestrator.select_mode_name = lambda *args: "sequential"
    container.orchestrator.build_context = lambda *args: (
        "sequential",
        {"primary_agent": "general_agent"},
    )
    container.cache = _NullCache()
    container.response_agent.cache = _NullCache()
    try:
        result = await _drain_graph(
            container, thread_id=f"handoff-disabled-{uuid4().hex}"
        )
        assert not result.get("handoff_ticket_id")
        assert mode.calls == 1
    finally:
        await container.close()


async def test_hitl_approval_failure_does_not_create_customer_handoff(monkeypatch):
    """An approval workflow failure is not authorization to transfer the customer."""
    from core.container import ServiceContainer

    factory, engine, path = make_sqlite_session_factory()
    svc = HandoffService(session_factory=factory)
    monkeypatch.setattr("core.config.HUMAN_HANDOFF_ENABLED", True)
    monkeypatch.setattr("core.config.EVAL_RETRY_ENABLED", False)
    monkeypatch.setattr("core.handoff_service.get_handoff_service", lambda: svc)

    async def denied_write_approval(state, config, container):
        return {
            "approval_results": [{"tool": "staging_refund", "status": "error"}],
            "pending_actions": [],
        }

    monkeypatch.setattr("core.hitl.gate.run_approval_gate", denied_write_approval)
    container = ServiceContainer()
    await container.initialize()
    mode = _Mode("请转人工客服处理您的退款申请。")
    container.orchestrator._modes["sequential"] = mode
    container.orchestrator.select_mode_name = lambda *args: "sequential"
    container.orchestrator.build_context = lambda *args: (
        "sequential",
        {"primary_agent": "general_agent"},
    )
    container.cache = _NullCache()
    container.response_agent.cache = _NullCache()
    thread = f"hitl-not-handoff-{uuid4().hex}"
    try:
        result = await _drain_graph(container, thread_id=thread)
        assert not result.get("handoff_ticket_id")
        assert svc.by_session(
            thread, actor_id="hitl-e2e-user", role="customer"
        ) is None
    finally:
        await container.close()
        dispose(engine, path)
