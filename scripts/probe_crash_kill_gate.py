#!/usr/bin/env python3
"""Probe the kill gate of ``test_worker_crash_resumes_from_postgres_checkpoint``.

Why this exists
---------------
That test is the source of the recorded "1 failure in 7 real-infra suite runs".
Its SIGKILL is gated on::

    _wait_for(lambda: checkpoint_rows() >= 1, 20)   # count(*) over the thread

``count(*) >= 1`` is satisfied by the *input* checkpoint, i.e. the row LangGraph
writes before any node runs. The invariant the test actually depends on is much
stronger: by the time ``second`` increments ``ckpt:node_second``, the checkpoint
carrying ``first``'s super-step result must already be **committed**, otherwise
recovery restarts from the input checkpoint, re-runs ``first`` and the
``first_after == 1`` assertion fails.

Between the two there is a real window: LangGraph hands each super-step
checkpoint to a ``BackgroundExecutor`` without awaiting the commit
(``langgraph/pregel/_loop.py``: *"save it, without blocking"*), so a node can
start before the previous super-step's checkpoint is durable.

This probe measures, over many short cycles, **how often the test's existing
gate admits a SIGKILL while that invariant is still false**. It does not attempt
to make the full test fail, and it does not modify any runtime behaviour: it
replays the same start → wait → gate → kill sequence and then *reads* the
committed checkpoint state at the kill instant.

What a cycle records
--------------------
``gate_satisfied``     the value of the test's own ``checkpoint_rows()`` predicate
``durable_has_first``  whether the newest committed checkpoint's ``channel_values``
                       already contains ``branch:to:second`` (first's result)
``kill_before_durable``  ``gate_satisfied and not durable_has_first`` — the exact
                       configuration in which the unmodified test fails

Because a kill before durability deterministically forces a re-run of ``first``,
``kill_before_durable`` is a direct, cheap estimator of the flake rate; it costs
one worker start per cycle instead of a full crash → visibility-timeout →
redelivery → completion cycle.

Usage::

    TEST_DISTRIBUTED_DB_URL=postgresql://... TEST_REDIS_URL=redis://... \\
        python3 scripts/probe_crash_kill_gate.py --cycles 60

Artifacts: ``artifacts/flake-investigation/killgate-<ts>/{summary.json,cycle-*.json}``
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

#: Mirrors ``tests/integration/runtime/test_worker_checkpoint_recovery.py``.
#: Kept as literals (not imported) so this probe keeps measuring *main's* gate
#: even after that test is hardened — otherwise the probe would silently follow
#: the fix it is supposed to be evidence for.
PROBE_POLL_SECONDS = 0.3
PROBE_GATE_TIMEOUT = 20.0
PROBE_ENTER_TIMEOUT = 60.0
PROBE_VISIBILITY_TIMEOUT = 5
PROBE_LEASE_SECONDS = 3
PROBE_BLOCK_SECONDS = 25

#: Channel that only appears in ``channel_values`` once ``first``'s super-step has
#: been committed. ``branch:to:first`` is written by the START -> first edge, so it
#: cannot be used as the durability marker.
DURABLE_FIRST_CHANNEL = "branch:to:second"

_LATEST_CHECKPOINT_SQL = (
    "SELECT checkpoint_id, checkpoint FROM checkpoints "
    "WHERE thread_id = :t ORDER BY checkpoint_id DESC LIMIT 1"
)
_COUNT_SQL = "SELECT count(*) FROM checkpoints WHERE thread_id = :t"
_PENDING_WRITES_SQL = (
    "SELECT checkpoint_id, channel, type FROM checkpoint_writes "
    "WHERE thread_id = :t ORDER BY checkpoint_id"
)


def _require_infra() -> None:
    missing = [
        name
        for name, value in (
            ("TEST_DISTRIBUTED_DB_URL", DB_URL),
            ("TEST_REDIS_URL", REDIS_URL),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"缺少环境变量：{', '.join(missing)}（需要真实 PostgreSQL + Redis；"
            "缺失时不静默 skip，避免把『没跑』记成『通过』）"
        )


def _wait_for(predicate, timeout: float):
    """Byte-for-byte the polling shape of the test under investigation."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(PROBE_POLL_SECONDS)
    return predicate()


