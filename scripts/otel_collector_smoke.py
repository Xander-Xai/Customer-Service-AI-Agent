"""Run the live OTLP Collector smoke end to end, with reliable teardown.

Sequence (one command, ``make otel-collector-smoke``):

1. Refuse to run if the host OTLP ports are already taken by something that is
   not ours. This script never kills another process to free a port.
2. ``docker compose up -d`` the traces-only Collector.
3. Wait for the Collector's own zpages ``servicez`` endpoint to answer 200
   ("Everything is ready"), plus the container reporting ``running``. No
   ``sleep``-and-assume.
4. Emit the semantic spans from a **separate application Python process** using
   ``core.tracing.setup_tracing`` + ``core.telemetry.span``, then flush via the
   SDK lifecycle.
5. Capture the Collector's own stdout and assert it actually received the spans.
6. Write a provenance-bearing evidence artifact.
7. Stop the Collector in a ``finally`` block — including on exception and
   ``KeyboardInterrupt`` — touching only the container this script started.

Exit code is non-zero unless every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "docker-compose.otel.yml"
SERVICE = "otel-collector"
CONTAINER = "customer-service-otel-collector"
SCRIPT_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPT_DIR))
import verify_otel_collector as verifier  # noqa: E402

READY_TIMEOUT_S = 90
POLL_INTERVAL_S = 1.0


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _preflight_ports(grpc_port: int, http_port: int, zpages_port: int) -> list[str]:
    """Refuse to touch ports owned by something else. Never kill a user process."""
    busy = [str(p) for p in (grpc_port, http_port, zpages_port) if not _port_free(p)]
    if busy:
        return [
            f"host port(s) {', '.join(busy)} already in use by another process. "
            f"This script will not kill it. Re-run with "
            f"OTEL_COLLECTOR_GRPC_PORT / OTEL_COLLECTOR_HTTP_PORT set to free ports."
        ]
    return []


def _container_health() -> str | None:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    value = proc.stdout.strip()
    return value or None


def _servicez_ok(zpages_port: int, timeout_s: float = 2.0) -> bool:
    """Real readiness: the Collector's own zpages servicez endpoint returns 200.

    This is the condition the Collector itself reports once every receiver and
    exporter has started ("Everything is ready"), so it is a fact about the
    process rather than an assumption about elapsed time.
    """
    url = f"http://127.0.0.1:{zpages_port}/debug/servicez"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - fixed localhost
            return resp.status == 200
    except Exception:  # noqa: BLE001 - not ready yet
        return False


def _wait_ready(deadline_s: int, zpages_port: int) -> tuple[bool, str]:
    """Poll container state + the collector's readiness endpoint. Not a sleep."""
    end = time.monotonic() + deadline_s
    last = "absent"
    while time.monotonic() < end:
        last = _container_health() or "none"
        if last in {"exited", "dead"}:
            return False, last
        if last == "running" and _servicez_ok(zpages_port):
            return True, "servicez-200"
        time.sleep(POLL_INTERVAL_S)
    return False, last


def _collector_version() -> str:
    proc = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/otelcol", verifier_image(), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout.strip().splitlines()
    return out[0].replace("otelcol version", "").strip() if out else "unknown"


def verifier_image() -> str:
    proc = _compose("config", "--format", "json")
    try:
        cfg = json.loads(proc.stdout)
        return cfg["services"][SERVICE]["image"]
    except Exception:  # noqa: BLE001 - fall back to the declared default
        return "otel/opentelemetry-collector:0.162.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Live OTLP Collector smoke")
    parser.add_argument("--keep-running", action="store_true", help="Do not stop the Collector.")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args()

    grpc_port = int(os.getenv("OTEL_COLLECTOR_GRPC_PORT", "4317"))
    http_port = int(os.getenv("OTEL_COLLECTOR_HTTP_PORT", "4318"))
    zpages_port = int(os.getenv("OTEL_COLLECTOR_ZPAGES_PORT", "55679"))
    service_name = os.getenv("OTEL_SERVICE_NAME", "csai-otel-smoke")
    endpoint = f"http://127.0.0.1:{grpc_port}"

    if not shutil.which("docker"):
        print("FAIL: docker is not available", file=sys.stderr)
        return 2

    problems = _preflight_ports(grpc_port, http_port, zpages_port)
    if problems:
        print("BLOCKED: " + " ".join(problems), file=sys.stderr)
        return 2

    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_dir = REPO_ROOT / "artifacts" / "observability" / f"otel-collector-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)
    logs_path = work_dir / "collector.log"
    flush_path = work_dir / "flush.json"

    started = False
    try:
        up = _compose("up", "-d", SERVICE)
        if up.returncode != 0:
            print(f"FAIL: could not start collector: {up.stderr.strip()}", file=sys.stderr)
            return 2
        started = True

        ready, state = _wait_ready(READY_TIMEOUT_S, zpages_port)
        if not ready:
            print(f"FAIL: collector never became healthy (last state: {state})", file=sys.stderr)
            return 1

        env = {
            **os.environ,
            # Running a script by path puts scripts/ on sys.path, not the repo
            # root, so `import core.telemetry` needs the root explicitly.
            "PYTHONPATH": str(REPO_ROOT),
            # The two switches stay separate, exactly as core/tracing.py documents.
            "OPENTELEMETRY_ENABLED": "true",
            "OTEL_ENABLED": "true",
            "OTEL_SERVICE_NAME": service_name,
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        }
        emit = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "verify_otel_collector.py"),
                "--emit",
                "--flush-out",
                str(flush_path),
                "--service-name",
                service_name,
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if emit.returncode != 0:
            print(f"FAIL: span emission failed: {emit.stderr.strip()}", file=sys.stderr)
            return 1

        logs = _compose("logs", "--no-color", SERVICE)
        logs_path.write_text(logs.stdout + logs.stderr, encoding="utf-8")

        flush = json.loads(flush_path.read_text(encoding="utf-8")) if flush_path.exists() else {}
        report = verifier.build_report(
            collector_logs=logs_path.read_text(encoding="utf-8", errors="replace"),
            flush=flush,
            service_name=service_name,
            endpoint=endpoint,
            protocol="grpc",
            collector_version=_collector_version(),
            collector_image=verifier_image(),
        )
        git_state = verifier._git_state()
        report["git"] = git_state
        report["checks"]["clean_tree"] = git_state.get("dirty") is False
        report["status"] = verifier.finalize_status(report)
        report_path = work_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(
            json.dumps(
                {
                    "report": str(report_path.relative_to(REPO_ROOT)),
                    "status": report["status"],
                    "observed_spans": report["observed_spans"],
                    "missing_spans": report["missing_spans"],
                    "privacy_canary_present": report["privacy_canary_present"],
                    "tested_git_sha": report["git"].get("git_sha"),
                    "clean_tree": report["checks"]["clean_tree"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report["status"] == "VERIFIED_LOCAL" else 1
    finally:
        if started and not args.keep_running:
            # Only ever the container this script started; no `down`, no prune.
            subprocess.run(
                ["docker", "rm", "-f", CONTAINER],
                capture_output=True,
                text=True,
                check=False,
            )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
