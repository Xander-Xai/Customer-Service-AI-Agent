"""Executor 级 HITL 集成：run 因高风险暂停 -> WAITING_APPROVAL -> 审批 -> resume。"""

from __future__ import annotations

import pytest

from core.hitl import approval_service as apmod
from core.hitl.approval_service import ApprovalService
from runtime.executor import execute_run
from runtime.statuses import RunStatus
from runtime.thread_lock import NullThreadLock

from .harness import make_run_env, noop_dispatcher, queued


class _InterruptRuntime:
    """模拟 graph 因 interrupt 暂停；resume 后完成。"""

    async def run(self, *, thread_id, query, user_id=None):
        return {"__interrupt__": [{"value": {"type": "human_approval_required"}}]}

    async def resume(self, *, thread_id, decision):
        return {
            "response": "done",
            "approval_results": [{"tool": "refund", "status": "executed"}],
        }


def _provider(runtime):
    async def _p():
        return runtime

    return _p


@pytest.mark.integration
async def test_executor_waits_then_resumes(monkeypatch):
    service, session_factory = make_run_env()
    approval_service = ApprovalService(session_factory)
    previous = apmod._default_service
    apmod._default_service = approval_service
    try:
        rid = queued(service, thread="T-hitl")
        runtime = _InterruptRuntime()

        first = await execute_run(
            rid,
            service=service,
            runtime_provider=_provider(runtime),
            lock_manager=NullThreadLock(),
            dispatcher=noop_dispatcher,
        )
        assert first == RunStatus.WAITING_APPROVAL.value
        assert service.get_run(rid)["status"] == RunStatus.WAITING_APPROVAL.value

        record = approval_service.create_or_get(
            run_id=rid,
            thread_id="T-hitl",
            action="refund",
            risk_level="high",
            proposal={"order": "O1"},
            user_id="cust-1",
        )
        approval_service.decide(
            record["approval_id"], reviewer_id="sup-1", decision="approve"
        )
        service.mark_resumed(rid)

        second = await execute_run(
            rid,
            service=service,
            runtime_provider=_provider(runtime),
            lock_manager=NullThreadLock(),
            dispatcher=noop_dispatcher,
        )
        assert second == RunStatus.SUCCEEDED.value
        assert service.get_run(rid)["result"]["response"] == "done"
    finally:
        apmod._default_service = previous
