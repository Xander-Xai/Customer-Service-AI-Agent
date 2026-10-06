"""PR #28 follow-up: retention enumeration + reconciler NULL-retry contract.

Real PostgreSQL / Redis integration (gated on ``TEST_DISTRIBUTED_DB_URL`` and
``TEST_REDIS_URL``).

These cover four defects the follow-up review found in #28's own new code, all of
which made a command or reconciler a silent no-op:

* ``checkpoint_ts`` does not exist on the ``checkpoints`` table that
  ``AsyncPostgresSaver.setup()`` creates, so the retention plan query raised
  ``UndefinedColumn`` instead of producing a plan.
* the session scan hardcoded ``session:*`` while the session manager writes
  ``{REDIS_SESSION_PREFIX}{id}:messages`` (default ``csai:session:``), so live
  session enumeration found nothing and aborted the purge.
* ``adelete_thread`` was called with a LangGraph config dict instead of the
  thread id string, so every candidate landed in ``plan.failed``.
* the reconciler filtered ``next_retry_at IS NOT NULL``, excluding exactly the
  ``QUEUED``/NULL broker-loss case its own docstring claims to cover.

Run::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_retention_and_reconciler_contract.py -q
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# 1. retention plan query must not reference a column that does not exist
# ---------------------------------------------------------------------------


def test_checkpoints_table_has_no_checkpoint_ts_column_and_ts_lives_in_payload():
    """The pinned saver schema has no ``checkpoint_ts``; the ts is in the JSON payload.

    Guards the *premise* of the retention query: if a future saver version adds a real
    column we would want to know, but until then the query must read
    ``checkpoint->>'ts'``.
    """
    import psycopg

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'checkpoints'"
            ).fetchall()
        }
        assert "checkpoint_ts" not in cols, (
            "AsyncPostgresSaver now exposes checkpoint_ts; the retention query and its "
            "docs should be revisited"
        )
        assert {"thread_id", "checkpoint", "metadata"} <= cols


def test_load_checkpoint_threads_reads_ts_from_payload(tmp_path):
    """``load_checkpoint_threads`` runs against the real table and returns real ages."""
    import psycopg

    from core.checkpoint_retention import load_checkpoint_threads

    thread_id = f"retention-probe-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
            "VALUES (%s, '', %s, 'json', %s::jsonb, '{}'::jsonb)",
            (
                thread_id,
                uuid.uuid4().hex,
                '{"v": 4, "id": "x", "ts": "2020-01-01T00:00:00+00:00"}',
            ),
        )
    try:
        with psycopg.connect(DB_URL) as conn:
            threads = load_checkpoint_threads(conn)
        assert thread_id in threads
        assert threads[thread_id]["count"] == 1
        assert threads[thread_id]["latest_ts"] is not None
        assert "2020-01-01" in str(threads[thread_id]["latest_ts"])
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))


# ---------------------------------------------------------------------------
# 2. live-session enumeration must use the configured Redis prefix
# ---------------------------------------------------------------------------


def test_live_session_enumeration_matches_configured_prefix(monkeypatch):
    """Session keys are written as ``{prefix}{id}:messages``; enumeration must find them."""
    import redis

    import core.config as config
    from scripts.purge_orphan_checkpoints import _load_live_session_ids

    # the script reads core.config.REDIS_URL (the app URL), not TEST_REDIS_URL
    monkeypatch.setattr(config, "REDIS_URL", REDIS_URL, raising=False)

    session_id = f"retention-live-{uuid.uuid4().hex[:8]}"
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.set(f"{config.REDIS_SESSION_PREFIX}{session_id}:messages", "[]")
        client.set(f"{config.REDIS_SESSION_PREFIX}{session_id}:meta", "{}")
        found = _load_live_session_ids()
        # The key must be discoverable under the *configured* prefix.
        assert (
            session_id in found
        ), f"expected {session_id} in live ids; prefix={config.REDIS_SESSION_PREFIX!r}"
        # and the returned value is the bare session id, not "<id>:messages"
        for got in found:
            assert not got.endswith(":messages")
            assert not got.endswith(":meta")
    finally:
        client.delete(
            f"{config.REDIS_SESSION_PREFIX}{session_id}:messages",
            f"{config.REDIS_SESSION_PREFIX}{session_id}:meta",
        )
        client.close()


def test_hardcoded_session_pattern_would_have_missed():
    """Regression premise: the old ``session:*`` pattern matches none of these keys."""
    import redis

    from core.config import REDIS_SESSION_PREFIX

    session_id = f"retention-probe-{uuid.uuid4().hex[:8]}"
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        key = f"{REDIS_SESSION_PREFIX}{session_id}:messages"
        client.set(key, "[]")
        old_hits = list(client.scan_iter(match="session:*", count=500))
        new_hits = list(client.scan_iter(match=f"{REDIS_SESSION_PREFIX}*", count=500))
        assert key not in old_hits
        assert key in new_hits
    finally:
        client.delete(f"{REDIS_SESSION_PREFIX}{session_id}:messages")
        client.close()


# ---------------------------------------------------------------------------
# 3. adelete_thread must receive the thread id string
# ---------------------------------------------------------------------------


def test_adelete_thread_accepts_thread_id_string_not_config_dict():
    """``adelete_thread`` takes a thread id; a config dict silently matches nothing."""
    import asyncio

    import psycopg

    from core.checkpointer import build_postgres_checkpointer, close_checkpoint_runtime

    thread_id = f"retention-del-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
                "VALUES (%s, '', %s, 'json', %s::jsonb, '{}'::jsonb)",
                (thread_id, uuid.uuid4().hex, '{"v": 4, "ts": "2020-01-01T00:00:00+00:00"}'),
            )

    async def run() -> tuple[int, int]:
        rt = await build_postgres_checkpointer(DB_URL, min_size=1, max_size=2)
        try:
            saver = rt.checkpointer
            # the bug: passing the config dict "succeeds" but deletes 0 rows
            await saver.adelete_thread({"configurable": {"thread_id": thread_id}})
            with psycopg.connect(DB_URL) as conn:
                left_after_dict = conn.execute(
                    "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
                ).fetchone()[0]
            # the fix: pass the thread id string
            await saver.adelete_thread(thread_id)
            with psycopg.connect(DB_URL) as conn:
                left_after_str = conn.execute(
                    "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
                ).fetchone()[0]
            return left_after_dict, left_after_str
        finally:
            await close_checkpoint_runtime(rt)

    left_after_dict, left_after_str = asyncio.run(run())
    assert left_after_dict == 2, "config-dict call should have deleted nothing (the bug)"
    assert left_after_str == 0, "thread-id string call must delete every checkpoint"


def test_purge_apply_path_actually_deletes_an_orphan(monkeypatch):
    """End-to-end: the script's own ``--apply`` call site must delete the orphan.

    This is the regression test for the real defect. The contract test above proves
    the two ``adelete_thread`` forms behave differently; this proves the *script* uses
    the working one, by running ``main()`` against a real orphan thread and asserting
    the rows are gone (previously every candidate landed in ``plan.failed``).
    """
    import sys

    import psycopg

    import scripts.purge_orphan_checkpoints as purge

    orphan = f"retention-orphan-{uuid.uuid4().hex[:8]}"
    keep = f"retention-live-keep-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for tid in (orphan, keep):
            for _ in range(2):
                conn.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
                    "VALUES (%s, '', %s, 'json', %s::jsonb, '{}'::jsonb)",
                    (tid, uuid.uuid4().hex, '{"v": 4, "ts": "2020-01-01T00:00:00+00:00"}'),
                )

    # the orphan is old; `keep` is treated as a live session and must survive
    monkeypatch.setattr(purge, "_load_live_session_ids", lambda: {keep})
    monkeypatch.setattr(purge, "_resolve_db_url", lambda: DB_URL)
    monkeypatch.setattr(sys, "argv", ["purge_orphan_checkpoints.py", "--apply", "--json"])

    try:
        rc = purge.main()
        with psycopg.connect(DB_URL) as conn:
            left_orphan = conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (orphan,)
            ).fetchone()[0]
            left_keep = conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (keep,)
            ).fetchone()[0]
        assert left_orphan == 0, "orphan checkpoint thread was not deleted by --apply"
        assert left_keep == 2, "a live session's checkpoints must never be purged"
        assert rc == 0, f"apply run reported failures (rc={rc})"
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("DELETE FROM checkpoints WHERE thread_id IN (%s, %s)", (orphan, keep))


# ---------------------------------------------------------------------------
# 4. reconciler must include QUEUED runs whose next_retry_at is NULL
# ---------------------------------------------------------------------------


def test_reconciler_recovers_queued_run_with_null_next_retry_at():
    """A normally-created QUEUED run has next_retry_at NULL; broker loss must be recoverable."""
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService
    from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

    session_factory, engine, path = make_sqlite_session_factory()
    try:
        repo = AgentRunRepository(session_factory)
        svc = RunService(repo)
        run = svc.create_run(query="q", session_id="T-queued-null", max_attempts=3)

        stored = repo.get(run["id"])
        assert stored["status"] == "QUEUED"
        assert stored["next_retry_at"] is None, "premise: normal QUEUED run has NULL retry ts"

        later = _utcnow() + timedelta(hours=1)
        ids = repo.list_recoverable_runs(now=later)
        assert run["id"] in ids, "QUEUED/NULL run is the broker-loss case and must be scanned"

        # the age guard must NOT sweep up a run that is not actually old.
        # scan 1h in the future; require a >1h grace so the run is still "young"
        # relative to the cutoff and must be skipped.
        fresh = repo.list_recoverable_runs(now=later, orphan_grace_seconds=7200)
        assert run["id"] not in fresh, (
            "a QUEUED run younger than the grace window must not be re-dispatched "
            "(its broker message may still be in flight)"
        )
    finally:
        dispose(engine, path)


def test_reconciler_still_respects_backoff_for_retrying_runs():
    """A RETRYING run whose backoff has not elapsed must not be re-dispatched."""
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService
    from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory

    session_factory, engine, path = make_sqlite_session_factory()
    try:
        repo = AgentRunRepository(session_factory)
        svc = RunService(repo)
        run = svc.create_run(query="q", session_id="T-retry-backoff", max_attempts=3)
        svc.mark_running(run["id"], worker_id="w1", lease_seconds=60)
        svc.mark_retrying(run["id"], delay_seconds=300.0, error_type="timeout", error_message="t")

        too_soon = repo.list_recoverable_runs(now=_utcnow())
        assert run["id"] not in too_soon

        after_backoff = repo.list_recoverable_runs(now=_utcnow() + timedelta(seconds=600))
        assert run["id"] in after_backoff
    finally:
        dispose(engine, path)
