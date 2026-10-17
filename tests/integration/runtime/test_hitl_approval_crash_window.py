"""Issue #14：审批 ``consume_resume`` 与 ``mark_resumed_running`` 之间的崩溃窗口。

缺陷
----
``runtime/executor.py`` 在把 run 从 ``WAITING_APPROVAL`` 迁到 ``RUNNING`` **之前**
就调用破坏性的 ``consume_resume``（写 ``resumed_at``）。若 worker 在这两步之间崩溃：

- 决策已被消费、载荷丢失；
- run 仍停在 ``WAITING_APPROVAL``；
- 下次派发 ``consume_resume`` 返回 None -> run 被永久卡住（无人自动回收）。

修复（本文件验证）
------------------
1. ``ApprovalService.peek_resume`` **只读**重建已被消费的终态决策载荷
   （审批记录是终态且被保留），但仅在该 run 没有未消费 ``PENDING`` 时；
2. ``_build_resume_command`` 在 ``consume_resume`` 返回 None 时回退到 ``peek_resume``；
3. reconciler（``reconcile_stuck_runs``）重新投递「有终态审批且无未消费 PENDING」
   的 ``WAITING_APPROVAL`` run。

重复恢复由 ``mark_resumed_running`` 的 CAS 与 side-effect ledger 兜底，不会重复执行
高风险写 —— 语义边界见 ``docs/limitations.md``。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

from core.hitl.approval_service import (
    DECISION_APPROVE,
    ApprovalService,
)

pytestmark = pytest.mark.skipif(
    not __import__("os").getenv("TEST_DISTRIBUTED_DB_URL", "").strip(),
    reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL",
)


@pytest.fixture
def session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def approvals(session_factory, monkeypatch):
    svc = ApprovalService(session_factory=session_factory)
    import core.hitl.approval_service as approval_mod

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


def _stuck_run(approvals, session_factory):
    """造一个「决策已做且已被消费、但 run 仍停在 WAITING_APPROVAL」的崩溃窗口。"""
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService
    from runtime.statuses import RunStatus

    service = RunService(repository=AgentRunRepository(session_factory=session_factory))
    thread_id = f"thread-14-{uuid.uuid4().hex[:8]}"
    run = service.create_run(query="退款", session_id=thread_id, status=RunStatus.WAITING_APPROVAL)
    run_id = run["id"]
    rec = approvals.create_or_get(
        run_id=run_id,
        thread_id=thread_id,
        action="staging_refund",
        risk_level="high",
        proposal={"order_id": "O-14", "amount": 100},
        user_id="customer-1",
    )
    approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
    # 模拟 worker 崩溃：决策被消费但 run 未迁移。
    consumed = approvals.consume_resume(run_id)
    assert consumed is not None
    assert approvals.require(rec["approval_id"])["resumed_at"] is not None
    return run_id, rec["approval_id"]


def test_peek_resume_recovers_consumed_decision(approvals, session_factory):
    """已消费的决策可被只读重建；再次 ``consume_resume`` 仍返回 None。"""
    svc = ApprovalService(session_factory=session_factory)
    run_id, approval_id = _stuck_run(approvals, session_factory)

    assert svc.consume_resume(run_id) is None, "决策已被消费"
    payload = svc.peek_resume(run_id)
    assert payload is not None, "崩溃窗口的决策必须可重建"
    assert payload["approval_id"] == approval_id
    assert payload["decision"] == DECISION_APPROVE


def test_peek_resume_does_not_resurrect_when_live_pending_exists(approvals, session_factory):
    """存在未消费 PENDING 时必须返回 None，避免用旧决策错误恢复。"""
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService
    from runtime.statuses import RunStatus

    service = RunService(repository=AgentRunRepository(session_factory=session_factory))
    thread_id = f"thread-14-{uuid.uuid4().hex[:8]}"
    run = service.create_run(query="退款", session_id=thread_id, status=RunStatus.WAITING_APPROVAL)
    run_id = run["id"]

    # 旧决策（已批准并消费）
    old = approvals.create_or_get(
        run_id=run_id,
        thread_id=thread_id,
        action="staging_refund",
        risk_level="high",
        proposal={"order_id": "O-old", "amount": 1},
        user_id="customer-1",
    )
    approvals.decide(old["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
    approvals.consume_resume(run_id)

    # 新的、仍在等待的审批
    approvals.create_or_get(
        run_id=run_id,
        thread_id=thread_id,
        action="staging_order_change",
        risk_level="high",
        proposal={"order_id": "O-new", "new_status": "shipped"},
        user_id="customer-1",
    )
    assert (
        approvals.peek_resume(run_id) is None
    ), "有未消费 PENDING 时不得用旧决策恢复，否则会跳过第二个审批"


def test_build_resume_command_recovers_crash_window(approvals, session_factory):
    """修复前：崩溃窗口后 ``_build_resume_command`` 返回 None -> run 卡死。"""
    from langgraph.types import Command

    from runtime.executor import _build_resume_command

    run_id, _approval_id = _stuck_run(approvals, session_factory)

    cmd = _build_resume_command(run_id)
    assert isinstance(cmd, Command), "崩溃窗口必须能重建 resume 载荷"
    assert cmd.resume["decision"] == DECISION_APPROVE


def test_reconciler_redispatches_decided_waiting_approval_run(
    run_service, approvals, session_factory, cleanup_created_runs
):
    """reconciler 必须把「决策已落定但未恢复」的 WAITING_APPROVAL run 重新投递。"""
    from runtime.retry import reconcile_stuck_runs

    run_id, _ = _stuck_run(approvals, session_factory)

    dispatched: list[str] = []

    async def _fake_dispatch(rid: str) -> None:
        dispatched.append(rid)

    asyncio.run(
        reconcile_stuck_runs(
            run_service,
            dispatcher=_fake_dispatch,
            reclaim_stale_running=False,
            reclaim_stale_pending=False,
        )
    )
    assert (
        run_id in dispatched
    ), "已决策却滞留在 WAITING_APPROVAL 的 run 必须被 reconciler 捞出（Issue #14）"


def test_reconciler_ignores_genuinely_pending_approval(
    run_service, approvals, session_factory, cleanup_created_runs
):
    """仍在等待人工决策（PENDING 未过期）的 run 不得被自动重投。"""
    from runtime.retry import reconcile_stuck_runs
    from runtime.statuses import RunStatus

    thread_id = f"thread-14-{uuid.uuid4().hex[:8]}"
    run = run_service.create_run(
        query="退款", session_id=thread_id, status=RunStatus.WAITING_APPROVAL
    )
    run_id = run["id"]
    approvals.create_or_get(
        run_id=run_id,
        thread_id=thread_id,
        action="staging_refund",
        risk_level="high",
        proposal={"order_id": "O-pending", "amount": 5},
        user_id="customer-1",
    )

    dispatched: list[str] = []

    async def _fake_dispatch(rid: str) -> None:
        dispatched.append(rid)

    asyncio.run(
        reconcile_stuck_runs(
            run_service,
            dispatcher=_fake_dispatch,
            reclaim_stale_running=False,
            reclaim_stale_pending=False,
        )
    )
    assert run_id not in dispatched, "仍在等待人审批的 run 不得被自动重投"
