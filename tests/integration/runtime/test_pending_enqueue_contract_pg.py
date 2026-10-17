"""PENDING 两阶段入队契约与崩溃注入（真实 PostgreSQL）。

契约
----
``create_run(status=PENDING)`` -> ``dispatch_run`` -> ``mark_queued()`` 是**两阶段
入队**：先提交数据库，再发消息，最后确认。中间任何一步被打断都会留下一个
``PENDING`` run，而 ``PENDING`` 不在 ``EXECUTABLE_STATUSES``、也不在
``list_recoverable_runs`` 的扫描范围内——**没有任何自动机制会再碰它**。修复前
这个状态是不可恢复的。

恢复契约（本文件钉住）
----------------------
1. **宽限期**：只扫描 ``updated_at`` 早于 ``now - grace`` 的 PENDING。直接重投无法
   区分「publish 之前被打断」（必须补投）与「publish 成功但 mark_queued 未提交」
   （补投多余），所以必须等一个宽限期。
2. **条件迁移**：`PENDING -> QUEUED`` 由 ``promote_pending_to_queued`` 在同一条
   ``UPDATE ... WHERE status='PENDING'`` 内裁决「谁有权发这条消息」。**不是**扫描
   全部 PENDING 后无条件发送。
3. **最终收敛**：迁移成功但投递失败时，run 停在 ``QUEUED`` 且
   ``next_retry_at IS NULL``，由 QUEUED 孤儿分支兜底，因此总能收敛。

覆盖的崩溃注入
--------------
C1 已提交数据库、尚未发送消息；
C2 消息发送成功、``mark_queued`` 尚未提交；
C3 ``mark_queued`` 成功后 broker 丢消息；
C4 两个 reconciler 同时扫描过期 PENDING；
C5 PENDING 被用户取消 / 已被其他流程处理；
C6 投递失败后由 QUEUED 孤儿分支最终收敛。

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_pending_enqueue_contract_pg.py -q
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from runtime.repository import AgentRunRepository
from runtime.retry import reconcile_stuck_runs
from runtime.run_service import RunService
from runtime.statuses import RunStatus

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip() and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)

GRACE = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def env(pg_engine, cleanup_created_runs):
    repo = AgentRunRepository(session_factory=_session_factory(pg_engine))
    return RunService(repository=repo), repo


def _age(
    svc: RunService,
    repo: AgentRunRepository,
    run_id: str,
    seconds: float,
    *,
    created_at_too: bool = True,
) -> None:
    """把 run 的 ``updated_at``（可选 ``created_at``）往回拨，模拟「已经放置了足够久」。

    ``created_at_too=False`` 用来**只**放行 PENDING 扫描、不放行 QUEUED 孤儿分支：
    孤儿分支按 ``created_at`` 判定年龄。两个分支都命中时投递给 2 条是**正确**行为
    （见 ``test_duplicate_dispatch_from_two_recovery_paths_executes_once``），
    所以要单独验证 PENDING 分支时必须把它隔离开。
    """
    from sqlalchemy import update

    from db.models import AgentRun

    when = _utcnow() - timedelta(seconds=seconds)
    values = {"updated_at": when}
    if created_at_too:
        values["created_at"] = when
    session = repo._session()  # noqa: SLF001
    try:
        session.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
        session.commit()
    finally:
        session.close()


class _Recorder:
    def __init__(self, fail: set[str] | None = None):
        self.calls: list[str] = []
        self._fail = fail or set()

    async def __call__(self, run_id: str, countdown: float | None = None):
        self.calls.append(run_id)
        if run_id in self._fail:
            raise RuntimeError("simulated broker publish failure")
        return f"task-{run_id}"

    @property
    def count(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for run_id in self.calls:
            out[run_id] = out.get(run_id, 0) + 1
        return out


# ---------------------------------------------------------------------------
# C1: PENDING 已提交数据库，尚未发送消息
# ---------------------------------------------------------------------------


@requires_infra
def test_pending_within_grace_is_not_dispatched(env):
    """宽限期内不补投：消息可能仍在投递途中，立刻重投只会制造多余投递。"""
    svc, _repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    assert run["status"] == RunStatus.PENDING.value
    # 共享测试库里有其它用例留下的行，断言必须限定在本 run 上。
    stale = svc.list_stale_pending_runs(limit=500, grace_seconds=GRACE)
    assert run["id"] not in stale

    dispatcher = _Recorder()
    asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert run["id"] not in dispatcher.calls
    assert svc.get_run(run["id"])["status"] == RunStatus.PENDING.value


@requires_infra
def test_stale_pending_is_promoted_and_dispatched(env):
    """C1 恢复：陈旧 PENDING -> 条件迁移 -> 投递。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)

    dispatcher = _Recorder()
    result = asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )

    assert dispatcher.count.get(run["id"]) == 1
    assert run["id"] in result
    after = svc.get_run(run["id"])
    assert after["status"] == RunStatus.QUEUED.value
    assert after["queued_at"] is not None


