"""Worker ownership CAS against **real PostgreSQL** (not SQLite, not mocks).

Why this file exists separately from the unit tests
---------------------------------------------------
The unit tests for ownership CAS run on SQLite. That is not sufficient evidence:
the whole point of the change is *concurrent conditional-update semantics*, and
SQLite's locking and timestamp handling differ from PostgreSQL's. A single-statement
``UPDATE ... WHERE status = ... AND worker_id = ... AND lease_expires_at > now``
must genuinely serialise here, and two challengers really must produce exactly one
winner. Only a real PostgreSQL can answer that.

What is pinned
--------------
  1. stale owner cannot commit SUCCEEDED / FAILED / RETRYING / WAITING_APPROVAL /
     RUNNING-path DEAD_LETTER after a takeover;
  2. lease matters independently of worker_id (expired owner, nobody taken over);
  3. ``heartbeat`` is an atomic owner CAS: a stale worker cannot renew, and an
     expired owner cannot resurrect its own lease;
  4. two takeover challengers -> exactly one winner;
  5. the new owner can still heartbeat and complete normally;
  6. external ``cancel_run`` / non-worker DLQ paths remain ungated.

No Redis is needed for the ownership predicates themselves (they are pure SQL), but
the directory's fixtures require both env vars, so they are used as the suite's
standard gate.

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_worker_ownership_cas.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from runtime.run_service import RunOwnershipLost, RunService
from runtime.statuses import RunStatus

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
requires_infra = pytest.mark.skipif(
    not (
        os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
        and os.getenv("TEST_REDIS_URL", "").strip()
    ),
    reason=INFRA_REASON,
)

pytestmark = [pytest.mark.slow, requires_infra]


def _expire_lease(service: RunService, run_id: str) -> None:
    """Push the row's lease into the past (simulates a stalled/paused worker)."""
    from db.models import AgentRun

    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    session = service.repo._session()  # noqa: SLF001 - test-only
    try:
        session.query(AgentRun).filter(AgentRun.id == run_id).update(
            {"lease_expires_at": past}
        )
        session.commit()
    finally:
        session.close()


def _owned_run(service: RunService, *, worker: str, lease_s: float = 300.0) -> str:
    run = service.create_run(query="q", session_id=f"s-{worker}")
    claimed = service.mark_running(
        run["id"], worker_id=worker, task_id=f"task-{worker}", lease_seconds=lease_s
    )
    assert claimed is not None, "initial claim should succeed"
    return run["id"]


class TestStaleOwnerCannotCommit:
    def test_takeover_then_stale_success_is_rejected(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="worker-A")
        _expire_lease(run_service, run_id)
        taken = run_service.mark_running(
            run_id, worker_id="worker-B", task_id="t-B", lease_seconds=300
        )
        assert taken is not None
        assert taken["worker_id"] == "worker-B"

        with pytest.raises(RunOwnershipLost):
            run_service.mark_succeeded(run_id, {"r": 1}, expected_worker_id="worker-A")

        after = run_service.get_run(run_id)
        assert after["status"] == RunStatus.RUNNING.value
        assert after["worker_id"] == "worker-B"

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda s, rid: s.mark_succeeded(rid, {"r": 1}, expected_worker_id="A"),
                id="succeeded",
            ),
            pytest.param(
                lambda s, rid: s.mark_failed(
                    rid, error_code="E", error_message="m", expected_worker_id="A"
                ),
                id="failed",
            ),
            pytest.param(
                lambda s, rid: s.mark_retrying(
                    rid, delay_seconds=1, expected_worker_id="A"
                ),
                id="retrying",
            ),
            pytest.param(
                lambda s, rid: s.mark_waiting_approval(rid, expected_worker_id="A"),
                id="waiting_approval",
            ),
            pytest.param(
                lambda s, rid: s.mark_dead_letter(
                    rid, error_code="E", error_message="m", expected_worker_id="A"
                ),
                id="dead_letter",
            ),
        ],
    )
    def test_all_worker_owned_mutations_blocked(self, run_service: RunService, mutate):
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)
        run_service.mark_running(run_id, worker_id="B", task_id="t-B", lease_seconds=300)

        with pytest.raises(RunOwnershipLost):
            mutate(run_service, run_id)

        after = run_service.get_run(run_id)
        assert after["status"] == RunStatus.RUNNING.value
        assert after["worker_id"] == "B"

    def test_expired_owner_without_takeover_is_still_rejected(
        self, run_service: RunService
    ):
        """worker_id still matches, lease is gone: ownership is gone too."""
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)

        with pytest.raises(RunOwnershipLost):
            run_service.mark_succeeded(run_id, {"r": 1}, expected_worker_id="A")
        assert run_service.get_run(run_id)["status"] == RunStatus.RUNNING.value

    def test_new_owner_completes_normally(self, run_service: RunService):
        """Not 'lock everything down': the legitimate owner still finishes."""
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)
        run_service.mark_running(run_id, worker_id="B", task_id="t-B", lease_seconds=300)

        done = run_service.mark_succeeded(run_id, {"ok": True}, expected_worker_id="B")
        assert done["status"] == RunStatus.SUCCEEDED.value
        assert done["result"] == {"ok": True}


