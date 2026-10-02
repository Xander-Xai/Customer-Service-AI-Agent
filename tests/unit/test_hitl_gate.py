"""审批闸门测试：拦在执行之前 + 已批准副作用恰好执行一次。

本文件锁的是**治理属性**，不是 LangGraph API 的用法：

1. 闸门判定只在「分布式 run 上下文 + HITL 开启 + HIGH」时生效（三重收敛）；
2. 已批准的副作用**必须**经 ``runtime.side_effects`` ledger，键为
   ``run_id:approval:{approval_id}``——这是「审批后崩溃重投不重复退款」的唯一
   依据。历史上这里调 ``execute_raw`` 不传 ``tool_call_id``，会静默降级为
   非幂等直调，等于审批通过后仍可重复扣款；
3. reject / 缺失决策 / 过期 / 未声明 side_effect 都**不执行**。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.hitl.gate import (
    collect_pending_actions,
    execute_approved_actions,
    has_pending_interrupt,
    proposal_to_pending_action,
    should_propose_approval,
)
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory
from tools.hitl_staging_tools import (
    register_hitl_staging_tools,
    reset_staging_ledger,
    staging_call_count,
)
from tools.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolated_ledger():
    """每个用例都从干净的 side-effect ledger + staging 账本开始。"""
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


@pytest.fixture
def container(registry):
    class _Container:
        pass

    c = _Container()
    c.tool_registry = registry
    return c


@contextlib.contextmanager
def _run_context(run_id: str = "run-1", thread_id: str = "thread-1"):
    """注入 run 上下文（ledger 与闸门都依赖它）。"""
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context(run_id, thread_id, "task-1")
    try:
        yield
    finally:
        reset_run_context(tokens)


def _pending(**overrides) -> list[dict]:
    action = {
        "tool": "staging_refund",
        "arguments": {"order_id": "O-1", "amount": 100},
        "_approval_id": "ap-1",
        "_decision": {"decision": "approve"},
    }
    action.update(overrides)
    return [action]


class TestShouldProposeApproval:
    def test_disabled_by_switch(self, monkeypatch):
        monkeypatch.setattr("core.config.HITL_ENABLED", False)
        assert should_propose_approval("staging_refund", {"amount": 1}, None) is False

    def test_requires_run_context(self, monkeypatch):
        """无 run 上下文（/api/chat 快路径）不拦：拦了也无法挂起/恢复。"""
        monkeypatch.setattr("core.config.HITL_ENABLED", True)
        assert should_propose_approval("staging_refund", {"amount": 1}, None) is False

    def test_high_risk_in_run_context_is_gated(self, monkeypatch, registry):
        monkeypatch.setattr("core.config.HITL_ENABLED", True)
        with _run_context():
            assert should_propose_approval("staging_refund", {"amount": 1}, registry) is True

    def test_low_risk_is_not_gated(self, monkeypatch, registry):
        monkeypatch.setattr("core.config.HITL_ENABLED", True)
        with _run_context():
            assert (
                should_propose_approval("staging_readonly_lookup", {"order_id": "O"}, registry)
                is False
            )

    def test_amount_threshold_gates_unlisted_tool(self, monkeypatch, registry):
        """白名单之外的大额写操作也必须被拦。"""
        monkeypatch.setattr("core.config.HITL_ENABLED", True)
        monkeypatch.setattr("core.config.HITL_HIGH_AMOUNT_THRESHOLD", 1000.0)
        monkeypatch.setattr("core.config.HITL_HIGH_RISK_TOOLS", "")
        with _run_context():
            assert should_propose_approval("unlisted_tool", {"amount": 5000}, registry) is True
            assert should_propose_approval("unlisted_tool", {"amount": 1}, registry) is False


class TestPendingActions:
    def test_builder(self):
        action = proposal_to_pending_action("staging_refund", {"order_id": "O"}, agent="a")
        assert action["tool"] == "staging_refund"
        assert action["arguments"] == {"order_id": "O"}
        assert action["agent"] == "a"

    def test_collect_filters_malformed(self):
        state = {
            "pending_actions": [
                {"tool": "staging_refund", "arguments": {}},
                {"arguments": {}},  # 无 tool -> 丢弃
                "not-a-dict",
            ]
        }
        assert len(collect_pending_actions(state)) == 1

    def test_collect_empty(self):
        assert collect_pending_actions({}) == []


class TestInterruptDetection:
    def test_detects_interrupt(self):
        assert has_pending_interrupt({"__interrupt__": [object()]}) is True

    def test_no_interrupt(self):
        assert has_pending_interrupt({"response": "ok"}) is False
        assert has_pending_interrupt(None) is False
        assert has_pending_interrupt("string") is False


class TestExecuteApprovedActions:
    @pytest.mark.parametrize(
        ("decision", "order_id"),
        [
            ({"decision": "reject", "reason": "policy"}, "O-rej"),
            ({}, "O-missing"),
            ({"decision": "expired"}, "O-exp"),
            ({"decision": "unknown"}, "O-unk"),
        ],
    )
    def test_non_approve_never_executes(self, container, decision, order_id):
        results = asyncio.run(
            execute_approved_actions(
                container,
                _pending(arguments={"order_id": order_id, "amount": 100}, _decision=decision),
            )
        )
        assert results[0]["status"] == "rejected"
        assert staging_call_count(order_id) == 0

    def test_approve_executes_once(self, container):
        with _run_context():
            results = asyncio.run(execute_approved_actions(container, _pending()))
        assert results[0]["status"] == "executed"
        assert staging_call_count("O-1") == 1

    def test_approved_action_is_deduplicated_by_ledger(self, container, _isolated_ledger):
        """核心治理属性：同一审批重复执行（崩溃重投）只真正扣款一次。"""
        with _run_context():
            first = asyncio.run(execute_approved_actions(container, _pending()))
            # 模拟 worker 崩溃后 at-least-once 重投递：同一 approval_id 再来一次。
            second = asyncio.run(execute_approved_actions(container, _pending()))

        assert first[0]["status"] == "executed"
        assert staging_call_count("O-1") == 1, "副作用必须只发生一次"

        row = _isolated_ledger.get("staging_refund", "run-1:approval:ap-1")
        assert row is not None, "已批准的执行必须落 ledger"
        assert row["status"] == "SUCCEEDED"
        assert second[0]["status"] in ("executed", "error")

    def test_distinct_approvals_are_not_deduplicated(self, container):
        """两次独立审批 = 两次合法操作，不能被 ledger 误伤成一次。"""
        with _run_context():
            asyncio.run(execute_approved_actions(container, _pending(_approval_id="ap-a")))
            asyncio.run(execute_approved_actions(container, _pending(_approval_id="ap-b")))
        assert staging_call_count("O-1") == 2

    def test_edit_uses_edited_args(self, container):
        with _run_context():
            results = asyncio.run(
                execute_approved_actions(
                    container,
                    _pending(
                        _decision={
                            "decision": "edit",
                            "edited_args": {"order_id": "O-1", "amount": 50},
                        }
                    ),
                )
            )
        assert results[0]["status"] == "executed"
        assert "amount=50.0" in str(results[0]["result"])

    def test_tool_without_side_effect_is_refused(self, container):
        """被审批的工具若未声明 side_effect，就没有幂等保护 -> 显式拒绝执行。"""
        with _run_context():
            results = asyncio.run(
                execute_approved_actions(container, _pending(tool="staging_readonly_lookup"))
            )
        assert results[0]["status"] == "error"
        assert results[0]["error"] == "ToolNotDeclaredSideEffect"

    def test_missing_registry_is_an_error_not_a_pass(self):
        class _Bare:
            tool_registry = None

        results = asyncio.run(execute_approved_actions(_Bare(), _pending()))
        assert results[0]["status"] == "error"
        assert results[0]["error"] == "ToolRegistryUnavailable"

    def test_unknown_tool_does_not_crash(self, container):
        with _run_context():
            results = asyncio.run(execute_approved_actions(container, _pending(tool="no_such")))
        assert results[0]["status"] == "error"
        assert results[0]["error"] == "ToolNotDeclaredSideEffect"