def _worker_env(queue: str, key_prefix: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": DB_URL,
            "LANGGRAPH_CHECKPOINT_DATABASE_URL": DB_URL,
            "REDIS_URL": REDIS_URL,
            "DEV_MODE": "true",
            "AGENT_RUN_DISPATCH": "celery",
            "AGENT_RUN_QUEUE": queue,
            "AGENT_RUN_VISIBILITY_TIMEOUT": str(PROBE_VISIBILITY_TIMEOUT),
            "AGENT_RUN_LEASE_SECONDS": str(PROBE_LEASE_SECONDS),
            "AGENT_RUN_HEARTBEAT_SECONDS": "1",
            "AGENT_RUN_THREAD_LOCK_TTL_SECONDS": str(PROBE_LEASE_SECONDS),
            "AGENT_RUN_THREAD_LOCK_BACKEND": "redis",
            "AGENT_RUN_RUNTIME_PROVIDER": "tests.integration.checkpoint_resume_provider:provide",
            "CHECKPOINT_RESUME_KEY_PREFIX": key_prefix,
            "CKPT_BLOCK_SECONDS": str(PROBE_BLOCK_SECONDS),
            "LANGGRAPH_CHECKPOINT_BACKEND": "postgres",
        }
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _start_worker(env: dict[str, str], log_path: str) -> subprocess.Popen:
    log = open(log_path, "w")  # noqa: SIM115 - closed by _stop_worker
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.celery_worker_runner"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._csai_log = log  # type: ignore[attr-defined]
    return proc


def _stop_worker(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)
    log = getattr(proc, "_csai_log", None)
    if log is not None:
        with contextlib.suppress(Exception):
            log.close()


def _checkpoint_count(engine, thread_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(_COUNT_SQL), {"t": thread_id}).scalar() or 0)


def _commit_state(engine, thread_id: str) -> dict[str, object]:
    """Read the newest **committed** checkpoint for a thread."""
    with engine.connect() as conn:
        count = int(
            conn.execute(text(_COUNT_SQL), {"t": thread_id}).scalar() or 0
        )
        latest = conn.execute(text(_LATEST_CHECKPOINT_SQL), {"t": thread_id}).mappings().first()
        writes = conn.execute(text(_PENDING_WRITES_SQL), {"t": thread_id}).all()

    if latest is None:
        return {
            "checkpoint_count": count,
            "checkpoint_id": None,
            "channels": [],
            "durable_has_first_result": False,
            "pending_writes": [],
        }
    values = ((latest["checkpoint"] or {}).get("channel_values")) or {}
    channels = sorted(values)
    return {
        "checkpoint_count": count,
        "checkpoint_id": latest["checkpoint_id"],
        "channels": channels,
        "durable_has_first_result": DURABLE_FIRST_CHANNEL in values,
        "pending_writes": [
            {"checkpoint_id": r[0], "channel": r[1], "type": r[2]} for r in writes
        ],
    }


