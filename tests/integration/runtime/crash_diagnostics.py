"""Read-only failure diagnostics for the real-infrastructure runtime suite.

Why this module exists
----------------------
The recorded symptom for Gate 7 was *"1 failure in 7 real-infra suite runs"*
with a one-line ``AssertionError`` and nothing else. That is not a
diagnosable failure: an assertion tells you the invariant broke, never whether
the message was delivered, whether it was acknowledged, when the worker died,
whether the checkpoint had committed, or whether a recovering worker ever
picked the run up again.

Every function here is **strictly read-only**. Collecting diagnostics must not
change the behaviour under investigation, so this module never writes to
PostgreSQL, never publishes to the broker and never signals a process. It only
reads committed state and OS process metadata, and serialises it to JSON.

What a snapshot can answer
--------------------------
======================  ====================================================
Question                Field
======================  ====================================================
Was the task delivered?  ``broker.queue_length_before/after``,
                        ``broker.unacked_size``
Was it acknowledged?    ``broker.unacked_size`` plus
                        ``broker.unacked_task_ids`` at the kill instant
When did the worker     ``workers[*].exit_code`` / ``.kill_sent_at`` /
die?                   ``.alive_after_kill``
Was the checkpoint      ``checkpoints.durable_*`` — whether the newest
committed?             committed checkpoint already carries the result the
                        test depends on
Was the run RUNNING?    ``agent_run.status`` / ``.attempt`` / ``.worker_id``
When did the message    ``broker.queue_first_seen_at`` and the
reappear?              ``_VISIBILITY_TIMEOUT`` the run was configured with
Did a new worker        ``agent_run.worker_id`` vs ``workers[*].pid`` and
consume it?            ``.worker_hostname``
Why no resume?          ``checkpoint_writes`` (pending tasks) plus
                        ``agent_run.result`` and the event stream
======================  ====================================================

Usage::

    from tests.integration.runtime.crash_diagnostics import CrashDiagnostics

    diag = CrashDiagnostics(tag=tag, run_id=run_id, thread_id=thread_id,
                            queue=queue, db_url=DB_URL, redis_url=REDIS_URL)
    diag.mark("worker_a_started")
    ...
    except BaseException as exc:
        diag.dump("wait_final_state", exc,
                  workers={"worker_a": worker_a}, rethrow=True)
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Repository root (``tests/integration/runtime/`` -> up three levels).
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Default artifact root. Kept under ``artifacts/`` so a failing run leaves its
#: evidence next to the other evidence rather than in ``/tmp`` (which CI wipes).
DEFAULT_ARTIFACT_ROOT = _REPO_ROOT / "artifacts" / "runtime-diagnostics"

#: Keys a Redis-backed Celery worker uses for in-flight bookkeeping. These are
#: **broker-global**, not per-queue: two crash tests running at once delete each
#: other's unacked entries. Recorded here because that interference is one of
#: the candidate causes under investigation and the snapshot should be able to
#: show it.
BROKER_GLOBAL_KEYS = ("unacked", "unacked_index")

_SECRET_HINTS = ("secret", "token", "password", "api_key", "apikey", "authorization")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> Any:
    """Best-effort scrub of anything that looks like a credential."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(h in str(k).lower() for h in _SECRET_HINTS) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, datetime):
        return value.isoformat()
    return f"<{type(value).__name__}>"


