"""RUNNING 重复领取的正确性契约（真实 PostgreSQL）。

被覆盖的缺陷
------------
``RunService.mark_running`` 在 ``cur == RUNNING`` 分支里**先判 attempt 预算、
后判 lease 是否过期**：

    if cur == RunStatus.RUNNING:
        if next_attempt > int(run["max_attempts"]):
            self._dead_letter_budget_exhausted(run, cur=cur, now=now, takeover=True)
            return None
        taken = self.repo.takeover_running(...)

而 ``_dead_letter_budget_exhausted`` 走 ``mark_dead_letter`` 且**不传**
``expected_worker_id``，于是 ``owner_gated = False``，最终执行的是一条只判
``status = RUNNING`` 的无条件 UPDATE（``repository.transition``）。

后果：``attempt`` 已经等于 ``max_attempts`` 是**最后一次正常执行**的常态
（不是异常状态）。此时任何一次重复投递——包括本仓库已实测存在的
``WorkerLostError`` 重投与 ``Reject(requeue=True)`` 立即重投——都会把一个
**持有有效 lease、正在正常跑图**的 run 打成 ``DEAD_LETTER``。worker A 随后
的 ``mark_succeeded`` 因状态已是 DEAD_LETTER 被拒，用户拿到 DLQ，而 A 的图已经
执行过（可能已产生工具副作用）。

所以本文件把「重复领取」的三条硬契约钉在真实引擎上：

  1. 有效 lease + attempt 达上限 -> **不得**改动状态（返回 None，不写库）；
  2. 过期 lease + attempt 达上限 -> 原子收敛到 DEAD_LETTER；
  3. 过期 lease + 预算未耗尽 -> 允许接管。

为什么必须在 PostgreSQL 上跑
----------------------------
修复依赖 ``UPDATE ... WHERE status='RUNNING' AND (lease predicate)`` 的条件
更新语义（``rowcount`` 判定 + 竞争者之间互斥）。SQLite 的写锁粒度与时间戳处理
与 PostgreSQL 不同，并发用例在 SQLite 上没有意义。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_duplicate_claim_lease_fencing_pg.py -q
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_running(
    repo: AgentRunRepository,
    *,
    attempt: int,
    max_attempts: int,
    lease_seconds: float | None,
    worker_id: str | None,
    thread_id: str,
) -> str:
    """直接落一行 RUNNING：绕开 mark_running，构造「已领取」的精确现场。"""
    now = _utcnow()
    run_id = str(uuid.uuid4())
    session = repo._session()  # noqa: SLF001 - 测试需要构造 service 层不便表达的状态
    try:
        from db.models import AgentRun

        session.add(
            AgentRun(
                id=run_id,
                thread_id=thread_id,
                session_id=thread_id,
                user_id=None,
                status=RunStatus.RUNNING.value,
                query="q",
                attempt=attempt,
                max_attempts=max_attempts,
                created_at=now,
                queued_at=now,
                started_at=now,
                updated_at=now,
                worker_id=worker_id,
                task_id=None,
                lease_expires_at=(
                    now + timedelta(seconds=lease_seconds) if lease_seconds is not None else None
                ),
                heartbeat_at=now,
            )
        )
        session.commit()
    finally:
        session.close()
    return run_id


# ---------------------------------------------------------------------------
# 1. 有效 lease + attempt 达上限：重复领取不得改动任何状态
# ---------------------------------------------------------------------------


@requires_infra
def test_duplicate_claim_keeps_live_lease_running_at_budget_limit(pg_engine, cleanup_created_runs):
    """Defect reproduction: duplicate claim kills a live execution at the budget limit.

    ``attempt == max_attempts`` 是最后一次正常执行的常态，不是异常状态。
    重复领取既不能接管（lease 有效）也不能落 DLQ（无任何所有权失效证据）。
    """
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"live-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=3,
        max_attempts=3,
        lease_seconds=180.0,  # 有效 lease：worker A 正在跑
        worker_id="worker-A",
        thread_id=thread,
    )

    # worker B 收到重复消息（B 是真实存在的路径，见 test_celery_delivery_semantics.py）
    claimed = svc.mark_running(run_id, worker_id="worker-B", task_id="task-B", lease_seconds=180.0)

    assert claimed is None, "有效 lease 被他人持有时不得授予所有权"
    after = svc.get_run(run_id)
    assert (
        after["status"] == RunStatus.RUNNING.value
    ), "有效 lease 的执行中 run 被误杀：重复领取不得把它推入终态"
    assert after["worker_id"] == "worker-A", "重复领取不得改写 owner"
    assert after["attempt"] == 3, "重复领取不得消耗或递增 attempt"
    assert svc.get_dead_letter(run_id) is None, "有效 lease 不得产生 DLQ 记录"

    # 原 owner 必须仍能正常提交成功（这是「没被误杀」的可观察后果）
    done = svc.mark_succeeded(run_id, {"ok": True}, expected_worker_id="worker-A")
    assert done["status"] == RunStatus.SUCCEEDED.value


@requires_infra
def test_duplicate_claim_at_budget_limit_does_not_disturb_lease_owner_completion(
    pg_engine, cleanup_created_runs
):
    """误杀的下游后果：worker A 跑完后的成功提交被拒，用户侧丢失已完成的工作。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"kill-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo, attempt=3, max_attempts=3, lease_seconds=180.0, worker_id="worker-A", thread_id=thread
    )

    svc.mark_running(run_id, worker_id="worker-B", task_id="task-B", lease_seconds=180.0)

    # worker A 正常跑完并提交 —— 缺陷存在时这里会抛 InvalidRunTransition
    done = svc.mark_succeeded(run_id, {"answer": "ok"}, expected_worker_id="worker-A")
    assert done["status"] == RunStatus.SUCCEEDED.value
    assert svc.get_dead_letter(run_id) is None