# ---------------------------------------------------------------------------
# C2: 消息发送成功，mark_queued 尚未提交
# ---------------------------------------------------------------------------


@requires_infra
def test_publish_without_mark_queued_recovers_without_double_execution(env):
    """C2：队列里已有一条消息、DB 仍是 PENDING。

    恢复会产生一次**多余投递**（at-least-once 入队）。正确性由 worker 侧的领取 CAS
    保证：并发重复投递中恰好一个把 QUEUED 变成 RUNNING，落败者不消耗 attempt。
    """
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)

    dispatcher = _Recorder()
    asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert svc.get_run(run["id"])["status"] == RunStatus.QUEUED.value

    # 原消息 + 补投消息同时到达
    async def _claim(worker: str):
        return await asyncio.to_thread(
            svc.mark_running, run["id"], worker_id=worker, task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(_claim("w-1"), _claim("w-2"), _claim("w-3"))

    results = asyncio.run(_main())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, "重复投递必须只有一个执行者"
    after = svc.get_run(run["id"])
    assert after["status"] == RunStatus.RUNNING.value
    assert after["attempt"] == 1, "落败的重复投递不得消耗 attempt 预算"


# ---------------------------------------------------------------------------
# C3: mark_queued 成功后 broker 丢消息
# ---------------------------------------------------------------------------


@requires_infra
def test_queued_after_mark_queued_is_recovered_by_orphan_branch(env):
    """C3：QUEUED + next_retry_at IS NULL + 超过宽限期 -> 孤儿分支重投。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    svc.mark_queued(run["id"])
    _age(svc, repo, run["id"], GRACE + 5)

    assert run["id"] in svc.list_recoverable_runs(limit=100000)
    dispatcher = _Recorder()
    result = asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert dispatcher.count.get(run["id"]) == 1
    assert run["id"] in result
    assert svc.get_run(run["id"])["status"] == RunStatus.QUEUED.value


@requires_infra
def test_queued_within_orphan_grace_is_not_dispatched(env):
    """刚入队的 QUEUED 不被立刻重投。"""
    svc, _repo = env
    run = svc.create_run(query="q", session_id=f"s-{uuid.uuid4().hex[:8]}")
    assert run["status"] == RunStatus.QUEUED.value
    assert run["id"] not in svc.list_recoverable_runs(limit=100000)


# ---------------------------------------------------------------------------
# C4: 两个 reconciler 同时扫描过期 PENDING
# ---------------------------------------------------------------------------


@requires_infra
def test_two_concurrent_reconcilers_promote_and_dispatch_exactly_once(env):
    """C4：条件迁移是唯一裁决点——并发 reconciler 在 PENDING 分支上只投递一次。

    ``created_at_too=False`` 把 QUEUED 孤儿分支隔离开，只测 PENDING 恢复路径本身。
    两条分支同时命中时的行为由下一个用例单独钉住。
    """
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5, created_at_too=False)
    dispatcher = _Recorder()

    async def _reconcile():
        return await reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )

    async def _main():
        return await asyncio.gather(_reconcile(), _reconcile())

    results = asyncio.run(_main())
    assert dispatcher.count.get(run["id"]) == 1, "并发 reconciler 不得重复投递"
    assert sum(1 for r in sum(results, []) if r == run["id"]) == 1
    assert svc.get_run(run["id"])["status"] == RunStatus.QUEUED.value


@requires_infra
def test_duplicate_dispatch_across_scans_executes_once(env):
    """跨扫描轮次的重复投递是预期行为，但执行**恰好一次**。

    两点事实（都由本用例钉住，避免把 at-least-once 说成 exactly-once）：

    1. **单轮内只有 1 条投递。** 孤儿分支先扫（此时 run 还是 PENDING，扫不到），
       PENDING 分支后扫并 promote + 投递，所以一轮恰好一条。
    2. **跨轮次会重复投递。** promote 把 run 变成 QUEUED 且 ``created_at`` 已过
       宽限期，下一轮扫描的孤儿分支会再投一条。

    这是 at-least-once 入队的固有结果，不是缺陷；正确性由领取 CAS 保证：并发领取
    中恰好一个赢，落败者**不消耗 attempt**。本契约承诺的是「恰好一次执行」，
    不是「恰好一次投递」。
    """
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)  # created_at 也放行 -> 孤儿分支也能命中

    dispatcher = _Recorder()
    later = _utcnow() + timedelta(seconds=GRACE + 5)

    async def _scan(when):
        return await reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
            now=when,
        )

    # 第一轮：PENDING 分支 promote + 投递（孤儿分支此时扫不到）
    first = asyncio.run(_scan(later))
    assert dispatcher.count.get(run["id"]) == 1, "单轮内恰好一条投递"
    # 共享测试库里可能有其它用例留下的可恢复行，断言限定在本 run 上
    assert run["id"] in first
    assert svc.get_run(run["id"])["status"] == RunStatus.QUEUED.value

    # 第二轮：QUEUED 孤儿分支再投一条（陈旧的 QUEUED + next_retry_at IS NULL）
    second = asyncio.run(_scan(later + timedelta(seconds=GRACE + 5)))
    assert dispatcher.count.get(run["id"]) == 2, "跨轮次重复投递是预期行为"
    assert run["id"] in second

    # 两条消息到达 worker：恰好一个执行，且不消耗预算
    async def _claim(worker: str):
        return await asyncio.to_thread(
            svc.mark_running, run["id"], worker_id=worker, task_id=None, lease_seconds=180.0
        )

    async def _main():
        return await asyncio.gather(*[_claim(f"w-{i}") for i in range(3)])

    results = asyncio.run(_main())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, "重复投递必须只有一个执行者"
    after = svc.get_run(run["id"])
    assert after["status"] == RunStatus.RUNNING.value
    assert after["attempt"] == 1, "落败的重复投递不得消耗 attempt 预算"


@requires_infra
def test_concurrent_promote_has_exactly_one_winner(env):
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)

    async def _promote():
        return await asyncio.to_thread(svc.promote_pending_to_queued, run["id"])

    async def _main():
        return await asyncio.gather(*[_promote() for _ in range(4)])

    results = asyncio.run(_main())
    assert len([r for r in results if r is not None]) == 1


# ---------------------------------------------------------------------------
# C5: PENDING 被用户取消 / 已被其他流程处理
# ---------------------------------------------------------------------------


@requires_infra
def test_cancelled_pending_is_never_dispatched(env):
    """C5-a：用户在恢复前取消 -> 谓词不成立，绝不发消息。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)
    svc.cancel_run(run["id"])
    assert svc.get_run(run["id"])["status"] == RunStatus.CANCELLED.value

    dispatcher = _Recorder()
    asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert run["id"] not in dispatcher.calls
    assert svc.get_run(run["id"])["status"] == RunStatus.CANCELLED.value


