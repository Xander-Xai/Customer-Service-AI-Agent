"""P0 regression against **real PostgreSQL**: expired-lease RUNNING runs must converge.

Real defect
-----------
``tests/integration/test_worker_crash_recovery.py`` failed **deterministically**
(3/3 pre-fix), always with the same signature::

    AssertionError: 未恢复成功: ('RUNNING', 2, '<new worker>', None)
    - 'RUNNING'
    + 'SUCCEEDED'

``attempt=2`` proves worker B *did* take over; the run then never reached a
terminal state. The chain:

  1. the ownership lease expires while the graph is still running;
  2. the worker finishes and calls ``mark_succeeded``, which is **correctly**
     rejected (``transition_owned`` requires ``lease_expires_at > now``);
  3. ``execute_run`` returns normally -> Celery ACKs -> no redelivery;
  4. ``list_recoverable_runs`` scans only ``RETRYING``/``QUEUED``.

Step 4 is the defect: nothing ever scans a stale ``RUNNING`` row again, so the
run is an orphan forever — no terminal state, no DLQ row, no alert. The shared
test database had accumulated 252 such rows, the oldest stranded a week earlier.

Why this file is separate from the unit tests
---------------------------------------------
The unit tests for this behaviour run on SQLite. That is not sufficient: the
fix rests on *conditional-update* semantics — ``UPDATE ... WHERE status='RUNNING'
AND lease_expires_at IS NULL-or-<=now`` — and SQLite's locking and timestamp
handling differ from PostgreSQL's. In particular the concurrent-reclaim case
(two reconcilers, two challengers) only has real meaning on a real engine.

Deliberately NOT covered here
-----------------------------
``WAITING_APPROVAL`` is never reclaimed: auto-resend after a claim-then-crash
cannot be proven free of duplicate side effects. That boundary is intentional
and pinned by an assertion below so it cannot be quietly removed.

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_stale_run_reconciliation.py -q
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from runtime.run_service import RunOwnershipLost, RunService
from runtime.statuses import RunStatus

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)

pytestmark = [pytest.mark.slow, requires_infra]


def _later(seconds: float = 3600.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _expire_lease(service: RunService, run_id: str) -> None:
    """Push the row's lease into the past (a stalled / SIGSTOPped worker)."""
    from db.models import AgentRun

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    session = service.repo._session()  # noqa: SLF001 - test-only
    try:
        session.query(AgentRun).filter(AgentRun.id == run_id).update({"lease_expires_at": past})
        session.commit()
    finally:
        session.close()


def _running_run(
    service: RunService,
    unique,
    created: list[str],
    *,
    worker: str = "w1",
    lease_s: float = 300.0,
) -> str:
    """建一个 RUNNING run 并登记到 ``created``（由 ``cleanup_runs`` 负责删除）。"""
    thread = unique("T-stale")
    run = service.create_run(query="stale-check", session_id=thread, thread_id=thread)
    created.append(run["id"])
    claimed = service.mark_running(run["id"], worker_id=worker, lease_seconds=lease_s)
    assert claimed is not None
    return run["id"]


@pytest.fixture
def cleanup_runs(pg_engine):
    """删除本用例创建的 run。

    这些用例**故意**把 run 留在过期 lease 的 ``RUNNING`` 上——那正是被测状态。
    不清理的话，它们会变成 reconciler 的下一次扫描目标，并且（本用例按
    ``lease_expires_at`` 升序 + LIMIT 扫描）挤掉后续用例新建的 run，让真正的
    断言因无关的历史数据而失败。
    """
    from sqlalchemy.orm import sessionmaker

    created: list[str] = []
    yield created
    if not created:
        return
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    session = factory()
    try:
        from sqlalchemy import text

        session.execute(text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": created})
        session.execute(
            text("DELETE FROM agent_dead_letters WHERE run_id = ANY(:ids)"),
            {"ids": created},
        )
        session.commit()
    finally:
        session.close()


class _RecordingDispatcher:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, run_id, **kwargs):
        self.calls.append(run_id)


