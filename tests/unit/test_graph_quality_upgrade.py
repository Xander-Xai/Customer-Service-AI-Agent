"""Pre-finalization quality upgrades are bounded, stateful and side-effect safe."""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.test_hitl_real_graph_gate import _drain_graph, _prepare

pytestmark = pytest.mark.unit


class _Mode:
    def __init__(self, name, response, tool_executions=None):
        self.name = name
        self.response = response
        self.tool_executions = tool_executions or []
        self.calls = 0

    async def execute(self, agents, state, context):
        self.calls += 1
        return {
            "response": self.response,
            "mode": self.name,
            "agents_used": ["general_agent"],
            "tool_executions": self.tool_executions,
            "pending_actions": [],
        }


class _Evaluator:
    def evaluate(self, response, context):
        return {
            "score": 5.0 if "初次回答" in response else 95.0,
            "factors": {},
            "suggestions": [],
        }


class _Cache:
    def __init__(self):
        self.writes = []

    def get(self, query, metadata=None):
        return None

    def put(self, query, response, metadata=None):
        self.writes.append(response)


async def _make_container(first, upgraded):
    from core.container import ServiceContainer

    container = ServiceContainer()
    await container.initialize()
    _prepare(container, "staging_readonly_lookup", {"order_id": "TEST"})
    container.orchestrator.select_mode_name = lambda *args: "sequential"
    container.orchestrator.build_context = lambda *args: (
        "sequential",
        {"primary_agent": "general_agent"},
    )
    container.orchestrator.upgrade_mode = lambda *args: (
        "consultation",
        {"primary_agent": "general_agent", "consult_agents": []},
    )
    container.orchestrator._modes["sequential"] = first
    container.orchestrator._modes["consultation"] = upgraded
    container.response_agent.evaluator = _Evaluator()
    cache = _Cache()
    container.cache = cache
    container.response_agent.cache = cache
    return container, cache


async def test_low_quality_retries_before_single_finalization(monkeypatch):
    monkeypatch.setattr("core.config.EVAL_RETRY_ENABLED", True)
    monkeypatch.setattr("core.config.MODE_UPGRADE_ENABLED", True)
    monkeypatch.setattr("core.config.EVAL_RETRY_THRESHOLD", 30)
    first = _Mode("sequential", "这是初次回答，针对客户问题仍然缺乏必要细节说明。")
    upgraded = _Mode("consultation", "这是升级后的最终客服回答，包含完整明确的处理说明。")
    container, cache = await _make_container(first, upgraded)
    try:
        observed = await _drain_graph(
            container, thread_id=f"quality-gate-{uuid4().hex}"
        )
        assert first.calls == 1
        assert upgraded.calls == 1
        assert observed["collaboration_mode"] == "consultation"
        assert observed["response"] == upgraded.response
        assert observed["_upgrade_attempts"] == 1
        assert observed["_needs_upgrade"] is False
        assert cache.writes == [upgraded.response], "Only the final candidate is cacheable"
    finally:
        await container.close()


async def test_tool_activity_suppresses_quality_retry(monkeypatch):
    monkeypatch.setattr("core.config.EVAL_RETRY_ENABLED", True)
    monkeypatch.setattr("core.config.MODE_UPGRADE_ENABLED", True)
    monkeypatch.setattr("core.config.EVAL_RETRY_THRESHOLD", 30)
    first = _Mode(
        "sequential",
        "这是初次回答，虽然低分但包含已经完成的工具执行记录。",
        tool_executions=[{"tool": "query_order", "ok": True}],
    )
    upgraded = _Mode("consultation", "不应该调用的升级协作响应。")
    container, cache = await _make_container(first, upgraded)
    try:
        observed = await _drain_graph(
            container, thread_id=f"quality-safe-{uuid4().hex}"
        )
        assert first.calls == 1
        assert upgraded.calls == 0
        assert observed["_upgrade_attempts"] == 0
        assert observed["collaboration_mode"] == "sequential"
    finally:
        await container.close()
