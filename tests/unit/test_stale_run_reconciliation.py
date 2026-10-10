"""P0 回归：过期 lease 的 RUNNING run 必须能收敛到终态（不得永久搁浅）。

背景（真实缺陷，非测试抖动）
----------------------------
worker 执行途中 lease 过期（续租被瞬时数据库异常打断、事件循环被饿死、GC 停顿、
进程被 SIGSTOP）时，它仍会把图跑完，随后 ``mark_succeeded`` 被 owner CAS **正确**
拒绝——严格 lease 语义不允许一个已过期的执行者用一次迟到的成功写入「证明」自己有效。
``execute_run`` 于是正常返回，Celery **ACK**，broker 不再投递。

到这里为止每一层都是对的。问题在于此后的收敛：``list_recoverable_runs`` 只扫描
``RETRYING`` / ``QUEUED``，**从不看 RUNNING**。于是没有任何扫描器会再碰到这个 run，
它永久停在 RUNNING：没有终态、没有 DLQ 记录、没有告警。这正是
``test_worker_checkpoint_recovery`` 偶发失败（run 未进终态）的形态。

本文件锁住两层防线：

  - 防线一（缩小窗口）：``_heartbeat_loop`` 不得因一次续租异常就永久停止续租；
  - 防线二（保证收敛）：``reconcile_stuck_runs`` 必须能发现并重新投递过期 lease 的
    RUNNING run，新执行者通过 ``takeover_running`` 原子接管后收敛到终态。

同时锁住**不可越界**的边界：CAS 不放宽、幂等不放宽、``WAITING_APPROVAL`` 不自动重发、
正在被扫到的 run 若刚好续上 lease 则不得被误杀。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from runtime.repository import AgentRunRepository
from runtime.run_service import RunService, _utcnow
from runtime.statuses import RunStatus
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

pytestmark = [pytest.mark.unit]


@pytest.fixture
def env():
    session_factory, engine, path = make_sqlite_session_factory()
    try:
        yield RunService(AgentRunRepository(session_factory))
    finally:
        dispose(engine, path)


class _RecordingDispatcher:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, run_id, **kwargs):
        self.calls.append(run_id)


class _SlowRuntime:
    """执行时间可控的 runtime 替身。"""

    def __init__(self, sleep_seconds: float = 0.0):
        self._sleep = sleep_seconds

    async def run(self, *, thread_id, query, user_id=None):
        await asyncio.sleep(self._sleep)
        return {"response": "ok"}


def _later(seconds: float = 3600.0) -> datetime:
    return _utcnow() + timedelta(seconds=seconds)


#: 足够长以致「扫描时刻」仍落在 lease 之内——用来断言**不该**被扫到的那些用例。
_LEASE = 600.0
_WITHIN_LEASE = 60.0


# ---------------------------------------------------------------------------
# 防线二：扫描器必须看见过期 lease 的 RUNNING run
# ---------------------------------------------------------------------------


def test_reconciler_scan_includes_expired_lease_running_run(env):
    """前提不变式：``list_recoverable_runs`` 从不看 RUNNING，必须由新扫描覆盖。"""
    run = env.create_run(query="q", session_id="T-stale", max_attempts=3)
    env.mark_running(run["id"], worker_id="dead-worker", lease_seconds=1.0)

    assert run["id"] not in env.list_recoverable_runs(
        now=_later()
    ), "前提：旧的 recoverable 扫描不覆盖 RUNNING——这正是缺陷来源"
    rows = env.list_stale_running_runs(now=_later())
    assert [r["id"] for r in rows] == [run["id"]], "过期 lease 的 RUNNING 必须被扫描到"
    assert rows[0]["attempt"] == 1
    assert rows[0]["max_attempts"] == 3


def test_reconciler_scan_never_touches_live_lease_running_run(env):
    """心跳正常的 run 不能被扫到——否则 reconciler 会打断正在执行的执行者。

    扫描时刻必须落在 lease **之内**，否则这条断言测的是「lease 已过期」而非
    「lease 仍存活」。
    """
    run = env.create_run(query="q", session_id="T-live", max_attempts=3)
    env.mark_running(run["id"], worker_id="alive", lease_seconds=_LEASE)

    assert env.list_stale_running_runs(now=_later(_WITHIN_LEASE)) == []
    assert env.list_recoverable_runs(now=_later(_WITHIN_LEASE)) == []


@pytest.mark.asyncio
async def test_reconciler_redispatches_stranded_running_run(env):
    """端到端收敛：搁浅的 RUNNING 被重新投递 -> 新执行者接管 -> 终态。"""
    from runtime.retry import reconcile_stuck_runs

    run = env.create_run(query="q", session_id="T-converge", max_attempts=3)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)

    dispatcher = _RecordingDispatcher()
    dispatched = await reconcile_stuck_runs(env, dispatcher=dispatcher, now=_later())

    assert run["id"] in dispatched
    assert dispatcher.calls == [run["id"]]

    # 新执行者原子接管（attempt+1），随后正常提交
    taken = env.mark_running(run["id"], worker_id="w2", lease_seconds=600.0, now=_later())
    assert taken is not None
    assert taken["attempt"] == 2
    env.mark_succeeded(run["id"], {"response": "ok"}, expected_worker_id="w2")
    assert env.get_run(run["id"])["status"] == RunStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_reconciler_can_be_restricted_to_retrying_queued_only(env):
    """运维可显式关闭 RUNNING 回收（兼容性开关），且默认是关不掉的。"""
    from runtime.retry import reconcile_stuck_runs

    run = env.create_run(query="q", session_id="T-noclaim", max_attempts=3)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)

    dispatcher = _RecordingDispatcher()
    dispatched = await reconcile_stuck_runs(
        env, dispatcher=dispatcher, now=_later(), reclaim_stale_running=False
    )
    assert dispatched == []
    assert dispatcher.calls == []


# ---------------------------------------------------------------------------
# 边界：预算耗尽落 DLQ（不得无界重投）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciler_dead_letters_attempt_exhausted_stale_run(env):
    """lease 反复失效的 run 不能被无限接管；预算耗尽后落 DLQ（可观测、可重放）。"""
    from runtime.retry import reconcile_stuck_runs

    run = env.create_run(query="q", session_id="T-dlq", max_attempts=1)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    # 模拟已经因为 lease 丢失而多消耗了预算
    stored = env.repo.transition(
        run["id"], from_statuses={RunStatus.RUNNING}, to_status=RunStatus.RUNNING, attempt=1
    )
    assert stored["attempt"] == 1 >= stored["max_attempts"]

    dispatcher = _RecordingDispatcher()
    dispatched = await reconcile_stuck_runs(env, dispatcher=dispatcher, now=_later())

    assert dispatcher.calls == [], "预算耗尽时不得再投递"
    assert dispatched == [run["id"]]
    assert env.get_run(run["id"])["status"] == RunStatus.DEAD_LETTER.value
    dlq = env.get_dead_letter(run["id"])
    assert dlq is not None, "必须留下 DLQ 记录"
    assert dlq["error_code"] == "stale_lease_attempts_exhausted"


# ---------------------------------------------------------------------------
# 边界：WAITING_APPROVAL 绝不自动重发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciler_never_redispatches_waiting_approval(env):
    """claim 之后 worker 崩溃的窗口里自动重发无法证明「不重复副作用」。

    保留人工 Runbook + 审批 TTL 的既有安全边界。
    """
    from runtime.retry import reconcile_stuck_runs

    run = env.create_run(query="q", session_id="T-approval", max_attempts=3)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=600.0)
    env.mark_waiting_approval(run["id"], expected_worker_id="w1")

    dispatcher = _RecordingDispatcher()
    dispatched = await reconcile_stuck_runs(env, dispatcher=dispatcher, now=_later(86400))

    assert dispatched == []
    assert dispatcher.calls == []
    assert env.get_run(run["id"])["status"] == RunStatus.WAITING_APPROVAL.value


# ---------------------------------------------------------------------------
# 边界：扫描与写入之间被重新续租的 run 不得被误杀
# ---------------------------------------------------------------------------


def test_dead_letter_stale_running_refuses_when_lease_was_renewed(env):
    """reconciler 扫描后、执行写入前，worker 续上了 lease —— 必须放弃写入。

    这是 ``transition_stale_running`` 把「仍然过期」放进同一条 UPDATE 的原因：
    只按 ``status=RUNNING`` 判断会误杀一个正在正常执行的 run。
    """
    run = env.create_run(query="q", session_id="T-raced", max_attempts=1)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)
    # 扫描时刻：确实过期
    assert [r["id"] for r in env.list_stale_running_runs(now=_later())] == [run["id"]]

    # 随后 worker 活过来并在「扫描时刻」重新领取，续上了长 lease
    later = _later()
    env.mark_running(run["id"], worker_id="w1", lease_seconds=_LEASE, now=later)
    renewed_at = _later(_WITHIN_LEASE)
    assert env.heartbeat(run["id"], worker_id="w1", lease_seconds=_LEASE) is True

    result = env.dead_letter_stale_running(
        run["id"],
        error_code="stale_lease_attempts_exhausted",
        error_message="should not apply",
        now=renewed_at,
    )
    assert result is None, "lease 已续上的 run 必须被放过"
    assert env.get_run(run["id"])["status"] == RunStatus.RUNNING.value


def test_dead_letter_stale_running_is_not_owner_matched_but_is_lease_gated(env):
    """reconciler 没有 worker_id 可匹配，但 lease 过期仍是硬前提。"""
    run = env.create_run(query="q", session_id="T-gated", max_attempts=1)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=_LEASE)

    assert (
        env.dead_letter_stale_running(
            run["id"], error_code="x", error_message="y", now=_later(_WITHIN_LEASE)
        )
        is None
    ), "lease 仍存活时不得落 DLQ"


# ---------------------------------------------------------------------------
# 防线一：续租不得被一次瞬时异常永久打断
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_survives_transient_renewal_error():
    """一次续租异常不得杀死续租循环（否则 lease 必然过期 -> run 搁浅）。"""
    from runtime.executor import _heartbeat_loop

    calls: list[int] = []

    class _Svc:
        def heartbeat(self, run_id, *, worker_id, lease_seconds):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("simulated transient DB error")
            return True

    task = asyncio.create_task(_heartbeat_loop(_Svc(), "r", "w", 60.0, 0.001))
    # 循环内部 `max(1.0, interval)`：最快每 1s 一轮，需要至少跨过两轮。
    await asyncio.sleep(2.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(calls) >= 2, (
        f"续租循环在第一次异常后停止了（只调用了 {len(calls)} 次）；"
        "lease 会过期，完成提交被拒，run 永久停在 RUNNING"
    )


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_when_lease_is_taken_over():
    """真正失效的信号是 renew=False（已被接管），此时才停止续租。"""
    from runtime.executor import _heartbeat_loop

    class _Svc:
        def __init__(self):
            self.n = 0

        def heartbeat(self, run_id, *, worker_id, lease_seconds):
            self.n += 1
            return False

    svc = _Svc()
    task = asyncio.create_task(_heartbeat_loop(svc, "r", "w", 60.0, 0.001))
    await asyncio.sleep(2.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert svc.n <= 2, "ownership 已失效后不应继续空转续租"


# ---------------------------------------------------------------------------
# 搁浅 run 的完整状态机（execute_run 真实路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_run_stranded_then_reconciled_reaches_terminal_state(env, monkeypatch):
    """真实执行路径：lease 过期 -> 提交被拒 -> 搁浅 -> reconciler -> 终态。"""
    from runtime.executor import execute_run

    monkeypatch.setattr("core.config.AGENT_RUN_LEASE_SECONDS", 1.0)
    monkeypatch.setattr("core.config.AGENT_RUN_HEARTBEAT_SECONDS", 1.0)

    run = env.create_run(query="q", session_id="T-stranded", max_attempts=3)

    async def provider():
        return _SlowRuntime(sleep_seconds=2.5)

    first = await execute_run(
        run["id"], service=env, runtime_provider=provider, lock_manager=None, worker_id="w1"
    )

    stored = env.get_run(run["id"])
    assert first == RunStatus.RUNNING.value
    assert stored["status"] == RunStatus.RUNNING.value, "前提：lease 过期导致完成提交被拒"

    from runtime.retry import reconcile_stuck_runs

    dispatcher = _RecordingDispatcher()
    await reconcile_stuck_runs(env, dispatcher=dispatcher, now=_later())

    taken = env.mark_running(run["id"], worker_id="w2", lease_seconds=600.0, now=_later())
    assert taken is not None
    env.mark_succeeded(run["id"], {"response": "ok"}, expected_worker_id="w2")
    assert env.get_run(run["id"])["status"] == RunStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_new_owner_result(env, monkeypatch):
    """回收之后，旧执行者的迟到提交仍必须被拒（不得为了收敛而放宽 CAS）。"""
    from runtime.executor import execute_run

    monkeypatch.setattr("core.config.AGENT_RUN_LEASE_SECONDS", 1.0)
    monkeypatch.setattr("core.config.AGENT_RUN_HEARTBEAT_SECONDS", 1.0)

    run = env.create_run(query="q", session_id="T-stale-write", max_attempts=3)

    async def provider():
        return _SlowRuntime(sleep_seconds=2.5)

    await execute_run(
        run["id"], service=env, runtime_provider=provider, lock_manager=None, worker_id="w1"
    )

    later = _later()
    env.mark_running(run["id"], worker_id="w2", lease_seconds=_LEASE, now=later)
    env.mark_succeeded(run["id"], {"response": "w2"}, expected_worker_id="w2")

    # 旧 worker 迟到写回：必须被拒。
    # 已经是 SUCCEEDED 时状态机先拒绝（InvalidRunTransition）；若新 owner 尚未提交，
    # 则 owner CAS 拒绝（RunOwnershipLost）。两者都不允许写入。
    from runtime.run_service import RunOwnershipLost
    from runtime.statuses import InvalidRunTransition

    with pytest.raises((RunOwnershipLost, InvalidRunTransition)):
        env.mark_succeeded(run["id"], {"response": "stale"}, expected_worker_id="w1")

    assert env.get_run(run["id"])["result"] == {
        "response": "w2"
    }, "旧执行者的迟到提交覆盖了新执行者的结果"


def test_stranded_run_records_dead_letter_row_for_replay(env):
    """DLQ 记录必须可被人工重放（复用原 run_id，不绕过工具幂等键）。"""
    run = env.create_run(query="q", session_id="T-replay", max_attempts=1)
    env.mark_running(run["id"], worker_id="w1", lease_seconds=1.0)

    env.dead_letter_stale_running(
        run["id"],
        error_code="stale_lease_attempts_exhausted",
        error_message="lease lost",
        now=_later(),
    )
    assert env.get_run(run["id"])["status"] == RunStatus.DEAD_LETTER.value

    requeued = env.requeue_dead_letter(run["id"])
    assert requeued["id"] == run["id"], "重放必须复用原 run_id"
    assert requeued["status"] == RunStatus.QUEUED.value
    assert requeued["attempt"] == 0
