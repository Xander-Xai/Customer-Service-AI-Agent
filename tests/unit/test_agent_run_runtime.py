"""AgentRun 数据模型 / 状态机 / 服务层 / 错误分类 / 退避 单元测试。

这些是本地确定性测试（无外部依赖），验证业务状态真相源（数据库表）上的
状态迁移、幂等与 DLQ 语义。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.errors import (
    PermanentError,
    TransientError,
    classify_exception,
    is_retryable,
)
from runtime.repository import AgentRunRepository
from runtime.retry import compute_backoff
from runtime.run_service import (
    RunNotFound,
    RunOwnershipLost,
    RunService,
    build_idempotency_scope,
)
from runtime.statuses import (
    InvalidRunTransition,
    RunStatus,
    can_transition,
    ensure_transition,
    is_terminal,
    parse_status,
)
from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory


@pytest.fixture
def repo():
    session_factory, engine, path = make_sqlite_session_factory()
    yield AgentRunRepository(session_factory=session_factory)
    dispose(engine, path)


@pytest.fixture
def service(repo):
    return RunService(repository=repo)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_transitions_are_guarded():
    assert can_transition(RunStatus.QUEUED, RunStatus.RUNNING)
    assert can_transition(RunStatus.RUNNING, RunStatus.RETRYING)
    assert can_transition(RunStatus.RETRYING, RunStatus.RUNNING)
    assert can_transition(RunStatus.RUNNING, RunStatus.DEAD_LETTER)
    # 终态不可回退
    assert not can_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
    assert not can_transition(RunStatus.FAILED, RunStatus.RUNNING)
    assert not can_transition(RunStatus.DEAD_LETTER, RunStatus.RUNNING)
    with pytest.raises(InvalidRunTransition):
        ensure_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING, "r1")


@pytest.mark.unit
def test_terminal_and_parse_status():
    assert is_terminal(RunStatus.SUCCEEDED)
    assert is_terminal(RunStatus.FAILED)
    assert is_terminal(RunStatus.DEAD_LETTER)
    assert not is_terminal(RunStatus.RUNNING)
    assert parse_status("running") is RunStatus.RUNNING
    with pytest.raises(ValueError):
        parse_status("bogus")


# ---------------------------------------------------------------------------
# 错误分类 / 退避
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_error_classification():
    assert classify_exception(TimeoutError("t")) == "timeout"
    assert classify_exception(TransientError("t")) == "transient"
    assert classify_exception(PermanentError("p")) == "permanent"
    assert classify_exception(ValueError("bad")) == "permanent"
    assert classify_exception(ConnectionError("reset")) == "transient"
    assert is_retryable("timeout") is True
    assert is_retryable("transient") is True
    assert is_retryable("permanent") is False


@pytest.mark.unit
def test_backoff_is_bounded_and_jittered():
    for attempt in range(0, 8):
        delay = compute_backoff(attempt, base_delay=1.0, max_delay=10.0, jitter=0.0)
        assert 0 < delay <= 10.0
    # 指数增长（无 jitter）
    assert compute_backoff(0, base_delay=1.0, max_delay=100.0, jitter=0.0) == 1.0
    assert compute_backoff(1, base_delay=1.0, max_delay=100.0, jitter=0.0) == 2.0
    assert compute_backoff(2, base_delay=1.0, max_delay=100.0, jitter=0.0) == 4.0
    # 封顶
    assert compute_backoff(20, base_delay=1.0, max_delay=10.0, jitter=0.0) == 10.0


# ---------------------------------------------------------------------------
# RunService：创建 / 幂等 / 迁移
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_run_starts_queued(service):
    run = service.create_run(query="你好", session_id="s-1", user_id="u-1")
    assert run["status"] == RunStatus.QUEUED.value
    assert run["attempt"] == 0
    assert run["thread_id"] == "s-1"
    assert run["queued_at"] is not None


@pytest.mark.unit
def test_create_run_idempotency_key_returns_same_run(service):
    scope = build_idempotency_scope("u-1", "POST:/api/runs", "abc")
    first = service.create_run(query="q", session_id="s-1", idempotency_key=scope)
    second = service.create_run(query="q", session_id="s-1", idempotency_key=scope)
    assert first["id"] == second["id"]


@pytest.mark.unit
def test_mark_running_increments_attempt_and_sets_ownership(service):
    run = service.create_run(query="q", session_id="s-1")
    running = service.mark_running(run["id"], worker_id="w1", task_id="t1", lease_seconds=60)
    assert running["status"] == RunStatus.RUNNING.value
    assert running["attempt"] == 1
    assert running["worker_id"] == "w1"
    assert running["task_id"] == "t1"
    assert running["started_at"] is not None


@pytest.mark.unit
def test_mark_running_skips_when_other_worker_holds_valid_lease(service):
    run = service.create_run(query="q", session_id="s-1")
    assert service.mark_running(run["id"], worker_id="w1", lease_seconds=60) is not None
    # 另一 worker 在 lease 有效期内重复投递 -> 不执行
    assert service.mark_running(run["id"], worker_id="w2", lease_seconds=60) is None


@pytest.mark.unit
def test_mark_running_takes_over_after_lease_expiry(service):
    run = service.create_run(query="q", session_id="s-1")
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60, now=past)
    # lease 已过期：其他 worker 可接管
    taken = service.mark_running(run["id"], worker_id="w2", lease_seconds=60)
    assert taken is not None
    assert taken["worker_id"] == "w2"
    assert taken["attempt"] == 2


@pytest.mark.unit
def test_succeed_and_terminal_noop(service):
    run = service.create_run(query="q", session_id="s-1")
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60)
    done = service.mark_succeeded(run["id"], {"response": "ok"})
    assert done["status"] == RunStatus.SUCCEEDED.value
    assert done["result"] == {"response": "ok"}
    # 终态不可再次迁移
    with pytest.raises(InvalidRunTransition):
        service.mark_failed(run["id"], error_code="X", error_message="x")


@pytest.mark.unit
def test_permanent_failure_goes_failed(service):
    run = service.create_run(query="q", session_id="s-1")
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60)
    failed = service.mark_failed(
        run["id"], error_code="ValueError", error_message="bad input", error_type="permanent"
    )
    assert failed["status"] == RunStatus.FAILED.value
    assert failed["attempt"] == 1
    assert service.get_dead_letter(run["id"]) is None


@pytest.mark.unit
def test_transient_failure_goes_retrying(service):
    run = service.create_run(query="q", session_id="s-1")
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60)
    retrying = service.mark_retrying(
        run["id"], delay_seconds=3.0, error_type="timeout", error_message="timeout"
    )
    assert retrying["status"] == RunStatus.RETRYING.value
    assert retrying["next_retry_at"] is not None
    # RETRYING 可再次被领取
    again = service.mark_running(run["id"], worker_id="w2", lease_seconds=60)
    assert again["attempt"] == 2


@pytest.mark.unit
def test_dead_letter_writes_queryable_evidence(service):
    run = service.create_run(query="q", session_id="s-1")
    service.mark_running(run["id"], worker_id="w1", lease_seconds=60)
    dead = service.mark_dead_letter(
        run["id"],
        error_code="TimeoutError",
        error_message="provider timeout",
        error_type="timeout",
        worker_id="w1",
    )
    assert dead["status"] == RunStatus.DEAD_LETTER.value

    record = service.get_dead_letter(run["id"])
    assert record is not None
    assert record["run_id"] == run["id"]
    assert record["attempt_count"] == 1
    assert record["max_attempts"] == 3
    assert record["error_code"] == "TimeoutError"
    assert record["error_message"] == "provider timeout"
    assert record["entered_at"] is not None
    # 可枚举
    assert any(r["run_id"] == run["id"] for r in service.list_dead_letters())


@pytest.mark.unit
def test_require_run_missing_raises(service):
    with pytest.raises(RunNotFound):
        service.require_run("does-not-exist")


@pytest.mark.unit
def test_defer_keeps_status_and_sets_next_retry(service):
    run = service.create_run(query="q", session_id="s-1")
    deferred = service.defer_run(run["id"], delay_seconds=5.0)
    assert deferred["status"] == RunStatus.QUEUED.value
    assert deferred["attempt"] == 0
    assert deferred["next_retry_at"] is not None


# ===========================================================================
# Worker ownership CAS
#
# The invariant under test: once a worker's ownership is gone (taken over, or
# lease expired), that worker can no longer mutate the AgentRun row — and
# losing ownership is never turned into a business failure.
#
# The lease matters as much as worker_id: a worker paused past its lease expiry
# still has its own worker_id, but no longer owns anything.
# ===========================================================================


def _expire_lease(repo: AgentRunRepository, run_id: str) -> None:
    """Force the row's lease into the past (simulates a stalled worker)."""
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    session = repo._session()  # noqa: SLF001 - test-only fixture access
    try:
        from db.models import AgentRun

        session.query(AgentRun).filter(AgentRun.id == run_id).update({"lease_expires_at": past})
        session.commit()
    finally:
        session.close()


