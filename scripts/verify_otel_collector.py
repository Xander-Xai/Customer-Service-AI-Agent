"""Verify the repository's existing OpenTelemetry spans against a **live** Collector.

What this proves
----------------
``application Python process -> OpenTelemetry SDK -> OTLPSpanExporter -> real
network hop -> real otel/opentelemetry-collector -> collector actually received
the spans``.

It deliberately reuses the production code paths rather than reimplementing
them: :func:`core.tracing.setup_tracing` installs the ``TracerProvider`` and the
real ``OTLPSpanExporter`` / ``BatchSpanProcessor``, and :func:`core.telemetry.span`
produces the application semantic spans. There is no second ``TracerProvider``
here, and no fake/in-memory exporter is used for the evidence path.

What this does NOT prove
------------------------
- It does **not** verify the production business call sites. The five spans are
  emitted directly through ``core.telemetry.span`` precisely so the smoke needs
  no real LLM / Qdrant / ERP / Celery workflow; call-site wiring is covered
  separately by ``tests/unit/test_telemetry.py`` and by the span literals in
  ``runtime/executor.py``, ``rag/qdrant_knowledge_base.py``, ``llm/client.py``
  and ``tools/tool_registry.py``.
- It does **not** verify a persistent or queryable trace backend. The Collector
  used here has a single ``debug`` exporter, so nothing is stored and there is no
  retention, query UI or dashboard.
- It does **not** verify production trace propagation.

Determinism
-----------
The two switches stay separate, exactly as ``core/tracing.py`` documents them:
``OPENTELEMETRY_ENABLED`` installs the provider/exporter, ``OTEL_ENABLED``
produces application semantic spans. Flushing uses the SDK lifecycle
(``force_flush()`` then ``shutdown()``) — never ``sleep()``, because
``BatchSpanProcessor`` exports asynchronously and a bare sleep would make the
result a coin flip.

A synthetic privacy canary is submitted on purpose. ``core.telemetry`` must drop
it, so its absence from the Collector's own output is part of the evidence.

Output: ``artifacts/observability/otel-collector-<UTC>/report.json``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "otel-collector-evidence/v1"

#: Span names this smoke expects the Collector to have received. Kept in sync
#: with the wired call sites; ``tests/unit/test_otel_collector_evidence.py``
#: pins them against the real source files so the list cannot silently rot.
EXPECTED_SPANS: tuple[str, ...] = (
    "csai.agent.execute",
    "csai.agent.execute.resume",
    "csai.rag.retrieve",
    "csai.llm.chat_completion",
    "csai.tool.execute",
)

#: Synthetic, never a real credential. If this string reaches the Collector,
#: the attribute whitelist / denylist has regressed and the run must fail.
PRIVACY_CANARY = "SYNTHETIC_SECRET_SHOULD_NOT_APPEAR"

#: Attributes that ARE allowed through, and must therefore be visible.
EXPECTED_WHITELIST_ATTRS: tuple[str, ...] = (
    "csai.run_id",
    "csai.rag.scene",
    "csai.model_name",
    "csai.tool_name",
)

#: Keys submitted only to prove the scrubber drops them. No real secret.
FILTERED_PROBE_KEYS: tuple[str, ...] = (
    "csai.raw_prompt",
    "csai.user_text",
    "csai.tool_arguments",
    "csai.api_key",
    "csai.customer_password",
    "csai.session_secret",
)

EVIDENCE_DIR = REPO_ROOT / "artifacts" / "observability"

#: Span names as printed by the Collector's `debug` exporter ("Name  : csai.x").
#: Constrained to the ``csai.`` prefix so the collector's own internal telemetry
#: and unrelated log lines cannot be mistaken for a received application span.
_SPAN_NAME_IN_LOG_RE = re.compile(r"\bName\s*:\s*(csai\.[A-Za-z0-9_.]+)")
#: The client resource's service.name, in debug-exporter attribute form.
_SERVICE_NAME_IN_LOG_RE = re.compile(r"service\.name:\s*Str\(([^)]+)\)")


def _git_state() -> dict[str, Any]:
    """SHA plus tree-dirty flag for the code under test.

    A pass rate measured on uncommitted code is not evidence about that code, so
    the dirty flag is recorded explicitly rather than folded into the SHA string
    the way ``verify_distributed_runtime.py`` does.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return {"git_sha": None, "dirty": None, "git_available": False}
    return {"git_sha": sha, "dirty": bool(porcelain), "git_available": True}


