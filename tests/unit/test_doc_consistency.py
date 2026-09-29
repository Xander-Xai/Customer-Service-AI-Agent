"""Regression tests for scripts/audit_doc_consistency.py.

Each test proves a drift class the guard is supposed to catch. Fixtures build
small synthetic repositories in tmp_path and call the underlying check
functions directly with ``root=tmp_path`` (not the whole main()), so tests stay
fast and hermetic. The final test asserts the real-repo invariant: the guard
passes on the actual checkout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_doc_consistency.py"
spec = importlib.util.spec_from_file_location("audit_doc_consistency", SCRIPT)
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_doc_consistency"] = audit
spec.loader.exec_module(audit)

REAL_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    return tmp_path


def write(tmp_repo: Path, rel: str, text: str) -> Path:
    p = tmp_repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------ A. broken links


def test_broken_markdown_link_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "docs/reference/foo.md", "see [x](../../nope/missing.py)")
    errors: list[str] = []
    audit.check_links(doc, doc.read_text(encoding="utf-8"), errors, root=tmp_repo)
    assert any("nope/missing.py" in e and "docs/reference/foo.md" in e for e in errors)


def test_valid_markdown_link_passes(tmp_repo: Path):
    write(tmp_repo, "core/config.py", "VERSION = '6.3'\n")
    doc = write(tmp_repo, "docs/reference/foo.md", "see [x](../../core/config.py)")
    errors: list[str] = []
    audit.check_links(doc, doc.read_text(encoding="utf-8"), errors, root=tmp_repo)
    assert errors == []


def test_docs_under_docs_may_not_use_root_relative_links(tmp_repo: Path):
    write(tmp_repo, "core/config.py", "")
    doc = write(tmp_repo, "docs/reference/foo.md", "see [x](core/config.py)")
    errors: list[str] = []
    audit.check_links(doc, doc.read_text(encoding="utf-8"), errors, root=tmp_repo)
    assert errors, "docs/ links must resolve relative to the file, not the repo root"


# ------------------------------------------------------- B/H. missing files


def test_missing_referenced_script_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", "run: `python3 scripts/does_not_exist.py`")
    errors: list[str] = []
    audit.check_file_refs_line_aware(doc, doc.read_text(encoding="utf-8"), errors, root=tmp_repo)
    assert any("scripts/does_not_exist.py" in e for e in errors)


def test_historical_context_line_is_tolerated(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/guide.md",
        "# 原来的 scripts/removed_tool.py 已移除，不再作为活动入口。",
    )
    errors: list[str] = []
    audit.check_file_refs_line_aware(doc, doc.read_text(encoding="utf-8"), errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------------------------ C. env coverage


def test_missing_public_tool_flag_in_env_example(tmp_repo: Path):
    (tmp_repo / "core").mkdir(parents=True)
    (tmp_repo / "core" / "config.py").write_text(
        'TOOL_RESULT_MAGIC_FLAG = os.getenv("TOOL_RESULT_MAGIC_FLAG", "false")\n',
        encoding="utf-8",
    )
    (tmp_repo / ".env.example").write_text("OTHER=1\n", encoding="utf-8")
    errors: list[str] = []
    audit.check_env_coverage(errors, root=tmp_repo)
    assert any("TOOL_RESULT_MAGIC_FLAG" in e for e in errors)


def test_missing_required_env_key_is_detected(tmp_repo: Path):
    (tmp_repo / "core").mkdir(parents=True)
    (tmp_repo / "core" / "config.py").write_text("", encoding="utf-8")
    keys = [k for k in audit.REQUIRED_ENV_KEYS if k != "HTTP_TIMEOUT"]
    (tmp_repo / ".env.example").write_text(
        "\n".join(f"{k}=x" for k in keys) + "\n", encoding="utf-8"
    )
    errors: list[str] = []
    audit.check_env_coverage(errors, root=tmp_repo)
    assert any(".env.example missing required key: HTTP_TIMEOUT" in e for e in errors)


# ------------------------------------------------------- D. canonical config


def _write_canonical_sources(
    tmp_repo: Path,
    runtime_model: str,
    env_model: str,
    compose_model: str | None,
) -> None:
    (tmp_repo / "core").mkdir(parents=True, exist_ok=True)
    (tmp_repo / "deploy" / "compose").mkdir(parents=True, exist_ok=True)
    (tmp_repo / "core" / "config.py").write_text(
        f'OPENAI_MODEL = os.getenv(\n'
        f'    "OPENAI_MODEL",\n'
        f'    "{runtime_model}",\n'
        f')\n',
        encoding="utf-8",
    )
    env_lines = [
        "LLM_PROVIDER=siliconflow",
        f"OPENAI_MODEL={env_model}",
        "OPENAI_BASE_URL=https://api.siliconflow.cn/v1",
        "EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5",
        "RERANKER_MODEL=BAAI/bge-reranker-v2-m3",
        "",
    ]
    (tmp_repo / ".env.example").write_text("\n".join(env_lines), encoding="utf-8")
    if compose_model is not None:
        (tmp_repo / "deploy" / "compose" / "docker-compose.yml").write_text(
            f"- OPENAI_MODEL=${{OPENAI_MODEL:-{compose_model}}}\n", encoding="utf-8"
        )


def test_runtime_model_drift_is_detected_when_env_and_compose_agree(tmp_repo: Path):
    """The guard must read the runtime fallback from core/config.py (via AST),
    not just compare template files against each other. Here .env.example and
    docker-compose agree on Qwen3, but the runtime code default drifted to
    Qwen4 — that must FAIL."""
    _write_canonical_sources(
        tmp_repo,
        runtime_model="Qwen/Qwen4-8B",
        env_model="Qwen/Qwen3-8B",
        compose_model="Qwen/Qwen3-8B",
    )
    errors: list[str] = []
    audit.check_canonical_config(errors, root=tmp_repo)
    assert any(
        "canonical OPENAI_MODEL drift" in e and "Qwen/Qwen4-8B" in e for e in errors
    ), errors


def test_required_env_key_missing_even_if_present_in_config_is_detected(tmp_repo: Path):
    """Strict .env.example contract: a key existing in core/config.py does not
    excuse its absence from the deployment template."""
    (tmp_repo / "core").mkdir(parents=True)
    (tmp_repo / "core" / "config.py").write_text(
        'HTTP_TIMEOUT = _int_env("HTTP_TIMEOUT", 15)\n', encoding="utf-8"
    )
    # .env.example deliberately omits HTTP_TIMEOUT
    keys = [k for k in audit.REQUIRED_ENV_KEYS if k != "HTTP_TIMEOUT"]
    (tmp_repo / ".env.example").write_text(
        "\n".join(f"{k}=x" for k in keys) + "\n", encoding="utf-8"
    )
    errors: list[str] = []
    audit.check_env_coverage(errors, root=tmp_repo)
    assert any(".env.example missing required key: HTTP_TIMEOUT" in e for e in errors)


def test_ast_extraction_reads_multiline_helper_calls(tmp_repo: Path):
    """AST extraction must handle real config.py formatting: multi-line
    os.getenv(...) calls and _int_env/_float_env helpers."""
    (tmp_repo / "core").mkdir(parents=True)
    (tmp_repo / "core" / "config.py").write_text(
        "import os\n"
        "def _int_env(key, default):\n"
        "    return default\n"
        "\n"
        "OPENAI_MODEL = os.getenv(\n"
        '    "OPENAI_MODEL",\n'
        '    "Qwen/Qwen3-8B",\n'
        ")\n"
        'LLM_MAX_TOKENS = _int_env("LLM_MAX_TOKENS", 4096)\n'
        'HTTP_TIMEOUT = _int_env("HTTP_TIMEOUT", 15)\n',
        encoding="utf-8",
    )
    defaults = audit.extract_runtime_env_defaults(tmp_repo / "core" / "config.py")
    assert defaults["OPENAI_MODEL"] == "Qwen/Qwen3-8B"
    assert defaults["LLM_MAX_TOKENS"] == "4096"
    assert defaults["HTTP_TIMEOUT"] == "15"


def test_model_drift_between_compose_and_runtime(tmp_repo: Path):
    (tmp_repo / "core").mkdir(parents=True)
    (tmp_repo / "deploy" / "compose").mkdir(parents=True)
    (tmp_repo / "core" / "config.py").write_text(
        'OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-8B")\n', encoding="utf-8"
    )
    (tmp_repo / ".env.example").write_text("OPENAI_MODEL=Qwen/Qwen3-8B\n", encoding="utf-8")
    (tmp_repo / "deploy" / "compose" / "docker-compose.yml").write_text(
        "- OPENAI_MODEL=${OPENAI_MODEL:-Qwen/Qwen2.5-7B-Instruct}\n", encoding="utf-8"
    )
    errors: list[str] = []
    audit.check_canonical_config(errors, root=tmp_repo)
    assert any("canonical OPENAI_MODEL drift" in e for e in errors)


def test_canonical_config_consistency_passes_on_real_repo():
    errors: list[str] = []
    audit.check_canonical_config(errors, root=REAL_ROOT)
    assert errors == []


# ----------------------------------------------------- E. OpenAPI drift


def _fake_app_module(monkeypatch, spec_dict: dict) -> None:
    class _FakeApp:
        @staticmethod
        def openapi():
            return spec_dict

    module = type(sys)("api.app_factory")
    module.app = _FakeApp()
    monkeypatch.setitem(sys.modules, "api.app_factory", module)


def test_openapi_path_count_drift_is_detected(tmp_repo: Path, monkeypatch):
    (tmp_repo / "docs").mkdir(parents=True)
    (tmp_repo / "docs" / "openapi.json").write_text(
        json.dumps({"paths": {"/x": {}}, "info": {"version": "6.3"}}), encoding="utf-8"
    )
    _fake_app_module(monkeypatch, {"paths": {"/x": {}, "/y": {}}, "info": {"version": "6.3"}})

    errors: list[str] = []
    audit.check_openapi_snapshot(errors, root=tmp_repo)
    assert any("path-count drift" in e for e in errors)


def test_openapi_version_drift_is_detected(tmp_repo: Path, monkeypatch):
    (tmp_repo / "docs").mkdir(parents=True)
    (tmp_repo / "docs" / "openapi.json").write_text(
        json.dumps({"paths": {"/x": {}}, "info": {"version": "6.2"}}), encoding="utf-8"
    )
    _fake_app_module(monkeypatch, {"paths": {"/x": {}}, "info": {"version": "6.3"}})

    errors: list[str] = []
    audit.check_openapi_snapshot(errors, root=tmp_repo)
    assert any("version drift" in e for e in errors)


def test_openapi_content_drift_same_count_is_detected(tmp_repo: Path, monkeypatch):
    (tmp_repo / "docs").mkdir(parents=True)
    (tmp_repo / "docs" / "openapi.json").write_text(
        json.dumps({"paths": {"/old": {}}, "info": {"version": "6.3"}}), encoding="utf-8"
    )
    _fake_app_module(monkeypatch, {"paths": {"/new": {}}, "info": {"version": "6.3"}})

    errors: list[str] = []
    audit.check_openapi_snapshot(errors, root=tmp_repo)
    assert any("API surface (path→methods) differs" in e for e in errors)


def test_openapi_snapshot_passes_on_real_repo():
    errors: list[str] = []
    audit.check_openapi_snapshot(errors, root=REAL_ROOT)
    assert not any("drift" in e for e in errors)


def test_schema_serialization_drift_with_same_surface_passes(tmp_repo: Path, monkeypatch):
    """CI regression: fastapi/pydantic version changes can alter serialized
    schema content while the API surface (path→methods) stays identical.
    The guard must NOT report drift in that case (floating requirements.txt)."""
    (tmp_repo / "docs").mkdir(parents=True)
    (tmp_repo / "docs" / "openapi.json").write_text(
        json.dumps(
            {"paths": {"/x": {"post": {"summary": "old schema"}}}, "info": {"version": "6.3"}}
        ),
        encoding="utf-8",
    )
    _fake_app_module(
        monkeypatch,
        {"paths": {"/x": {"post": {"summary": "different serialization"}}}, "info": {"version": "6.3"}},
    )

    errors: list[str] = []
    audit.check_openapi_snapshot(errors, root=tmp_repo)
    assert not any("drift" in e for e in errors)


# ------------------------------------------------- F. benchmark metadata


def test_benchmark_metadata_mismatch_is_detected(tmp_repo: Path):
    (tmp_repo / "tests" / "eval").mkdir(parents=True)
    (tmp_repo / "tests" / "eval" / "rag_benchmark.json").write_text(
        json.dumps({"metadata": {"total_queries": 500}, "queries": [{}, {}, {}]}),
        encoding="utf-8",
    )
    errors: list[str] = []
    audit.check_benchmark_metadata(errors, root=tmp_repo)
    assert any("metadata mismatch" in e for e in errors)


def test_benchmark_metadata_consistency_passes_on_real_repo():
    errors: list[str] = []
    audit.check_benchmark_metadata(errors, root=REAL_ROOT)
    assert errors == []


# ---------------------------------------------------- G. stale terminology


def test_active_doc_with_stale_current_claim_fails(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/runbook.md",
        "| `siliconflow` | `https://x/v1` | `Qwen/Qwen2.5-7B-Instruct` |",
    )
    errors: list[str] = []
    audit.check_stale_terms([doc], [], errors, root=tmp_repo)
    assert any("Qwen" in e and "runbook.md" in e for e in errors)


def test_active_doc_with_stale_hardcoded_test_count_fails(tmp_repo: Path):
    doc = write(tmp_repo, "docs/checklists/gate.md", "- [x] tests pass (1,361+ 项)")
    errors: list[str] = []
    audit.check_stale_terms([doc], [], errors, root=tmp_repo)
    assert any("1,?361" in e for e in errors)


def test_historical_context_line_is_allowed_for_terms(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/design/history.md",
        "2026-06 历史记录：当时曾使用 bge-small-zh-v1.5 本地模型与 ChromaDB（已迁移）。",
    )
    errors: list[str] = []
    audit.check_stale_terms([doc], [], errors, root=tmp_repo)
    assert errors == []


def test_decision_docs_keep_historical_wording(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/decisions/003-qwen-default-llm.md",
        "# ADR-003: Qwen2.5-7B 作为默认 LLM\n\n默认使用 Qwen2.5-7B-Instruct（历史决策正文）。\n",
    )
    errors: list[str] = []
    audit.check_stale_terms([doc], [], errors, root=tmp_repo)
    assert errors == []


def test_discovery_excludes_historical_banner(tmp_repo: Path):
    write(
        tmp_repo,
        "docs/audit/old.md",
        "# Old\n\n> **HISTORICAL AUDIT SNAPSHOT**\n> ChromaDB\n",
    )
    write(tmp_repo, "docs/design/live.md", "# Live\n\nno stale terms here\n")
    docs = audit.discover_docs(root=tmp_repo)
    rels = {p.relative_to(tmp_repo) for p in docs}
    assert Path("docs/design/live.md") in rels
    assert Path("docs/audit/old.md") not in rels


def test_discovery_excludes_archive_and_milestone_dirs(tmp_repo: Path):
    write(tmp_repo, "docs/archive/old.md", "ChromaDB stale")
    write(tmp_repo, "docs/reports/milestone/old.md", "1400+ tests")
    write(tmp_repo, "docs/reports/plans/2026-01-01-audit.md", "Qwen/Qwen2.5-7B-Instruct")
    write(tmp_repo, "docs/design/live.md", "clean doc")
    docs = audit.discover_docs(root=tmp_repo)
    rels = {p.relative_to(tmp_repo) for p in docs}
    assert rels == {Path("docs/design/live.md")}


# ------------------------------------------------- real-repo invariants


def test_real_repo_passes_guard():
    """The whole guard must pass on the actual checkout (CI contract)."""
    assert audit.main() == 0