def test_expired_lease_running_run_is_scanned_and_live_lease_is_not(
    run_service: RunService, unique, cleanup_runs
):
    """扫描必须看到 lease 已过期的 RUNNING，且跳过心跳正常的 RUNNING。"""
    stale = _running_run(run_service, unique, cleanup_runs, worker="dead", lease_s=1.0)
    _expire_lease(run_service, stale)
    alive = _running_run(run_service, unique, cleanup_runs, worker="alive", lease_s=600.0)

    # 前提：旧的 recoverable 扫描从不看 RUNNING —— 这正是缺陷来源
    assert stale not in run_service.list_recoverable_runs()

    # 扫描是**全表**的（共享库里可能还有历史搁浅 run），故只断言自己那两条的归属
    scanned = {r["id"] for r in run_service.list_stale_running_runs()}
    assert stale in scanned, "过期 lease 的 RUNNING 必须被扫到"
    assert alive not in scanned, "心跳正常的 RUNNING 绝不能被回收"


@pytest.mark.asyncio
async def test_stranded_run_reaches_terminal_state_after_reconcile(
    run_service: RunService, unique, cleanup_runs
):
    """完整收敛：搁浅 -> reconciler 重新投递 -> 新执行者原子接管 -> SUCCEEDED。"""
    from runtime.retry import reconcile_stuck_runs

    run_id = _running_run(run_service, unique, cleanup_runs, lease_s=1.0)
    _expire_lease(run_service, run_id)

    dispatcher = _RecordingDispatcher()
    result = await reconcile_stuck_runs(run_service, dispatcher=dispatcher)
    assert run_id in dispatcher.calls
    assert run_id in result

    taken = run_service.mark_running(run_id, worker_id="w2", lease_seconds=600.0)
    assert taken is not None, "过期 lease 的 run 必须可被接管"
    assert taken["attempt"] == 2
    assert taken["worker_id"] == "w2"

    run_service.mark_succeeded(run_id, {"response": "ok"}, expected_worker_id="w2")
    assert run_service.get_run(run_id)["status"] == RunStatus.SUCCEEDED.value


def test_stale_worker_cannot_overwrite_recovered_result(
    run_service: RunService, unique, cleanup_runs
):
    """收敛之后旧执行者的迟到提交仍必须被拒——不得为收敛而放宽 CAS。

    新执行者接管后**尚未**提交时，旧执行者的迟到完成必须被 owner CAS 拒绝：
    这正是 fencing 的作用，放行它就等于允许覆盖新执行者的结果。
    """
    run_id = _running_run(run_service, unique, cleanup_runs, lease_s=1.0)
    _expire_lease(run_service, run_id)
    taken = run_service.mark_running(run_id, worker_id="w2", lease_seconds=600.0)
    assert taken is not None

    with pytest.raises(RunOwnershipLost):
        run_service.mark_succeeded(run_id, {"response": "stale"}, expected_worker_id="w1")

    after = run_service.get_run(run_id)
    assert after["status"] == RunStatus.RUNNING.value
    assert after["worker_id"] == "w2"

    # 新执行者照常提交，且结果不会被旧执行者污染
    run_service.mark_succeeded(run_id, {"response": "w2"}, expected_worker_id="w2")
    assert run_service.get_run(run_id)["result"] == {"response": "w2"}