@requires_infra
def test_already_marked_queued_pending_is_not_promoted_twice(env):
    """C5-b：``mark_queued`` 已提交 -> PENDING 扫描不再命中，``mark_queued`` 幂等。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    svc.mark_queued(run["id"])
    _age(svc, repo, run["id"], GRACE + 5)

    stale = svc.list_stale_pending_runs(limit=500, grace_seconds=GRACE)
    assert run["id"] not in stale
    assert svc.promote_pending_to_queued(run["id"]) is None
    again = svc.mark_queued(run["id"])
    assert again["status"] == RunStatus.QUEUED.value


@requires_infra
def test_terminal_pending_is_not_promoted(env):
    """C5-c：SUCCEEDED 的 run 不会被 promote（谓词含 status=PENDING）。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    # 直接构造终态（模拟其他流程已处理完）
    from sqlalchemy import update

    from db.models import AgentRun

    session = repo._session()  # noqa: SLF001
    try:
        session.execute(
            update(AgentRun)
            .where(AgentRun.id == run["id"])
            .values(status=RunStatus.SUCCEEDED.value, updated_at=_utcnow() - timedelta(seconds=300))
        )
        session.commit()
    finally:
        session.close()

    assert svc.promote_pending_to_queued(run["id"]) is None
    assert svc.get_run(run["id"])["status"] == RunStatus.SUCCEEDED.value