def _running_with_owner(svc: RunService, *, worker: str, lease_s: float = 60.0):
    run = svc.create_run(query="q", session_id="s1")
    claimed = svc.mark_running(run["id"], worker_id=worker, task_id="t-1", lease_seconds=lease_s)
    assert claimed is not None
    return claimed


class TestOwnershipCASBlocksStaleWorker:
    def test_takeover_is_atomic_and_lease_is_required(self, service, repo):
        """A expires, B takes over atomically, A can no longer commit."""
        run = _running_with_owner(service, worker="worker-A")
        _expire_lease(repo, run["id"])

        taken = service.mark_running(
            run["id"], worker_id="worker-B", task_id="t-2", lease_seconds=60
        )
        assert taken is not None
        assert taken["worker_id"] == "worker-B"
        assert service.get_run(run["id"])["worker_id"] == "worker-B"

        with pytest.raises(RunOwnershipLost):
            service.mark_succeeded(run["id"], {"r": 1}, expected_worker_id="worker-A")

        after = service.get_run(run["id"])
        assert after["status"] == RunStatus.RUNNING.value
        assert after["worker_id"] == "worker-B"
        assert after["result"] in (None, {}, "")

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda s, rid: s.mark_succeeded(rid, {"r": 1}, expected_worker_id="A"),
            lambda s, rid: s.mark_failed(
                rid,
                error_code="E",
                error_message="m",
                expected_worker_id="A",
            ),
            lambda s, rid: s.mark_retrying(rid, delay_seconds=1, expected_worker_id="A"),
            lambda s, rid: s.mark_waiting_approval(rid, expected_worker_id="A"),
            lambda s, rid: s.mark_dead_letter(
                rid, error_code="E", error_message="m", expected_worker_id="A"
            ),
        ],
        ids=["succeeded", "failed", "retrying", "waiting_approval", "dead_letter"],
    )
    def test_every_worker_owned_mutation_is_blocked(self, service, repo, mutation):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        assert (
            service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)
            is not None
        )

        with pytest.raises(RunOwnershipLost):
            mutation(service, run["id"])

        after = service.get_run(run["id"])
        assert after["status"] == RunStatus.RUNNING.value, "stale worker mutated the row"
        assert after["worker_id"] == "B"

    def test_expired_owner_cannot_commit_even_without_takeover(self, service, repo):
        """worker_id still matches, but the lease has expired: still not owner."""
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])

        with pytest.raises(RunOwnershipLost):
            service.mark_succeeded(run["id"], {"r": 1}, expected_worker_id="A")
        assert service.get_run(run["id"])["status"] == RunStatus.RUNNING.value

    def test_new_owner_can_still_complete(self, service, repo):
        """Guard against 'fix' being just 'lock everything down'."""
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)

        done = service.mark_succeeded(run["id"], {"ok": True}, expected_worker_id="B")
        assert done["status"] == RunStatus.SUCCEEDED.value
        assert done["result"] == {"ok": True}

    def test_new_owner_can_heartbeat(self, service, repo):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)
        assert service.heartbeat(run["id"], worker_id="B", lease_seconds=60) is True


