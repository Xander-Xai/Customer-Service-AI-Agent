"""PR #27-C: side-effect claim must be an atomic, mutually-exclusive claim.

Real PostgreSQL concurrency proof (gated on ``TEST_DISTRIBUTED_DB_URL``).

Before the fix, ``claim()`` was SELECT-then-UPDATE: two workers could both read
the same PENDING row and both receive ``CLAIM_EXECUTE``, so one refund could be
written twice. This file locks in the fixed semantics:

  * N concurrent claimers on one ``(tool_name, operation_key)`` -> **exactly one**
    ``execute``, the rest ``in_progress``;
  * ``mark_succeeded`` / ``mark_failed`` are owner-conditional, so a losing or
    stale executor cannot overwrite the stored result;
  * an expired lease (owner crashed) can be safely taken over.

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    pytest tests/integration/runtime/test_side_effect_claim_concurrency.py -q
"""

from __future__ import annotations

import os
import threading

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not DB_URL, reason="TEST_DISTRIBUTED_DB_URL 未设置；需要真实 PostgreSQL"),
]

_CLAIM_TTL = 60.0


def _store(engine):
    from sqlalchemy.orm import sessionmaker

    from runtime.side_effects import SideEffectStore

    return SideEffectStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))


@pytest.mark.timeout(180)
def test_concurrent_claims_have_exactly_one_executor(pg_engine, pg_url, unique):
    from sqlalchemy import create_engine

    from runtime.side_effects import CLAIM_EXECUTE, CLAIM_IN_PROGRESS

    n = 8
    tool = "refund"
    operation_key = unique("op")

    # Dedicated pool so every thread owns a real connection during the race.
    engine = create_engine(pg_url, pool_size=n, max_overflow=0)
    barrier = threading.Barrier(n)
    results: list[str | None] = [None] * n
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        store = _store(engine)
        barrier.wait(timeout=30)
        try:
            claim = store.claim(
                tool_name=tool,
                operation_key=operation_key,
                run_id=f"run-{index}",
                thread_id=f"T-claim-{index}",
                fingerprint="fp-same",
                claim_ttl_seconds=_CLAIM_TTL,
                owner=f"owner-{index}",
            )
            results[index] = claim.state
        except BaseException as e:  # noqa: BLE001 - surfaced below
            errors.append(e)

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, errors
        assert results.count(CLAIM_EXECUTE) == 1, results
        assert results.count(CLAIM_IN_PROGRESS) == n - 1, results
    finally:
        engine.dispose()


@pytest.mark.timeout(120)
def test_mark_succeeded_is_owner_conditional(pg_engine, unique):
    from runtime.side_effects import (
        CLAIM_EXECUTE,
        CLAIM_SUCCEEDED,
        SideEffectStore,
    )

    store = SideEffectStore(session_factory=_session_factory(pg_engine))
    tool = "refund"
    operation_key = unique("op")

    claim = store.claim(
        tool_name=tool,
        operation_key=operation_key,
        run_id="run-a",
        thread_id="T-owner",
        fingerprint="fp",
        claim_ttl_seconds=_CLAIM_TTL,
        owner="owner-A",
    )
    assert claim.state == CLAIM_EXECUTE

    # A losing/stale executor must not be able to overwrite the stored result.
    assert store.mark_succeeded(tool, operation_key, "wrong", owner="owner-B") is False
    assert store.mark_succeeded(tool, operation_key, "right", owner="owner-A") is True

    # A later caller sees the stored result instead of executing again.
    again = store.claim(
        tool_name=tool,
        operation_key=operation_key,
        run_id="run-b",
        thread_id="T-owner",
        fingerprint="fp",
        claim_ttl_seconds=_CLAIM_TTL,
    )
    assert again.state == CLAIM_SUCCEEDED
    assert again.result == "right"


@pytest.mark.timeout(120)
def test_expired_claim_can_be_taken_over_without_overwrite(pg_engine, unique):
    from runtime.side_effects import CLAIM_EXECUTE, SideEffectStore

    store = SideEffectStore(session_factory=_session_factory(pg_engine))
    tool = "refund"
    operation_key = unique("op")

    # Owner A claims, then "crashes" (lease already expired).
    first = store.claim(
        tool_name=tool,
        operation_key=operation_key,
        run_id="run-a",
        thread_id="T-takeover",
        fingerprint="fp",
        claim_ttl_seconds=-1.0,
        owner="owner-A",
    )
    assert first.state == CLAIM_EXECUTE

    # Owner B takes over the expired lease.
    second = store.claim(
        tool_name=tool,
        operation_key=operation_key,
        run_id="run-b",
        thread_id="T-takeover",
        fingerprint="fp",
        claim_ttl_seconds=_CLAIM_TTL,
        owner="owner-B",
    )
    assert second.state == CLAIM_EXECUTE

    # Stale owner A can no longer write; owner B can.
    assert (
        store.mark_failed(
            tool, operation_key, error_type="X", error_message="stale", owner="owner-A"
        )
        is False
    )
    assert store.mark_succeeded(tool, operation_key, "from-B", owner="owner-B") is True


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)