# ---------------------------------------------------------------------------
# 2. 过期 lease + attempt 达上限：安全收敛到 DEAD_LETTER
# ---------------------------------------------------------------------------


@requires_infra
def test_expired_lease_at_budget_limit_converges_to_dead_letter(pg_engine, cleanup_created_runs):
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"expired-dlq-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=3,
        max_attempts=3,
        lease_seconds=-1.0,  # 已过期：owner 已被证明失效
        worker_id="worker-A",
        thread_id=thread,
    )

    claimed = svc.mark_running(run_id, worker_id="worker-B", task_id="task-B", lease_seconds=180.0)

    assert claimed is None, "预算耗尽时不得再授予执行权"
    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.DEAD_LETTER.value
    assert after["lease_expires_at"] is None
    dlq = svc.get_dead_letter(run_id)
    assert dlq is not None and dlq["attempt_count"] == 3 and dlq["max_attempts"] == 3
    assert dlq["error_code"] == "max_attempts_exhausted_at_claim"


@requires_infra
def test_expired_lease_reconciler_path_unchanged(pg_engine, cleanup_created_runs):
    """``dead_letter_stale_running``（reconciler 路径）行为不回退。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"reconciler-dlq-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=2,
        max_attempts=2,
        lease_seconds=-5.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    updated = svc.dead_letter_stale_running(
        run_id,
        error_code="stale_lease_attempts_exhausted",
        error_message="stale + exhausted",
    )
    assert updated is not None
    assert updated["status"] == RunStatus.DEAD_LETTER.value
    assert svc.get_dead_letter(run_id) is not None


# ---------------------------------------------------------------------------
# 3. 过期 lease + 预算未耗尽：允许接管
# ---------------------------------------------------------------------------


@requires_infra
def test_expired_lease_under_budget_limit_allows_takeover(pg_engine, cleanup_created_runs):
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"takeover-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=1,
        max_attempts=3,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    taken = svc.mark_running(run_id, worker_id="worker-B", task_id="task-B", lease_seconds=180.0)

    assert taken is not None, "过期 lease + 预算未耗尽必须允许安全接管"
    assert taken["worker_id"] == "worker-B"
    assert taken["attempt"] == 2, "接管递增 attempt"
    assert taken["lease_expires_at"] > _utcnow()
    assert svc.get_dead_letter(run_id) is None


# ---------------------------------------------------------------------------
# 4. 两个 worker 并发领取同一个过期 RUNNING：恰好一个赢家，且不误杀
# ---------------------------------------------------------------------------


@requires_infra
def test_concurrent_claims_on_expired_lease_at_budget_limit_yield_single_dlq(
    pg_engine, cleanup_created_runs
):
    """并发竞争 + 预算耗尽：DLQ 只能被写一次，且不能出现双份/误杀。

    同时钉住一个并发下真实可达的次序：第一个竞争者收敛到 DEAD_LETTER 之后，第二
    个才读到终态。后者必须得到 ``None``（benign no-op），而不是
    ``InvalidRunTransition(DEAD_LETTER -> RUNNING)``——终态是合法且预期的收敛结果。
    这个次序由 CI 的机器负载暴露过（本地不一定复现），所以显式再断言一遍。
    """
    from sqlalchemy import text

    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"race-dlq-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=3,
        max_attempts=3,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    async def _claim(worker: str):
        return await asyncio.to_thread(
            svc.mark_running, run_id, worker_id=worker, task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(_claim("worker-B"), _claim("worker-C"))

    results = asyncio.run(_main())
    assert all(r is None for r in results), "两个竞争者都不得取得执行权"

    # 收敛之后才到达的重复投递：必须是 benign no-op，不是异常
    late = svc.mark_running(run_id, worker_id="worker-D", task_id=None, lease_seconds=180.0)
    assert late is None, "已收敛的 run 上重复投递必须返回 None 而不是抛异常"

    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.DEAD_LETTER.value
    with pg_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM agent_dead_letters WHERE run_id = :r"), {"r": run_id}
        ).scalar()
    assert rows == 1, "DLQ 记录必须恰好一条（agent_dead_letters.run_id 唯一）"


@requires_infra
def test_duplicate_claim_on_each_terminal_state_is_a_benign_noop(pg_engine, cleanup_created_runs):
    """终态上的重复投递一律返回 None，不抛 ``InvalidRunTransition``。

    ``DEAD_LETTER -> RUNNING`` 确实不在 ``ALLOWED_TRANSITIONS`` 里，但对**领取**语义
    来说这不是「非法迁移」，而是「这次投递不该执行」。报成状态机错误既不准确，也会
    打断任何不恰好捕获该异常的调用方。
    """
    from sqlalchemy import update

    from db.models import AgentRun

    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"terminal-dup-{uuid.uuid4().hex[:8]}"

    for terminal in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        run_id = _seed_running(
            repo,
            attempt=1,
            max_attempts=1,
            lease_seconds=-1.0,
            worker_id="A",
            thread_id=f"{thread}-{terminal.value}",
        )
        # 直接收敛到目标终态，绕开各自的业务前置条件
        session = repo._session()  # noqa: SLF001
        try:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(status=terminal.value, lease_expires_at=None)
            )
            session.commit()
        finally:
            session.close()

        claimed = svc.mark_running(run_id, worker_id="dup", task_id=None, lease_seconds=180.0)
        assert claimed is None, f"{terminal.value} 上的重复领取必须是 benign no-op"
        assert svc.get_run(run_id)["status"] == terminal.value


@requires_infra
def test_concurrent_claims_on_expired_lease_under_budget_limit_single_winner(
    pg_engine, cleanup_created_runs
):
    """并发接管（预算未耗尽）：恰好一个 worker 拿到执行权。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"race-take-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=1,
        max_attempts=5,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    async def _claim(worker: str):
        return await asyncio.to_thread(
            svc.mark_running, run_id, worker_id=worker, task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(_claim("worker-B"), _claim("worker-C"), _claim("worker-D"))

    results = asyncio.run(_main())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"必须恰好一个赢家，实际 {len(winners)}"
    after = svc.get_run(run_id)
    assert after["worker_id"] == winners[0]["worker_id"]
    assert after["attempt"] == 2, "并发接管不得把 attempt 推到 3"


@requires_infra
def test_concurrent_duplicate_claims_on_live_lease_never_kill(pg_engine, cleanup_created_runs):
    """并发重复领取 + 有效 lease：无论多少竞争者，执行中的 run 都必须存活。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"race-live-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=3,
        max_attempts=3,
        lease_seconds=180.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    async def _claim(worker: str):
        return await asyncio.to_thread(
            svc.mark_running, run_id, worker_id=worker, task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(*[_claim(f"dup-{i}") for i in range(6)])

    results = asyncio.run(_main())
    assert all(r is None for r in results)
    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.RUNNING.value
    assert after["worker_id"] == "worker-A"
    assert after["attempt"] == 3
    assert svc.get_dead_letter(run_id) is None


# ---------------------------------------------------------------------------
# 5. 扫描后、落 DLQ 前 lease 被续约或被新 worker 接管
# ---------------------------------------------------------------------------


@requires_infra
def test_lease_renewed_after_scan_blocks_dead_letter(pg_engine, cleanup_created_runs):
    """TOCTOU 窗口：扫描判定「过期」之后、写入 DLQ 之前，原 owner 续租成功。

    这条落在 ``dead_letter_stale_running`` 与 ``mark_running`` 两条路径上，
    判定必须在同一条 UPDATE 内。
    """
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"renew-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=3,
        max_attempts=3,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    # reconciler 先扫描（此时确实过期）。扫描是全库的，不能假设只有本 run。
    stale_ids = [r["id"] for r in svc.list_stale_running_runs(limit=500)]
    assert run_id in stale_ids

    # 扫描之后 worker A 的心跳补上了（模拟 GC 停顿恢复）
    from sqlalchemy import update

    from db.models import AgentRun

    session = repo._session()  # noqa: SLF001
    try:
        session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(lease_expires_at=_utcnow() + timedelta(seconds=180.0))
        )
        session.commit()
    finally:
        session.close()

    updated = svc.dead_letter_stale_running(
        run_id, error_code="stale_lease_attempts_exhausted", error_message="late DLQ attempt"
    )
    assert updated is None, "lease 已续约：写入必须被谓词挡下"
    assert svc.get_run(run_id)["status"] == RunStatus.RUNNING.value
    assert svc.get_dead_letter(run_id) is None


@requires_infra
def test_taken_over_between_scan_and_dlq_blocks_dead_letter(pg_engine, cleanup_created_runs):
    """扫描之后、落 DLQ 之前另一个 worker 已接管：新 owner 的执行不被误杀。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"taken-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=1,
        max_attempts=2,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    stale_ids = [r["id"] for r in svc.list_stale_running_runs(limit=500)]
    assert run_id in stale_ids

    taken = repo.takeover_running(run_id, worker_id="worker-B", task_id="t", lease_seconds=180.0)
    assert taken is not None, "前置条件：接管成功"

    updated = svc.dead_letter_stale_running(
        run_id, error_code="stale_lease_attempts_exhausted", error_message="late DLQ attempt"
    )
    assert updated is None, "已被接管的 run 不得再被落 DLQ"
    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.RUNNING.value
    assert after["worker_id"] == "worker-B"
    assert svc.get_dead_letter(run_id) is None


@requires_infra
def test_duplicate_claim_after_takeover_does_not_dead_letter_new_owner(
    pg_engine, cleanup_created_runs
):
    """新 owner 接管并跑满预算后，**更晚**到的重复消息仍不得杀掉它。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"post-take-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=1,
        max_attempts=2,
        lease_seconds=-1.0,
        worker_id="worker-A",
        thread_id=thread,
    )

    taken = svc.mark_running(run_id, worker_id="worker-B", task_id="t-B", lease_seconds=180.0)
    assert taken is not None and taken["attempt"] == 2 == taken["max_attempts"]

    # 迟到的重复消息（worker-C）
    claimed = svc.mark_running(run_id, worker_id="worker-C", task_id="t-C", lease_seconds=180.0)
    assert claimed is None
    after = svc.get_run(run_id)
    assert after["status"] == RunStatus.RUNNING.value, "新 owner 正在执行，不得被杀"
    assert after["worker_id"] == "worker-B"
    assert svc.get_dead_letter(run_id) is None
    assert svc.mark_succeeded(run_id, {"ok": True}, expected_worker_id="worker-B")


# ---------------------------------------------------------------------------
# 6. 状态迁移成功但 DLQ 记录写入失败：如何保证可恢复
# ---------------------------------------------------------------------------


@requires_infra
def test_dead_letter_transition_and_dlq_row_are_atomic(
    pg_engine, monkeypatch, cleanup_created_runs
):
    """状态迁移与 DLQ 行写入必须同生共死——否则出现静默消失的 run。

    分两次提交的历史形态会留下一个**不可恢复**的窗口：状态已是终态（不再被任何
    扫描器捞起）而 DLQ 行缺失，于是这个 run 既不会重投、也没有记录供人工
    ``replay_dead_run.py`` 重放。

    这里在写 DLQ 行时注入故障，断言状态迁移被**回滚**（run 仍是 RUNNING、仍可被
    恢复路径捞起），而不是留下终态孤行。
    """
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"dlq-atomic-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo, attempt=2, max_attempts=2, lease_seconds=-1.0, worker_id="A", thread_id=thread
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DLQ row write failure")

    monkeypatch.setattr(repo, "_attach_dead_letter", _boom)

    with pytest.raises(RuntimeError, match="simulated DLQ row write failure"):
        svc.dead_letter_stale_running(
            run_id, error_code="stale_lease_attempts_exhausted", error_message="first attempt"
        )

    # 原子性：状态迁移被回滚，run 没有变成无法恢复的终态
    assert svc.get_run(run_id)["status"] == RunStatus.RUNNING.value
    assert svc.get_dead_letter(run_id) is None

    # 故障消失后重试：状态与 DLQ 行一起落地
    monkeypatch.undo()
    again = svc.dead_letter_stale_running(
        run_id, error_code="stale_lease_attempts_exhausted", error_message="retry attempt"
    )
    assert again is not None and again["status"] == RunStatus.DEAD_LETTER.value
    dlq = svc.get_dead_letter(run_id)
    assert dlq is not None, "DLQ 行必须与状态迁移同事务落地"
    assert dlq["attempt_count"] == 2 and dlq["max_attempts"] == 2


@requires_infra
def test_dead_letter_is_idempotent_for_terminal_run(pg_engine, cleanup_created_runs):
    """DLQ 行写入幂等：重复补写不会产生第二行、也不改写 run 状态。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"dlq-idem-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo, attempt=2, max_attempts=2, lease_seconds=-1.0, worker_id="A", thread_id=thread
    )
    svc.dead_letter_stale_running(run_id, error_code="c1", error_message="m1", now=_utcnow())
    first = svc.get_dead_letter(run_id)

    svc.mark_dead_letter(run_id, error_code="c2", error_message="m2")
    second = svc.get_dead_letter(run_id)

    assert first == second, "既有 DLQ 行不可变（保留首次进入的原因与 attempt）"
    assert svc.get_run(run_id)["status"] == RunStatus.DEAD_LETTER.value


