"""Issue #123: realtime write tools fail closed without a durable Run context.

The realtime fast path (``POST /api/chat`` / ``/api/chat/stream`` → REST / SSE /
WebSocket / multimodal) has no ``run_id`` context, so it has neither human
approval nor the ``runtime.side_effects`` idempotency ledger. A declared
side-effect tool must therefore be refused at the shared execution boundary
(``ToolRegistry.execute_raw``) **before** its handler runs.

This file proves the governance properties, not a live-production exploit:

1. without a run context a HIGH/declared side-effect tool is refused and its
   handler is invoked **zero** times;
2. read-only tools keep working on the fast path;
3. the refusal is enforced in the registry itself (a direct
   ``ToolRegistry.execute_raw`` call cannot bypass it) and independent of the
   ``HITL_ENABLED`` switch;
4. the async Run path is unchanged: with a run context the tool executes once
   and the side-effect ledger deduplicates retries;
5. the agent tool loop (the path every chat transport funnels through) receives
   the explicit refusal instead of executing the write.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.hitl.gate import is_ungoverned_side_effect
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory
from tools.hitl_staging_tools import (
    register_hitl_staging_tools,
    reset_staging_ledger,
    staging_call_count,
)
from tools.tool_registry import ToolCachePolicy, ToolRegistry


@pytest.fixture(autouse=True)
def _isolated_ledger():
    """Clean side-effect ledger + staging ledger per test."""
    from runtime.side_effects import (
        SideEffectStore,
        reset_side_effect_store_for_tests,
    )

    reset_staging_ledger()
    reset_side_effect_store_for_tests()
    factory, engine, path = make_sqlite_session_factory()
    store = SideEffectStore(session_factory=factory)
    import runtime.side_effects as se

    original = se._default_store
    se._default_store = store
    try:
        yield store
    finally:
        se._default_store = original
        reset_side_effect_store_for_tests()
        reset_staging_ledger()
        dispose(engine, path)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_hitl_staging_tools(reg)
    return reg


@contextlib.contextmanager
def _run_context(run_id: str = "run-1", thread_id: str = "thread-1"):
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context(run_id, thread_id, "task-1")
    try:
        yield
    finally:
        reset_run_context(tokens)


class TestFastPathWriteBoundary:
    def test_high_side_effect_refused_without_run_context(self, registry):
        result = asyncio.run(
            registry.execute_raw("staging_refund", {"order_id": "O-1", "amount": 100})
        )
        assert "拒绝执行" in result
        assert staging_call_count("O-1") == 0, "被拒绝的写操作 handler 必须零调用"

    def test_execute_wrapper_also_refuses(self, registry):
        result = asyncio.run(
            registry.execute("staging_order_change", {"order_id": "O-2", "new_status": "CANCELLED"})
        )
        assert "拒绝执行" in result
        assert staging_call_count("O-2:CANCELLED") == 0

    def test_refusal_is_independent_of_hitl_switch(self, registry, monkeypatch):
        # Even with HITL disabled, an ungoverned fast-path write cannot run:
        # there is still no ledger, so the write would be non-idempotent.
        monkeypatch.setattr("core.config.HITL_ENABLED", False)
        result = asyncio.run(
            registry.execute_raw("staging_refund", {"order_id": "O-3", "amount": 10})
        )
        assert "拒绝执行" in result
        assert staging_call_count("O-3") == 0

    def test_readonly_tool_still_runs_without_run_context(self, registry):
        result = asyncio.run(registry.execute_raw("staging_readonly_lookup", {"order_id": "O-9"}))
        assert "staging-order-info" in result
        assert "拒绝执行" not in result

    def test_predicate_does_not_block_readonly_or_unknown(self, registry):
        assert is_ungoverned_side_effect("staging_readonly_lookup", {}, registry) is False
        assert is_ungoverned_side_effect("no_such_tool", {}, registry) is False

    def test_run_context_preserves_async_write_path(self, registry, _isolated_ledger):
        # Async Run path: ledger governs; HITL is off by default -> execute once
        # and dedupe the retry by operation key.
        with _run_context():
            first = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "O-4", "amount": 100},
                    tool_call_id="tc-1",
                )
            )
            second = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "O-4", "amount": 100},
                    tool_call_id="tc-1",
                )
            )
        assert "拒绝执行" not in str(first)
        assert "拒绝执行" not in str(second)
        assert staging_call_count("O-4") == 1, "同一 operation key 只能真正执行一次"
        row = _isolated_ledger.get("staging_refund", "run-1:tc-1")
        assert row is not None and row["status"] == "SUCCEEDED"


class TestAgentToolLoopBoundary:
    def test_agent_loop_refuses_write_without_run_context(self, registry):
        """The loop every chat transport funnels through never invokes the handler."""
        from agents.base_agent import BaseAgent

        class DummyAgent(BaseAgent):
            async def process(self, state):
                return state

        agent = DummyAgent(name="t", role="t", expertise=["t"])
        agent.logger = MagicMock()

        first = MagicMock()
        first.tool_calls = [
            {
                "id": "call_1",
                "name": "staging_refund",
                "arguments": '{"order_id": "O-7", "amount": 100}',
            }
        ]
        first.content = ""
        second = MagicMock()
        second.tool_calls = None
        second.content = "该操作需要走异步审批。"

        responses = [first, second]
        seen: dict[str, list] = {}

        async def fake_invoke(messages, tools=None):
            seen["messages"] = list(messages)
            return responses.pop(0)

        agent.llm = MagicMock()
        agent.llm.async_invoke = fake_invoke
        agent.set_tool_registry(registry)
        agent._resolve_prompt_for_variant = MagicMock(return_value=("p", "control", None))
        agent._prepare_llm_messages = AsyncMock(return_value=("sid", [], False))
        agent._add_message_to_session = AsyncMock()
        agent._publish_event = AsyncMock()

        state = {"customer_query": "帮我退款", "session_id": "s1"}
        asyncio.run(agent._process_with_tools(state, "prompt"))

        assert staging_call_count("O-7") == 0
        joined = "\n".join(str(getattr(m, "content", "")) for m in seen["messages"])
        assert "拒绝执行" in joined


class TestDeclaredSideEffectBoundaryContract:
    def test_medium_side_effect_is_also_refused_without_run_context(self):
        """Strict fail-closed: any declared side effect, not only HIGH, is refused.

        Guards against a future MEDIUM write (e.g. create-ticket) silently
        running on the fast path with no idempotency protection.
        """
        reg = ToolRegistry()
        calls = {"n": 0}

        async def _handler(args):
            calls["n"] += 1
            return "done"

        reg.register(
            name="medium_write",
            description="medium write",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            cache_policy=ToolCachePolicy(enabled=False),
            side_effect=True,
            risk_level="medium",
        )
        result = asyncio.run(reg.execute_raw("medium_write", {}))
        assert "拒绝执行" in result
        assert calls["n"] == 0