def test_dead_letter_is_refused_when_lease_renewed_after_scan(
    run_service: RunService, unique, cleanup_runs
):
    """扫描与写入之间续租成功的 run 不得被落 DLQ（原子「仍然过期」谓词）。"""
    run_id = _running_run(run_service, unique, cleanup_runs, lease_s=1.0)
    _expire_lease(run_service, run_id)
    assert run_id in {r["id"] for r in run_service.list_stale_running_runs()}

    # worker 活过来并续上长 lease
    assert run_service.mark_running(run_id, worker_id="w1", lease_seconds=600.0) is not None
    assert run_service.heartbeat(run_id, worker_id="w1", lease_seconds=600.0) is True

    applied = run_service.dead_letter_stale_running(
        run_id,
        error_code="stale_lease_attempts_exhausted",
        error_message="should not apply",
    )
    assert applied is None, "lease 已续上的 run 必须被放过"
    assert run_service.get_run(run_id)["status"] == RunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_attempt_exhausted_stale_run_is_dead_lettered_and_replayable(
    run_service: RunService, unique, cleanup_runs
):
    """预算耗尽必须落 DLQ 而不是无限接管；DLQ 行必须能人工重放。"""
    from runtime.retry import reconcile_stuck_runs

    thread = unique("T-dlq")
    run = run_service.create_run(query="q", session_id=thread, thread_id=thread, max_attempts=1)
    cleanup_runs.append(run["id"])
    claimed = run_service.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    assert claimed is not None
    assert claimed["attempt"] == 1 >= claimed["max_attempts"]
    _expire_lease(run_service, run["id"])

    dispatcher = _RecordingDispatcher()
    result = await reconcile_stuck_runs(run_service, dispatcher=dispatcher)

    assert run["id"] not in dispatcher.calls, "预算耗尽时不得再投递"
    assert run["id"] in result
    assert run_service.get_run(run["id"])["status"] == RunStatus.DEAD_LETTER.value

    dlq = run_service.get_dead_letter(run["id"])
    assert dlq is not None, "必须留下 DLQ 记录"
    assert dlq["error_code"] == "stale_lease_attempts_exhausted"

    requeued = run_service.requeue_dead_letter(run["id"])
    assert requeued["id"] == run["id"], "重放必须复用原 run_id（工具幂等键依赖它）"
    assert requeued["status"] == RunStatus.QUEUED.value


@pytest.mark.asyncio
async def test_waiting_approval_is_never_auto_redispatched(
    run_service: RunService, unique, cleanup_runs
):
    """claim 后崩溃的窗口里自动重发无法证明「不重复副作用」——保留人工边界。"""
    from runtime.retry import reconcile_stuck_runs

    run_id = _running_run(run_service, unique, cleanup_runs, lease_s=600.0)
    run_service.mark_waiting_approval(run_id, expected_worker_id="w1")

    dispatcher = _RecordingDispatcher()
    result = await reconcile_stuck_runs(run_service, dispatcher=dispatcher)

    assert run_id not in dispatcher.calls
    assert run_id not in result
    assert run_service.get_run(run_id)["status"] == RunStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_concurrent_reconcilers_and_challengers_see_exactly_one_winner(
    run_service: RunService, unique, cleanup_runs
):
    """两个 reconciler 并发扫同一条搁浅 run，两个 challenger 并发接管。

    投递是幂等的（两个扫描都应看到它）；**接管**必须恰好一个赢家。
    """
    from runtime.retry import reconcile_stuck_runs

    run_id = _running_run(run_service, unique, cleanup_runs, lease_s=1.0)
    _expire_lease(run_service, run_id)

    dispatcher = _RecordingDispatcher()
    results = await asyncio.gather(
        reconcile_stuck_runs(run_service, dispatcher=dispatcher),
        reconcile_stuck_runs(run_service, dispatcher=dispatcher),
    )
    assert sum(run_id in r for r in results) == 2, "两个扫描都应看到它"

    taken = await asyncio.gather(
        asyncio.to_thread(
            run_service.mark_running, run_id, worker_id="wa", lease_seconds=600.0, task_id="ta"
        ),
        asyncio.to_thread(
            run_service.mark_running, run_id, worker_id="wb", lease_seconds=600.0, task_id="tb"
        ),
        return_exceptions=True,
    )
    winners = [t for t in taken if isinstance(t, dict)]
    assert len(winners) == 1, f"恰好一个接管者，实际 {len(winners)}"
    assert winners[0]["attempt"] == 2
