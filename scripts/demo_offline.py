#!/usr/bin/env python3
"""One-command, evidence-graded OFFLINE customer-service demo (Issue #120).

Design goals
------------
- **One command**: ``make demo-offline`` (or ``python3 scripts/demo_offline.py``).
- **Reuse, do not rebuild**: it runs the canonical Demo 1 end-to-end test
  anchors (``docs/guides/offline-demo.md``) through the existing graph
  orchestration and the existing MockLLM/ERP fixtures. No new Agent, no website.
- **Offline by construction**: the offline lane providers are ``local`` and a
  hard egress guard (``scripts/offline_egress_guard.py``) makes any real HTTP or
  socket egress fail the run. No API key, no real model.
- **Honest**: the exit status of the underlying pytest run is the exit status of
  this command; a failed or uncollected selection is never reported as PASS.
- **Auditable**: prints a proof card containing scenario, MOCK/OFFLINE marker,
  source/test anchors, git SHA, UTC timestamp, command, verdict and the
  environment limitations that this demo does *not* cover.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

SCENARIO = "Demo 1 — 正常客服路由（产品咨询 / 四层状态机）"

SOURCE_ANCHORS = (
    "core/graph_builder.py::build_graph",
    "router/query_router.py (asyncio.gather(llm, rule))",
    "collaboration/modes.py (5 CollaborationMode)",
)

DEFAULT_NODES = (
    "tests/integration/test_integration.py::TestGraphEndToEnd::test_simple_query_end_to_end",
    "tests/integration/test_integration.py::TestGraphEndToEnd::test_cache_hit_skips_routing",
    "tests/integration/test_integration.py::TestRoutingLogic",
)

ENVIRONMENT_LIMITATIONS = (
    "MOCK/offline: MockLLM + local embedding; NOT a real provider call.",
    "Real provider e2e (API key required): NOT_RUN.",
    "Real ERP write: NOT_VERIFIED (Issue #7).",
    "Production cluster / multi-replica / QPS / P95: NOT_VERIFIED.",
    "Demo 4/5 (durable runtime) need real PostgreSQL + Redis and are out of scope here.",
)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        )
        return out.decode().strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def verdict_for_return_code(return_code: int) -> str:
    """Map a pytest return code to an honest verdict.

    ``0`` -> PASS, ``5`` (no tests collected) -> NOT_RUN, anything else -> FAIL.
    "Did not run" is deliberately not success.
    """
    if return_code == 0:
        return "PASS"
    if return_code == 5:
        return "NOT_RUN"
    return "FAIL"


def build_command(nodes: tuple[str, ...]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *nodes,
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "offline_egress_guard",
    ]


def build_proof_card(
    *,
    verdict: str,
    return_code: int,
    command: list[str],
    git_sha: str,
    timestamp: str,
) -> dict:
    return {
        "scenario": SCENARIO,
        "mode": "MOCK/OFFLINE",
        "source_anchor": list(SOURCE_ANCHORS),
        "test_anchor": list(DEFAULT_NODES),
        "git_sha": git_sha,
        "timestamp_utc": timestamp,
        "test_command": " ".join(command),
        "verdict": verdict,
        "pytest_return_code": return_code,
        "environment_limitations": list(ENVIRONMENT_LIMITATIONS),
    }


def _print_card(card: dict) -> None:
    line = "=" * 72
    print(line)
    print(f"  DEMO PROOF CARD — {card['scenario']}")
    print(line)
    print(f"  Mode           : {card['mode']}")
    print(f"  Verdict        : {card['verdict']}  (pytest rc={card['pytest_return_code']})")
    print(f"  Git SHA        : {card['git_sha']}")
    print(f"  UTC timestamp  : {card['timestamp_utc']}")
    print(f"  Test command   : {card['test_command']}")
    print("  Source anchors :")
    for anchor in card["source_anchor"]:
        print(f"    - {anchor}")
    print("  Test anchors   :")
    for anchor in card["test_anchor"]:
        print(f"    - {anchor}")
    print("  Environment limitations (not covered by this demo):")
    for lim in card["environment_limitations"]:
        print(f"    - {lim}")
    print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-command deterministic OFFLINE customer-service demo."
    )
    parser.add_argument(
        "--node",
        action="append",
        dest="nodes",
        help="Override the pytest node(s) to run (repeatable). Defaults to Demo 1.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the proof card as JSON to this path.",
    )
    args = parser.parse_args(argv)

    nodes = tuple(args.nodes) if args.nodes else DEFAULT_NODES

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SCRIPTS), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )

    command = build_command(nodes)
    print(f"$ {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    rc = completed.returncode
    verdict = verdict_for_return_code(rc)

    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = build_proof_card(
        verdict=verdict,
        return_code=rc,
        command=command,
        git_sha=_git_sha(),
        timestamp=timestamp,
    )
    _print_card(card)

    json_out = args.json_out or str(ROOT / "artifacts" / "demo" / f"offline-{timestamp}.json")
    try:
        out_path = Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"evidence card written: {out_path}")
    except Exception as exc:  # pragma: no cover - artifact writing is best effort
        print(f"warning: could not write evidence card JSON: {exc}", file=sys.stderr)

    # Never turn a failure / no-op into a success.
    return 0 if verdict == "PASS" else (rc if rc != 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())