def run_cycle(
    engine,
    redis_client,
    index: int,
    workdir: Path,
    *,
    tail_on_hit: bool = False,
    kill_immediately: bool = False,
) -> dict[str, object]:
    tag = uuid.uuid4().hex[:8]
    run_id = f"killgate-{tag}"
    thread_id = f"T-killgate-{tag}"
    queue = f"agent_runs_test_{tag}"
    key_prefix = f"ckpt:{tag}"
    ckpt_keys = (
        f"{key_prefix}:node_first",
        f"{key_prefix}:node_second",
        f"{key_prefix}:trace",
        f"{key_prefix}:recovered",
    )

    redis_client.delete("unacked", "unacked_index", queue, *ckpt_keys)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs "
                "(id, thread_id, session_id, status, query, attempt, max_attempts, "
                " created_at, queued_at, updated_at) "
                "VALUES (:id, :tid, :tid, 'QUEUED', 'killgate-probe', 0, 3, "
                "now(), now(), now())"
            ),
            {"id": run_id, "tid": thread_id},
        )

    from celery import Celery

    Celery(broker=REDIS_URL).send_task("runtime.execute_agent_run", args=[run_id], queue=queue)

    worker = None
    worker_b = None
    record: dict[str, object] = {
        "index": index,
        "tag": tag,
        "run_id": run_id,
        "thread_id": thread_id,
        "queue": queue,
    }
    t0 = time.perf_counter()
    try:
        worker = _start_worker(
            _worker_env(queue, key_prefix), str(workdir / f"cycle-{index:03d}.worker.log")
        )
        entered = _wait_for(
            lambda: int(redis_client.get(f"{key_prefix}:node_second") or 0) >= 1,
            PROBE_ENTER_TIMEOUT,
        )
        record["entered_second"] = bool(entered)
        if not entered:
            record["error"] = "worker never entered the second node"
            return record
        record["t_entered_second_s"] = round(time.perf_counter() - t0, 4)

        # --- the test's own gate, verbatim ---------------------------------
        if kill_immediately:
            # Maximum-race configuration: skip the gate entirely and kill the
            # instant ``second`` is observed. Used to (a) smoke-test the recovery
            # tail deterministically and (b) bound how wide the durability
            # window actually is. Not the test's behaviour — the test does run
            # the gate checks.
            record["gate_satisfied"] = True
            record["gate_skipped"] = True
        else:
            def _gate() -> bool:
                with engine.connect() as conn:
                    count = int(conn.execute(text(_COUNT_SQL), {"t": thread_id}).scalar() or 0)
                return count >= 1

            gate = _wait_for(_gate, PROBE_GATE_TIMEOUT)
            record["gate_satisfied"] = bool(gate)
        record["gate_checkpoint_count"] = _checkpoint_count(engine, thread_id)

        record["first_before"] = int(redis_client.get(f"{key_prefix}:node_first") or 0)
        with engine.connect() as conn:
            running = conn.execute(
                text("SELECT status, worker_id FROM agent_runs WHERE id=:id"), {"id": run_id}
            ).fetchone()
        record["running_row"] = list(running) if running is not None else None

        # --- durability at the kill instant --------------------------------
        # Read *immediately before* the SIGKILL. This read is itself a couple of
        # milliseconds of latency, and LangGraph commits super-step checkpoints
        # on a background executor, so a "not yet durable" reading here does NOT
        # prove the commit was lost. The post-kill read below closes that gap:
        # if the newest checkpoint gains first's channel between the two reads,
        # the commit landed inside the kill window and nothing was lost.
        pre = _commit_state(engine, thread_id)
        record.update({f"pre_{k}": v for k, v in pre.items()})
        record["durable_has_first_result"] = pre["durable_has_first_result"]
        record["checkpoint_count"] = pre["checkpoint_count"]
        record["channels"] = pre["channels"]
        record["t_kill_s"] = round(time.perf_counter() - t0, 4)

        _stop_worker(worker)
        worker = None
        record["t_postkill_read_s"] = round(time.perf_counter() - t0, 4)

        post = _commit_state(engine, thread_id)
        record["post_checkpoint_count"] = post["checkpoint_count"]
        record["post_channels"] = post["channels"]
        record["post_durable_has_first_result"] = post["durable_has_first_result"]

        record["gate_admitted_kill"] = bool(
            record["gate_satisfied"] and not pre["durable_has_first_result"]
        )
        # Classify the "hit": did the commit land inside the kill window (a
        # measurement artifact of this probe) or was it genuinely absent?
        record["commit_landed_during_kill_window"] = bool(
            not pre["durable_has_first_result"] and post["durable_has_first_result"]
        )
        record["commit_absent_after_kill"] = bool(
            not pre["durable_has_first_result"] and not post["durable_has_first_result"]
        )
        # Kept as the original headline metric so earlier artifacts stay
        # comparable; see ``gate_admitted_kill`` for the honest interpretation.
        record["kill_before_durable"] = record["gate_admitted_kill"]

        if not (tail_on_hit and record["gate_admitted_kill"]):
            return record

        # --- full recovery tail, only for a caught race ---------------------
        # A kill before durability is *deterministically* fatal to the test's
        # assertions: recovery resumes from the input checkpoint, so ``first``
        # runs a second time. Running the real tail here turns the cheap
        # detector into an end-to-end reproducer, and costs the ~30s recovery
        # only on the cycles that actually hit the window.
        record["tail"] = _run_recovery_tail(
            engine, redis_client, thread_id, run_id, key_prefix, workdir, index
        )
        record["tail"]["worker_b_started"] = True
        worker_b = _start_worker(
            _worker_env(queue, key_prefix), str(workdir / f"cycle-{index:03d}.worker-b.log")
        )
        record["tail"].update(
            _await_recovery(engine, redis_client, thread_id, run_id, key_prefix)
        )
        return record
    finally:
        _stop_worker(worker)
        _stop_worker(worker_b)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id})
            conn.execute(
                text("DELETE FROM agent_dead_letters WHERE run_id=:id"), {"id": run_id}
            )
        redis_client.delete(*ckpt_keys, queue, "unacked", "unacked_index")


