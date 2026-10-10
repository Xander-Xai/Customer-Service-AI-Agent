"""审批恢复的故障注入：重复投递 + Worker 崩溃下的**恰好一次**副作用。

为什么需要本文件
----------------
``test_hitl_langgraph_interrupt.py`` 验证的是 LangGraph ``interrupt`` /
``Command(resume)`` 语义（真图 + 真 checkpoint）。``test_hitl_approval_flow.py``
验证审批服务的并发/持久化性质。但**两者都没有走 ``runtime/executor.py`` 的真实
恢复路径**：审批恢复的副作用恰好一次，靠的是 executor 的
``consume_resume``（``resumed_at IS NULL`` 原子认领）+ ``runtime.side_effects``
ledger + 审批 ID 派生的 ``operation_key`` 三者叠加。任何一环断链，都会在
"worker 崩溃 / at-least-once 重投递"下变成**重复扣款**。

本文件在真实 PostgreSQL 上驱动 executor，覆盖三种故障场景：

1. **重复投递（已终态）**：resume 成功后同一投递再来一次 → no-op，副作用仍 1 次；
2. **崩溃于副作用之后、提交之前**：resume 执行了副作用但抛错 → run 进入
   RETRYING → 重投递时**不得重复执行**（ledger 去重）；
3. **崩溃于认领决策之后、执行之前**：决策已被消费但尚未执行 → 重投递
   **不会**静默重复消费，run 停在 WAITING_APPROVAL（liveness 边界，见报告）。

``TEST_DISTRIBUTED_DB_URL`` 未设置时整体 skip（与同目录约定一致）。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from types import SimpleNamespace

import pytest

from core.hitl.approval_service import (
    DECISION_APPROVE,
    DECISION_REJECT,
    STATUS_APPROVED,
    ApprovalService,
)
from core.hitl.gate import execute_approved_actions
from runtime.side_effects import SideEffectStore
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
def ledger(session_factory, monkeypatch):
    """真实 PG 上的 side-effect ledger，并接管进程默认 store。"""
    import runtime.side_effects as se

    store = SideEffectStore(session_factory=session_factory)
    monkeypatch.setattr(se, "_default_store", store, raising=False)
    yield store
    from sqlalchemy import delete

    from db.models import ToolSideEffect

    with contextlib.suppress(Exception):
        session = session_factory()
        try:
            session.execute(delete(ToolSideEffect))
            session.commit()
        finally:
            session.close()


@pytest.fixture
def registry_holder():
    reset_staging_ledger()
    registry = ToolRegistry()
    register_hitl_staging_tools(registry)
    holder = SimpleNamespace(tool_registry=registry)
    yield holder
    reset_staging_ledger()


def _async_provider(obj):
    async def _provider():
        return obj

    return _provider


def _service(session_factory):
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService

    return RunService(repository=AgentRunRepository(session_factory=session_factory))


def _park(service, thread_id: str) -> str:
    """创建 run 并让它停在 WAITING_APPROVAL（runtime 返回 ``__interrupt__``）。"""
    from runtime.executor import execute_run
    from runtime.statuses import RunStatus

    run = service.create_run(query="退款", session_id=thread_id, status=RunStatus.QUEUED)
    run_id = run["id"]

    class _Parking:
        async def run(self, *, thread_id, query, user_id=None, resume_command=None):
            return {"__interrupt__": [{"value": {"action": "staging_refund"}}]}

    status = asyncio.run(
        execute_run(run_id, service=service, runtime_provider=_async_provider(_Parking()))
    )
    assert status == RunStatus.WAITING_APPROVAL.value
    return run_id


class _ResumeRuntime:
    """模拟生产恢复路径：``Command(resume=decision)`` -> 执行已批准动作。

    生产里这一步由图节点 ``run_approval_gate`` 完成；这里直接调用同一个
    ``execute_approved_actions``（生产代码），确保验证的是真实执行边界与
    side-effect ledger，而不是自造的替身。
    """

    def __init__(self, container, arguments, *, crash_after_effect=False, resume_counter=None):
        self.container = container
        self.arguments = arguments
        self.crash_after_effect = crash_after_effect
        self.resume_counter = resume_counter if resume_counter is not None else {}

    async def run(self, *, thread_id, query, user_id=None, resume_command=None):
        self.resume_counter["resume"] = self.resume_counter.get("resume", 0) + 1
        if resume_command is None:
            # 崩溃重投递走的是 ainvoke(None)：图从 checkpoint 重放会被再次 interrupt。
            return {"__interrupt__": [{"value": {"action": "staging_refund"}}]}
        decision = resume_command.resume
        action = {
            "tool": decision["action"],
            "arguments": self.arguments,
            "_approval_id": decision["approval_id"],
            "_decision": decision,
        }
        results = await execute_approved_actions(self.container, [action])
        if self.crash_after_effect:
            # 副作用已发生，但 worker 在提交终态前崩溃（这里用可重试超时模拟）。
            raise asyncio.TimeoutError("simulated worker crash after side effect")
        return {"response": "退款已执行", "approval_results": results}


class TestDuplicateResumeDelivery:
    def test_terminal_replay_does_not_duplicate_side_effect(
        self, session_factory, approvals, ledger, registry_holder, thread_lock_off
    ):
        from runtime.executor import execute_run
        from runtime.statuses import RunStatus

        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)

        order_id = f"O-dup-{uuid.uuid4().hex[:8]}"
        arguments = {"order_id": order_id, "amount": 88}
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal=arguments,
            user_id="customer-1",
        )
        approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
        assert approvals.require(rec["approval_id"])["status"] == STATUS_APPROVED

        runtime = _ResumeRuntime(registry_holder, arguments)
        first = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(runtime))
        )
        assert first == RunStatus.SUCCEEDED.value
        assert staging_call_count(order_id) == 1

        # 重复投递（at-least-once）：run 已终态 -> no-op，副作用不得再发生。
        second = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(runtime))
        )
        assert second == RunStatus.SUCCEEDED.value
        assert staging_call_count(order_id) == 1, "重复投递不得重复执行已批准的副作用"

    def test_decision_is_consumed_exactly_once(self, session_factory, approvals, thread_lock_off):
        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal={"order_id": "O-once", "amount": 10},
            user_id="customer-1",
        )
        approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)

        first = approvals.consume_resume(run_id)
        second = approvals.consume_resume(run_id)
        assert first is not None
        assert second is None, "同一决策只能被消费一次（resumed_at IS NULL 原子认领）"


class TestCrashDuringResume:
    def test_crash_after_side_effect_does_not_duplicate_on_redelivery(
        self, session_factory, approvals, ledger, registry_holder, thread_lock_off
    ):
        """崩溃于副作用之后：重投递不得重复执行；run 状态按实际收敛。"""
        from runtime.executor import execute_run
        from runtime.statuses import RunStatus

        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)

        order_id = f"O-crash-{uuid.uuid4().hex[:8]}"
        arguments = {"order_id": order_id, "amount": 42}
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal=arguments,
            user_id="customer-1",
        )
        approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)

        dispatched: list[str] = []

        async def _recording_dispatcher(target_run_id, countdown=None):
            dispatched.append(target_run_id)

        crashing = _ResumeRuntime(registry_holder, arguments, crash_after_effect=True)
        status = asyncio.run(
            execute_run(
                run_id,
                service=service,
                runtime_provider=_async_provider(crashing),
                dispatcher=_recording_dispatcher,
            )
        )
        # 副作用已经发生一次；失败被记为可重试，而不是成功。
        assert staging_call_count(order_id) == 1
        assert status == RunStatus.RETRYING.value
        assert dispatched == [run_id], "可重试失败应重新投递一次"

        # 重投递：图从 checkpoint 重放，会被再次 interrupt；不得重复执行副作用。
        replay = _ResumeRuntime(registry_holder, arguments)
        status2 = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(replay))
        )
        assert staging_call_count(order_id) == 1, "崩溃重投递不得重复执行副作用"
        # 决策已被消费，运行无法再次消费它 -> 停在等待态。这是**已知 liveness
        # 边界**：不重复（安全），但需要运维介入才能收敛。
        assert status2 == RunStatus.WAITING_APPROVAL.value
        third = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(replay))
        )
        assert third == RunStatus.WAITING_APPROVAL.value
        assert staging_call_count(order_id) == 1

    def test_crash_before_execution_leaves_run_parked_without_side_effect(
        self, session_factory, approvals, ledger, registry_holder, thread_lock_off
    ):
        """崩溃于认领决策之后、执行之前：副作用不得发生；run 停在 WAITING_APPROVAL。

        这是**已知 liveness 边界**（不是重复副作用）：决策被 worker 原子认领后
        该 worker 崩溃，重投递不会重新消费该决策，因此需要运维介入
        （重新决策 / 或未来的 re-issue 机制）。本测试把该行为钉死，避免它被
        误读为「审批恢复已经万无一失」。
        """
        from runtime.executor import execute_run
        from runtime.statuses import RunStatus

        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)

        order_id = f"O-claim-{uuid.uuid4().hex[:8]}"
        arguments = {"order_id": order_id, "amount": 7}
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal=arguments,
            user_id="customer-1",
        )
        approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)

        # 模拟：worker 认领了决策（resumed_at 落下）后崩溃，未执行任何副作用。
        claimed = approvals.consume_resume(run_id)
        assert claimed is not None

        runtime = _ResumeRuntime(registry_holder, arguments)
        status = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(runtime))
        )
        assert status == RunStatus.WAITING_APPROVAL.value
        assert staging_call_count(order_id) == 0, "认领后崩溃不得产生副作用"


class TestRejectAndExpireDoNotExecute:
    def test_rejected_resume_executes_nothing(
        self, session_factory, approvals, ledger, registry_holder, thread_lock_off
    ):
        from runtime.executor import execute_run
        from runtime.statuses import RunStatus

        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)

        order_id = f"O-rej-{uuid.uuid4().hex[:8]}"
        arguments = {"order_id": order_id, "amount": 15}
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal=arguments,
            user_id="customer-1",
        )
        approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_REJECT)

        runtime = _ResumeRuntime(registry_holder, arguments)
        status = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(runtime))
        )
        assert status == RunStatus.SUCCEEDED.value
        assert staging_call_count(order_id) == 0, "被拒绝的审批绝不能执行副作用"

    def test_expired_approval_executes_nothing(
        self, session_factory, approvals, ledger, registry_holder, thread_lock_off
    ):
        from sqlalchemy import update

        from db.models import HumanApproval
        from runtime.executor import execute_run
        from runtime.statuses import RunStatus

        service = _service(session_factory)
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        run_id = _park(service, thread_id)

        order_id = f"O-exp-{uuid.uuid4().hex[:8]}"
        arguments = {"order_id": order_id, "amount": 21}
        rec = approvals.create_or_get(
            run_id=run_id,
            thread_id=thread_id,
            action="staging_refund",
            risk_level="high",
            proposal=arguments,
            user_id="customer-1",
            ttl_seconds=1.0,
        )
        # 把 expires_at 推到过去，模拟 TTL 到期（未决策）。
        session = session_factory()
        try:
            session.execute(
                update(HumanApproval)
                .where(HumanApproval.approval_id == rec["approval_id"])
                .values(expires_at=None)
            )
            # expires_at=None 表示「永不过期」，因此显式写一个过去时间：
            from datetime import datetime, timedelta, timezone

            session.execute(
                update(HumanApproval)
                .where(HumanApproval.approval_id == rec["approval_id"])
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))
            )
            session.commit()
        finally:
            session.close()

        runtime = _ResumeRuntime(registry_holder, arguments)
        status = asyncio.run(
            execute_run(run_id, service=service, runtime_provider=_async_provider(runtime))
        )
        assert status == RunStatus.SUCCEEDED.value
        assert staging_call_count(order_id) == 0, "过期审批按拒绝处理，不得执行副作用"