class TestHeartbeatOwnershipCAS:
    def test_stale_owner_after_takeover_cannot_renew(self, service, repo):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)
        before = service.get_run(run["id"])

        assert service.heartbeat(run["id"], worker_id="A", lease_seconds=600) is False

        after = service.get_run(run["id"])
        assert after["worker_id"] == "B", "stale heartbeat changed the owner"
        assert (
            after["lease_expires_at"] == before["lease_expires_at"]
        ), "stale heartbeat extended/replaced the new owner's lease"

    def test_expired_owner_cannot_resurrect_its_lease(self, service, repo):
        """Strict lease expiry: nobody has taken over yet, and A is still out."""
        run = _running_with_owner(service, worker="A", lease_s=60)
        _expire_lease(repo, run["id"])
        expired_at = service.get_run(run["id"])["lease_expires_at"]

        assert service.heartbeat(run["id"], worker_id="A", lease_seconds=600) is False
        assert service.get_run(run["id"])["lease_expires_at"] == expired_at

    def test_live_owner_can_renew(self, service):
        run = _running_with_owner(service, worker="A", lease_s=300)
        assert service.heartbeat(run["id"], worker_id="A", lease_seconds=300) is True


class TestRunningTakeoverIsAtomic:
    def test_only_one_of_two_challengers_wins(self, service, repo):
        """Both B and C see an expired owner; only one may take over."""
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])

        b = service.mark_running(run["id"], worker_id="B", task_id="tB", lease_seconds=60)
        c = service.mark_running(run["id"], worker_id="C", task_id="tC", lease_seconds=60)

        winners = [x for x in (b, c) if x is not None]
        assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
        assert service.get_run(run["id"])["worker_id"] in {"B", "C"}

    def test_live_lease_blocks_takeover(self, service):
        run = _running_with_owner(service, worker="A", lease_s=300)
        assert (
            service.mark_running(run["id"], worker_id="B", task_id="tB", lease_seconds=60) is None
        )
        assert service.get_run(run["id"])["worker_id"] == "A"

    def test_same_owner_reclaim_renews(self, service):
        run = _running_with_owner(service, worker="A", lease_s=300)
        again = service.mark_running(run["id"], worker_id="A", task_id="t1", lease_seconds=300)
        assert again is not None
        assert again["worker_id"] == "A"