def _parse_debug_log(text: str) -> dict[str, Any]:
    """Extract span names / resource attrs / canary presence from collector stdout.

    The ``debug`` exporter prints a structured ``Name:`` line per span. Parsing
    is intentionally tolerant about surrounding formatting: the assertion is
    about which span names appear, not about the collector's exact layout.
    """
    observed: list[str] = []
    for raw in text.splitlines():
        # The collector's stdout is prefixed by `docker compose logs`
        # ("<container>  | ") and the debug exporter pads its labels, so the
        # match is deliberately unanchored and only the value is constrained.
        match = _SPAN_NAME_IN_LOG_RE.search(raw)
        if match:
            name = match.group(1)
            if name not in observed:
                observed.append(name)
    # Only the CLIENT resource is of interest. The collector's own telemetry
    # repeats service.name in its JSON log prefix ("service.name": "otelcol"),
    # so match the debug exporter's attribute form specifically.
    service_names = sorted(set(_SERVICE_NAME_IN_LOG_RE.findall(text)))
    return {
        "observed_spans": observed,
        "service_names": service_names,
        "privacy_canary_present": PRIVACY_CANARY in text,
        "whitelist_attrs_present": sorted(
            attr for attr in EXPECTED_WHITELIST_ATTRS if attr in text
        ),
    }


