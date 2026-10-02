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