class TestOwnershipLostIsDistinctFromInvalidTransition:
    def test_status_mismatch_still_raises_invalid_transition(self, service):
        """A finished run is a state-machine problem, not an ownership problem."""
        run = _running_with_owner(service, worker="A", lease_s=300)
        service.mark_succeeded(run["id"], {"r": 1}, expected_worker_id="A")

        with pytest.raises(InvalidRunTransition):
            service.mark_succeeded(run["id"], {"r": 2}, expected_worker_id="A")

    def test_owner_mismatch_raises_ownership_lost_not_invalid(self, service, repo):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)

        with pytest.raises(RunOwnershipLost) as ei:
            service.mark_succeeded(run["id"], {"r": 1}, expected_worker_id="A")
        lost = ei.value
        assert lost.run_id == run["id"]
        assert lost.expected_worker_id == "A"
        assert lost.current_status == RunStatus.RUNNING.value
        assert lost.current_worker_id == "B"
        # Explicitly not a state-machine error.
        assert not isinstance(lost, InvalidRunTransition)

    def test_ownership_lost_carries_no_user_data(self, service, repo):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)
        with pytest.raises(RunOwnershipLost) as ei:
            service.mark_succeeded(run["id"], {"secret": "SENSITIVE"}, expected_worker_id="A")
        blob = f"{ei.value} {ei.value.run_id} {ei.value.expected_worker_id}"
        assert "SENSITIVE" not in blob
        assert "q" not in blob.replace(run["id"], "")


