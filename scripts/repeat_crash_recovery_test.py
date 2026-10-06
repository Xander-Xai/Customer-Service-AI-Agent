#!/usr/bin/env python3
"""Repeat a real-infrastructure test N times and keep every iteration's evidence.

Why
---
``test_worker_crash_resumes_from_postgres_checkpoint`` was recorded as flaky
("1 failure in 7 real-infra suite runs") with no way to tell a real runtime
defect from a bad test precondition. A single CI run cannot answer that: the
questions are *how often* and *what did the failing iteration actually look
like*. This harness answers both, and it is deliberately the only supported way
to measure the rate — running the test once proves nothing.

It must not be run concurrently with another crash-recovery test. All of them
``DELETE`` the Redis broker's **global** ``unacked`` / ``unacked_index``
bookkeeping, so two of them in flight destroy each other's in-flight delivery
and produce failures that have nothing to do with the code under test.

What it records
---------------
Per iteration (``iter-0001.json``): pass/fail, duration, pytest exit status,
the failure tail, and any diagnostics artifact the test wrote.
Aggregated (``summary.json`` / ``summary.md``): pass/fail counts, failure rate,
duration percentiles, per-iteration git SHA and dirty flag, and the list of
iterations that produced a diagnostics artifact.

``tested_code_sha`` is recorded per iteration *and* the summary refuses to call
a run clean if the tree was dirty — a 50/50 pass measured on uncommitted code is
not evidence about that code.

Usage::

    TEST_DISTRIBUTED_DB_URL=postgresql://... TEST_REDIS_URL=redis://... \\
        python3 scripts/repeat_crash_recovery_test.py --runs 50

    # a different target
    python3 scripts/repeat_crash_recovery_test.py \\
        --target tests/integration/runtime/test_tool_idempotency.py --runs 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TARGET = "tests/integration/runtime/test_worker_checkpoint_recovery.py"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "flake-investigation"

#: Anything slower than this per iteration means the harness is pointed at the
#: wrong target; failing loudly beats reporting a "0% flake rate" that only
#: measured a hang.
MAX_ITERATION_SECONDS = 900


def _require_infra() -> None:
    missing = [
        name
        for name, value in (
            ("TEST_DISTRIBUTED_DB_URL", os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()),
            ("TEST_REDIS_URL", os.getenv("TEST_REDIS_URL", "").strip()),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"缺少环境变量：{', '.join(missing)}（需要真实 PostgreSQL + Redis；"
            "缺失时不静默 skip）"
        )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    ).stdout.strip()


def _code_is_dirty(artifact_root: Path) -> tuple[bool, list[str]]:
    """Is the **code** dirty? Ignore this harness's and the suite's own output.

    ``artifacts/`` is not gitignored (only ``artifacts/evidence/`` is), so writing
    the harness report — or the failure diagnostics that
    ``tests/integration/runtime/conftest.py`` emits — would make every iteration
    look dirty and render ``clean_tree_throughout`` useless. What matters for a
    measurement is whether the code under test differs from the recorded SHA, so
    paths under the artifact roots are excluded. Everything else counts.

    Comparison is done in **repo-relative** form on both sides. ``git status``
    reports relative paths while ``--out-root`` may be given either way, so
    resolving only one side makes the exclusion silently never match — and a
    50-iteration run then reports ``clean_tree_throughout: false`` for the
    harness's own output.
    """
    repo = REPO_ROOT.resolve()
    roots = {
        str((artifact_root if artifact_root.is_absolute() else repo / artifact_root).resolve()),
        str((REPO_ROOT / "artifacts" / "runtime-diagnostics").resolve()),
    }
    entries = _git("status", "--porcelain").splitlines()
    offending: list[str] = []
    for line in entries:
        raw = line[3:].strip().strip('"')
        absolute = (repo / raw).resolve()
        if any(str(absolute) == root or str(absolute).startswith(root + os.sep) for root in roots):
            continue
        offending.append(line)
    return bool(offending), offending


def _redact_url(url: str) -> str:
    return re.sub(r"//[^@/]+@", "//***@", url)


def _new_diagnostics() -> Path | None:
    """The directory the runtime suite writes failure diagnostics into."""
    root = Path(
        os.getenv("RUNTIME_DIAGNOSTICS_DIR", REPO_ROOT / "artifacts" / "runtime-diagnostics")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_iteration(
    *,
    index: int,
    target: str,
    timeout: float,
    pytest_args: list[str],
    iteration_dir: Path,
    artifact_root: Path,
) -> dict[str, object]:
    diag_root = _new_diagnostics()
    before = {p.name for p in diag_root.glob("*.json")}

    env = dict(os.environ)
    env["RUNTIME_DIAGNOSTICS_DIR"] = str(diag_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    cmd = [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", *pytest_args]
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    duration = time.perf_counter() - started
    combined = (proc.stdout or "") + (proc.stderr or "")

    after = {p.name for p in diag_root.glob("*.json")}
    produced = sorted(after - before)
    for name in produced:
        shutil.copy2(diag_root / name, iteration_dir / name)

    code_dirty, dirty_paths = _code_is_dirty(artifact_root)

    tail = combined.strip().splitlines()[-40:]
    return {
        "index": index,
        "target": target,
        "passed": proc.returncode == 0,
        "exit_code": proc.returncode,
        "duration_seconds": round(duration, 2),
        "timed_out": False,
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": code_dirty,
        "git_dirty_paths": dirty_paths,
        "diagnostics_artifacts": produced,
        "output_tail": tail,
    }


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def _pct(p: float) -> float:
        if len(ordered) == 1:
            return round(ordered[0], 2)
        idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
        return round(ordered[idx], 2)

    return {
        "min": round(ordered[0], 2),
        "p50": _pct(0.50),
        "p95": _pct(0.95),
        "max": round(ordered[-1], 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument(
        "--timeout",
        type=float,
        default=MAX_ITERATION_SECONDS,
        help="per-iteration wall-clock limit in seconds (default 900)",
    )
    parser.add_argument("--out-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument(
        "--stop-after-failures",
        type=int,
        default=0,
        help="stop early after this many failures (0 = run all iterations)",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="extra args forwarded to pytest, e.g. -k test_worker_crash",
    )
    args = parser.parse_args()

    _require_infra()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = Path(args.out_root) / f"repeat-{stamp}"
    iterations_dir = outdir / "iterations"
    iterations_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("pgrep") and _pgrep_running_lookalike():
        print(
            "[warn] 检测到可能仍在运行的 crash-recovery 相关进程；"
            "这些用例会互相摧毁 broker 的 unacked 记账，结果不可信。",
            flush=True,
        )

    iterations: list[dict[str, object]] = []
    started = time.perf_counter()
    failures = 0
    for index in range(1, args.runs + 1):
        iter_dir = iterations_dir / f"iter-{index:04d}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        try:
            record = _run_iteration(
                index=index,
                target=args.target,
                timeout=args.timeout,
                pytest_args=args.pytest_args,
                iteration_dir=iter_dir,
                artifact_root=outdir,
            )
        except subprocess.TimeoutExpired as exc:
            record = {
                "index": index,
                "target": args.target,
                "passed": False,
                "exit_code": None,
                "duration_seconds": args.timeout,
                "timed_out": True,
                "git_sha": _git("rev-parse", "HEAD"),
                "git_dirty": True,
                "git_dirty_paths": ["<iteration timed out>"],
                "diagnostics_artifacts": [],
                "output_tail": [
                    f"iteration exceeded {args.timeout}s and was killed"
                    for _ in (exc.stdout or [])[-1:]
                ],
            }
        iterations.append(record)
        (iter_dir / "result.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag = "PASS" if record["passed"] else "FAIL"
        print(
            f"[{index:>3}/{args.runs}] {flag} {record['duration_seconds']:>7.2f}s"
            f"  sha={record['git_sha'][:8]} dirty={record['git_dirty']}"
            f"  artifacts={len(record['diagnostics_artifacts'])}",
            flush=True,
        )
        if not record["passed"]:
            failures += 1
            for line in (record["output_tail"] or [])[-8:]:
                print(f"        {line}", flush=True)
            if args.stop_after_failures and failures >= args.stop_after_failures:
                print(f"[stop] 达到 {failures} 次失败，停止。", flush=True)
                break

    passed = sum(1 for r in iterations if r["passed"])
    total = len(iterations)
    dirty_iterations = [r["index"] for r in iterations if r["git_dirty"]]
    shas = sorted({str(r["git_sha"]) for r in iterations})
    summary = {
        "schema": "crash-recovery-repeat/v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": args.target,
        "pytest_args": args.pytest_args,
        "requested_runs": args.runs,
        "executed_runs": total,
        "passed": passed,
        "failed": total - passed,
        "failure_rate": round((total - passed) / total, 4) if total else None,
        "timeout_seconds_per_iteration": args.timeout,
        "db_url": _redact_url(os.getenv("TEST_DISTRIBUTED_DB_URL", "")),
        "redis_url": _redact_url(os.getenv("TEST_REDIS_URL", "")),
        "duration_seconds": _percentiles([float(r["duration_seconds"]) for r in iterations]),
        "total_wall_seconds": round(time.perf_counter() - started, 2),
        "git_shas_observed": shas,
        "single_sha": len(shas) == 1,
        "dirty_iterations": dirty_iterations,
        "clean_tree_throughout": not dirty_iterations,
        "dirty_paths_seen": sorted(
            {
                path
                for r in iterations
                for path in (r.get("git_dirty_paths") or [])
                if not str(path).startswith("<")
            }
        ),
        "dirty_paths_note": (
            "paths under this run's own artifact directory are excluded; "
            "'artifacts/' is not gitignored, so the harness would otherwise "
            "report every iteration as dirty"
        ),
        "failed_iterations": [
            {
                "index": r["index"],
                "duration_seconds": r["duration_seconds"],
                "git_sha": r["git_sha"],
                "timed_out": r["timed_out"],
                "diagnostics_artifacts": r["diagnostics_artifacts"],
                "output_tail": r["output_tail"],
            }
            for r in iterations
            if not r["passed"]
        ],
        "iterations_with_diagnostics": [
            r["index"] for r in iterations if r["diagnostics_artifacts"]
        ],
        "artifacts_dir": str(outdir),
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (outdir / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


def _pgrep_running_lookalike() -> bool:
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, check=False
        ).stdout
    except Exception:  # noqa: BLE001 - advisory only
        return False
    for line in out.splitlines():
        if "celery_worker_runner" in line or "test_worker_crash_recovery" in line:
            return True
    return False


def _render_markdown(summary: dict) -> str:
    lines = [
        "# Crash-recovery repeat run",
        "",
        f"- schema: `{summary['schema']}`",
        f"- target: `{summary['target']}`",
        f"- generated: `{summary['generated_at']}`",
        f"- git SHA(s): {', '.join(summary['git_shas_observed']) or 'unknown'}",
        f"- clean tree throughout: **{summary['clean_tree_throughout']}**",
        f"- executed: **{summary['executed_runs']}** / requested {summary['requested_runs']}",
        f"- passed: **{summary['passed']}**, failed: **{summary['failed']}**"
        f" (failure rate {summary['failure_rate']})",
        f"- duration seconds: {summary['duration_seconds']}",
        f"- total wall time: {summary['total_wall_seconds']}s",
        f"- artifacts: `{summary['artifacts_dir']}`",
        "",
    ]
    if summary["failed_iterations"]:
        lines += ["## Failed iterations", ""]
        for item in summary["failed_iterations"]:
            lines += [
                f"### iteration {item['index']}",
                "",
                f"- duration: {item['duration_seconds']}s",
                f"- git SHA: {item['git_sha']}",
                f"- timed out: {item['timed_out']}",
                f"- diagnostics artifacts: {item['diagnostics_artifacts'] or 'none'}",
                "",
                "```",
                *(item["output_tail"] or []),
                "```",
                "",
            ]
    else:
        lines += ["## Failed iterations", "", "None.", ""]
    if not summary["clean_tree_throughout"]:
        lines += [
            "## Caveat",
            "",
            "Some iterations ran against a dirty working tree, so this run does "
            "**not** attest to a single commit. Re-run on a clean tree before "
            "treating the rate as evidence.",
            "",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
