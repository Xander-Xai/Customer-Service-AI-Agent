"""Real PostgreSQL admission races; no model calls or external ERP."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from runtime.admission import RunAdmissionRejected
from runtime.statuses import RunStatus


def test_concurrent_industrial_user_admission(run_service, cleanup_created_runs, unique, monkeypatch):
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS", "100000")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_USER", "2")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_THREAD", "1")
    owner = unique("quota-user")

    def submit(index):
        sid = f"{owner}-{index}"
        try:
            return run_service.create_run(
                query="fixture", session_id=sid, user_id=owner, idempotency_key=sid
            )
        except RunAdmissionRejected as exc:
            return exc.scope

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(submit, range(6)))
    accepted = [item for item in outcomes if isinstance(item, dict)]
    assert len(accepted) == 2
    assert outcomes.count("user") == 4
    saved = accepted[0]
    replay = run_service.create_run(
        query="fixture",
        session_id=saved["session_id"],
        user_id=owner,
        idempotency_key=saved["idempotency_key"],
    )
    assert replay["id"] == saved["id"]
    assert replay["created_by_request"] is False
    # Terminal completion/cancellation releases capacity without Redis counters.
    run_service.mark_cancelled(saved["id"])
    assert isinstance(submit(7), dict)


def test_industrial_thread_capacity_includes_pending(run_service, cleanup_created_runs, unique, monkeypatch):
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS", "100000")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_USER", "5")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_THREAD", "1")
    owner, sid = unique("quota-user"), unique("quota-thread")
    run_service.create_run(
        query="fixture",
        session_id=sid,
        user_id=owner,
        idempotency_key=unique("quota-key"),
        status=RunStatus.PENDING.value,
    )
    with pytest.raises(RunAdmissionRejected) as exc:
        run_service.create_run(
            query="other", session_id=sid, user_id=owner, idempotency_key=unique("quota-key")
        )
    assert exc.value.scope == "thread"