class TestNonWorkerPathsStayUngated:
    def test_cancel_run_works_against_a_live_worker(self, service):
        """External cancellation must not require matching the worker."""
        run = _running_with_owner(service, worker="A", lease_s=300)
        cancelled = service.cancel_run(run["id"])
        assert cancelled["status"] == RunStatus.CANCELLED.value

    def test_cancel_run_works_after_ownership_moved(self, service, repo):
        run = _running_with_owner(service, worker="A")
        _expire_lease(repo, run["id"])
        service.mark_running(run["id"], worker_id="B", task_id="t2", lease_seconds=60)
        assert service.cancel_run(run["id"])["status"] == RunStatus.CANCELLED.value

    def test_dlq_from_queued_needs_no_owner(self, service):
        """Admin/replay path, not a worker ownership commit."""
        run = service.create_run(query="q", session_id="s1")
        dlq = service.mark_dead_letter(
            run["id"], error_code="X", error_message="y", error_type="timeout"
        )
        assert dlq["status"] == RunStatus.DEAD_LETTER.value

    def test_dlq_from_retrying_needs_no_owner(self, service):
        run = service.create_run(query="q", session_id="s1")
        service.mark_running(run["id"], worker_id="A", lease_seconds=300)
        service.mark_retrying(run["id"], delay_seconds=0, expected_worker_id="A")
        dlq = service.mark_dead_letter(
            run["id"], error_code="X", error_message="y", error_type="timeout"
        )
        assert dlq["status"] == RunStatus.DEAD_LETTER.value

    def test_requeue_dead_letter_still_works(self, service):
        run = service.create_run(query="q", session_id="s1")
        service.mark_dead_letter(run["id"], error_code="X", error_message="y", error_type="timeout")
        again = service.requeue_dead_letter(run["id"])
        assert again["status"] == RunStatus.QUEUED.value

    def test_approval_resume_grants_fresh_ownership(self, service):
        """WAITING_APPROVAL clears owner; resume must not demand the old one."""
        run = _running_with_owner(service, worker="A", lease_s=300)
        service.mark_waiting_approval(run["id"], expected_worker_id="A")
        parked = service.get_run(run["id"])
        assert parked["worker_id"] is None and parked["lease_expires_at"] is None

        resumed = service.mark_resumed_running(
            run["id"], worker_id="B", task_id="tB", lease_seconds=60
        )
        assert resumed["status"] == RunStatus.RUNNING.value
        assert resumed["worker_id"] == "B"
        # and B can commit with the NEW ownership
        assert (
            service.mark_succeeded(run["id"], {"ok": 1}, expected_worker_id="B")["status"]
            == RunStatus.SUCCEEDED.value
        )

    def test_initial_queued_claim_still_works(self, service):
        run = service.create_run(query="q", session_id="s1")
        claimed = service.mark_running(run["id"], worker_id="A", lease_seconds=60)
        assert claimed["status"] == RunStatus.RUNNING.value
        assert claimed["attempt"] == 1


class TestRepositoryOwnershipPrimitives:
    def test_transition_owned_returns_none_on_owner_mismatch(self, repo, service):
        run = _running_with_owner(service, worker="A", lease_s=300)
        out = repo.transition_owned(
            run["id"],
            from_statuses={RunStatus.RUNNING},
            to_status=RunStatus.SUCCEEDED,
            expected_worker_id="B",
            result={},
        )
        assert out is None
        assert service.get_run(run["id"])["status"] == RunStatus.RUNNING.value

    def test_transition_owned_can_require_lease(self, repo, service):
        run = _running_with_owner(service, worker="A", lease_s=300)
        _expire_lease(repo, run["id"])
        out = repo.transition_owned(
            run["id"],
            from_statuses={RunStatus.RUNNING},
            to_status=RunStatus.SUCCEEDED,
            expected_worker_id="A",
            require_live_lease=True,
            result={},
        )
        assert out is None

    def test_dedicated_method_avoids_none_sentinel_ambiguity(self):
        """`None` must not silently mean both 'no owner check' and
        'require worker_id IS NULL'. transition_owned requires a real owner."""
        import inspect

        sig = inspect.signature(AgentRunRepository.transition_owned)
        assert sig.parameters["expected_worker_id"].default is inspect.Parameter.empty

    def test_executor_passes_owner_at_every_worker_owned_call_site(self):
        """Structural guard: the CAS exists, so production must actually use it."""
        import ast
        import inspect
        import textwrap

        from runtime import executor as ex

        src = textwrap.dedent(inspect.getsource(ex))
        tree = ast.parse(src)
        owned = {
            "mark_succeeded",
            "mark_failed",
            "mark_retrying",
            "mark_waiting_approval",
            "mark_dead_letter",
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in owned:
                continue
            kws = {k.arg for k in node.keywords if k.arg}
            assert "expected_worker_id" in kws, (
                f"executor calls svc.{name}(...) without expected_worker_id — the owner "
                f"CAS would not be enforced on the worker path"
            )
