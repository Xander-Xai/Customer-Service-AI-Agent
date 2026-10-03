"""PR #27 / #26 unresolved review-finding regressions (A–I).

One focused, deterministic (no external infra) test group per finding that was
LIVE on the branch head. Real-infrastructure variants live under
``tests/integration/runtime/`` (claim concurrency, checkpoint setup race,
session-delete -> checkpoint-delete) and are gated on real PG/Redis.

Mapping
-------
A. worker Compose config boundary         -> ``TestWorkerConfigBoundary``
B. retry publication failure must not ACK -> ``tests/unit/test_run_executor.py``
C. atomic side-effect claim (real PG)     -> integration test
D. Redis session production fail-fast     -> ``TestSessionRedisFailFast``
E. Idempotency-Key length contract        -> ``TestIdempotencyKeyContract``
F. SSE callback not in checkpoint state   -> ``TestStreamCallbackNotCheckpointed``
G. setup() timeout + advisory lock        -> ``TestCheckpointSetupGuard``
H. session delete -> checkpoint delete    -> ``TestSessionCheckpointDeletion``
I. checkpoint id mapping compatibility    -> ``TestExtractCheckpointId``
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "deploy" / "compose" / "docker-compose.yml"


# ---------------------------------------------------------------------------
# A. worker Compose config boundary
# ---------------------------------------------------------------------------


def _compose_services() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]


def _compose_env(service: str) -> dict[str, str]:
    raw = _compose_services()[service]["environment"]
    out: dict[str, str] = {}
    for entry in raw:
        if "=" in entry:
            key, value = entry.split("=", 1)
            out[key.strip()] = value.strip()
    return out


class TestWorkerConfigBoundary:
    @pytest.mark.unit
    def test_worker_service_declares_worker_role(self):
        """Worker must declare SERVICE_ROLE=worker so API-only validation is skipped."""
        assert _compose_env("worker")["SERVICE_ROLE"] == "worker"

    @pytest.mark.unit
    def test_api_service_keeps_api_role(self):
        """Default role stays `api`: production API behaviour is unchanged."""
        assert _compose_env("app")["SERVICE_ROLE"] == "api"

    @pytest.mark.unit
    def test_worker_forwards_runtime_config_it_consumes(self):
        """The worker must receive the non-API config it actually reads."""
        env = _compose_env("worker")
        for key in (
            "DATABASE_URL",
            "REDIS_URL",
            "LANGGRAPH_CHECKPOINT_BACKEND",
            "LANGGRAPH_CHECKPOINT_DATABASE_URL",
            "AGENT_EXECUTION_MODE",
            "AGENT_RUN_THREAD_LOCK_BACKEND",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS",
            "AGENT_RUN_LEASE_SECONDS",
            "AGENT_RUN_VISIBILITY_TIMEOUT",
            "LANGGRAPH_CHECKPOINT_SETUP_TIMEOUT",
            "CHECKPOINT_RETENTION_MIN_AGE_SECONDS",
        ):
            assert key in env, f"worker env missing {key}"

    @pytest.mark.unit
    def test_worker_does_not_require_api_only_credentials(self):
        """Worker must not need API_KEY / JWT / CORS to boot (its env omits them)."""
        env = _compose_env("worker")
        for key in ("API_KEY", "JWT_SECRET", "SESSION_TOKEN_SECRET", "CORS_ORIGINS"):
            assert key not in env, f"worker env unexpectedly carries API-only {key}"

    @staticmethod
    def _import_config(role: str, cwd: Path) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": str(PROJECT_ROOT),
            "SERVICE_ROLE": role,
            "API_KEY_ENABLED": "true",
            "API_KEY": "",
            "DEV_MODE": "true",
        }
        return subprocess.run(
            [sys.executable, "-c", "import core.config"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )

    @pytest.mark.unit
    def test_worker_role_imports_config_without_api_key(self, tmp_path):
        """Reproduce the finding: `import core.config` as worker must not raise."""
        result = self._import_config("worker", tmp_path)
        assert result.returncode == 0, result.stderr

    @pytest.mark.unit
    def test_api_role_still_fails_closed_without_api_key(self, tmp_path):
        """Default role keeps the original fail-closed behaviour (no regression)."""
        result = self._import_config("api", tmp_path)
        assert result.returncode != 0
        assert "API_KEY" in result.stderr


# ---------------------------------------------------------------------------
# D. Redis session production fail-fast
# ---------------------------------------------------------------------------


class TestSessionRedisFailFast:
    @pytest.mark.unit
    def test_assert_session_redis_ready_raises_when_ping_fails(self, monkeypatch):
        import redis as redis_lib

        from core.container import ServiceContainer

        class _DeadClient:
            def ping(self):
                raise ConnectionError("redis down")

            def close(self):
                pass

        class _DeadRedis:
            @staticmethod
            def from_url(*args, **kwargs):
                return _DeadClient()

        monkeypatch.setattr(redis_lib, "Redis", _DeadRedis)
        with pytest.raises(ConnectionError):
            ServiceContainer._assert_session_redis_ready(object(), "redis://x")

    @pytest.mark.unit
    def test_assert_session_redis_ready_rejects_degraded_backend(self, monkeypatch):
        import redis as redis_lib

        from core.container import ServiceContainer

        class _OkClient:
            def ping(self):
                return True

            def close(self):
                pass

        class _OkRedis:
            @staticmethod
            def from_url(*args, **kwargs):
                return _OkClient()

        monkeypatch.setattr(redis_lib, "Redis", _OkRedis)
        degraded = SimpleNamespace(storage_backend="memory")
        with pytest.raises(RuntimeError, match="degraded"):
            ServiceContainer._assert_session_redis_ready(degraded, "redis://x")

    @pytest.mark.unit
    def test_container_production_refuses_broken_redis_session(self, monkeypatch):
        """DEV_MODE=false + SESSION_STORAGE_BACKEND=redis + dead Redis -> startup fails."""
        import redis as redis_lib

        import core.config as config
        import core.container as container_mod

        class _DeadClient:
            def ping(self):
                raise ConnectionError("redis down")

            def close(self):
                pass

        class _DeadRedis:
            @staticmethod
            def from_url(*args, **kwargs):
                return _DeadClient()

        monkeypatch.setattr(redis_lib, "Redis", _DeadRedis)
        monkeypatch.setattr(config, "SESSION_STORAGE_BACKEND", "redis")
        monkeypatch.setattr(config, "DEV_MODE", False)
        with pytest.raises(config.ConfigurationError):
            container_mod.ServiceContainer()

    @pytest.mark.unit
    def test_container_dev_mode_degrades_without_raising(self, monkeypatch):
        """DEV_MODE=true keeps the documented in-process fallback."""
        import redis as redis_lib

        import core.config as config
        import core.container as container_mod

        class _DeadClient:
            def ping(self):
                raise ConnectionError("redis down")

            def close(self):
                pass

        class _DeadRedis:
            @staticmethod
            def from_url(*args, **kwargs):
                return _DeadClient()

        monkeypatch.setattr(redis_lib, "Redis", _DeadRedis)
        monkeypatch.setattr(config, "SESSION_STORAGE_BACKEND", "redis")
        monkeypatch.setattr(config, "DEV_MODE", True)
        container = container_mod.ServiceContainer()
        assert container.session_mgr is not None


# ---------------------------------------------------------------------------
# E. Idempotency-Key length contract
# ---------------------------------------------------------------------------


class TestIdempotencyKeyContract:
    @pytest.mark.unit
    def test_overlong_scoped_key_is_hashed_to_fixed_length(self):
        from runtime.run_service import IDEMPOTENCY_KEY_MAX_LENGTH, build_idempotency_scope

        raw = "x" * 128
        scoped = build_idempotency_scope("alice", "POST:/api/runs", raw)
        assert len(scoped) <= IDEMPOTENCY_KEY_MAX_LENGTH
        assert len(scoped) == 64  # sha256 hex digest
        # Deterministic and still scoped by user/endpoint.
        assert scoped == build_idempotency_scope("alice", "POST:/api/runs", raw)
        assert scoped != build_idempotency_scope("bob", "POST:/api/runs", raw)
        assert scoped != build_idempotency_scope("alice", "POST:/api/other", raw)

    @pytest.mark.unit
    def test_short_scoped_key_is_left_human_readable(self):
        from runtime.run_service import build_idempotency_scope

        scoped = build_idempotency_scope("alice", "POST:/api/runs", "abc")
        assert scoped == "alice:POST:/api/runs:abc"


# ---------------------------------------------------------------------------
# F. SSE stream callback must never enter checkpointed state
# ---------------------------------------------------------------------------


class TestStreamCallbackNotCheckpointed:
    @pytest.mark.unit
    def test_run_graph_does_not_put_callable_into_state(self):
        import inspect

        from api.app import _run_graph

        src = inspect.getsource(_run_graph)
        assert 'state["stream_callback"]' not in src
        assert "set_stream_callback" in src

    @pytest.mark.unit
    def test_stream_callback_travels_in_contextvar(self):
        from core.streaming_context import (
            get_stream_callback,
            reset_stream_callback,
            set_stream_callback,
        )

        async def cb(event):  # pragma: no cover - callable identity only
            pass

        # state-only (legacy) still works, but a clean state has no callback
        assert get_stream_callback({"q": 1}) is None
        token = set_stream_callback(cb)
        try:
            assert get_stream_callback({"q": 1}) is cb
            assert get_stream_callback() is cb
        finally:
            reset_stream_callback(token)
        assert get_stream_callback() is None


# ---------------------------------------------------------------------------
# G. checkpoint setup: advisory lock + timeout close
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, log: list[str], lock_attempts: list[bool]):
        self._log = log
        self._lock_attempts = lock_attempts
        self._last = ""

    async def execute(self, sql, params=None):
        self._log.append(sql)
        self._last = sql

    async def fetchone(self):
        if "pg_try_advisory_lock" in self._last:
            return {"got": self._lock_attempts.pop(0) if self._lock_attempts else True}
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log, lock_attempts):
        self._log = log
        self._lock_attempts = lock_attempts

    def cursor(self):
        return _FakeCursor(self._log, self._lock_attempts)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, log, lock_attempts):
        self._log = log
        self._lock_attempts = lock_attempts

    def connection(self):
        return _FakeConn(self._log, self._lock_attempts)


class TestCheckpointSetupGuard:
    @pytest.mark.unit
    async def test_setup_serialized_under_advisory_lock_and_unlocked(self):
        from core.checkpointer import _setup_with_advisory_lock

        log: list[str] = []
        saver = SimpleNamespace(conn=_FakePool(log, [True]), setup=AsyncMock())
        await _setup_with_advisory_lock(saver)

        saver.setup.assert_awaited_once()
        assert any("pg_try_advisory_lock" in sql for sql in log)
        assert any("pg_advisory_unlock" in sql for sql in log)

    @pytest.mark.unit
    async def test_setup_retries_until_lock_acquired(self):
        from core.checkpointer import _setup_with_advisory_lock

        log: list[str] = []
        saver = SimpleNamespace(conn=_FakePool(log, [False, False, True]), setup=AsyncMock())
        await _setup_with_advisory_lock(saver)

        saver.setup.assert_awaited_once()
        assert sum("pg_try_advisory_lock" in sql for sql in log) == 3

    @pytest.mark.unit
    async def test_setup_falls_back_when_no_pool(self):
        from core.checkpointer import _setup_with_advisory_lock

        # A custom saver whose ``conn`` has no psycopg-style ``connection``
        # context manager: fall back to a plain ``setup()`` (no advisory lock).
        saver = SimpleNamespace(conn=object(), setup=AsyncMock())
        await _setup_with_advisory_lock(saver)
        saver.setup.assert_awaited_once()

    @pytest.mark.unit
    async def test_build_postgres_closes_pool_on_setup_timeout(self, monkeypatch):
        """Timeout during setup must close the pool and raise CheckpointBackendError."""
        pytest.importorskip("psycopg")
        pytest.importorskip("langgraph.checkpoint.postgres.aio")

        import psycopg
        import psycopg.rows
        import psycopg_pool
        from langgraph.checkpoint.postgres import aio as pg_aio

        import core.checkpointer as cp

        state = {"closed": False}

        class _Probe:
            async def close(self):
                pass

        class _Conn:
            @staticmethod
            async def connect(*args, **kwargs):
                return _Probe()

        class _Pool:
            def __init__(self, **kwargs):
                pass

            async def open(self, **kwargs):
                return None

            async def close(self, timeout=None):
                state["closed"] = True

        async def _slow_setup(saver):
            raise TimeoutError()

        monkeypatch.setattr(psycopg, "AsyncConnection", _Conn)
        monkeypatch.setattr(psycopg.rows, "dict_row", object())
        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _Pool)
        monkeypatch.setattr(pg_aio, "AsyncPostgresSaver", lambda pool: object())
        monkeypatch.setattr(cp, "_setup_with_advisory_lock", _slow_setup)

        with pytest.raises(cp.CheckpointBackendError):
            await cp.build_postgres_checkpointer("postgresql://u:p@h:5432/db", setup_timeout=0.05)
        assert state["closed"] is True


# ---------------------------------------------------------------------------
# H. session delete -> checkpoint thread delete
# ---------------------------------------------------------------------------


class TestSessionCheckpointDeletion:
    @pytest.mark.unit
    async def test_delete_uses_adelete_thread_with_thread_id_string(self):
        """Official savers take the thread_id string, NOT a config dict."""
        from api.routes.sessions import delete_session_checkpoint

        deleted: list[str] = []

        class _AsyncSaver:
            async def adelete_thread(self, thread_id: str):
                deleted.append(thread_id)

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(graph_app=SimpleNamespace(checkpointer=_AsyncSaver()))
            )
        )
        assert await delete_session_checkpoint(request, "sess-1") is True
        assert deleted == ["sess-1"]

    @pytest.mark.unit
    async def test_delete_uses_sync_delete_thread_fallback(self):
        from api.routes.sessions import delete_session_checkpoint

        deleted: list[str] = []

        class _SyncSaver:
            def delete_thread(self, thread_id: str):
                deleted.append(thread_id)

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(graph_app=SimpleNamespace(checkpointer=_SyncSaver()))
            )
        )
        assert await delete_session_checkpoint(request, "sess-2") is True
        assert deleted == ["sess-2"]

    @pytest.mark.unit
    async def test_delete_returns_false_when_saver_unsupported(self):
        from api.routes.sessions import delete_session_checkpoint

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(graph_app=SimpleNamespace(checkpointer=object()))
            )
        )
        assert await delete_session_checkpoint(request, "sess-3") is False

    @pytest.mark.unit
    async def test_delete_returns_false_without_graph_app(self):
        from api.routes.sessions import delete_session_checkpoint

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(graph_app=None)))
        assert await delete_session_checkpoint(request, "sess-4") is False


# ---------------------------------------------------------------------------
# I. checkpoint id extraction (mapping + compatible object)
# ---------------------------------------------------------------------------


class TestExtractCheckpointId:
    @pytest.mark.unit
    def test_mapping_id_is_read(self):
        from api.routes.sessions import extract_checkpoint_id

        assert extract_checkpoint_id({"id": "cp-map"}) == "cp-map"

    @pytest.mark.unit
    def test_mapping_checkpoint_id_fallback(self):
        from api.routes.sessions import extract_checkpoint_id

        assert extract_checkpoint_id({"checkpoint_id": "cp-alt"}) == "cp-alt"

    @pytest.mark.unit
    def test_object_id_is_read(self):
        from api.routes.sessions import extract_checkpoint_id

        assert extract_checkpoint_id(SimpleNamespace(id="cp-obj")) == "cp-obj"

    @pytest.mark.unit
    def test_none_and_empty_return_none(self):
        from api.routes.sessions import extract_checkpoint_id

        assert extract_checkpoint_id(None) is None
        assert extract_checkpoint_id({}) is None
        assert extract_checkpoint_id({"id": ""}) is None

    @pytest.mark.unit
    def test_non_string_id_is_not_fabricated(self):
        """A MagicMock/non-str `.id` must not be reported as a checkpoint id."""
        from api.routes.sessions import extract_checkpoint_id

        assert extract_checkpoint_id(MagicMock()) is None
        assert extract_checkpoint_id({"id": 123}) is None

    @pytest.mark.unit
    def test_real_memory_saver_mapping_roundtrip(self):
        """Real langgraph CheckpointTuple.checkpoint is a mapping with `id`."""
        from typing import TypedDict

        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import StateGraph

        from api.routes.sessions import extract_checkpoint_id

        class _S(TypedDict, total=False):
            v: int

        graph = StateGraph(_S)
        graph.add_node("n", lambda s: {"v": 1})
        graph.set_entry_point("n")
        graph.set_finish_point("n")
        app = graph.compile(checkpointer=MemorySaver())

        async def scenario():
            cfg = {"configurable": {"thread_id": "cp-roundtrip"}}
            await app.ainvoke({"v": 0}, config=cfg)
            return await app.checkpointer.aget_tuple(cfg)

        tup = asyncio.run(scenario())
        assert tup is not None
        assert extract_checkpoint_id(tup.checkpoint) == tup.checkpoint["id"]


# ---------------------------------------------------------------------------
# PR #28 round 2 — Celery failure-ack semantics (the retry-durability fix was
# incomplete: raising RetryPublicationError does NOT by itself cause redelivery)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_celery_does_not_ack_failed_tasks():
    """A raised exception must NOT be ACKed, or retry publication is not recoverable.

    ``acks_late`` only defers the ack for tasks that *complete*. On the failure path
    Celery consults ``task_acks_on_failure_or_timeout``, whose default is True — i.e.
    the message is ACKed and merely marked FAILURE. The executor relies on the
    exception escaping so the broker redelivers, so this must be False.
    """
    from runtime.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False
    assert celery_app.conf.task_reject_on_worker_lost is True


@pytest.mark.unit
def test_retry_publication_failure_depends_on_rejection_not_acks_late():
    """Documents the mechanism so the flag is not 'simplified' away later.

    If this flag were True, ``execute_run`` raising ``RetryPublicationError`` would be
    ACKed and the run would sit in RETRYING with no replacement message — the exact
    silent-stuck failure the reconciler is the backstop for.
    """
    from runtime.celery_app import celery_app
    from runtime.executor import RetryPublicationError

    # the escape hatch exists...
    assert issubclass(RetryPublicationError, RuntimeError)
    # ...and it only achieves redelivery because failures are not ACKed
    assert not celery_app.conf.task_acks_on_failure_or_timeout


# ---------------------------------------------------------------------------
# J. Gate 11 wait-helper timeout contract
#
# Finding: ``tests/integration/runtime/test_tool_idempotency.py::_wait_for``
# returned the *last predicate value* on timeout. The terminal-state predicate
# yields ``False`` when the run never reaches a terminal state, so
# ``assert final is not None`` passed and the next line raised
# ``TypeError: 'bool' object is not subscriptable`` — replacing the intended,
# diagnosable "run did not reach a terminal state" failure with an unreadable
# crash. PR #32 fixed the same bug class at a *call site* in the sibling file;
# the helper's own contract was left ambiguous and this caller still returned
# False.
#
# The helper is loaded by path on purpose: ``tests/integration`` deliberately has
# no ``__init__.py`` (importing it would shadow the application ``runtime``
# package), and the module's top level only computes constants — no DB, no Redis,
# no worker, no network.
# ---------------------------------------------------------------------------


def _load_idempotency_module():
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "integration"
        / "runtime"
        / "test_tool_idempotency.py"
    )
    spec = importlib.util.spec_from_file_location("_tiem_idem_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWaitForTimeoutContract:
    def test_false_predicate_timeout_returns_none(self):
        """The exact regression: old code returned False here."""
        mod = _load_idempotency_module()
        result = mod._wait_for(lambda: False, 0)
        assert result is None, (
            "timeout must be reported as 'no result'; returning a falsy predicate "
            "value lets `assert final is not None` pass and turns the real failure "
            "into a bool-subscript TypeError"
        )

    def test_none_predicate_timeout_returns_none(self):
        mod = _load_idempotency_module()
        assert mod._wait_for(lambda: None, 0) is None

    def test_never_true_predicate_timeout_returns_none(self):
        mod = _load_idempotency_module()
        assert mod._wait_for(lambda: 0, 0) is None

    def test_truthy_payload_is_preserved_verbatim(self):
        """Not normalised to True — callers index into the real result.

        A real (small) timeout is used here on purpose: with ``timeout=0`` the
        loop body never runs, which is the correct "deadline already passed"
        behaviour asserted in the separate no-post-deadline-I/O test, not the
        "predicate succeeds" case.
        """
        mod = _load_idempotency_module()
        payload = ("SUCCEEDED", 2)
        assert mod._wait_for(lambda: payload, 1) == payload

    def test_eventually_true_payload_is_preserved(self):
        mod = _load_idempotency_module()
        payload = ("SUCCEEDED", 3)
        calls = {"n": 0}

        def _predicate():
            calls["n"] += 1
            return payload if calls["n"] >= 2 else None

        assert mod._wait_for(_predicate, 5) == payload
        assert calls["n"] >= 2

    def test_predicate_is_not_invoked_after_the_deadline(self):
        """Deadline reached -> None, with no extra DB/Redis round trip."""
        mod = _load_idempotency_module()
        calls = {"n": 0}

        def _predicate():
            calls["n"] += 1
            return None

        assert mod._wait_for(_predicate, 0) is None
        assert calls["n"] == 0, "predicate ran after the deadline expired"

    def test_timeout_surfaces_the_real_assertion_not_a_typeerror(self):
        """The failure shape Gate 11 must now produce.

        Old: final=False -> `assert final is not None` passes -> final[0] raises
        TypeError. New: final=None -> the intended AssertionError fires.
        """
        mod = _load_idempotency_module()

        def _never_terminal():
            return None

        final = mod._wait_for(_never_terminal, 0)

        with pytest.raises(AssertionError, match="run 未进入终态"):
            assert final is not None, f"run 未进入终态: {('RUNNING', 1)}"

        # and crucially the subscript is never reached with a bool
        assert final is None

    def test_regression_is_zero_real_time(self):
        """Must not burn a real timeout; assert the helper is instant."""
        mod = _load_idempotency_module()
        started = time.monotonic()
        mod._wait_for(lambda: False, 0)
        assert time.monotonic() - started < 1.0, "regression test must not wait"