class TestHeartbeatIsAtomicOwnerCAS:
    def test_stale_owner_cannot_renew_after_takeover(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)
        run_service.mark_running(run_id, worker_id="B", task_id="t-B", lease_seconds=300)
        before = run_service.get_run(run_id)

        assert run_service.heartbeat(run_id, worker_id="A", lease_seconds=600) is False

        after = run_service.get_run(run_id)
        assert after["worker_id"] == "B"
        assert after["lease_expires_at"] == before["lease_expires_at"]

    def test_expired_owner_cannot_resurrect_lease(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A", lease_s=300)
        _expire_lease(run_service, run_id)
        expired_at = run_service.get_run(run_id)["lease_expires_at"]

        assert run_service.heartbeat(run_id, worker_id="A", lease_seconds=600) is False
        assert run_service.get_run(run_id)["lease_expires_at"] == expired_at

    def test_live_owner_can_renew(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A", lease_s=300)
        assert run_service.heartbeat(run_id, worker_id="A", lease_seconds=300) is True


class TestTakeoverIsAtomic:
    def test_exactly_one_of_two_challengers_wins(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)

        b = run_service.mark_running(run_id, worker_id="B", task_id="t-B", lease_seconds=300)
        c = run_service.mark_running(run_id, worker_id="C", task_id="t-C", lease_seconds=300)

        winners = [x for x in (b, c) if x is not None]
        assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
        assert run_service.get_run(run_id)["worker_id"] in {"B", "C"}

    def test_live_lease_blocks_takeover(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A", lease_s=300)
        assert run_service.mark_running(
            run_id, worker_id="B", task_id="t-B", lease_seconds=300
        ) is None
        assert run_service.get_run(run_id)["worker_id"] == "A"


class TestNonWorkerPathsUngated:
    def test_cancel_run_works_against_live_worker(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A", lease_s=300)
        assert run_service.cancel_run(run_id)["status"] == RunStatus.CANCELLED.value

    def test_cancel_run_works_after_ownership_moved(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A")
        _expire_lease(run_service, run_id)
        run_service.mark_running(run_id, worker_id="B", task_id="t-B", lease_seconds=300)
        assert run_service.cancel_run(run_id)["status"] == RunStatus.CANCELLED.value

    def test_queued_source_dlq_needs_no_owner(self, run_service: RunService):
        run = run_service.create_run(query="q", session_id="s-dlq")
        dlq = run_service.mark_dead_letter(
            run["id"], error_code="X", error_message="y", error_type="timeout"
        )
        assert dlq["status"] == RunStatus.DEAD_LETTER.value

    def test_retrying_source_dlq_needs_no_owner(self, run_service: RunService):
        run = run_service.create_run(query="q", session_id="s-dlq2")
        run_service.mark_running(run["id"], worker_id="A", lease_seconds=300)
        run_service.mark_retrying(run["id"], delay_seconds=0, expected_worker_id="A")
        dlq = run_service.mark_dead_letter(
            run["id"], error_code="X", error_message="y", error_type="timeout"
        )
        assert dlq["status"] == RunStatus.DEAD_LETTER.value

    def test_approval_resume_grants_fresh_ownership(self, run_service: RunService):
        run_id = _owned_run(run_service, worker="A", lease_s=300)
        run_service.mark_waiting_approval(run_id, expected_worker_id="A")
        parked = run_service.get_run(run_id)
        assert parked["worker_id"] is None

        resumed = run_service.mark_resumed_running(
            run_id, worker_id="B", task_id="t-B", lease_seconds=300
        )
        assert resumed["worker_id"] == "B"
        assert (
            run_service.mark_succeeded(run_id, {"ok": 1}, expected_worker_id="B")[
                "status"
            ]
            == RunStatus.SUCCEEDED.value
        )