def _safe(fn, default: Any = None) -> Any:
    """Run a collector; a broken collector must not mask the real failure."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort by design
        return {"__collector_error__": f"{type(exc).__name__}: {exc}"}


def proc_alive(proc: subprocess.Popen | None) -> bool | None:
    if proc is None:
        return None
    return proc.poll() is None


def worker_state(
    proc: subprocess.Popen | None, *, name: str, kill_sent_at: str | None = None
) -> dict[str, Any]:
    """Everything about a worker subprocess that survives its death.

    ``exit_code`` is the part pytest never shows: a Celery prefork worker
    SIGKILLed as a process group reports ``-9`` on the parent, which is how you
    tell "the test killed it on purpose" apart from "it died on its own".
    """
    if proc is None:
        return {"name": name, "started": False}
    return {
        "name": name,
        "started": True,
        "pid": proc.pid,
        "alive_now": proc_alive(proc),
        "returncode": proc.poll(),
        "exit_code": (proc.returncode if proc.returncode is not None else None),
        "killed_by_signal": (
            signal.Signals(-proc.returncode).name
            if proc.returncode is not None and proc.returncode < 0
            else None
        ),
        "kill_sent_at": kill_sent_at,
        "pgid": _safe(lambda: os.getpgid(proc.pid)),
    }


def all_related_worker_processes() -> list[dict[str, Any]]:
    """Any live ``celery_worker_runner`` on this host.

    A worker leaked by an earlier test keeps consuming and keeps its own entry
    in the broker's shared unacked bookkeeping, so its presence (or absence) is
    directly relevant to cross-test interference.
    """
    def _scan() -> list[dict[str, Any]]:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,pgid,etimes,args"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        rows = []
        for line in out.splitlines():
            if "celery_worker_runner" not in line or "ps -eo" in line:
                continue
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            rows.append(
                {
                    "pid": parts[0],
                    "ppid": parts[1],
                    "pgid": parts[2],
                    "age_s": parts[3],
                    "args_tail": parts[4][-160:],
                }
            )
        return rows

    return _safe(_scan, [])


def broker_snapshot(redis_client, queue: str) -> dict[str, Any]:
    """Delivery-side state: is the message waiting, in flight, or gone?"""
    def _read() -> dict[str, Any]:
        out: dict[str, Any] = {
            "queue": queue,
            "queue_length": redis_client.llen(queue),
            "broker_global_keys": {},
        }
        for key in BROKER_GLOBAL_KEYS:
            kind = redis_client.type(key)
            size = None
            if kind == "hash":
                size = redis_client.hlen(key)
            elif kind == "zset":
                size = redis_client.zcard(key)
            out["broker_global_keys"][key] = {"type": kind, "size": size}
        unacked = redis_client.type("unacked") == "hash"
        out["unacked_size"] = redis_client.hlen("unacked") if unacked else 0
        out["unacked_task_ids"] = sorted(redis_client.hkeys("unacked"))[:20] if unacked else []
        return out

    return _safe(_read, {})


def run_snapshot(engine, run_id: str) -> dict[str, Any]:
    """The ``agent_runs`` row plus the durable pieces of its state machine."""
    def _read() -> dict[str, Any]:
        from sqlalchemy import text

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, thread_id, session_id, status, attempt, max_attempts, "
                    "worker_id, lease_expires_at, next_retry_at, error_type, error_code, "
                    "queued_at, started_at, finished_at, updated_at, result "
                    "FROM agent_runs WHERE id = :id"
                ),
                {"id": run_id},
            ).mappings().first()
            dl = conn.execute(
                text(
                    "SELECT run_id, attempts, error_type, error_code, created_at "
                    "FROM agent_dead_letters WHERE run_id = :id"
                ),
                {"id": run_id},
            ).mappings().all()
        out: dict[str, Any] = {"agent_run": dict(row) if row is not None else None}
        out["dead_letter"] = [dict(r) for r in dl]
        out["row_count"] = 1 if row is not None else 0
        return out

    return _safe(_read, {})


def checkpoint_snapshot(engine, thread_id: str) -> dict[str, Any]:
    """Committed checkpoint state, in commit order.

    ``durable_channels`` on the newest row is the field that matters: a thread
    can have checkpoints on disk while the newest one still predates the result
    a resume depends on. Counting rows cannot distinguish those two states;
    reading ``channel_values`` can.
    """
    def _read() -> dict[str, Any]:
        from sqlalchemy import text

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint "
                    "FROM checkpoints WHERE thread_id = :t ORDER BY checkpoint_id"
                ),
                {"t": thread_id},
            ).mappings().all()
            writes = conn.execute(
                text(
                    "SELECT checkpoint_id, task_id, channel, type "
                    "FROM checkpoint_writes WHERE thread_id = :t ORDER BY checkpoint_id"
                ),
                {"t": thread_id},
            ).all()

        serialized = []
        latest_channels: list[str] = []
        for r in rows:
            values = ((r["checkpoint"] or {}).get("channel_values")) or {}
            channels = sorted(values)
            latest_channels = channels
            serialized.append(
                {
                    "checkpoint_id": r["checkpoint_id"],
                    "parent_checkpoint_id": r["parent_checkpoint_id"],
                    "channels": channels,
                }
            )
        return {
            "thread_id": thread_id,
            "committed_checkpoint_count": len(rows),
            "durable_channels": latest_channels,
            "checkpoints": serialized,
            "pending_write_count": len(writes),
            "pending_writes": [
                {
                    "checkpoint_id": w[0],
                    "task_id": w[1],
                    "channel": w[2],
                    "type": w[3],
                }
                for w in writes
            ],
        }

    return _safe(_read, {})


def event_snapshot(redis_client, run_id: str) -> dict[str, Any]:
    """The run event stream. An observation channel, not a source of truth."""
    key = f"agent:run:{run_id}:events"
    return _safe(
        lambda: {
            "key": key,
            "exists": bool(redis_client.exists(key)),
            "length": redis_client.llen(key) if redis_client.exists(key) else 0,
            "entries": redis_client.lrange(key, 0, -1)[:50] if redis_client.exists(key) else [],
        },
        {},
    )


def redis_signal_snapshot(redis_client, key_prefix: str) -> dict[str, Any]:
    """The provider's own counters (``ckpt:*``) — the test's evidence channel."""
    names = ("node_first", "node_second", "recovered", "trace")
    return _safe(
        lambda: {
            **{name: redis_client.get(f"{key_prefix}:{name}") for name in names},
            "key_prefix": key_prefix,
        },
        {},
    )


class CrashDiagnostics:
    """Timeline + snapshot collector for a single run under test.

    Construct it before the first side effect, call :meth:`mark` at each
    decision point, and :meth:`dump` in the ``except`` block. ``dump`` never
    raises and never re-raises by itself: pass ``rethrow=True`` when the caller
    wants the original exception preserved after the artifact is written.
    """

    def __init__(
        self,
        *,
        tag: str,
        run_id: str,
        thread_id: str,
        queue: str,
        db_url: str,
        redis_url: str,
        key_prefix: str | None = None,
        artifact_root: str | Path | None = None,
    ) -> None:
        self.tag = tag
        self.run_id = run_id
        self.thread_id = thread_id
        self.queue = queue
        self.db_url = db_url
        self.redis_url = redis_url
        self.key_prefix = key_prefix or f"ckpt:{tag}"
        self.started_at = _utcnow()
        self._t0 = time.perf_counter()
        self._marks: list[dict[str, Any]] = []
        self._kill_sent_at: dict[str, str] = {}

    # -- timeline ---------------------------------------------------------

    def mark(self, name: str, **fields: Any) -> None:
        self._marks.append(
            {
                "mark": name,
                "at": _utcnow(),
                "t_s": round(time.perf_counter() - self._t0, 4),
                **fields,
            }
        )

    def note_kill(self, name: str) -> None:
        self._kill_sent_at[name] = _utcnow()
        self.mark(f"kill_sent:{name}")

    def timeline(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "marks": self._marks,
            "marks_seconds_since_start": [round(m["t_s"], 4) for m in self._marks],
        }

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        import redis as redis_lib
        from sqlalchemy import create_engine

        engine = _safe(lambda: create_engine(self.db_url, pool_pre_ping=True))
        client = _safe(
            lambda: redis_lib.Redis.from_url(
                self.redis_url, decode_responses=True, socket_timeout=5
            )
        )
        env = {
            k: os.getenv(k)
            for k in (
                "AGENT_RUN_QUEUE",
                "AGENT_RUN_VISIBILITY_TIMEOUT",
                "AGENT_RUN_LEASE_SECONDS",
                "AGENT_RUN_MAX_ATTEMPTS",
                "LANGGRAPH_CHECKPOINT_BACKEND",
                "AGENT_EXECUTION_MODE",
            )
            if os.getenv(k)
        }
        snapshot: dict[str, Any] = {
            "schema": "runtime-crash-diagnostics/v1",
            "tag": self.tag,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "queue": self.queue,
            "key_prefix": self.key_prefix,
            "host": _safe(socket_fqdn),
            "pid": os.getpid(),
            "env": env,
            "timeline": self.timeline(),
            "visible_worker_processes": all_related_worker_processes(),
        }
        if isinstance(engine, str):
            snapshot["agent_run"] = engine
            snapshot["checkpoints"] = engine
        else:
            snapshot["agent_run"] = run_snapshot(engine, self.run_id)
            snapshot["checkpoints"] = checkpoint_snapshot(engine, self.thread_id)
            with contextlib.suppress(Exception):
                engine.dispose()
        if isinstance(client, str):
            snapshot["broker"] = client
            snapshot["run_events"] = client
            snapshot["redis_signals"] = client
        else:
            snapshot["broker"] = broker_snapshot(client, self.queue)
            snapshot["run_events"] = event_snapshot(client, self.run_id)
            snapshot["redis_signals"] = redis_signal_snapshot(client, self.key_prefix)
            with contextlib.suppress(Exception):
                client.close()
        return snapshot

    def dump(
        self,
        phase: str,
        exc: BaseException | None = None,
        *,
        workers: dict[str, subprocess.Popen | None] | None = None,
        extra: dict[str, Any] | None = None,
        rethrow: bool = False,
        artifact_root: str | Path | None = None,
    ) -> Path | None:
        """Write one JSON artifact. Returns its path, or ``None`` if writing failed."""
        try:
            self.mark(f"failure:{phase}")
            payload: dict[str, Any] = self.snapshot()
            payload["phase"] = phase
            payload["exception"] = (
                None
                if exc is None
                else {
                    "type": type(exc).__name__,
                    "message": str(exc)[:4000],
                    "repr": repr(exc)[:2000],
                }
            )
            payload["workers"] = {
                name: worker_state(
                    proc,
                    name=name,
                    kill_sent_at=self._kill_sent_at.get(name),
                )
                for name, proc in (workers or {}).items()
            }
            if extra:
                payload["extra"] = extra

            root = Path(artifact_root or self.artifact_root())
            root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = root / f"{self.tag}-{phase}-{stamp}.json"
            path.write_text(
                json.dumps(_redact(payload), indent=2, ensure_ascii=False, default=_json_default),
                encoding="utf-8",
            )
            return path
        except Exception as dump_exc:  # noqa: BLE001 - never mask the real failure
            if os.getenv("CSAI_DIAG_VERBOSE"):
                print(f"[crash-diagnostics] dump failed: {dump_exc}", flush=True)
            return None
        finally:
            if rethrow and exc is not None:
                raise exc

    def artifact_root(self) -> Path:
        return Path(os.getenv("RUNTIME_DIAGNOSTICS_DIR", DEFAULT_ARTIFACT_ROOT))


def socket_fqdn() -> str:
    import socket

    return socket.getfqdn()


def write_suite_snapshot(
    *,
    phase: str,
    nodeid: str,
    outcome: str,
    env: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
    artifact_root: str | Path | None = None,
) -> Path | None:
    """Package-level snapshot for *any* failing test in the runtime suite.

    The Gate 7 flake is recorded at suite level ("1 of 7 suite runs"), so a
    snapshot scoped to one test is not enough: the evidence that distinguishes
    "this run raced" from "a previous test in the same process corrupted shared
    state" is the state of the *shared* broker keys and the set of live worker
    processes at the moment of failure. That is what this captures.
    """
    import redis as redis_lib
    from sqlalchemy import create_engine

    db_url = (env or os.environ).get("TEST_DISTRIBUTED_DB_URL", "").strip()
    redis_url = (env or os.environ).get("TEST_REDIS_URL", "").strip()
    payload: dict[str, Any] = {
        "schema": "runtime-suite-diagnostics/v1",
        "captured_at": _utcnow(),
        "phase": phase,
        "nodeid": nodeid,
        "outcome": outcome,
        "host": _safe(socket_fqdn),
        "pid": os.getpid(),
        "visible_worker_processes": all_related_worker_processes(),
        "queues": {},
    }

    if redis_url:
        client = _safe(
            lambda: redis_lib.Redis.from_url(
                redis_url, decode_responses=True, socket_timeout=5
            )
        )
        if isinstance(client, str):
            payload["redis"] = client
        else:
            def _queues() -> dict[str, Any]:
                found = sorted(
                    k.decode() if isinstance(k, bytes) else k
                    for k in client.scan_iter(match="agent_runs_test_*", count=200)
                )
                return {
                    "broker_global_keys": {
                        key: {
                            "type": client.type(key),
                            "size": (
                                client.hlen(key)
                                if client.type(key) == "hash"
                                else (client.zcard(key) if client.type(key) == "zset" else None)
                            ),
                        }
                        for key in BROKER_GLOBAL_KEYS
                    },
                    "test_queues": {name: client.llen(name) for name in found},
                    "production_queue": {
                        "agent_runs": client.llen("agent_runs"),
                    },
                }

            payload["redis"] = _safe(_queues, {})
            with contextlib.suppress(Exception):
                client.close()

    if db_url:
        engine = _safe(lambda: create_engine(db_url, pool_pre_ping=True))
        if isinstance(engine, str):
            payload["postgres"] = engine
        else:
            def _tables() -> dict[str, Any]:
                from sqlalchemy import text

                with engine.connect() as conn:
                    runs = conn.execute(
                        text(
                            "SELECT status, count(*) FROM agent_runs "
                            "GROUP BY status ORDER BY status"
                        )
                    ).all()
                    stale = conn.execute(
                        text(
                            "SELECT id, thread_id, status, attempt, worker_id, updated_at "
                            "FROM agent_runs "
                            "WHERE status NOT IN ('SUCCEEDED','FAILED','DEAD_LETTER','CANCELLED') "
                            "ORDER BY updated_at"
                        )
                    ).mappings().all()
                    ckpt = conn.execute(
                        text(
                            "SELECT count(*) AS c FROM checkpoints"
                        )
                    ).scalar()
                    writes = conn.execute(
                        text("SELECT count(*) AS c FROM checkpoint_writes")
                    ).scalar()
                return {
                    "runs_by_status": {str(r[0]): int(r[1]) for r in runs},
                    "non_terminal_runs": [dict(r) for r in stale],
                    "checkpoints_rows": int(ckpt or 0),
                    "checkpoint_writes_rows": int(writes or 0),
                }

            payload["postgres"] = _safe(_tables, {})
            with contextlib.suppress(Exception):
                engine.dispose()

    if extra:
        payload["extra"] = extra

    try:
        root = Path(artifact_root or os.getenv("RUNTIME_DIAGNOSTICS_DIR", DEFAULT_ARTIFACT_ROOT))
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_node = nodeid.replace("/", "_").replace("::", "_")[:120]
        # ``phase`` must be part of the name: the at-failure and post-teardown
        # snapshots are written within the same second, so a name built only from
        # nodeid+timestamp makes the second write silently overwrite the first and
        # lose the failure-time evidence.
        path = root / f"suite-{phase}-{outcome}-{safe_node}-{stamp}.json"
        path.write_text(
            json.dumps(_redact(payload), indent=2, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
        return path
    except Exception as dump_exc:  # noqa: BLE001
        if os.getenv("CSAI_DIAG_VERBOSE"):
            print(f"[suite-diagnostics] dump failed: {dump_exc}", flush=True)
        return None