# ---------------------------------------------------------------------------
# 取消与领取/promote 的并发：不得产生「并发假失败」
# ---------------------------------------------------------------------------


@requires_infra
def test_cancel_racing_a_claim_never_raises(pg_engine, cleanup_created_runs):
    """取消对每个非终态都合法，所以必须用单条 ``NOT IN (终态)`` 判定。

    旧实现「先读状态、再用该状态做 CAS」：读到 QUEUED 之后、写入之前 worker 把它
    领取成 RUNNING，CAS 落空并抛 ``InvalidRunTransition``——纯并发假失败。
    """
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"cancel-race-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=0,
        max_attempts=3,
        lease_seconds=-1.0,
        worker_id=None,
        thread_id=thread,
    )

    async def _cancel():
        return await asyncio.to_thread(svc.cancel_run, run_id)

    async def _claim():
        return await asyncio.to_thread(
            svc.mark_running, run_id, worker_id="w-claim", task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(_cancel(), _claim(), return_exceptions=True)

    results = asyncio.run(_main())
    for r in results:
        assert not isinstance(r, Exception), f"并发下不得抛异常：{r!r}"

    final = svc.get_run(run_id)["status"]
    assert final in {RunStatus.CANCELLED.value, RunStatus.RUNNING.value}
    # 取消赢 -> 领取落败者拿不到执行权；领取赢 -> 后续取消把它收敛到 CANCELLED
    if final == RunStatus.RUNNING.value:
        svc.cancel_run(run_id)
        assert svc.get_run(run_id)["status"] == RunStatus.CANCELLED.value
    assert svc.get_dead_letter(run_id) is None, "取消不是失败，不应产生 DLQ 记录"


@requires_infra
def test_cancel_of_terminal_run_is_a_noop(pg_engine, cleanup_created_runs):
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"cancel-term-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo, attempt=1, max_attempts=1, lease_seconds=-1.0, worker_id="A", thread_id=thread
    )
    svc.mark_succeeded(run_id, {"ok": True})
    out = svc.cancel_run(run_id)
    assert out["status"] == RunStatus.SUCCEEDED.value
    assert svc.get_run(run_id)["status"] == RunStatus.SUCCEEDED.value