def emit_semantic_spans(service_name: str) -> dict[str, Any]:
    """Install the real provider/exporter, then emit the semantic spans.

    Returns the SDK flush outcome so the caller can prove the export was driven
    by the SDK lifecycle rather than by a sleep.
    """
    from core import tracing as core_tracing
    from core.telemetry import span

    enabled = core_tracing.setup_tracing()
    flush: dict[str, Any] = {"force_flush_success": None, "shutdown_ok": None}
    switches = {
        # Read inside the application process on purpose: the orchestrator does
        # not have these set, so reading them here is the only way the evidence
        # records the switches as the span-producing process actually saw them.
        "opentelemetry_enabled": os.getenv("OPENTELEMETRY_ENABLED"),
        "otel_enabled": os.getenv("OTEL_ENABLED"),
        "otel_exporter_otlp_endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    }
    if not enabled:
        return {"setup_tracing": enabled, **switches, **flush}

    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()

    # Each span carries a couple of whitelisted attributes so the evidence also
    # shows the attribute whitelist still lets legitimate facts through.
    with span("csai.agent.execute", attributes={"csai.run_id": "otel-smoke-run"}):
        pass
    with span(
        "csai.agent.execute.resume",
        attributes={"csai.run_id": "otel-smoke-run", "csai.resumed": True},
    ):
        pass
    with span(
        "csai.rag.retrieve",
        attributes={"csai.rag.scene": "product_knowledge", "csai.run_id": "otel-smoke-run"},
    ) as rag_span:
        # One stage event, matching how the RAG call site enriches the span.
        rag_span.add_event(
            "rag.stage.VECTOR",
            {
                "csai.stage.status": "ok",
                "csai.stage.candidate_in": 10,
                "csai.stage.candidate_out": 3,
                "csai.stage.duration_ms": 1.25,
            },
        )
    with span("csai.llm.chat_completion", attributes={"csai.model_name": service_name}):
        pass
    with span(
        "csai.tool.execute",
        attributes={"csai.tool_name": "otel_smoke_tool", "csai.tool_side_effect": False},
    ):
        pass

    # Privacy probe: these keys must be dropped by core.telemetry. The canary
    # value is synthetic; no real credential is involved.
    with span(
        "csai.agent.execute",
        attributes={
            "csai.run_id": "otel-smoke-run",
            "csai.raw_prompt": PRIVACY_CANARY,
            "csai.user_text": PRIVACY_CANARY,
            "csai.tool_arguments": PRIVACY_CANARY,
            "csai.api_key": PRIVACY_CANARY,
            "csai.customer_password": PRIVACY_CANARY,
            "csai.session_secret": PRIVACY_CANARY,
        },
    ):
        pass

    # Deterministic export: SDK lifecycle, not sleep.
    try:
        flush["force_flush_success"] = bool(provider.force_flush(timeout_millis=15000))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        flush["force_flush_success"] = False
        flush["force_flush_error"] = f"{type(exc).__name__}: {exc}"
    try:
        provider.shutdown()
        flush["shutdown_ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        flush["shutdown_ok"] = False
        flush["shutdown_error"] = f"{type(exc).__name__}: {exc}"

    return {"setup_tracing": enabled, **switches, **flush}


def build_report(
    *,
    collector_logs: str,
    flush: dict[str, Any],
    service_name: str,
    endpoint: str,
    protocol: str,
    collector_version: str,
    collector_image: str,
) -> dict[str, Any]:
    parsed = _parse_debug_log(collector_logs)
    observed = parsed["observed_spans"]
    missing = [name for name in EXPECTED_SPANS if name not in observed]
    attrs_ok = sorted(EXPECTED_WHITELIST_ATTRS) == parsed["whitelist_attrs_present"]
    service_ok = service_name in parsed["service_names"]
    canary_absent = parsed["privacy_canary_present"] is False

    checks = {
        "collector_received_all_expected_spans": not missing,
        "service_name_matches": service_ok,
        "privacy_canary_absent": canary_absent,
        "whitelist_attrs_present": attrs_ok,
        "force_flush_succeeded": flush.get("force_flush_success") is True,
        "shutdown_completed": flush.get("shutdown_ok") is True,
        "clean_tree": None,  # filled by caller
    }
    verifiable = {k: v for k, v in checks.items() if v is not None}
    passed = all(verifiable.values())

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "VERIFIED_LOCAL" if passed else "FAILED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_state(),
        "collector": {
            "distribution": "opentelemetry-collector",
            "image": collector_image,
            "version": collector_version,
            "otlp_protocol": protocol,
            "endpoint": endpoint,
            "exporters": ["debug"],
            "pipelines": ["traces"],
            "readiness": "zpages servicez HTTP endpoint polled to healthy",
        },
        "application": {
            "service_name": service_name,
            "opentelemetry_enabled": flush.get("opentelemetry_enabled"),
            "otel_enabled": flush.get("otel_enabled"),
            "otlp_endpoint_env": flush.get("otel_exporter_otlp_endpoint"),
            "setup_tracing_returned": flush.get("setup_tracing"),
            "force_flush": flush,
            "uses_core_tracing_setup": True,
            "uses_core_telemetry_span": True,
            "fake_exporter_used": False,
        },
        "expected_spans": list(EXPECTED_SPANS),
        "observed_spans": observed,
        "missing_spans": missing,
        "expected_whitelist_attrs": list(EXPECTED_WHITELIST_ATTRS),
        "observed_whitelist_attrs": parsed["whitelist_attrs_present"],
        "privacy_canary": PRIVACY_CANARY,
        "privacy_canary_present": parsed["privacy_canary_present"],
        "filtered_probe_keys": list(FILTERED_PROBE_KEYS),
        "checks": checks,
        "scope": {
            "proves": (
                "core.telemetry semantic spans -> OTel SDK -> OTLPSpanExporter -> "
                "network -> live otel collector received them"
            ),
            "does_not_prove": [
                "production business call sites (covered by tests/unit/test_telemetry.py)",
                "persistent or queryable trace backend (collector has only a debug exporter)",
                "production trace propagation",
            ],
        },
    }


def finalize_status(report: dict[str, Any]) -> str:
    """Decide the evidence status, keeping dirty-tree evidence out of VERIFIED.

    Split out from the orchestrator so the decision is unit-testable without
    Docker. A pass measured on uncommitted code is not evidence about a commit,
    so a dirty tree downgrades the result instead of silently claiming it.
    """
    checks = report.get("checks") or {}
    git_state = report.get("git") or {}
    # clean_tree is deliberately excluded from the aggregate: it does not make the
    # run fail, it downgrades the label. Folding it in would report a dirty tree
    # as FAILED and hide the fact that every actual check passed.
    verifiable = {k: v for k, v in checks.items() if v is not None and k != "clean_tree"}
    if not all(verifiable.values()):
        return "FAILED"
    if git_state.get("dirty") is not False:
        return "VERIFIED_LOCAL_DIRTY"
    return "VERIFIED_LOCAL"


def write_report(report: dict[str, Any], stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = EVIDENCE_DIR / f"otel-collector-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Live OTLP Collector 证据生成")
    parser.add_argument(
        "--emit",
        action="store_true",
        help=(
            "Emit the semantic spans through core.telemetry and write the SDK "
            "flush outcome as JSON, then exit. Used by the smoke orchestrator so "
            "the spans are produced by a real application Python process."
        ),
    )
    parser.add_argument("--flush-out", help="Where --emit writes its flush JSON.")
    parser.add_argument(
        "--collector-logs",
        help="Path to the captured otel-collector stdout (debug exporter output).",
    )
    parser.add_argument("--endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    parser.add_argument("--protocol", default="grpc")
    parser.add_argument("--collector-version", default="unknown")
    parser.add_argument("--collector-image", default="unknown")
    parser.add_argument(
        "--service-name",
        default=os.getenv("OTEL_SERVICE_NAME", "csai-otel-smoke"),
    )
    parser.add_argument("--flush-json", help="JSON file with the SDK flush outcome.")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args()

    if args.emit:
        outcome = emit_semantic_spans(args.service_name)
        if args.flush_out:
            Path(args.flush_out).write_text(
                json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome.get("setup_tracing") else 2

    if not args.collector_logs:
        print("FAIL: --collector-logs is required unless --emit is used", file=sys.stderr)
        return 2

    logs_path = Path(args.collector_logs)
    if not logs_path.exists():
        print(f"FAIL: collector log file not found: {logs_path}", file=sys.stderr)
        return 2

    flush: dict[str, Any] = {}
    if args.flush_json:
        flush = json.loads(Path(args.flush_json).read_text(encoding="utf-8"))

    report = build_report(
        collector_logs=logs_path.read_text(encoding="utf-8", errors="replace"),
        flush=flush,
        service_name=args.service_name,
        endpoint=args.endpoint,
        protocol=args.protocol,
        collector_version=args.collector_version,
        collector_image=args.collector_image,
    )
    path = write_report(report, stamp=args.stamp)
    print(json.dumps({"report": str(path), "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] == "VERIFIED_LOCAL" else 1


if __name__ == "__main__":
    sys.exit(main())
