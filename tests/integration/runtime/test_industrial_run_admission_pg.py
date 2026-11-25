"""Real PostgreSQL admission races; no model calls or external ERP."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from runtime.admission import RunAdmissionRejected
from runtime.statuses import RunStatus


def test_concurrent_industrial_user_admission(
    run_service, cleanup_created_runs, unique, monkeypatch
):
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
    run_service.cancel_run(saved["id"])
    assert isinstance(submit(7), dict)


def test_industrial_thread_capacity_includes_pending(
    run_service, cleanup_created_runs, unique, monkeypatch
):
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


def test_industrial_global_capacity_counts_all_owners(
    run_service, pg_engine, cleanup_created_runs, unique, monkeypatch
):
    from sqlalchemy import func, select

    from db.models import AgentRun
    from runtime.statuses import TERMINAL_STATUSES

    with pg_engine.connect() as conn:
        active = AgentRun.status.notin_([status.value for status in TERMINAL_STATUSES])
        count = select(func.count()).select_from(AgentRun).where(active)
        baseline = conn.execute(count).scalar_one()
    monkeypatch.setenv("CSAI_RUNTIME_PROFILE", "industrial")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS", str(baseline + 1))
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_USER", "5")
    monkeypatch.setenv("CSAI_MAX_OUTSTANDING_RUNS_PER_THREAD", "1")
    run_service.create_run(
        query="fixture", session_id=unique(), user_id=unique(), idempotency_key=unique()
    )
    with pytest.raises(RunAdmissionRejected) as exc:
        run_service.create_run(
            query="other", session_id=unique(), user_id=unique(), idempotency_key=unique()
        )
    assert exc.value.scope == "global"
