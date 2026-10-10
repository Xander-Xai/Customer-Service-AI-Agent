"""P0 regression (real PostgreSQL): 过期 lease 的 RUNNING run 必须能收敛到终态。

真实缺陷
--------
``tests/integration/test_worker_crash_recovery.py`` 曾**确定性**失败
（3/3 pre-fix, 5/5 post-fix），现象是 ``('RUNNING', 2, <new worker>, None)``：
worker B 已经接管（attempt=2），却永远进不了终态。链路是

  1. lease 在图执行途中过期（续租被事件循环阻塞 / 瞬时 DB 异常打断）；
  2. worker 跑完图，``mark_succeeded`` 被 owner CAS **正确**拒绝（严格 lease 语义
     不允许已过期的执行者用一次迟到的成功写入「证明」自己有效）；
  3. ``execute_run`` 正常返回 -> Celery **ACK** -> broker 不再投递；
  4. ``list_recoverable_runs`` 只扫 RETRYING/QUEUED，**从不看 RUNNING**。

第 4 步是缺陷本身：没有任何扫描器会再看到这条 run，它永久停在 RUNNING——没有终态、
没有 DLQ 记录、没有告警。

本文件用真实 PostgreSQL 覆盖收敛路径与并发安全：扫描、原子回收、预算封顶、
以及「扫描与写入之间被重新续租」的竞态。放在 ``tests/integration/runtime/`` 下，
因此 ``make runtime-e2e`` 会真正跑到它——此前这条路径**没有任何集成覆盖**。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_stale_run_reconciliation.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not DB_URL or not REDIS_URL,
        reason="TEST_DISTRIBUTED_DB_URL + TEST_REDIS_URL 未设置；需要真实 PostgreSQL + Redis",
    ),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _later(seconds: float = 3600.0) -> datetime:
    return _utcnow() + timedelta(seconds=seconds)


@pytest.fixture
def service():
    from db.database import get_db_session
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService

    svc = RunService(AgentRunRepository(get_db_session))
    created: list[str] = []
    yield svc, created
    for run_id in created:
        with get_db_session() as s:
            from sqlalchemy import text

            s.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            s.execute(text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id})
            s.commit()


def _make_run(svc, created: list[str], *, thread: str, max_attempts: int = 3):
    run = svc.create_run(
        query="stale-check", session_id=thread, thread_id=thread, max_attempts=max_attempts
    )
    created.append(run["id"])
    return run


def test_expired_lease_running_run_is_reclaimable(service):
    """扫描必须看到 lease 已过期的 RUNNING，且看不到心跳正常的 RUNNING。"""
    svc, created = service
    stale = _make_run(svc, created, thread=f"T-stale-{uuid.uuid4().hex[:8]}")
    alive = _make_run(svc, created, thread=f"T-alive-{uuid.uuid4().hex[:8]}")

    svc.mark_running(stale["id"], worker_id="dead", lease_seconds=1.0)
    svc.mark_running(alive["id"], worker_id="alive", lease_seconds=600.0)

    # 扫描时刻必须**同时**满足：stale 的 1s lease 已过、alive 的 600s lease 仍在。
    scan_at = _later(60)
    # 前提：旧扫描器看不见 RUNNING（这正是缺陷来源）
    assert stale["id"] not in svc.list_recoverable_runs(now=scan_at)

    # 扫描是**全表**的（共享库里可能还有历史搁浅 run），因此只断言自己那一条的归属，
    # 不对集合做等值比较。
    scanned = {r["id"] for r in svc.list_stale_running_runs(now=scan_at)}
    assert stale["id"] in scanned
    assert alive["id"] not in scanned, "心跳正常的 run 不该被回收"


@pytest.mark.asyncio
async def test_stale_run_reaches_terminal_state_after_reconcile(service):
    """完整收敛：搁浅 -> reconciler 重新投递 -> 新执行者原子接管 -> SUCCEEDED。"""
    svc, created = service
    from runtime.retry import reconcile_stuck_runs

    run = _make_run(svc, created, thread=f"T-converge-{uuid.uuid4().hex[:8]}")
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)

    dispatched: list[str] = []

    async def dispatcher(run_id, **kwargs):
        dispatched.append(run_id)

    result = await reconcile_stuck_runs(svc, dispatcher=dispatcher, now=_later())
    # 扫描是全表的：只断言自己这条被重新投递，不对总量做等值比较。
    assert run["id"] in dispatched
    assert run["id"] in result

    taken = svc.mark_running(run["id"], worker_id="w2", lease_seconds=600.0, now=_later())
    assert taken is not None
    assert taken["attempt"] == 2
    svc.mark_succeeded(run["id"], {"response": "ok"}, expected_worker_id="w2")
    assert svc.get_run(run["id"])["status"] == "SUCCEEDED"


def test_stale_worker_cannot_overwrite_recovered_result(service):
    """收敛之后，旧执行者的迟到提交仍必须被拒（不得为收敛而放宽 CAS）。"""
    svc, created = service
    from runtime.run_service import RunOwnershipLost
    from runtime.statuses import InvalidRunTransition

    run = _make_run(svc, created, thread=f"T-stalewrite-{uuid.uuid4().hex[:8]}")
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    svc.mark_running(run["id"], worker_id="w2", lease_seconds=600.0, now=_later())
    svc.mark_succeeded(run["id"], {"response": "w2"}, expected_worker_id="w2")

    with pytest.raises((RunOwnershipLost, InvalidRunTransition)):
        svc.mark_succeeded(run["id"], {"response": "stale"}, expected_worker_id="w1")

    assert svc.get_run(run["id"])["result"] == {"response": "w2"}


def test_dead_letter_is_skipped_when_lease_renewed_after_scan(service):
    """扫描与写入之间续租成功的 run 不得被落 DLQ（原子「仍然过期」谓词）。"""
    svc, created = service
    run = _make_run(svc, created, thread=f"T-race-{uuid.uuid4().hex[:8]}", max_attempts=1)
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    assert run["id"] in {r["id"] for r in svc.list_stale_running_runs(now=_later())}

    svc.mark_running(run["id"], worker_id="w1", lease_seconds=600.0, now=_later())
    assert svc.heartbeat(run["id"], worker_id="w1", lease_seconds=600.0) is True

    applied = svc.dead_letter_stale_running(
        run["id"],
        error_code="stale_lease_attempts_exhausted",
        error_message="should not apply",
        now=_later(60),
    )
    assert applied is None
    assert svc.get_run(run["id"])["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_attempt_exhausted_stale_run_is_dead_lettered(service):
    """预算耗尽必须落 DLQ 而不是无限接管；且 DLQ 行可人工重放。"""
    svc, created = service
    from runtime.retry import reconcile_stuck_runs

    run = _make_run(svc, created, thread=f"T-dlq-{uuid.uuid4().hex[:8]}", max_attempts=1)
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    assert svc.get_run(run["id"])["attempt"] == 1

    dispatched: list[str] = []

    async def dispatcher(run_id, **kwargs):
        dispatched.append(run_id)

    result = await reconcile_stuck_runs(svc, dispatcher=dispatcher, now=_later())

    assert run["id"] not in dispatched, "预算耗尽时不得再投递"
    assert run["id"] in result
    stored = svc.get_run(run["id"])
    assert stored["status"] == "DEAD_LETTER"
    dlq = svc.get_dead_letter(run["id"])
    assert dlq is not None
    assert dlq["error_code"] == "stale_lease_attempts_exhausted"

    requeued = svc.requeue_dead_letter(run["id"])
    assert requeued["id"] == run["id"], "重放必须复用原 run_id（工具幂等键依赖它）"
    assert requeued["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_waiting_approval_is_never_auto_redispatched(service):
    """claim 后崩溃的窗口里自动重发无法证明「不重复副作用」——保留人工边界。"""
    svc, created = service
    from runtime.retry import reconcile_stuck_runs

    run = _make_run(svc, created, thread=f"T-approval-{uuid.uuid4().hex[:8]}")
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=600.0)
    svc.mark_waiting_approval(run["id"], expected_worker_id="w1")

    dispatched: list[str] = []

    async def dispatcher(run_id, **kwargs):
        dispatched.append(run_id)

    result = await reconcile_stuck_runs(svc, dispatcher=dispatcher, now=_later(86400))

    assert run["id"] not in dispatched
    assert run["id"] not in result
    assert svc.get_run(run["id"])["status"] == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_concurrent_reconcilers_do_not_double_claim(service):
    """两个 reconciler 并发扫描同一条搁浅 run：都不该造成重复的终态副作用。

    回收本身是「重新投递」——真正的单次性由 worker 侧的 ``takeover_running``
    原子 CAS 与工具幂等 ledger 保证；这里断言的是 reconciler 不会把 run 改成
    两个不同的终态，也不会丢记录。
    """
    svc, created = service
    from runtime.retry import reconcile_stuck_runs

    run = _make_run(svc, created, thread=f"T-conc-{uuid.uuid4().hex[:8]}")
    svc.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)

    seen: list[str] = []

    async def dispatcher(run_id, **kwargs):
        seen.append(run_id)

    results = await asyncio.gather(
        reconcile_stuck_runs(svc, dispatcher=dispatcher, now=_later()),
        reconcile_stuck_runs(svc, dispatcher=dispatcher, now=_later()),
    )
    assert sum(run["id"] in r for r in results) == 2, "两个扫描都应看到它（投递是幂等的）"

    # 并发接管：lease 已过期的前提下，恰好一个 worker 能拿到 ownership
    # （``takeover_running`` 是单条 UPDATE 谓词，不是「先看后写」）
    takeover_now = _later()
    taken = await asyncio.gather(
        asyncio.to_thread(
            svc.mark_running, run["id"], worker_id="wa", lease_seconds=600.0, now=takeover_now
        ),
        asyncio.to_thread(
            svc.mark_running, run["id"], worker_id="wb", lease_seconds=600.0, now=takeover_now
        ),
        return_exceptions=True,
    )
    winners = [t for t in taken if isinstance(t, dict)]
    assert len(winners) == 1, f"恰好一个接管者，实际 {len(winners)}"
    assert winners[0]["attempt"] == 2
