"""Unit tests for the live OTLP Collector smoke evidence pipeline.

Deliberately Docker-free. The live leg (``make otel-collector-smoke``) is an
explicit integration command; these tests pin only the deterministic parts:

- the evidence report schema and the expected/observed/missing span comparison
- a missing span must fail the run
- the privacy canary must fail the run when it leaks
- dirty-tree evidence must not be labelled verified
- the Collector config is traces-only and pins an image version
- the expected span list still matches the real wired call sites
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_otel_collector.py"
_spec = importlib.util.spec_from_file_location("verify_otel_collector", VERIFIER)
verifier = importlib.util.module_from_spec(_spec)
sys.modules["verify_otel_collector"] = verifier
_spec.loader.exec_module(verifier)


def _collector_log(*, spans: tuple[str, ...], service: str = "csai-otel-smoke", **extra: str) -> str:
    """Render a collector `debug` exporter log the way the real one does.

    Reproduces the two shapes that actually matter and that previously broke the
    parser: the ``docker compose logs`` container prefix, and the debug
    exporter's space-padded labels.
    """
    prefix = "customer-service-otel-collector  | "
    lines = [
        f"{prefix}2026-10-03T03:53:59.064Z\tinfo\tResourceSpans #0",
        f"{prefix}Resource attributes:",
        f'{prefix}     -> telemetry.sdk.language: Str(python)',
        f"{prefix}     -> service.name: Str({service})",
    ]
    for span in spans:
        lines += [
            f"{prefix}ScopeSpans #0",
            f"{prefix}Span #0",
            f"{prefix}     Trace ID       : a9d0c2ebd969b495946575aae343e122",
            f"{prefix}     Name           : {span}",
            f"{prefix}     Kind           : Internal",
        ]
    # The collector repeats service.name in its own JSON telemetry prefix; it
    # must not be mistaken for the client's resource.
    lines.append(
        '{"resource": {"service.name": "otelcol"}, "otelcol.component.id": "debug"}'
    )
    for line in extra.get("attr_lines", "").splitlines():
        lines.append(f"{prefix}     -> {line}")
    if extra.get("canary"):
        lines.append(f"{prefix}     -> csai.leaked: Str({extra['canary']})")
    return "\n".join(lines)


def _report(*, spans: tuple[str, ...], canary_leak: bool = False) -> dict:
    return verifier.build_report(
        collector_logs=_collector_log(
            spans=spans,
            canary=verifier.PRIVACY_CANARY if canary_leak else "",
            attr_lines="\n".join(
                [
                    "csai.run_id: Str(otel-smoke-run)",
                    "csai.rag.scene: Str(product_knowledge)",
                    "csai.model_name: Str(csai-otel-smoke)",
                    "csai.tool_name: Str(otel_smoke_tool)",
                    "csai.stage.status: Str(ok)",
                ]
            ),
        ),
        flush={"setup_tracing": True, "force_flush_success": True, "shutdown_ok": True},
        service_name="csai-otel-smoke",
        endpoint="http://127.0.0.1:4317",
        protocol="grpc",
        collector_version="0.162.0",
        collector_image="otel/opentelemetry-collector:0.162.0",
    )


# --------------------------------------------------------------- happy path


def test_all_five_spans_observed_and_report_is_verified():
    report = _report(spans=verifier.EXPECTED_SPANS)
    report["git"] = {"git_sha": "a" * 40, "dirty": False, "git_available": True}
    report["checks"]["clean_tree"] = True
    assert report["missing_spans"] == []
    assert report["observed_spans"] == list(verifier.EXPECTED_SPANS)
    assert verifier.finalize_status(report) == "VERIFIED_LOCAL"


def test_report_has_the_required_provenance_fields():
    report = _report(spans=verifier.EXPECTED_SPANS)
    assert report["schema_version"] == verifier.SCHEMA_VERSION
    assert report["generated_at"]
    assert report["collector"]["image"] == "otel/opentelemetry-collector:0.162.0"
    assert report["collector"]["version"] == "0.162.0"
    assert report["collector"]["otlp_protocol"] == "grpc"
    assert report["collector"]["endpoint"] == "http://127.0.0.1:4317"
    assert report["application"]["service_name"] == "csai-otel-smoke"
    assert report["application"]["uses_core_tracing_setup"] is True
    assert report["application"]["uses_core_telemetry_span"] is True
    assert report["application"]["fake_exporter_used"] is False
    assert set(report["expected_spans"]) == set(verifier.EXPECTED_SPANS)
    assert report["missing_spans"] == []


def test_report_records_the_scope_boundary():
    report = _report(spans=verifier.EXPECTED_SPANS)
    assert "does_not_prove" in report["scope"]
    joined = " ".join(report["scope"]["does_not_prove"]).lower()
    assert "backend" in joined
    assert "production" in joined


# ------------------------------------------------------------------ negative


def test_missing_span_fails_the_run():
    spans = tuple(s for s in verifier.EXPECTED_SPANS if s != "csai.tool.execute")
    report = _report(spans=spans)
    assert report["missing_spans"] == ["csai.tool.execute"]
    assert report["checks"]["collector_received_all_expected_spans"] is False
    report["git"] = {"git_sha": "b" * 40, "dirty": False, "git_available": True}
    report["checks"]["clean_tree"] = True
    assert verifier.finalize_status(report) == "FAILED"


def test_privacy_canary_leak_fails_the_run():
    report = _report(spans=verifier.EXPECTED_SPANS, canary_leak=True)
    assert report["privacy_canary_present"] is True
    assert report["checks"]["privacy_canary_absent"] is False
    report["git"] = {"git_sha": "c" * 40, "dirty": False, "git_available": True}
    report["checks"]["clean_tree"] = True
    assert verifier.finalize_status(report) == "FAILED"


def test_wrong_service_name_fails_the_run():
    report = verifier.build_report(
        collector_logs=_collector_log(spans=verifier.EXPECTED_SPANS, service="somebody-else"),
        flush={"setup_tracing": True, "force_flush_success": True, "shutdown_ok": True},
        service_name="csai-otel-smoke",
        endpoint="http://127.0.0.1:4317",
        protocol="grpc",
        collector_version="0.162.0",
        collector_image="otel/opentelemetry-collector:0.162.0",
    )
    assert report["checks"]["service_name_matches"] is False


def test_collector_internal_service_name_is_not_mistaken_for_the_client():
    """The collector logs its own service.name in JSON; only the client's
    debug-exporter attribute form may satisfy the check."""
    parsed = verifier._parse_debug_log(
        '{"resource": {"service.name": "otelcol"}, "otelcol.component.id": "debug"}'
    )
    assert parsed["service_names"] == []


def test_failed_force_flush_fails_the_run():
    report = verifier.build_report(
        collector_logs=_collector_log(spans=verifier.EXPECTED_SPANS),
        flush={"setup_tracing": True, "force_flush_success": False, "shutdown_ok": True},
        service_name="csai-otel-smoke",
        endpoint="http://127.0.0.1:4317",
        protocol="grpc",
        collector_version="0.162.0",
        collector_image="otel/opentelemetry-collector:0.162.0",
    )
    assert report["checks"]["force_flush_succeeded"] is False
    report["git"] = {"git_sha": "d" * 40, "dirty": False, "git_available": True}
    report["checks"]["clean_tree"] = True
    assert verifier.finalize_status(report) == "FAILED"


# ------------------------------------------------------------- dirty-tree rule


def test_dirty_tree_evidence_is_not_labelled_verified():
    report = _report(spans=verifier.EXPECTED_SPANS)
    report["git"] = {"git_sha": "e" * 40, "dirty": True, "git_available": True}
    report["checks"]["clean_tree"] = False
    assert verifier.finalize_status(report) == "VERIFIED_LOCAL_DIRTY"


def test_unknown_git_state_is_not_labelled_verified():
    report = _report(spans=verifier.EXPECTED_SPANS)
    report["git"] = {"git_sha": None, "dirty": None, "git_available": False}
    report["checks"]["clean_tree"] = None
    assert verifier.finalize_status(report) == "VERIFIED_LOCAL_DIRTY"


def test_clean_tree_is_checked_without_counting_untracked_artifacts():
    """The evidence artifact directory is untracked by design; folding untracked
    files into the dirty flag would make the evidence impossible to produce."""
    src = (REPO_ROOT / "scripts" / "verify_otel_collector.py").read_text(encoding="utf-8")
    assert "--untracked-files=no" in src


# --------------------------------------------------------- collector config


def test_collector_config_is_traces_only_and_pinned():
    text = (REPO_ROOT / "deploy" / "otel" / "collector-config.yaml").read_text(encoding="utf-8")
    assert "otel/opentelemetry-collector" not in text  # version pinning lives in compose
    assert re.search(r"^\s*otlp:", text, re.M)
    assert "4317" in text and "4318" in text
    pipelines = re.search(r"traces:\s*\n\s*receivers: \[otlp\]\s*\n\s*exporters: \[debug\]", text)
    assert pipelines, "expected a single traces pipeline wired otlp -> debug"
    for forbidden in ("metrics:", "logs:", "prometheus", "jaeger", "langfuse", "tempo", "otlphttp"):
        assert forbidden not in text, f"{forbidden} must not appear in a traces-only collector config"


def test_compose_pins_the_collector_image_version():
    text = (REPO_ROOT / "deploy" / "compose" / "docker-compose.otel.yml").read_text(encoding="utf-8")
    images = re.findall(r"^\s*image:\s*(\S+)", text, re.M)
    assert images, "expected an explicit image"
    for image in images:
        assert image.startswith("otel/opentelemetry-collector:")
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest", "collector image must not float on latest"
        assert re.fullmatch(r"\d+\.\d+\.\d+", tag), f"tag must be an exact version, got {tag}"


# ------------------------------------------------- expected spans vs reality


@pytest.mark.parametrize(
    ("rel", "span"),
    [
        ("runtime/executor.py", "csai.agent.execute"),
        ("rag/qdrant_knowledge_base.py", "csai.rag.retrieve"),
        ("llm/client.py", "csai.llm.chat_completion"),
        ("tools/tool_registry.py", "csai.tool.execute"),
    ],
)
def test_expected_spans_are_really_wired_in_the_call_sites(rel: str, span: str):
    """Pins EXPECTED_SPANS against the production call sites so the smoke cannot
    quietly drift away from the spans the system actually emits."""
    assert f'"{span}"' in (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_resume_span_is_still_a_separate_trace_segment():
    assert "csai.agent.execute.resume" in verifier.EXPECTED_SPANS
    executor = (REPO_ROOT / "runtime" / "executor.py").read_text(encoding="utf-8")
    assert '"csai.agent.execute.resume"' in executor