# ---------------------------------------------------------------------------
# 同一 worker identity 的重复领取：不得消耗预算、不得改写有效 lease
# ---------------------------------------------------------------------------


@requires_infra
def test_same_identity_duplicate_claim_does_not_double_count_attempt(
    pg_engine, cleanup_created_runs
):
    """显式传入相同 worker_id 的重复领取（自重复投递）不得双计 attempt。"""
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    svc = RunService(repository=repo)
    thread = f"same-id-{uuid.uuid4().hex[:8]}"
    run_id = _seed_running(
        repo,
        attempt=1,
        max_attempts=5,
        lease_seconds=180.0,  # 有效 lease：同一次执行还在跑
        worker_id="worker-A",
        thread_id=thread,
    )

    claimed = svc.mark_running(run_id, worker_id="worker-A", task_id="t", lease_seconds=180.0)
    if claimed is None:
        # 严格行为：有效 lease 一律不授予接管权（重复领取）
        after = svc.get_run(run_id)
        assert after["attempt"] == 1, "重复领取不得双计 attempt"
        assert after["status"] == RunStatus.RUNNING.value
    else:
        # 宽松行为（同一 identity 允许自接管）：attempt 至多加一次，且必须在预算内
        after = svc.get_run(run_id)
        assert after["attempt"] == 2
        assert after["attempt"] < after["max_attempts"]
    assert svc.get_dead_letter(run_id) is None
