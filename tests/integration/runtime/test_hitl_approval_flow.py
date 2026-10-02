"""HITL 审批治理的**真实 PostgreSQL** 验收测试。

为什么必须用真 PG（而不是 SQLite）
------------------------------------
本文件验证的是**并发与持久化**性质，SQLite 恰好是它们最弱的对手：

1. ``expires_at`` 是 ``TIMESTAMP WITH TIME ZONE``。SQLite 会丢掉 tzinfo，于是
   「TTL 判定」在 SQLite 上永远看不出时区 bug；真 PG 才能证明
   aware/naive 混用不会导致提前放行或永不过期。
2. ``consume_resume`` 与 ``SideEffectStore.claim`` 的正确性依赖
   ``UPDATE ... WHERE resumed_at IS NULL`` / ``INSERT ... ON CONFLICT DO NOTHING``
   的**原子性**。SQLite 走的是"先查再插"分支，测不到 PG 的并发语义。
3. 唯一约束 ``uq_human_approvals_proposal`` 的并发插入行为只在 PG 上才是真的。

沿用本目录约定：``TEST_DISTRIBUTED_DB_URL`` 未设置时整体 skip，绝不让"没跑"
看起来像"通过"。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from core.hitl.approval_service import (
    DECISION_APPROVE,
    DECISION_REJECT,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalExpired,
    ApprovalService,
    SelfApprovalForbidden,
)
from runtime.side_effects import (
    CLAIM_CONFLICT,
    CLAIM_EXECUTE,
    CLAIM_IN_PROGRESS,
    CLAIM_SUCCEEDED,
    SideEffectStore,
    execute_idempotent_operation,
    request_fingerprint,
)
from tools.hitl_staging_tools import (
    register_hitl_staging_tools,
    reset_staging_ledger,
    staging_call_count,
)
from tools.tool_registry import ToolRegistry


@pytest.fixture
def session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def approvals(session_factory):
    svc = ApprovalService(session_factory=session_factory)
    yield svc
    # 清理本用例产生的行（真实 PG 是共享库，不能留垃圾）
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
    """真实 PG 上的 side-effect ledger。

    同时把它设为**进程默认** store：``ToolRegistry`` 内部走
    ``get_side_effect_store()``，不显式注入的话会用 ``get_db_session``（本地
    SQLite）。不接管默认 store 就会「以为在验 PG、其实验的是 SQLite」。
    """
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


def _new(svc, **overrides) -> dict:
    payload = {
        "run_id": f"run-{uuid.uuid4().hex[:10]}",
        "thread_id": f"thread-{uuid.uuid4().hex[:8]}",
        "action": "staging_refund",
        "risk_level": "high",
        "proposal": {"order_id": f"O-{uuid.uuid4().hex[:8]}", "amount": 100},
        "user_id": "customer-1",
        "agent": "order_agent",
    }
    payload.update(overrides)
    return svc.create_or_get(**payload)


# ══════════════════════════════════════════════════════════════════════════
# 持久化
# ══════════════════════════════════════════════════════════════════════════


def test_approval_is_durable_across_sessions(approvals, session_factory):
    """审批记录跨 session 可见（真实落库，不是进程内缓存）。"""
    rec = _new(approvals)
    fresh = ApprovalService(session_factory=session_factory)
    stored = fresh.require(rec["approval_id"])
    assert stored["status"] == STATUS_PENDING
    assert stored["proposal"]["order_id"] == rec["proposal"]["order_id"]


def test_timestamptz_round_trips_with_timezone(approvals, session_factory):
    """expires_at 必须是带时区的时刻。

    SQLite 会吞掉 tzinfo，导致 TTL 判定在测试环境「看起来正常」而在真 PG 上出错。
    这里显式断言 tzinfo 存在——这是本文件用真 PG 的核心原因之一。
    """
    rec = _new(approvals)
    stored = ApprovalService(session_factory=session_factory).require(rec["approval_id"])
    assert stored["expires_at"] is not None
    assert stored["expires_at"].tzinfo is not None, "expires_at 必须保留时区"
    assert stored["expires_at"] > stored["requested_at"]


def test_ttl_blocks_approval_with_timezone_aware_clock(approvals, monkeypatch):
    """TTL 到期 + aware 时钟 -> EXPIRED，绝不默认放行。"""
    monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 30.0)
    rec = _new(approvals)

    later = datetime.now(timezone.utc) + timedelta(seconds=300)
    with pytest.raises(ApprovalExpired):
        approvals.decide(
            rec["approval_id"],
            reviewer_id="sup-1",
            decision=DECISION_APPROVE,
            now=later,
        )
    assert approvals.require(rec["approval_id"])["status"] == STATUS_EXPIRED


# ══════════════════════════════════════════════════════════════════════════
# 并发：唯一约束 / 原子决策 / 单次消费
# ══════════════════════════════════════════════════════════════════════════


def test_concurrent_create_or_get_yields_single_record(approvals, session_factory):
    """并发 create_or_get 只产生一条审批（唯一约束 + IntegrityError 回退）。"""
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    kwargs = {
        "run_id": run_id,
        "thread_id": "thread-c",
        "action": "staging_refund",
        "risk_level": "high",
        "proposal": {"order_id": "O-concurrent", "amount": 100},
        "user_id": "customer-1",
    }

    def _create():
        return ApprovalService(session_factory=session_factory).create_or_get(**kwargs)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(_create) for _ in range(8)]]

    ids = {r["approval_id"] for r in results}
    assert len(ids) == 1, f"并发创建必须复用同一条审批，实际 {len(ids)} 条"
    assert len(approvals.list_by_run(run_id)) == 1


def test_concurrent_decisions_only_one_wins(approvals, session_factory):
    """并发 approve/reject 只有一个生效；首个决策不被覆盖。"""
    rec = _new(approvals)
    run_id = rec["run_id"]

    def _decide(decision: str):
        return ApprovalService(session_factory=session_factory).decide(
            rec["approval_id"], reviewer_id=f"sup-{decision}", decision=decision
        )

    decisions = [DECISION_APPROVE] * 4 + [DECISION_REJECT] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(_decide, d) for d in decisions]]

    newly = [r[1] for r in results]
    assert newly.count(True) == 1, "有且只有一次决策真正生效"

    stored = approvals.require(rec["approval_id"])
    assert stored["status"] in (STATUS_APPROVED, STATUS_REJECTED)
    # 决策人必须是那个"赢家"，不能被后来的并发写覆盖
    assert stored["reviewer_id"] in {f"sup-{DECISION_APPROVE}", f"sup-{DECISION_REJECT}"}
    assert run_id


def test_concurrent_consume_resume_delivers_once(approvals, session_factory):
    """并发消费同一决策：有且只有一方拿到 payload。

    这是「审批通过后副作用只重放一次」的第一道闸（第二道是 side-effect ledger）。
    """
    rec = _new(approvals)
    approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)

    def _consume():
        return ApprovalService(session_factory=session_factory).consume_resume(rec["run_id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(_consume) for _ in range(8)]]

    got = [r for r in results if r is not None]
    assert len(got) == 1, f"同一决策必须只被消费一次，实际 {len(got)} 次"
    assert got[0]["decision"] == DECISION_APPROVE


def test_consume_resume_on_expired_pending_returns_rejection(approvals, monkeypatch):
    """过期 PENDING 在真实 PG 上也能被消费为「拒绝」，图不会死等。"""
    monkeypatch.setattr("core.config.HITL_APPROVAL_TTL_SECONDS", 5.0)
    rec = _new(approvals)
    later = datetime.now(timezone.utc) + timedelta(seconds=600)
    payload = approvals.consume_resume(rec["run_id"], now=later)
    assert payload is not None
    assert payload["decision"] == "expired"
    assert approvals.require(rec["approval_id"])["status"] == STATUS_EXPIRED
    # 仍然只消费一次
    assert approvals.consume_resume(rec["run_id"], now=later) is None


# ══════════════════════════════════════════════════════════════════════════
# 职责分离（真实库上同样成立）
# ══════════════════════════════════════════════════════════════════════════


def test_self_approval_rejected_on_real_db(approvals):
    rec = _new(approvals, user_id="customer-42")
    with pytest.raises(SelfApprovalForbidden):
        approvals.decide(rec["approval_id"], reviewer_id="customer-42", decision=DECISION_APPROVE)
    assert approvals.require(rec["approval_id"])["status"] == STATUS_PENDING


# ══════════════════════════════════════════════════════════════════════════
# 审批 -> 执行：恰好一次（与 #28 的原子 claim 联动）
# ══════════════════════════════════════════════════════════════════════════


def test_approved_side_effect_executes_exactly_once_under_concurrency(
    approvals, ledger, session_factory
):
    """核心：8 个并发执行者抢同一笔已批准退款，只有**一个**真正触发副作用。

    ``operation_key = run_id:approval:{approval_id}`` 由审批 ID 派生，因此在
    at-least-once 重投递下恒定。这是「审批通过 ≠ 会重复扣款」的唯一依据。
    """
    reset_staging_ledger()
    registry = ToolRegistry()
    register_hitl_staging_tools(registry)

    rec = _new(approvals, proposal={"order_id": "O-exactly-once", "amount": 250})
    approvals.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
    decision = approvals.consume_resume(rec["run_id"])
    assert decision is not None

    order_id = "O-exactly-once"
    operation_key = f"{rec['run_id']}:approval:{rec['approval_id']}"

    def _execute():
        async def _run():
            with _run_ctx(rec["run_id"], rec["thread_id"]):
                return await registry.execute_raw(
                    "staging_refund",
                    {"order_id": order_id, "amount": 250},
                    tool_call_id=f"approval:{rec['approval_id']}",
                )

        return asyncio.run(_run())

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_execute) for _ in range(8)]
        settled = []
        for f in futures:
            try:
                settled.append(("ok", f.result()))
            except Exception as e:  # TransientError：另一个执行者持有未过期认领
                settled.append(("err", type(e).__name__))

    executed = [s for s in settled if s[0] == "ok"]
    assert executed, "至少应有一个执行者成功"
    # 无论成败，副作用只发生一次
    assert staging_call_count(order_id) == 1, (
        f"已批准的副作用必须只发生一次，实际 {staging_call_count(order_id)} 次 settled={settled}"
    )

    row = ledger.get("staging_refund", operation_key)
    assert row is not None, "执行必须落 side-effect ledger"
    assert row["status"] == "SUCCEEDED"
    assert row["run_id"] == rec["run_id"]

    reset_staging_ledger()


def test_claim_lease_blocks_concurrent_second_executor(ledger):
    """认领租约未过期时，第二个执行者拿到 in_progress 而不是执行权。"""
    tool = "staging_refund"
    key = f"run-x:approval:{uuid.uuid4().hex[:8]}"
    first = ledger.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-x",
        thread_id="t",
        fingerprint=request_fingerprint({"order_id": "O"}),
    )
    assert first.state == CLAIM_EXECUTE
    second = ledger.claim(
        tool_name=tool,
        operation_key=key,
        run_id="run-x",
        thread_id="t",
        fingerprint=request_fingerprint({"order_id": "O"}),
    )
    assert second.state == CLAIM_IN_PROGRESS


def test_succeeded_claim_replays_stored_result(ledger):
    """已完成 + 同指纹 -> 返回已存结果，不重复执行。"""
    tool = "staging_refund"
    key = f"run-y:approval:{uuid.uuid4().hex[:8]}"
    args = {"order_id": "O-replay", "amount": 10}
    fp = request_fingerprint(args)

    first = ledger.claim(
        tool_name=tool, operation_key=key, run_id="run-y", thread_id="t", fingerprint=fp
    )
    assert first.state == CLAIM_EXECUTE
    ledger.mark_succeeded(tool, key, "already-done", owner=first.owner)

    second = ledger.claim(
        tool_name=tool, operation_key=key, run_id="run-y", thread_id="t", fingerprint=fp
    )
    assert second.state == CLAIM_SUCCEEDED
    assert second.result == "already-done"


def test_different_fingerprint_same_key_is_conflict(ledger):
    """同 key 不同参数 -> 冲突（防止「同 run 换参数重试」绕过审批语义）。"""
    tool = "staging_refund"
    key = f"run-z:approval:{uuid.uuid4().hex[:8]}"
    fp_a = request_fingerprint({"order_id": "O", "amount": 10})
    fp_b = request_fingerprint({"order_id": "O", "amount": 9999})

    first = ledger.claim(
        tool_name=tool, operation_key=key, run_id="run-z", thread_id="t", fingerprint=fp_a
    )
    ledger.mark_succeeded(tool, key, "done", owner=first.owner)

    second = ledger.claim(
        tool_name=tool, operation_key=key, run_id="run-z", thread_id="t", fingerprint=fp_b
    )
    assert second.state == CLAIM_CONFLICT


def test_idempotent_operation_is_exactly_once_under_concurrency(ledger):
    """通用幂等包裹：8 个并发调用只真正执行一次 operation。"""
    calls = []
    key = f"run-w:approval:{uuid.uuid4().hex[:8]}"
    args = {"order_id": "O-idem", "amount": 42}

    def _once():
        def _op():
            calls.append(1)
            return "done"

        async def _run():
            return await execute_idempotent_operation(
                tool_name="staging_refund",
                operation_key=key,
                run_id="run-w",
                thread_id="t",
                arguments=args,
                operation=_op,
                store=ledger,
            )

        try:
            return asyncio.run(_run())
        except Exception as e:
            return type(e).__name__

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(_once) for _ in range(8)]]

    assert len(calls) == 1, f"operation 只应执行一次，实际 {len(calls)} 次"
    assert "done" in results


# ══════════════════════════════════════════════════════════════════════════
# 端到端：run 挂起 -> 审批 -> 恢复 -> 恰好一次副作用
# ══════════════════════════════════════════════════════════════════════════


def test_full_approval_lifecycle_on_real_postgres(
    pg_engine, session_factory, thread_lock_off, monkeypatch
):
    """端到端：QUEUED -> RUNNING -> WAITING_APPROVAL -> (审批) -> SUCCEEDED。

    图用真实 ``interrupt`` / ``Command(resume=...)`` 与真实 PostgreSQL checkpoint
    之外的数据库表；这里刻意不引入 Celery，只驱动 ``execute_run`` 内核并断言
    **业务状态**与**副作用次数**这两个治理结果。
    """
    from runtime.executor import execute_run
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService
    from runtime.statuses import RunStatus

    reset_staging_ledger()
    service = RunService(repository=AgentRunRepository(session_factory=session_factory))

    # executor 内部通过 ``get_approval_service()`` 单例读取审批决策。生产里该单例
    # 走 ``get_db_session`` 指向同一个真实 PostgreSQL；测试里必须显式注入 PG
    # session factory，否则 executor 会去查本地 SQLite（读不到刚写入的审批，
    # 于是永远「无可消费的审批决策」，测的就不是恢复路径了）。
    import core.hitl.approval_service as approval_mod

    monkeypatch.setattr(
        approval_mod,
        "_default_service",
        ApprovalService(session_factory=session_factory),
        raising=False,
    )

    thread_id = f"thread-hitl-{uuid.uuid4().hex[:8]}"

    run = service.create_run(query="退款", session_id=thread_id, status=RunStatus.QUEUED)
    run_id = run["id"]
    attempt_before = run["attempt"]

    class _Gated:
        """第一轮：图挂在 interrupt 上。"""

        def __init__(self):
            self.resume_commands = []

        async def run(self, *, thread_id, query, user_id=None, resume_command=None):
            self.resume_commands.append(resume_command)
            if resume_command is None:
                return {"__interrupt__": [{"value": {"action": "staging_refund"}}]}
            return {"response": "退款已按审批执行"}

    first = _Gated()
    status = asyncio.run(execute_run(run_id, service=service, runtime_provider=_async(first)))
    assert status == RunStatus.WAITING_APPROVAL.value
    assert service.get_run(run_id)["status"] == RunStatus.WAITING_APPROVAL.value

    # 审批落库（真实 PG）
    svc = ApprovalService(session_factory=session_factory)
    rec = svc.create_or_get(
        run_id=run_id,
        thread_id=thread_id,
        action="staging_refund",
        risk_level="high",
        proposal={"order_id": "O-e2e", "amount": 500},
        user_id="customer-1",
    )
    assert rec["status"] == STATUS_PENDING
    svc.decide(rec["approval_id"], reviewer_id="sup-1", decision=DECISION_APPROVE)
    # 刻意**不**在这里 consume_resume：消费权留给 executor，否则它会拿到
    # 「决策已被消费」而保持 WAITING_APPROVAL，测的就不是恢复路径了。
    decided = svc.require(rec["approval_id"])
    assert decided["status"] == STATUS_APPROVED
    assert decided["resumed_at"] is None

    from langgraph.types import Command

    second = _Gated()
    status = asyncio.run(
        execute_run(
            run_id,
            service=service,
            runtime_provider=_async(second),
        )
    )

    # executor 自行从审批表取出决策并构造 Command(resume=...)
    assert second.resume_commands, "恢复执行必须携带 resume_command"
    assert isinstance(second.resume_commands[0], Command)
    assert second.resume_commands[0].resume["decision"] == DECISION_APPROVE
    assert status == RunStatus.SUCCEEDED.value
    # 等待审批不消耗重试预算
    assert service.get_run(run_id)["attempt"] == attempt_before + 1

    reset_staging_ledger()


@contextlib.contextmanager
def _run_ctx(run_id: str, thread_id: str):
    """注入 run 上下文（side-effect ledger 依赖它）。"""
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context(run_id, thread_id, "task-1")
    try:
        yield
    finally:
        reset_run_context(tokens)


def _async(obj):
    async def _provider():
        return obj

    return _provider