@requires_infra
def test_cancel_racing_promote_ends_in_a_terminal_state(env):
    """取消与 promote 并发：终态确定，绝不出现「已投递的 PENDING」。"""
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)

    async def _cancel():
        return await asyncio.to_thread(svc.cancel_run, run["id"])

    async def _promote():
        return await asyncio.to_thread(svc.promote_pending_to_queued, run["id"])

    async def _main():
        return await asyncio.gather(_cancel(), _promote())

    asyncio.run(_main())
    final = svc.get_run(run["id"])["status"]
    assert final in {RunStatus.CANCELLED.value, RunStatus.QUEUED.value}
    if final == RunStatus.QUEUED.value:
        # promote 赢了：QUEUED 可被正常领取/取消，不会卡死
        svc.cancel_run(run["id"])
        assert svc.get_run(run["id"])["status"] == RunStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# C6: 投递失败后的最终收敛
# ---------------------------------------------------------------------------


@requires_infra
def test_dispatch_failure_after_promote_converges_via_orphan_branch(env):
    """C6：迁移成功但投递失败 -> QUEUED + next_retry_at IS NULL -> 孤儿分支兜底。

    这是「宽限期 + 条件迁移 + 可重试投递 + 最终收敛」四段闭环的最后一环：不存在
    永久搁浅的 PENDING/QUEUED。
    """
    svc, repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    _age(svc, repo, run["id"], GRACE + 5)

    failing = _Recorder(fail={run["id"]})
    first = asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=failing,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert run["id"] not in first
    after = svc.get_run(run["id"])
    assert after["status"] == RunStatus.QUEUED.value, "投递失败后停在 QUEUED（可再被捞起）"
    assert after["next_retry_at"] is None

    # 下一轮扫描（broker 恢复）：QUEUED 孤儿分支接管
    healthy = _Recorder()
    second = asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=healthy,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
            now=_utcnow() + timedelta(seconds=GRACE + 5),
        )
    )
    assert healthy.count.get(run["id"]) == 1
    assert run["id"] in second


@requires_infra
def test_idempotent_replay_of_create_does_not_duplicate_dispatch(env):
    """幂等请求重试：同一个 idempotency_key 只产生一个 run、一条投递。"""
    svc, _repo = env
    key = f"scope:{uuid.uuid4().hex[:8]}"
    first = svc.create_run(query="q", session_id=f"s-{uuid.uuid4().hex[:8]}", idempotency_key=key)
    second = svc.create_run(query="q", session_id=f"s-{uuid.uuid4().hex[:8]}", idempotency_key=key)
    assert first["id"] == second["id"], "幂等键命中同一个 run"

    dispatcher = _Recorder()
    asyncio.run(
        reconcile_stuck_runs(
            svc,
            dispatcher=dispatcher,
            limit=100000,
            reclaim_stale_running=False,
            pending_grace_seconds=GRACE,
        )
    )
    assert dispatcher.count.get(first["id"], 0) <= 1


@requires_infra
def test_pending_status_is_not_executable(env):
    """PENDING 不是可执行状态：worker 领取必须被状态机拒绝。"""
    from runtime.statuses import EXECUTABLE_STATUSES, InvalidRunTransition

    svc, _repo = env
    run = svc.create_run(
        query="q",
        session_id=f"s-{uuid.uuid4().hex[:8]}",
        status=RunStatus.PENDING.value,
    )
    assert RunStatus.PENDING not in EXECUTABLE_STATUSES
    with pytest.raises(InvalidRunTransition):
        svc.mark_running(run["id"], worker_id="w", task_id=None, lease_seconds=60.0)
    assert svc.get_run(run["id"])["status"] == RunStatus.PENDING.value