def _run_recovery_tail(engine, redis_client, thread_id, run_id, key_prefix, workdir, index):
    """Wait out the visibility timeout before the recovering worker starts."""
    time.sleep(PROBE_VISIBILITY_TIMEOUT + 3)
    return {"visibility_timeout_elapsed_s": PROBE_VISIBILITY_TIMEOUT + 3}


def _await_recovery(engine, redis_client, thread_id, run_id, key_prefix) -> dict[str, object]:
    """Mirror the test's post-recovery waits and record the outcome verbatim."""

    def _row():
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT status, attempt, worker_id, result FROM agent_runs WHERE id=:id"
                ),
                {"id": run_id},
            ).fetchone()

    final = _wait_for(
        lambda: (
            (r := _row()) is not None and r[0] in ("SUCCEEDED", "FAILED", "DEAD_LETTER") and r
        ),
        150,
    )
    out: dict[str, object] = {"final_state": list(final) if final is not None else None}
    out["first_after"] = int(redis_client.get(f"{key_prefix}:node_first") or 0)
    out["second_after"] = int(redis_client.get(f"{key_prefix}:node_second") or 0)
    out["recovered"] = int(redis_client.get(f"{key_prefix}:recovered") or 0)
    out["trace"] = redis_client.lrange(f"{key_prefix}:trace", 0, -1) or []
    out["checkpoint_state_after"] = _commit_state(engine, thread_id)
    # Exactly the two assertions that break when the kill precedes durability.
    out["assertion_first_after_equals_1"] = out["first_after"] == 1
    out["assertion_recovered_at_least_1"] = out["recovered"] >= 1
    out["test_would_pass"] = bool(
        final is not None
        and final[0] == "SUCCEEDED"
        and out["assertion_first_after_equals_1"]
        and out["second_after"] >= 2
        and out["assertion_recovered_at_least_1"]
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument(
        "--tail-on-hit",
        action="store_true",
        help=(
            "when a cycle lands the kill before first's checkpoint is committed, "
            "run the full visibility-timeout + redelivery + recovery tail and record "
            "the resulting assertions (turns the detector into an end-to-end reproducer)"
        ),
    )
    parser.add_argument(
        "--kill-immediately",
        action="store_true",
        help=(
            "maximum-race diagnostic: skip the gate checks and SIGKILL the instant "
            "'second' is observed, instead of replaying the test's gate sequence. "
            "Not the test's behaviour; used to bound the durability window and to "
            "smoke-test the recovery tail deterministically."
        ),
    )
    parser.add_argument(
        "--stop-after-first-repro",
        action="store_true",
        help="with --tail-on-hit: stop as soon as one cycle reproduces the failure",
    )
    parser.add_argument(
        "--out-root",
        default=str(REPO_ROOT / "artifacts" / "flake-investigation"),
        help="artifact root (a killgate-<ts>/ subdirectory is created under it)",
    )
    args = parser.parse_args()
    _require_infra()

    import redis as redis_lib
    from sqlalchemy import create_engine

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = Path(args.out_root) / f"killgate-{stamp}"
    workdir = outdir / "logs"
    workdir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(DB_URL, pool_pre_ping=True)
    redis_client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)

    cycles: list[dict[str, object]] = []
    repros: list[dict[str, object]] = []
    started = time.perf_counter()
    for index in range(1, args.cycles + 1):
        record = run_cycle(
            engine,
            redis_client,
            index,
            workdir,
            tail_on_hit=args.tail_on_hit,
            kill_immediately=args.kill_immediately,
        )
        cycles.append(record)
        (outdir / f"cycle-{index:03d}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag = "KILL-BEFORE-DURABLE" if record.get("kill_before_durable") else "durable"
        print(
            f"[{index:>3}/{args.cycles}] gate={record.get('gate_satisfied')} "
            f"durable={record.get('durable_has_first_result')} "
            f"count={record.get('checkpoint_count')} {flag}",
            flush=True,
        )
        tail = record.get("tail")
        if isinstance(tail, dict) and tail.get("test_would_pass") is False:
            repros.append(record)
            print(
                f"      REPRODUCED: first_after={tail.get('first_after')} "
                f"recovered={tail.get('recovered')} trace={tail.get('trace')}",
                flush=True,
            )
            if args.stop_after_first_repro:
                break

    measured = [c for c in cycles if "kill_before_durable" in c]
    hits = [c for c in measured if c["kill_before_durable"]]
    gate_unreached = [c for c in measured if not c["gate_satisfied"]]
    landed_in_window = [c for c in hits if c.get("commit_landed_during_kill_window")]
    absent_after_kill = [c for c in hits if c.get("commit_absent_after_kill")]
    summary = {
        "schema": "crash-kill-gate-probe/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        ).stdout.strip(),
        "git_dirty": bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            ).stdout.strip()
        ),
        "langgraph_version": _langgraph_version(),
        "cycles_requested": args.cycles,
        "cycles_measured": len(measured),
        "cycles_gate_unreached": len(gate_unreached),
        "kill_before_durable_cycles": len(hits),
        "kill_before_durable_rate": round(len(hits) / len(measured), 4) if measured else None,
        "kill_before_durable_interpretation": (
            "COUNTING ARTIFACT UNLESS END-END REPRODUCED: this metric is read "
            "immediately BEFORE the SIGKILL, and LangGraph commits super-step "
            "checkpoints on a background executor, so a not-yet-visible reading "
            "here does not mean the commit was lost. "
            "'commit_landed_during_kill_window' counts how many of these hits the "
            "post-kill read shows were commits landing inside the measurement gap. "
            "Only 'end_to_end_reproductions' is ground truth."
        ),
        "hits_with_commit_landed_in_kill_window": len(landed_in_window),
        "hits_with_commit_absent_after_kill": len(absent_after_kill),
        "tail_on_hit": bool(args.tail_on_hit),
        "kill_immediately": bool(args.kill_immediately),
        "end_to_end_reproductions": len(repros),
        "repro_cycles": [c["index"] for c in repros],
        "durable_first_channel": DURABLE_FIRST_CHANNEL,
        "replicates_gate_from": (
            "tests/integration/runtime/test_worker_checkpoint_recovery.py"
            "@617cbe0 (count(*) >= 1)"
        ),
        "probe_poll_seconds": PROBE_POLL_SECONDS,
        "duration_seconds": round(time.perf_counter() - started, 2),
        "artifacts_dir": str(outdir),
        "verdict": _verdict(len(repros), len(measured), len(landed_in_window)),
        "hit_cycles": [c["index"] for c in hits],
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _langgraph_version() -> str:
    try:
        from importlib.metadata import version

        return version("langgraph")
    except Exception:  # noqa: BLE001 - provenance only
        return "unknown"


def _verdict(repros: int, measured: int, landed_in_window: int = 0) -> str:
    """Ground-truth verdict.

    A gate-admitted kill is *not* a demonstrated race. LangGraph persists
    super-step checkpoints on a background executor it does not await, so the
    "input checkpoint only" state is observable for a few milliseconds before
    the commit lands. Only a full redelivery + recovery cycle that actually
    re-runs ``first`` demonstrates the race end to end.
    """
    if not measured:
        return "NOT_MEASURED"
    if repros:
        return "RACE_REPRODUCED_END_TO_END"
    if landed_in_window:
        return (
            "H1_FALSIFIED_commit_lands_in_kill_window"
            f" ({landed_in_window} hit(s) recovered without re-running 'first')"
        )
    return "NO_HIT"
    # NOTE: a bare "NOT_REPRODUCED" is reserved for the case where the gate never
    # admitted a pre-durability kill at all, i.e. the hypothesis was never even
    # exercised. Distinguishing it from a falsified hypothesis matters: the first
    # means "untested", the second means "tested and wrong".


if __name__ == "__main__":
    raise SystemExit(main())
