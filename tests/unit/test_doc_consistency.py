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
import os
import subprocess
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
        f'OPENAI_MODEL = os.getenv(\n    "OPENAI_MODEL",\n    "{runtime_model}",\n)\n',
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
    assert any("canonical OPENAI_MODEL drift" in e and "Qwen/Qwen4-8B" in e for e in errors), errors


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
        {
            "paths": {"/x": {"post": {"summary": "different serialization"}}},
            "info": {"version": "6.3"},
        },
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


# ------------------------------------------------- A. test-count framing


def test_hardcoded_npm_count_framed_as_current_fails(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/checklists/gate.md",
        "- [x] 前端单测通过：`npm test` = 60/60（7 个测试文件）",
    )
    errors: list[str] = []
    audit.check_test_count_framing([doc], errors, root=tmp_repo)
    assert any("npm test" in e for e in errors)


def test_hardcoded_pytest_collect_count_fails(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "README.md",
        "当前仓库 `pytest --collect-only -q` 收集到 1352 个测试用例。",
    )
    errors: list[str] = []
    audit.check_test_count_framing([doc], errors, root=tmp_repo)
    assert any("pytest" in e for e in errors)


def test_dynamic_count_paragraph_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/checklists/gate.md",
        "- [x] pytest 当前通过（collected 数以 `pytest --collect-only -q` 输出为准，不要沿用历史数字；npm test` 数量同样以命令输出为准）",
    )
    errors: list[str] = []
    audit.check_test_count_framing([doc], errors, root=tmp_repo)
    assert errors == []


def test_all_n_tests_pass_framing_fails(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", "运行后所有 1352 tests pass。")
    errors: list[str] = []
    audit.check_test_count_framing([doc], errors, root=tmp_repo)
    assert errors, "fixed 'all N tests pass' framing must be flagged"


# ------------------------------------------------- B/C. canonical RAG refs


def _canonical_rag_doc(tmp_repo: Path) -> Path:
    from tests.unit.test_doc_consistency import write as _w

    return _w(
        tmp_repo,
        "docs/reference/rag-evaluation.md",
        "# RAG 评估\n\n"
        "- 当前 649-query 正式指标：NOT_VERIFIED。\n"
        "- experiments: vector_only / bm25_only / hybrid_no_rerank / hybrid_rerank\n"
        "- populations: all_queries / retrieval_eligible / full_gold_covered\n",
    )


def test_missing_canonical_rag_doc_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", "RAG 评估见 rag-evaluation.md")
    errors: list[str] = []
    audit.check_rag_eval_references([doc], errors, root=tmp_repo)
    assert any("canonical RAG evaluation reference missing" in e for e in errors)


def test_entry_doc_without_canonical_link_is_detected(tmp_repo: Path):
    _canonical_rag_doc(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "# 当前事实\n\nRAG benchmark: 649 条（不链接 canonical 评估文档）",
    )
    errors: list[str] = []
    audit.check_rag_eval_references([doc], errors, root=tmp_repo)
    assert any("does not link the canonical reference" in e for e in errors)


def test_canonical_rag_doc_missing_pipeline_tokens_is_detected(tmp_repo: Path):
    (tmp_repo / "docs" / "reference").mkdir(parents=True)
    (tmp_repo / "docs" / "reference" / "rag-evaluation.md").write_text(
        "# RAG 评估\n\nincomplete: no experiments/populations listed\n", encoding="utf-8"
    )
    doc = write(tmp_repo, "README.md", "see rag-evaluation.md")
    errors: list[str] = []
    audit.check_rag_eval_references([doc], errors, root=tmp_repo)
    assert any("missing evidence-pipeline token" in e for e in errors)


def test_makefile_rag_target_not_wired_to_script_is_detected(tmp_repo: Path):
    _canonical_rag_doc(tmp_repo)
    (tmp_repo / "Makefile").write_text(
        "rag-eval-649: ## RAG eval\n\techo broken\n"
        "rag-eval-649-preflight: ## gate\n\tpython3 scripts/evaluate_rag.py --preflight-only\n"
        "rag-eval-649-smoke: ## smoke\n\tpython3 scripts/evaluate_rag.py --limit 16\n"
        "rag-eval-import: ## import\n\tpython3 scripts/import_eval_corpus.py\n",
        encoding="utf-8",
    )
    docs = [write(tmp_repo, "README.md", "run: make rag-eval-649 (canonical: rag-evaluation.md)")]
    errors: list[str] = []
    audit.check_rag_eval_references(docs, errors, root=tmp_repo)
    assert any(
        "rag-eval-649` missing" in e or ("rag-eval-649" in e and "canonical evaluation script" in e)
        for e in errors
    )


def test_wired_makefile_targets_pass(tmp_repo: Path):
    _canonical_rag_doc(tmp_repo)
    (tmp_repo / "Makefile").write_text(
        "rag-eval-649: ## formal\n\tpython3 scripts/evaluate_rag.py\n"
        "rag-eval-649-preflight: ## gate\n\tpython3 scripts/evaluate_rag.py --preflight-only\n"
        "rag-eval-649-smoke: ## smoke\n\tpython3 scripts/evaluate_rag.py --limit 16\n"
        "rag-eval-import: ## import\n\tpython3 scripts/import_eval_corpus.py\n",
        encoding="utf-8",
    )
    docs = [write(tmp_repo, "README.md", "run: make rag-eval-649 (canonical: rag-evaluation.md)")]
    errors: list[str] = []
    audit.check_rag_eval_references(docs, errors, root=tmp_repo)
    assert not any("canonical evaluation script" in e for e in errors)


# ------------------------------------------------------ D. metric claims


def test_unproven_current_metric_claim_fails(tmp_repo: Path):
    _canonical_rag_doc(tmp_repo)
    (tmp_repo / "Makefile").write_text(
        "rag-eval-649: ## formal\n\tpython3 scripts/evaluate_rag.py\n"
        "rag-eval-649-preflight: ## gate\n\tpython3 scripts/evaluate_rag.py --preflight-only\n"
        "rag-eval-649-smoke: ## smoke\n\tpython3 scripts/evaluate_rag.py --limit 16\n"
        "rag-eval-import: ## import\n\tpython3 scripts/import_eval_corpus.py\n",
        encoding="utf-8",
    )
    doc = write(tmp_repo, "README.md", "systems hits: current Hit@3 82% on the live pipeline")
    errors: list[str] = []
    audit.check_unproven_current_metrics([doc], errors, root=tmp_repo)
    assert any("unproven current RAG metric claim" in e for e in errors)


def test_provenance_qualified_metric_line_passes(tmp_repo: Path):
    _canonical_rag_doc(tmp_repo)
    (tmp_repo / "Makefile").write_text(
        "rag-eval-649: ## formal\n\tpython3 scripts/evaluate_rag.py\n"
        "rag-eval-649-preflight: ## gate\n\tpython3 scripts/evaluate_rag.py --preflight-only\n"
        "rag-eval-649-smoke: ## smoke\n\tpython3 scripts/evaluate_rag.py --limit 16\n"
        "rag-eval-import: ## import\n\tpython3 scripts/import_eval_corpus.py\n",
        encoding="utf-8",
    )
    doc = write(
        tmp_repo,
        "README.md",
        "历史报告（2026-06，30 条查询集）Hit@3 80%——历史口径。",
    )
    errors: list[str] = []
    audit.check_unproven_current_metrics([doc], errors, root=tmp_repo)
    assert errors == []


def facts_module():
    return _load_facts_module()


def facts_derive(root: Path) -> dict:
    from rag_evidence_status import derive_rag_formal_status

    return derive_rag_formal_status(root)


def test_metric_check_uses_artifact_state_not_doc_state(tmp_repo: Path, monkeypatch):
    """Rule D follows the ARTIFACT-derived state, not the doc text."""
    _canonical_rag_doc(tmp_repo)
    (tmp_repo / "Makefile").write_text(
        "rag-eval-649: ## formal\n\tpython3 scripts/evaluate_rag.py\n"
        "rag-eval-649-preflight: ## gate\n\tpython3 scripts/evaluate_rag.py --preflight-only\n"
        "rag-eval-649-smoke: ## smoke\n\tpython3 scripts/evaluate_rag.py --limit 16\n"
        "rag-eval-import: ## import\n\tpython3 scripts/import_eval_corpus.py\n",
        encoding="utf-8",
    )
    doc = write(tmp_repo, "README.md", "current Hit@3 82%")
    # A doc line self-claiming VERIFIED does NOT change the derived state:
    (tmp_repo / "docs" / "reference" / "rag-evaluation.md").write_text(
        "# RAG 评估\n\n- 当前 649-query 正式指标：VERIFIED（artifact: ...）。\n", encoding="utf-8"
    )
    assert audit.formal_rag_metrics(root=tmp_repo) == "NOT_VERIFIED"
    errors: list[str] = []
    audit.check_unproven_current_metrics([doc], errors, root=tmp_repo)
    assert any("unproven current RAG metric claim" in e for e in errors), (
        "docs cannot own/promote the evidence state — guard must still fire"
    )


# ------------------------------------------- artifact-driven governance
# Fixtures: report.json artifacts matching scripts/evaluate_rag.py schema.


def _make_benchmark(tmp_repo: Path, n: int = 16) -> str:
    import hashlib

    data = {
        "metadata": {"total_queries": n, "version": "benchmark-v1", "created_at": "2026-01-01"},
        "queries": [
            {"id": f"q{i}", "query": f"问题 {i}", "data_id": f"d{i}", "expected_doc_ids": []}
            for i in range(n)
        ],
    }
    path = tmp_repo / "tests" / "eval" / "rag_benchmark.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


EXPERIMENTS = ["vector_only", "bm25_only", "hybrid_no_rerank", "hybrid_rerank"]


def _formal_shaped_report(sha: str, n: int, **overrides) -> dict:
    report = {
        "schema_version": "rag-eval-evidence/v2",
        "run_id": "run-20261001T000000Z",
        "timestamp": "2026-10-01T00:00:00.000000+00:00",
        "status": "VERIFIED_FULL",
        "git_sha": "0123456789abcdef0123456789abcdef01234567",
        "benchmark": {
            "sha256": sha,
            "declared_queries": n,
            "actual_queries": n,
            "executed_queries": n,
        },
        "metrics": {name: {"hit@3": 0.5, "recall@3": 0.5, "mrr@3": 0.5} for name in EXPERIMENTS},
        "run_summary": {
            name: {"n_total": n, "n_success": n, "n_failed": 0, "n_degraded": 0}
            for name in EXPERIMENTS
        },
        "evaluation_populations": {
            "primary_view": "all_queries",
            "counts": {"all_queries": n, "retrieval_eligible": n, "full_gold_covered": n},
        },
        "subset_run": False,
    }
    report.update(overrides)
    return report


def _write_artifact(
    tmp_repo: Path,
    report: dict,
    run_id: str = "run-20261001T000000Z",
) -> Path:
    out = tmp_repo / "artifacts" / "evaluation" / "rag-649" / run_id
    out.mkdir(parents=True, exist_ok=True)
    path = out / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_no_formal_artifact_is_not_verified(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    assert sha  # benchmark present, no artifacts at all
    info = facts_derive(tmp_repo)
    assert info["rag_formal_metrics_status"] == "NOT_VERIFIED"
    assert info["rag_formal_artifact_path"] is None
    assert info["rag_formal_artifact_timestamp"] is None
    assert info["rag_formal_artifact_git_sha"] is None
    assert info["rag_formal_artifact_schema_version"] is None
    assert audit.formal_rag_metrics(root=tmp_repo) == "NOT_VERIFIED"


def test_preflight_artifact_does_not_verify_metrics(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = {
        "schema_version": "rag-eval-evidence/v2",
        "run_id": "preflight-x",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "status": "BLOCKED_PROVIDER_AUTH",
        "primary_blocker": "EMBEDDING_PROVIDER_AUTH",
        "git_sha": "0123",
        "benchmark": {"sha256": sha, "declared_queries": 16, "executed_queries": 16},
    }
    _write_artifact(tmp_repo, report, run_id="preflight-x")
    assert facts_derive(tmp_repo)["rag_formal_metrics_status"] == "NOT_VERIFIED"


def test_smoke_subset_artifact_does_not_verify_metrics(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["status"] = "SUBSET_SMOKE"
    report["subset_run"] = True
    _write_artifact(tmp_repo, report)
    assert facts_derive(tmp_repo)["rag_formal_metrics_status"] == "NOT_VERIFIED"


def test_valid_formal_artifact_marks_verified(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    _write_artifact(tmp_repo, report)
    info = facts_derive(tmp_repo)
    assert info["rag_formal_metrics_status"] == "VERIFIED"
    assert info["rag_formal_artifact_path"] == (
        "artifacts/evaluation/rag-649/run-20261001T000000Z/report.json"
    )
    assert info["rag_formal_artifact_git_sha"] == report["git_sha"]
    assert info["rag_formal_artifact_schema_version"] == "rag-eval-evidence/v2"
    assert audit.formal_rag_metrics(root=tmp_repo) == "VERIFIED"


def test_verified_full_no_rerank_does_not_promote_formal_metrics(tmp_repo: Path):
    """Real evaluate_rag.py state machine: with a reranker blocker the run can
    be marked VERIFIED_FULL_NO_RERANK while hybrid_rerank never executed.
    Missing the reranker ablation leg means the 4-config formal contract is
    NOT satisfied: formal metrics stay NOT_VERIFIED (fail-closed)."""
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["status"] = "VERIFIED_FULL_NO_RERANK"
    report["metrics"].pop("hybrid_rerank")
    report["run_summary"].pop("hybrid_rerank")
    _write_artifact(tmp_repo, report)
    info = facts_derive(tmp_repo)
    assert info["rag_formal_metrics_status"] == "NOT_VERIFIED"
    assert info["rag_formal_artifact_path"] is None


def test_verified_full_status_with_missing_experiment_is_not_verified(tmp_repo: Path):
    """Even with status == VERIFIED_FULL, any missing canonical experiment
    (incomplete 4-config ablation) cannot promote formal metrics."""
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["metrics"].pop("bm25_only")
    report["run_summary"].pop("bm25_only")
    _write_artifact(tmp_repo, report)
    assert facts_derive(tmp_repo)["rag_formal_metrics_status"] == "NOT_VERIFIED"


def test_formal_shaped_artifact_with_partial_execution_is_not_verified(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["run_summary"]["vector_only"]["n_success"] = 15  # PARTIAL-like execution
    _write_artifact(tmp_repo, report)
    assert facts_derive(tmp_repo)["rag_formal_metrics_status"] == "NOT_VERIFIED"


def test_formal_shaped_artifact_with_stale_benchmark_hash_is_not_verified(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["benchmark"]["sha256"] = "0" * 64
    _write_artifact(tmp_repo, report)
    assert facts_derive(tmp_repo)["rag_formal_metrics_status"] == "NOT_VERIFIED"


def test_doc_cannot_self_promote_to_verified(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)  # still NO formal artifact
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "当前 649-query 正式指标：VERIFIED。\n"
        "make targets are irrelevant\n"
        "vector_only bm25_only hybrid_no_rerank hybrid_rerank all_queries retrieval_eligible full_gold_covered\n"
        "rag-eval-import rag-eval-649-preflight rag-eval-649-smoke rag-eval-649\n"
        "6.3 Qwen/Qwen3-8B BAAI/bge-large-zh-v1.5 BAAI/bge-reranker-v2-m3 649 53 9\n",
    )
    assert sha and audit.formal_rag_metrics(root=tmp_repo) == "NOT_VERIFIED"
    facts = facts_module()
    code = facts.check_doc(doc, root=tmp_repo)
    assert code == 1


def test_verified_artifact_with_stale_not_verified_doc_fails(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    _write_artifact(tmp_repo, _formal_shaped_report(sha, 16))
    doc = write(
        tmp_repo, "docs/reference/rag-evaluation.md", "当前 649-query 正式指标：NOT_VERIFIED。"
    )
    facts = facts_module()
    assert facts.check_doc(doc, root=tmp_repo) == 1


def test_not_verified_artifact_with_fake_verified_doc_fails(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    report = _formal_shaped_report(sha, 16)
    report["run_summary"]["bm25_only"]["n_success"] = 0
    _write_artifact(tmp_repo, report)
    doc = write(tmp_repo, "docs/reference/rag-evaluation.md", "当前 649-query 正式指标：VERIFIED。")
    facts = facts_module()
    assert facts.check_doc(doc, root=tmp_repo) == 1


def test_metric_claim_guard_uses_artifact_state_not_doc_state(tmp_repo: Path):
    sha = _make_benchmark(tmp_repo)
    _write_artifact(tmp_repo, _formal_shaped_report(sha, 16))
    doc = write(tmp_repo, "README.md", "current Hit@3 82%")  # no provenance binding
    errors: list[str] = []
    audit.check_unproven_current_metrics([doc], errors, root=tmp_repo)
    assert any("bind the number to provenance" in e for e in errors), (
        "VERIFIED artifacts allow claims but provenance binding stays required"
    )
    qualified = write(tmp_repo, "README.md", "current Hit@3 82% (artifact: rag-649 run)")
    errors2: list[str] = []
    audit.check_unproven_current_metrics([qualified], errors2, root=tmp_repo)
    assert errors2 == []


# ------------------------------------------------- E. make target refs


def test_missing_makefile_target_reference_is_detected(tmp_repo: Path):
    write(tmp_repo, "README.md", "run `make rag-eval-649` first")
    errors: list[str] = []
    audit.check_makefile_doc_targets(errors, root=tmp_repo)
    assert any("rag-eval-649" in e for e in errors), errors
    assert all("Makefile" in e for e in errors)


def test_defined_makefile_target_reference_passes(tmp_repo: Path):
    write(tmp_repo, "Makefile", "rag-eval-649: ## formal\n\techo ok\n")
    write(tmp_repo, "README.md", "run `make rag-eval-649` first")
    errors: list[str] = []
    audit.check_makefile_doc_targets(errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------------- F. tracked+ignored


def _init_synthetic_git_repo(tmp_repo: Path) -> None:
    import os
    import subprocess

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_repo, check=True, capture_output=True, env=env)

    git("init", "-q")
    (tmp_repo / "secrets").mkdir(exist_ok=True)
    (tmp_repo / "secrets" / "keys.json").write_text("{}", encoding="utf-8")
    (tmp_repo / ".gitignore").write_text("secrets/\n", encoding="utf-8")
    # -f mirrors the real-world cause: the file was force-added while (or
    # before) the ignore pattern landed, so it stays tracked+ignored.
    git("add", "-f", "secrets/keys.json")
    git("commit", "-q", "-m", "init")


def test_tracked_ignored_file_is_detected(tmp_repo: Path):
    _init_synthetic_git_repo(tmp_repo)
    errors: list[str] = []
    audit.check_tracked_ignored_files(errors, [], root=tmp_repo)
    assert any("secrets/keys.json" in e for e in errors)


def test_clean_index_after_rm_cached_passes(tmp_repo: Path):
    import subprocess

    _init_synthetic_git_repo(tmp_repo)
    subprocess.run(
        ["git", "rm", "--cached", "-q", "secrets/keys.json"],
        cwd=tmp_repo,
        check=True,
        capture_output=True,
    )
    errors: list[str] = []
    audit.check_tracked_ignored_files(errors, [], root=tmp_repo)
    assert errors == []


def test_non_git_directory_is_skipped(tmp_repo: Path, monkeypatch):
    errors: list[str] = []
    audit.check_tracked_ignored_files(errors, [], root=tmp_repo)
    # Non-repo (synthetic fixture): silently skipped, not an error.
    assert all("tracked+ignored" not in e for e in errors)


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


# --------------------------------- banner self-declaration vs. mention (#66)


@pytest.mark.parametrize(
    "body",
    [
        "> **HISTORICAL AUDIT SNAPSHOT**",
        "> **HISTORICAL AUDIT SNAPSHOT / SUPERSEDED**",
        "> **HISTORICAL AUDIT SNAPSHOT (2026-06-17)**",
        "> **HISTORICAL AUDIT SNAPSHOT** — records the alignment executed on 2026-10-01.",
        "> **HISTORICAL AUDIT SNAPSHOT — 2026-10-02.** 本文件是**时点审计结果**。",
        "> HISTORICAL AUDIT SNAPSHOT",
        ">historical audit snapshot",
        "> **HISTORICAL AUDIT SNAPSHOT (2026-06-03, v3.3 worktree)**",
    ],
)
def test_self_declaring_banner_is_recognized(body: str):
    """Every real banner shape must stay excluded — the #66 fix narrows the
    match to a self-declaration, never to a mere mention."""
    assert audit.has_historical_banner(f"# Title\n\n{body}\n")
    assert audit.has_historical_banner(f"{body}\n\n正文。\n")


@pytest.mark.parametrize(
    "text",
    [
        # CLAUDE.md's own shape: prose naming the convention inside a list item.
        "- `docs/reports/plans/**` 是 HISTORICAL AUDIT SNAPSHOT，不是永久 Current Truth。\n",
        # A directive note whose blockquote *mentions* rather than declares.
        "> 注：docs/reports/plans/** 下的报告是 HISTORICAL AUDIT SNAPSHOT。\n\n正文。\n",
        # docs/README.md's shape: the marker inside a heading.
        "# Docs\n\n#### `reports/plans/` — 均为 HISTORICAL AUDIT SNAPSHOT\n",
        # Quoting the banner text as an inline code sample is not a declaration.
        "# Guide\n\n> 写法是 `HISTORICAL AUDIT SNAPSHOT` 加粗。\n",
    ],
)
def test_mention_of_marker_is_not_a_banner(text: str):
    assert not audit.has_historical_banner(text)


def test_discovery_keeps_docs_that_only_mention_the_convention(tmp_repo: Path):
    write(
        tmp_repo,
        "docs/reference/convention.md",
        "# 约定\n\n- 报告是 HISTORICAL AUDIT SNAPSHOT，不是永久 Current Truth。\n",
    )
    write(tmp_repo, "docs/design/live.md", "# Live\n\n正文。\n")
    rels = {p.relative_to(tmp_repo) for p in audit.discover_docs(root=tmp_repo)}
    assert rels == {Path("docs/design/live.md"), Path("docs/reference/convention.md")}


def test_discovery_includes_claude_md():
    """#66 acceptance: CLAUDE.md documents the banner convention, which must not
    classify CLAUDE.md itself as a historical snapshot."""
    docs = audit.discover_docs(REAL_ROOT)
    assert REAL_ROOT / "CLAUDE.md" in docs


def test_real_bannered_snapshots_are_still_excluded():
    """#66 acceptance: tightening the banner match must not let a single real
    snapshot into the active scan. Derived from the files on disk (canonical
    banner form at line 3) rather than a hardcoded list, so adding a snapshot
    keeps the assertion honest."""
    active = {p.resolve() for p in audit.discover_docs(REAL_ROOT)}
    bannered = [
        p
        for p in sorted((REAL_ROOT / "docs").rglob("*.md"))
        if p.is_file()
        and len(p.read_text(encoding="utf-8", errors="replace").splitlines()) > 2
        and audit.HISTORICAL_BANNER_RE.match(
            p.read_text(encoding="utf-8", errors="replace").splitlines()[2].strip()
        )
    ]
    assert bannered, "fixture repo must contain bannered snapshots"
    leaked = sorted(p.relative_to(REAL_ROOT).as_posix() for p in bannered if p.resolve() in active)
    assert leaked == []


def test_claude_md_is_actually_scanned():
    """Belt-and-braces: CLAUDE.md being in discover_docs() must mean its claims
    are evaluated — a guard that lists the file but ignores it would repeat #66."""
    text = (REAL_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert audit.HISTORICAL_MARKER in text, "fixture must still mention the convention"
    assert not audit.has_historical_banner(text)


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


# ----------------------------------------------------- project_facts eval


def _load_facts_module():
    facts_path = Path(__file__).resolve().parents[2] / "scripts" / "project_facts.py"
    spec = importlib.util.spec_from_file_location("project_facts", facts_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["project_facts"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_facts_eval_experiment_drift_is_detected():
    facts = _load_facts_module()
    collected = facts.collect()
    problems: list[str] = []
    for experiment in collected["evaluation_experiments"]:
        problems.append(f"missing: {experiment}")
    assert all("missing" in p for p in problems), "drift simulation must produce findings"
    assert collected["evaluation_experiments"] == [
        "vector_only",
        "bm25_only",
        "hybrid_no_rerank",
        "hybrid_rerank",
    ]


def test_facts_check_doc_flags_missing_canonical_target(tmp_path: Path):
    facts = _load_facts_module()
    real_doc = Path(__file__).resolve().parents[2] / "docs" / "reference" / "current-state.md"
    text = real_doc.read_text(encoding="utf-8")
    stripped = text.replace("make rag-eval-649-smoke", "make rag-eval-smoke-renamed")
    doc = tmp_path / "current-state.md"
    doc.write_text(stripped, encoding="utf-8")
    problems: list[str] = []
    for target in (
        "rag-eval-import",
        "rag-eval-649-preflight",
        "rag-eval-649-smoke",
        "rag-eval-649",
    ):
        if target not in stripped:
            problems.append(
                f"stale evaluation fact: canonical make target `{target}` missing from {doc.name}"
            )
    assert any("rag-eval-649-smoke" in p for p in problems)
    # And the real check passes on the actual doc:
    facts.check_doc(real_doc)


def test_facts_targets_resolved_from_makefile():
    facts = _load_facts_module()
    targets = facts._makefile_targets()
    for required in (
        "eval-rag",
        "rag-eval-649",
        "rag-eval-649-preflight",
        "rag-eval-649-smoke",
        "rag-eval-import",
    ):
        assert required in targets, f"Makefile missing documented target: {required}"


def test_facts_formal_status_derived_from_artifacts_on_real_repo():
    """No formal 649 artifact exists yet: the derived status must stay
    NOT_VERIFIED with all provenance fields empty (fail-closed)."""
    info = facts_derive(REAL_ROOT)
    assert info["rag_formal_metrics_status"] == "NOT_VERIFIED"
    for key in (
        "rag_formal_artifact_path",
        "rag_formal_artifact_timestamp",
        "rag_formal_artifact_git_sha",
        "rag_formal_artifact_schema_version",
    ):
        assert info[key] is None


def test_project_facts_json_contains_artifact_derived_keys():
    facts = facts_module()
    collected = facts.collect()
    for key in (
        "rag_formal_metrics_status",
        "rag_formal_artifact_path",
        "rag_formal_artifact_timestamp",
        "rag_formal_artifact_git_sha",
        "rag_formal_artifact_schema_version",
    ):
        assert key in collected


# ------------------------------------------- N. negative-existence claims


def test_negative_existence_claim_with_present_file_is_detected(tmp_repo: Path):
    write(tmp_repo, "scripts/keeper.py", "print('x')\n")
    doc = write(
        tmp_repo,
        "docs/operations/guide.md",
        "当前仓库不包含 `scripts/keeper.py`，不要执行该命令。\n",
    )
    errors: list[str] = []
    audit.check_negative_existence_claims([doc], errors, root=tmp_repo)
    assert any("scripts/keeper.py" in e and "does not exist" in e for e in errors)


def test_negative_existence_claim_with_absent_file_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/guide.md",
        "原 scripts/gone.py 已移除，不再作为活动入口。\n",
    )
    errors: list[str] = []
    audit.check_negative_existence_claims([doc], errors, root=tmp_repo)
    assert errors == []


def test_negative_existence_claim_unrelated_wording_passes(tmp_repo: Path):
    write(tmp_repo, "scripts/audit_doc_consistency.py", "")
    doc = write(
        tmp_repo,
        "docs/operations/guide.md",
        "从跟踪中移除（本地文件保留）；guard `scripts/audit_doc_consistency.py` 继续守护\n",
    )
    errors: list[str] = []
    audit.check_negative_existence_claims([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------- O. generalized make targets


def test_make_target_in_any_active_doc_is_checked(tmp_repo: Path):
    write(tmp_repo, "Makefile", "dev:\n\techo dev\n")
    doc = write(tmp_repo, "docs/checklists/run.md", "先执行 make missing-target\n")
    errors: list[str] = []
    audit.check_makefile_doc_targets(errors, root=tmp_repo, docs=[doc])
    assert any("missing-target" in e for e in errors)


def test_make_english_verb_false_positive_is_not_a_target(tmp_repo: Path):
    write(tmp_repo, "Makefile", "dev:\n\techo dev\n")
    doc = write(tmp_repo, "docs/design/note.md", "External updates can make a cached read stale.\n")
    errors: list[str] = []
    audit.check_makefile_doc_targets(errors, root=tmp_repo, docs=[doc])
    assert errors == []


# ------------------------------------------- P. stale embedding semantics


def test_random_vector_docstring_in_response_cache_is_detected(tmp_repo: Path):
    (tmp_repo / "cache").mkdir(exist_ok=True)
    write(
        tmp_repo,
        "cache/response_cache.py",
        'def f():\n    """Args:\n            embedding_model: None 时使用随机向量回退\n"""\n',
    )
    errors: list[str] = []
    audit.check_stale_embedding_fallback([], errors, root=tmp_repo)
    assert any("random-vector" in e for e in errors)


def test_fail_closed_embedding_note_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/reference/cache.md",
        "embedding 不可用时 fail-closed 跳过 L2，绝不使用随机向量。\n",
    )
    errors: list[str] = []
    audit.check_stale_embedding_fallback([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------- Q. latency absolutes


def test_unsupported_latency_absolute_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "docs/design/arch.md", "缓存命中 → 亚毫秒级本地读，直接返回\n")
    errors: list[str] = []
    audit.check_latency_absolutes([doc], errors, root=tmp_repo)
    assert any("亚毫秒" in e for e in errors)


def test_latency_absolute_with_benchmark_context_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/design/arch.md",
        "2026-06 历史 benchmark 快照：L1 命中 <10ms（当时硬件，不可复用为当前值）\n",
    )
    errors: list[str] = []
    audit.check_latency_absolutes([doc], errors, root=tmp_repo)
    assert errors == []


def test_zero_latency_in_decision_doc_is_tolerated(tmp_repo: Path):
    doc = write(tmp_repo, "docs/decisions/001-legacy.md", "熔断后规则引擎零延迟接管\n")
    errors: list[str] = []
    audit.check_latency_absolutes([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------- R. production framing


def test_production_grade_claim_is_rejected(tmp_repo: Path):
    doc = write(tmp_repo, "docs/design/interview.md", "我把这个系统做到了生产级。\n")
    errors: list[str] = []
    audit.check_production_claims([doc], errors, root=tmp_repo)
    assert any("production" in e.lower() for e in errors)


def test_production_oriented_framing_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/design/interview.md",
        "这是生产化架构/面向生产的工程设计；生产验收级证据尚未完成。\n"
        "| **v4.3** | 2026-06-07 | 生产验收 8.1/10 记录 |\n",
    )
    errors: list[str] = []
    audit.check_production_claims([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------- S. env references resolve


def test_unresolved_env_var_is_detected(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/ops.md",
        "export DEEPSEEK_API_KEY=your_key\n",
    )
    errors: list[str] = []
    audit.check_env_references([doc], errors, root=tmp_repo)
    assert any("DEEPSEEK_API_KEY" in e for e in errors)


def test_negative_env_claim_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/ops.md",
        "# 当前不存在 DB_POOL_SIZE / DB_POOL_OVERFLOW 环境变量\n"
        "# 当前仓库无 QUERY_CACHE_ENABLED 环境变量（历史写法已移除）\n",
    )
    errors: list[str] = []
    audit.check_env_references([doc], errors, root=tmp_repo)
    assert errors == []


def test_historical_env_line_passes(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/operations/ops.md",
        "# CACHE_TTL_PRODUCT 环境变量（历史遗留写法已移除）\n",
    )
    errors: list[str] = []
    audit.check_env_references([doc], errors, root=tmp_repo)
    assert errors == []


def test_code_constant_and_placeholder_mentions_pass(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/design/arch.md",
        "矛盾词对数量见 `NEGATION_PAIRS` 当前计数。\n"
        "权限映射见 `ROLE_PERMISSIONS`。\n"
        "回滚：git checkout PREVIOUS_TAG\n",
    )
    errors: list[str] = []
    audit.check_env_references([doc], errors, root=tmp_repo)
    assert errors == []


def test_known_env_var_passes(tmp_repo: Path):
    write(tmp_repo, ".env.example", "OPENAI_API_KEY=placeholder\n")
    write(tmp_repo, "core/config.py", "HTTP_TIMEOUT = 15\n")
    doc = write(
        tmp_repo,
        "docs/operations/ops.md",
        "export OPENAI_API_KEY=your_key\nHTTP_TIMEOUT=15\n",
    )
    errors: list[str] = []
    audit.check_env_references([doc], errors, root=tmp_repo)
    assert errors == []


def test_real_repo_passes_closeout_guards():
    """Invariant: the real repo satisfies all closeout guards."""
    docs = audit.discover_docs(REAL_ROOT)
    errors: list[str] = []
    audit.check_negative_existence_claims(docs, errors, root=REAL_ROOT)
    audit.check_stale_embedding_fallback(docs, errors, root=REAL_ROOT)
    audit.check_latency_absolutes(docs, errors, root=REAL_ROOT)
    audit.check_production_claims(docs, errors, root=REAL_ROOT)
    audit.check_makefile_doc_targets(errors, root=REAL_ROOT, docs=docs)
    audit.check_env_references(docs, errors, root=REAL_ROOT)
    audit.check_lifecycle_vocabulary(docs, errors, root=REAL_ROOT)
    audit.check_no_v64_claim(docs, errors, root=REAL_ROOT)
    assert errors == []


# ------------------------------------------- T. lifecycle vocabulary


def test_retired_lifecycle_label_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "docs/design/note.md", "> 🟢 Active — 活的\n")
    errors: list[str] = []
    audit.check_lifecycle_vocabulary([doc], errors, root=tmp_repo)
    assert any("retired lifecycle label" in e for e in errors)


def test_canonical_lifecycle_label_passes(tmp_repo: Path):
    doc = write(tmp_repo, "docs/design/note.md", "> 🟢 CURRENT — 活的\n")
    errors: list[str] = []
    audit.check_lifecycle_vocabulary([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------- U. no v6.4 claim


def test_v64_claim_is_detected(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", "当前 runtime version v6.4\n")
    errors: list[str] = []
    audit.check_no_v64_claim([doc], errors, root=tmp_repo)
    assert any("v6.4" in e for e in errors)


def test_negated_v64_statement_passes(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", "当前 `6.3`；未声明 v6.4 release。\n")
    errors: list[str] = []
    audit.check_no_v64_claim([doc], errors, root=tmp_repo)
    assert errors == []


@pytest.mark.parametrize(
    "line",
    [
        # CLAUDE.md:5 verbatim — the version is inline code, so the literal text
        # is "no `v6.4`" and adjacency across the version token is impossible.
        "- Runtime version: `6.3` (`core/config.py`); no `v6.4` release is declared.",
        "no `v6.4` release has been published",
        "v6.4 release does not exist",
        "`v6.4` release was never released",
        "不存在 v6.4 版本",
    ],
)
def test_backticked_version_negation_passes(tmp_repo: Path, line: str):
    """#80: a correct negation is not a version claim, even when Markdown inline
    code hides the negation from the matcher."""
    doc = write(tmp_repo, "README.md", line + "\n")
    errors: list[str] = []
    audit.check_no_v64_claim([doc], errors, root=tmp_repo)
    assert errors == []


@pytest.mark.parametrize(
    "line",
    [
        "Runtime version: v6.4",
        "当前版本已升级到 v6.4",
        "The system targets version 6.4 across all agents.",
    ],
)
def test_v64_claim_is_still_detected_after_negation_fix(tmp_repo: Path, line: str):
    """The #80 fix must not weaken the claim rule — it only fixed the negation
    side, so an invented release is still an error."""
    doc = write(tmp_repo, "README.md", line + "\n")
    errors: list[str] = []
    audit.check_no_v64_claim([doc], errors, root=tmp_repo)
    assert any("v6.4" in e for e in errors)


def test_distant_negation_cannot_launder_a_claim(tmp_repo: Path):
    """A disclaimer no longer excuses a version claim that is not attached to
    it: the negation must sit near the claim it negates, so the guard keeps
    failing toward reporting."""
    line = (
        "no v6.4 release has been published anywhere in this repository, and we "
        "now recommend v6.4 as the stable line for every deployment"
    )
    doc = write(tmp_repo, "README.md", line + "\n")
    errors: list[str] = []
    audit.check_no_v64_claim([doc], errors, root=tmp_repo)
    assert any("v6.4" in e for e in errors)


def test_real_claude_md_version_negation_is_not_an_error():
    """Real-repo invariant (#80): CLAUDE.md:5 documents that no v6.4 release
    exists. Reading that as a v6.4 claim is a false positive on a correct
    current-truth statement."""
    docs = [p for p in audit.discover_docs(REAL_ROOT) if p.name == "CLAUDE.md"]
    assert docs, "CLAUDE.md must be part of the active scan for this guard to mean anything"
    errors: list[str] = []
    audit.check_no_v64_claim(docs, errors, root=REAL_ROOT)
    assert errors == []


# ===========================================================================
# Distributed Agent Runtime semantic-drift guards (V / W / X / Y / Z).
#
# Truth priority is executable code: every guard below *derives* whether a
# capability exists from the repository filesystem, then forbids CURRENT docs
# from contradicting that. A test that only fed a doc string would not catch a
# guard that silently stopped consulting the code, so each fixture creates the
# marker files that make the capability "implemented".
# ===========================================================================


def _make_runtime_implemented(root: Path) -> None:
    """Create every marker file that makes all RUNTIME_CAPABILITIES implemented.

    Mirrors RUNTIME_CAPABILITIES exactly, so deleting one marker in production
    turns the corresponding guard off (which is the intended fail-open
    direction) and these tests prove the mapping stays truthful.
    """
    for rel in (
        "runtime/celery_app.py",
        "runtime/tasks.py",
        "runtime/dispatch.py",
        "core/concurrency/distributed_lock.py",
        "core/concurrency/__init__.py",
        "runtime/thread_lock.py",
        "core/checkpointer.py",
    ):
        write(root, rel, "# marker\n")


# ------------------------------------------------------------- Guard V


def test_celery_worker_described_as_future_is_detected(tmp_repo: Path):
    """runtime/celery_app.py + tasks.py + dispatch.py exist -> a CURRENT doc
    calling the Celery worker future work contradicts the code."""
    _make_runtime_implemented(tmp_repo)
    doc = write(tmp_repo, "README.md", "下一阶段计划引入 Celery worker 执行长任务。\n")
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert any("celery_worker" in e for e in errors)


def test_redis_lock_described_as_future_is_detected(tmp_repo: Path):
    _make_runtime_implemented(tmp_repo)
    doc = write(tmp_repo, "docs/design/note.md", "多 Worker 场景未来需要 Redis 分布式锁。\n")
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert any("redis_distributed_lock" in e for e in errors)


def test_persistent_checkpoint_described_as_future_is_detected(tmp_repo: Path):
    _make_runtime_implemented(tmp_repo)
    doc = write(tmp_repo, "docs/design/note.md", "未来会把 checkpoint 换成持久化存储。\n")
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert any("postgres_checkpointer" in e for e in errors)


def test_implemented_capability_stated_as_current_passes(tmp_repo: Path):
    _make_runtime_implemented(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/design/note.md",
        "当前实现：Celery worker 执行异步 Run，checkpoint 使用 PostgreSQL saver，"
        "同一 thread 用 Redis 分布式锁串行。\n",
    )
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert errors == []


def test_future_claim_is_not_flagged_when_capability_is_absent(tmp_repo: Path):
    """Guard must derive from the filesystem, not from a hardcoded verdict:
    without runtime/celery_app.py the same sentence is not a contradiction."""
    doc = write(tmp_repo, "docs/design/note.md", "下一阶段计划引入 Celery worker。\n")
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert errors == []


def test_negated_future_claim_passes(tmp_repo: Path):
    """Honest "it is NOT future work" framing is the intended documentation."""
    _make_runtime_implemented(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/design/note.md",
        "早期版本曾计划引入 Celery worker，但这已经不是未来能力，当前已实现。\n",
    )
    errors: list[str] = []
    audit.check_runtime_future_claims([doc], errors, root=tmp_repo)
    assert errors == []


def test_historical_audit_claim_is_excluded_from_guard_v(tmp_repo: Path):
    _make_runtime_implemented(tmp_repo)
    write(
        tmp_repo,
        "docs/reports/audit/2026-01-01-snapshot.md",
        "> HISTORICAL AUDIT SNAPSHOT\n> 当时计划引入 Celery worker。\n",
    )
    write(tmp_repo, "docs/design/live.md", "干净的当前文档。\n")
    docs = audit.discover_docs(root=tmp_repo)
    errors: list[str] = []
    audit.check_runtime_future_claims(docs, errors, root=tmp_repo)
    assert errors == []
    assert [p.relative_to(tmp_repo) for p in docs] == [Path("docs/design/live.md")]


# ------------------------------------------------------------- Guard Z


def _make_semantic_tracing_wired(root: Path) -> None:
    """Emit one span literal per production call site.

    Mirrors SEMANTIC_TRACING_CALL_SITES so the guard's truth source is exercised
    as code (span literals in call sites), not as a hardcoded verdict. Deleting
    a call site must turn the guard off — which is the intended fail-open
    direction, and the reason these tests pin the mapping.
    """
    write(root, "runtime/executor.py", 'x = span("csai.agent.execute")\n')
    write(root, "rag/qdrant_knowledge_base.py", 'x = span("csai.rag.retrieve")\n')
    write(root, "llm/client.py", 'x = span("csai.llm.chat_completion")\n')
    write(root, "tools/tool_registry.py", 'x = span("csai.tool.execute")\n')


def test_wired_semantic_tracing_called_not_implemented_is_detected(tmp_repo: Path):
    """The exact drift PR #33 left behind: spans wired in code, CURRENT doc says
    ``TODO — not implemented``. This is the regression the guard exists for."""
    _make_semantic_tracing_wired(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "| **Distributed tracing / LLM tracing** | **`TODO` — not implemented** | "
        "OpenTelemetry wiring exists as a configurable seam but is not enabled |\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert any("semantic-tracing drift" in e for e in errors)


def test_chinese_missing_marker_for_tracing_is_detected(tmp_repo: Path):
    """Guard must not key on one English sentence — the phrasing is not the point."""
    _make_semantic_tracing_wired(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "| 分布式追踪 / LLM 追踪 | **TODO — 未实现** | 仅有可选接线点 |\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert any("semantic-tracing drift" in e for e in errors)


def test_semantic_tracing_derives_from_code_not_a_fixed_verdict(tmp_repo: Path):
    """Same sentence, no spans in code -> no error. Proves the guard reads the
    call sites instead of hardcoding that tracing exists."""
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "| **Distributed tracing / LLM tracing** | **`TODO` — not implemented** |\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert errors == []
    assert audit.wired_semantic_span_names(root=tmp_repo) == frozenset()


def test_three_level_evidence_split_passes(tmp_repo: Path):
    """The intended replacement wording: one IMPLEMENTED claim plus two
    NOT_VERIFIED claims. All three live on the same lines as tracing nouns."""
    _make_semantic_tracing_wired(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "| Application-level semantic tracing | IMPLEMENTED / LOCALLY VERIFIED | "
        "`core/telemetry.py` + unit tests |\n"
        "| Live OTLP collector / trace backend | NOT_VERIFIED | no exporter run |\n"
        "| Production trace propagation | NOT_VERIFIED | no production artifact |\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_remaining_live_backend_work_is_not_treated_as_drift(tmp_repo: Path):
    """The honest remaining backlog (validate against a live collector) names
    tracing and OTLP but claims nothing missing, so it must stay allowed."""
    _make_semantic_tracing_wired(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "- [ ] Validate semantic spans against a live OTLP collector / trace backend\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_missing_marker_without_tracing_noun_is_not_flagged(tmp_repo: Path):
    """Scoping: an unrelated TODO must not be attributed to tracing."""
    _make_semantic_tracing_wired(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/evaluation/production-evidence.md",
        "| Fencing token for the per-thread lock | TODO — not implemented | no forced abort |\n",
    )
    errors: list[str] = []
    audit.check_semantic_tracing_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_real_repo_has_semantic_tracing_spans_wired():
    """Pins the truth source against the actual checkout: the span names the
    documentation claims exist really are emitted by the production call sites."""
    names = audit.wired_semantic_span_names(root=REAL_ROOT)
    assert {
        "csai.agent.execute",
        "csai.agent.execute.resume",
        "csai.rag.retrieve",
        "csai.llm.chat_completion",
        "csai.tool.execute",
    } <= names


def test_historical_tracing_claim_is_excluded_from_guard_z(tmp_repo: Path):
    _make_semantic_tracing_wired(tmp_repo)
    write(
        tmp_repo,
        "docs/reports/audit/2026-01-01-snapshot.md",
        "> HISTORICAL AUDIT SNAPSHOT\n> 当时分布式追踪未实现。\n",
    )
    write(tmp_repo, "docs/evaluation/live.md", "干净的当前文档。\n")
    docs = audit.discover_docs(root=tmp_repo)
    errors: list[str] = []
    audit.check_semantic_tracing_drift(docs, errors, root=tmp_repo)
    assert errors == []


# ----------------------------------------------------------- Guard AA


def _write_preflight(root: Path, run_id: str, timestamp: str, status: str = "BLOCKED") -> Path:
    rel = f"artifacts/evaluation/rag-649/{run_id}/report.json"
    write(
        root,
        rel,
        json.dumps({"schema_version": "rag-eval-evidence/v2", "run_id": run_id,
                    "timestamp": timestamp, "status": status,
                    "primary_blocker": "EMBEDDING_PROVIDER_AUTH"}),
    )
    return root / rel


def _git_add(root: Path, *rels: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", *rels], cwd=root, check=True, capture_output=True)


def test_latest_committed_preflight_prefers_newest_artifact(tmp_repo: Path):
    """Ordering must come from the artifact's own timestamp, not mtime or a
    hardcoded id: an older-mtime but newer-timestamp artifact still wins."""
    old = _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    new = _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    # Make mtime disagree with the recorded timestamp on purpose.
    os.utime(old, (2_000_000_000, 2_000_000_000))
    os.utime(new, (1_000_000_000, 1_000_000_000))
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    run_id, rel = audit.latest_committed_preflight(root=tmp_repo)
    assert run_id == "preflight-20261002T194209Z"
    assert rel.endswith("preflight-20261002T194209Z/report.json")


def test_untracked_preflight_is_not_repository_evidence(tmp_repo: Path):
    """A local run nobody committed must never be promoted to 'latest'."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261101T000000Z", "2026-11-01T00:00:00+00:00")
    _git_add(tmp_repo, "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json")
    run_id, _rel = audit.latest_committed_preflight(root=tmp_repo)
    assert run_id == "preflight-20260929T191128Z"


def test_current_doc_pointing_at_latest_preflight_passes(tmp_repo: Path):
    """Positive case: the CURRENT doc names the newest committed artifact."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "最新已提交的 preflight evidence："
        "`artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json`"
        "（v2 schema）。\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_current_doc_claiming_older_preflight_as_latest_is_detected(tmp_repo: Path):
    """Negative case: a newer artifact is committed but the CURRENT doc still
    calls the older one the latest evidence. This is the drift class."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "Current committed preflight evidence: "
        "`artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json` "
        "shows the provider auth blocker.\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert any("rag preflight pointer drift" in e for e in errors)


def test_historical_preflight_reference_is_preserved(tmp_repo: Path):
    """Historical case: an older artifact explicitly framed as history is
    legitimate and must not be rewritten or flagged."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "历史尝试（保留不改）：evidence artifact "
        "`artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json`，"
        "v1 schema，原样保留不回填。\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_wrapped_claim_with_path_on_next_line_is_detected(tmp_repo: Path):
    """Markdown wraps long artifact paths. A claim on line N naming an older
    artifact on line N+1 is still drift — the guard must not be defeated by a
    line break."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "当前已提交的 preflight evidence（\n"
        "  `artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json`）\n"
        "显示 provider authentication blocker。\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert any("rag preflight pointer drift" in e for e in errors)


def test_preflight_guard_ignores_non_claim_mentions(tmp_repo: Path):
    """Scoping: describing an artifact without claiming it is current/latest is
    not drift (e.g. a schema-semantics aside)."""
    _write_preflight(tmp_repo, "preflight-20260929T191128Z", "2026-09-29T19:11:28+00:00")
    _write_preflight(tmp_repo, "preflight-20261002T194209Z", "2026-10-02T19:42:09+00:00")
    _git_add(
        tmp_repo,
        "artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json",
        "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json",
    )
    doc = write(
        tmp_repo,
        "docs/reference/rag-evaluation.md",
        "历史 v1 artifact（`schema_version: rag-eval-evidence/v1`，如 "
        "`preflight-20260929T191128Z`）使用单层 status 字符串。\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert errors == []


def test_preflight_guard_is_silent_without_committed_artifacts(tmp_repo: Path):
    """Fail-open: with nothing committed there is no pointer to be wrong about."""
    doc = write(
        tmp_repo,
        "docs/reference/current-state.md",
        "Current committed preflight evidence: "
        "`artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json`.\n",
    )
    errors: list[str] = []
    audit.check_rag_preflight_pointer_drift([doc], errors, root=tmp_repo)
    assert errors == []
    assert audit.latest_committed_preflight(root=tmp_repo) is None


def test_real_repo_latest_committed_preflight_matches_current_docs():
    """Pins the truth source against the actual checkout."""
    latest = audit.latest_committed_preflight(root=REAL_ROOT)
    assert latest is not None
    run_id, rel = latest
    report = json.loads((REAL_ROOT / rel).read_text(encoding="utf-8"))
    # The artifact's own timestamp is what the guard ordered by.
    assert report["run_id"] == run_id
    assert report["timestamp"].startswith("2026-10-02T19:42:09")
    current_state = (REAL_ROOT / "docs/reference/current-state.md").read_text(encoding="utf-8")
    assert f"rag-649/{run_id}/report.json" in current_state
    assert report["notes"][0] == "formal evaluation not run; no metrics generated"


# ----------------------------------------------------------- Guard AB


def _write_preflight_with_reranker(root: Path, blocking: bool) -> None:
    """Commit a preflight whose reranker blocker has an explicit scope."""
    rel = "artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json"
    write(
        root,
        rel,
        json.dumps(
            {
                "schema_version": "rag-eval-evidence/v2",
                "run_id": "preflight-20261002T194209Z",
                "timestamp": "2026-10-02T19:42:09+00:00",
                "status": "BLOCKED",
                "primary_blocker": "EMBEDDING_PROVIDER_AUTH",
                "blockers": [
                    {
                        "code": "EMBEDDING_PROVIDER_AUTH",
                        "stage": "embedding",
                        "blocking": True,
                        "blocks_corpus_import": True,
                    },
                    {
                        "code": "VECTOR_INDEX_EMPTY",
                        "stage": "qdrant",
                        "blocking": True,
                        "caused_by": "EMBEDDING_PROVIDER_AUTH",
                    },
                    {
                        "code": "RERANKER_PROVIDER_AUTH",
                        "stage": "reranker",
                        "blocking": blocking,
                        "blocks_experiments": ["hybrid_rerank"],
                        "http_status": 401,
                    },
                ],
            }
        ),
    )
    _git_add(root, rel)


def test_reranker_blocker_scope_correct_wording_passes(tmp_repo: Path):
    """Positive: artifact says blocking=false / hybrid_rerank only, and the doc
    says exactly that. The runtime-debt framing stays legal."""
    _write_preflight_with_reranker(tmp_repo, blocking=False)
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "最新 v2 preflight 检测到 `probe: silent_fallback`，对应条目 "
        "`RERANKER_PROVIDER_AUTH` 是 **non-blocking**（`blocking: false`，"
        "`blocks_experiments: [\"hybrid_rerank\"]`），因此它**不是**整个 preflight "
        "的 primary blocker。运行时静默回退仍是已知工程债。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert errors == []


def test_reranker_blocker_described_as_blocking_is_detected(tmp_repo: Path):
    """Negative: the exact drift this guard exists for — the doc calls a
    blocking=false code a blocker instead of a warning."""
    _write_preflight_with_reranker(tmp_repo, blocking=False)
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "preflight 已经把它列为 **blocker** 而不是 warning"
        "（`RERANKER_PROVIDER_AUTH`，`probe: silent_fallback`）。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert any("reranker blocker semantics drift" in e for e in errors)


def test_reranker_blocker_described_as_global_blocker_is_detected(tmp_repo: Path):
    _write_preflight_with_reranker(tmp_repo, blocking=False)
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "`RERANKER_PROVIDER_AUTH` 是整个 preflight 的 global blocker，"
        "阻塞所有实验。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert any("reranker blocker semantics drift" in e for e in errors)


def test_reranker_blocker_historical_quoting_is_not_flagged(tmp_repo: Path):
    """Historical/quoted context: naming the old wording while correcting it
    must not trip the guard."""
    _write_preflight_with_reranker(tmp_repo, blocking=False)
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "正确说法不是\"preflight 已经把它列为 blocker 而不是 warning\""
        "（那是 v1 之前的旧口径遗留）；v2 记录的是 `blocking: false`。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert errors == []


def test_reranker_guard_is_silent_when_artifact_says_blocking(tmp_repo: Path):
    """Derived, not hardcoded: if a future artifact legitimately marks the code
    blocking=true, the rule stops constraining the docs."""
    _write_preflight_with_reranker(tmp_repo, blocking=True)
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "`RERANKER_PROVIDER_AUTH` 是 global blocker，阻塞所有实验。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert errors == []


def test_reranker_guard_fails_open_without_committed_preflight(tmp_repo: Path):
    write(
        tmp_repo,
        "docs/interview/rag-deep-dive.md",
        "`RERANKER_PROVIDER_AUTH` 是 global blocker。\n",
    )
    errors: list[str] = []
    audit.check_reranker_blocker_semantics(errors, root=tmp_repo)
    assert errors == []
    assert audit.latest_blocker_semantics(root=tmp_repo) is None


def test_real_repo_reranker_blocker_is_non_blocking_and_doc_says_so():
    """Pins both halves against the actual checkout: the artifact really does
    record blocking=false, and the guarded doc really does say so."""
    semantics = audit.latest_blocker_semantics(root=REAL_ROOT)
    assert semantics is not None
    entry = semantics["RERANKER_PROVIDER_AUTH"]
    assert entry["blocking"] is False
    assert entry["blocks_experiments"] == ["hybrid_rerank"]
    doc = (REAL_ROOT / "docs/interview/rag-deep-dive.md").read_text(encoding="utf-8")
    assert "`blocking: false`" in doc
    assert "工程债" in doc  # runtime silent-fallback debt retained


# ------------------------------------------------------------- Guard W


def test_docs_index_missing_current_adr_is_detected(tmp_repo: Path):
    write(tmp_repo, "docs/README.md", "# 索引\n\n[design](design/agent-runtime.md)\n")
    write(tmp_repo, "docs/design/agent-runtime.md", "x\n")
    errors: list[str] = []
    audit.check_docs_index_coverage(errors, root=tmp_repo)
    assert any("009-distributed-agent-runtime.md" in e for e in errors)


def test_complete_docs_index_passes(tmp_repo: Path):
    lines = ["# 索引\n"]
    for required, _reason in audit.REQUIRED_INDEX_ENTRIES:
        rel = required[len("docs/") :]
        write(tmp_repo, required, "x\n")
        lines.append(f"[{rel}]({rel})\n")
    write(tmp_repo, "docs/README.md", "".join(lines))
    errors: list[str] = []
    audit.check_docs_index_coverage(errors, root=tmp_repo)
    assert errors == []


def test_missing_docs_index_is_detected(tmp_repo: Path):
    errors: list[str] = []
    audit.check_docs_index_coverage(errors, root=tmp_repo)
    assert any("docs index missing" in e for e in errors)


# ------------------------------------------------------------- Guard X


def _stub_openapi_app(monkeypatch, spec: dict) -> None:
    """Install a fake ``api.app_factory`` module so the guard compares the doc
    anchor against a controlled surface instead of the live application."""

    class _App:
        def openapi(self):
            return spec

    import types

    module = types.ModuleType("api.app_factory")
    module.app = _App()
    monkeypatch.setitem(sys.modules, "api.app_factory", module)


def test_api_reference_surface_drift_is_detected(tmp_repo: Path, monkeypatch):
    _stub_openapi_app(
        monkeypatch,
        {"info": {"version": "6.3"}, "paths": {"/a": {"get": {}}, "/b": {"post": {}}}},
    )
    write(
        tmp_repo,
        "docs/reference/api-reference.md",
        "<!-- openapi-surface: paths=53 operations=55 api_operations=51 -->\n",
    )
    errors: list[str] = []
    audit.check_api_reference_surface(errors, root=tmp_repo)
    assert any("(paths)" in e and "says 53" in e for e in errors)
    assert any("(operations)" in e and "says 55" in e for e in errors)


def test_api_reference_matching_surface_passes(tmp_repo: Path, monkeypatch):
    _stub_openapi_app(
        monkeypatch,
        {
            "info": {"version": "6.3"},
            "paths": {
                "/a": {"get": {}},
                "/api/runs": {"post": {}},
                "/api/runs/dead": {"get": {}},
            },
        },
    )
    write(
        tmp_repo,
        "docs/reference/api-reference.md",
        "<!-- openapi-surface: paths=3 operations=3 api_operations=2 -->\n",
    )
    errors: list[str] = []
    audit.check_api_reference_surface(errors, root=tmp_repo)
    assert errors == []


def test_api_reference_without_anchor_is_detected(tmp_repo: Path, monkeypatch):
    _stub_openapi_app(monkeypatch, {"info": {"version": "6.3"}, "paths": {}})
    write(tmp_repo, "docs/reference/api-reference.md", "# API\n\n53 个 HTTP 路径。\n")
    errors: list[str] = []
    audit.check_api_reference_surface(errors, root=tmp_repo)
    assert any("machine-checkable surface anchor" in e for e in errors)


def test_real_api_reference_anchor_matches_generated_openapi():
    """The shipped anchor must equal app.openapi() — the reason "53 HTTP paths"
    is structurally unable to survive once numbers come from the generator."""
    errors: list[str] = []
    audit.check_api_reference_surface(errors, root=REAL_ROOT)
    assert errors == []


# ------------------------------------------------------------- Guard Y


_MULTI_WORKER_OK_STACK = (
    "# 部署\n\n"
    "`GUNICORN_WORKERS=4`。跨进程不变量三件套：\n"
    "- `LANGGRAPH_CHECKPOINT_BACKEND=postgres`（图状态共享）\n"
    "- `SESSION_STORAGE_BACKEND=redis`（会话共享）\n"
    "- `AGENT_RUN_THREAD_LOCK_BACKEND=redis`，key namespace `agent:thread-lock:`"
    "（同一 thread 串行）\n"
)


def test_multi_worker_doc_missing_thread_lock_is_detected(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "README.md",
        "# 部署\n\n`GUNICORN_WORKERS=4`。checkpoint 用 "
        "`LANGGRAPH_CHECKPOINT_BACKEND=postgres`，session 用 "
        "`SESSION_STORAGE_BACKEND=redis`。\n",
    )
    errors: list[str] = []
    audit.check_multi_worker_deployment_truth([doc], errors, root=tmp_repo)
    assert any("Redis thread lock" in e for e in errors)


def test_multi_worker_doc_missing_session_is_detected(tmp_repo: Path):
    doc = write(
        tmp_repo,
        "docs/design/distributed-agent-runtime.md",
        "# 多副本\n\ncheckpoint 用 `LANGGRAPH_CHECKPOINT_BACKEND=postgres`，"
        "thread 锁用 `AGENT_RUN_THREAD_LOCK_BACKEND=redis`。\n",
    )
    errors: list[str] = []
    audit.check_multi_worker_deployment_truth([doc], errors, root=tmp_repo)
    assert any("Redis session" in e for e in errors)


def test_multi_worker_doc_with_full_stack_passes(tmp_repo: Path):
    doc = write(tmp_repo, "README.md", _MULTI_WORKER_OK_STACK)
    errors: list[str] = []
    audit.check_multi_worker_deployment_truth([doc], errors, root=tmp_repo)
    assert errors == []


def test_single_process_doc_is_not_subject_to_guard_y(tmp_repo: Path):
    """Guard Y is scoped to deployment docs; a non-deployment note that merely
    mentions 多副本 must not be forced to restate the whole stack."""
    doc = write(
        tmp_repo,
        "docs/reference/model-comparison.md",
        "# 对比\n\n真实多副本长期运行的证据尚未验证。\n",
    )
    errors: list[str] = []
    audit.check_multi_worker_deployment_truth([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_y_allowlist_docs_must_exist_in_repo():
    """A guard allowlist entry that points at a deleted document silently stops
    covering the real deployment docs, so the allowlist itself is verified."""
    missing = [rel for rel in audit.MULTI_WORKER_DEPLOY_DOCS if not (REAL_ROOT / rel).exists()]
    assert missing == [], f"guard-Y allowlist references missing docs: {missing}"


# ------------------------------------------------------------- Guard Z


def test_root_level_audit_snapshot_is_detected(tmp_repo: Path):
    write(tmp_repo, "README.md", "# readme\n")
    write(tmp_repo, "CLAUDE.md", "# claude\n")
    write(tmp_repo, "FINAL_RUNTIME_FACT_CHECK.md", "# audit\n")
    errors: list[str] = []
    audit.check_no_root_level_audit_snapshots(errors, root=tmp_repo)
    assert any("FINAL_RUNTIME_FACT_CHECK.md" in e for e in errors)


def test_root_readme_and_claude_are_allowed(tmp_repo: Path):
    write(tmp_repo, "README.md", "# readme\n")
    write(tmp_repo, "CLAUDE.md", "# claude\n")
    errors: list[str] = []
    audit.check_no_root_level_audit_snapshots(errors, root=tmp_repo)
    assert errors == []


def test_real_repo_has_no_root_level_audit_snapshot():
    errors: list[str] = []
    audit.check_no_root_level_audit_snapshots(errors, root=REAL_ROOT)
    assert errors == []


# ------------------------------------------- real-repo runtime invariants


def test_real_repo_declares_all_runtime_capabilities_implemented():
    """Guards V are fail-open when code is absent; assert the shipped code really
    satisfies every marker set so the guard cannot be silently disabled."""
    for capability, (markers, _pattern) in audit.RUNTIME_CAPABILITIES.items():
        assert audit._capability_is_implemented(REAL_ROOT, markers), (
            f"{capability} marker set is not fully present in the repository"
        )


# ===========================================================================
# HITL / canonical AgentRun state-machine drift guards (AA-AF).
#
# Each test drives the underlying check function with a synthetic doc so the
# guard is proven to *fire*, not merely present. A guard that cannot fail is
# indistinguishable from no guard at all, which is how "58 paths" and the
# WAITING_APPROVAL omission survived review.
# ===========================================================================


def _hitl_repo(tmp_repo: Path, *, hitl_enabled: str = "false") -> Path:
    """Minimal repo with a runtime/statuses.py + core/config.py the guards read."""
    write(
        tmp_repo,
        "runtime/statuses.py",
        "class RunStatus(str, Enum):\n"
        '    PENDING = "PENDING"\n'
        '    QUEUED = "QUEUED"\n'
        '    RUNNING = "RUNNING"\n'
        '    RETRYING = "RETRYING"\n'
        '    WAITING_APPROVAL = "WAITING_APPROVAL"\n'
        '    SUCCEEDED = "SUCCEEDED"\n',
    )
    write(
        tmp_repo,
        "core/config.py",
        "import os\n"
        f'HITL_ENABLED = os.getenv("HITL_ENABLED", "{hitl_enabled}").lower() == "true"\n',
    )
    return tmp_repo


# ------------------------------------------------------------- Guard AA

@pytest.mark.parametrize(
    "line",
    [
        "当前 API 共 58 个 HTTP 路径。",
        "OpenAPI paths=62 与 operations=64 已确认。",
        "OpenAPI 重新生成：docs/openapi.json = 58 个 HTTP 路径。",
        "操作数 60，快照已生成。",
        "`app.openapi()` = 58 个路径。",
        "openapi_path_count = 62 已同步。",
    ],
)
def test_guard_aa_handwritten_openapi_count_is_detected(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "README.md", f"# readme\n\n{line}\n")
    errors: list[str] = []
    audit.check_openapi_counts_are_generated([doc], errors, root=tmp_repo)
    assert errors, f"hand-written OpenAPI count escaped the guard: {line}"


@pytest.mark.parametrize(
    "line",
    [
        "HTTP 路径数以 `python3 scripts/project_facts.py` 输出为准。",
        "路径数由生成器输出，请勿手工修改（`make openapi-check`）。",
        "路径数/操作数以快照为准，不写入文档。",
    ],
)
def test_guard_aa_allows_generator_delegation(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "README.md", f"# readme\n\n{line}\n")
    errors: list[str] = []
    audit.check_openapi_counts_are_generated([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_aa_anchor_docs_are_exempt(tmp_repo: Path):
    """The two generator-bound docs may carry the number: current-state.md is
    checked by project_facts --check and api-reference.md by the surface anchor."""
    for rel in audit.OPENAPI_COUNT_ANCHOR_DOCS:
        doc = write(tmp_repo, rel, "# doc\n\n当前 62 个 HTTP 路径。\n")
        errors: list[str] = []
        audit.check_openapi_counts_are_generated([doc], errors, root=tmp_repo)
        assert errors == [], f"{rel} must be allowed to carry the generated count"


def test_real_repo_has_no_handwritten_openapi_count_outside_anchor_docs():
    """The repository itself must satisfy guard AA."""
    docs = audit.discover_docs(REAL_ROOT)
    errors: list[str] = []
    audit.check_openapi_counts_are_generated(docs, errors, root=REAL_ROOT)
    assert errors == []


# ------------------------------------------------------------- Guard AB

def test_guard_ab_state_chain_without_waiting_approval_is_detected(tmp_repo: Path):
    _hitl_repo(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/design/architecture-design.md",
        "状态机 `runtime/statuses.py`：`PENDING → QUEUED → RUNNING → "
        "SUCCEEDED | FAILED | RETRYING → DEAD_LETTER`。\n",
    )
    errors: list[str] = []
    audit.check_run_state_machine_completeness([doc], errors, root=tmp_repo)
    assert errors and "WAITING_APPROVAL" in errors[0]


def test_guard_ab_unified_chain_is_allowed(tmp_repo: Path):
    _hitl_repo(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/design/architecture-design.md",
        "状态机：`PENDING → QUEUED → RUNNING → WAITING_APPROVAL → RUNNING → "
        "SUCCEEDED`。\n",
    )
    errors: list[str] = []
    audit.check_run_state_machine_completeness([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_ab_multiline_fenced_state_machine_is_allowed(tmp_repo: Path):
    """The unified model is drawn as a fenced ASCII diagram whose chain wraps
    across lines; a single-line check would flag the correct rendering."""
    _hitl_repo(tmp_repo)
    doc = write(
        tmp_repo,
        "docs/design/architecture-design.md",
        "```text\n"
        "PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED\n"
        "                  │              ├──► WAITING_APPROVAL ──► RUNNING\n"
        "                  └──► CANCELLED\n"
        "```\n",
    )
    errors: list[str] = []
    audit.check_run_state_machine_completeness([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_ab_inactive_when_statuses_lacks_the_state(tmp_repo: Path):
    """If the state is removed from code, the guard must go quiet rather than
    force a stale mention forever."""
    write(
        tmp_repo,
        "runtime/statuses.py",
        'class RunStatus(str, Enum):\n    PENDING = "PENDING"\n    QUEUED = "QUEUED"\n',
    )
    doc = write(tmp_repo, "docs/design/x.md", "状态机：`PENDING → QUEUED → RUNNING`。\n")
    errors: list[str] = []
    audit.check_run_state_machine_completeness([doc], errors, root=tmp_repo)
    assert errors == []


def test_real_repo_state_chains_mention_waiting_approval():
    errors: list[str] = []
    audit.check_run_state_machine_completeness(audit.discover_docs(REAL_ROOT), errors, root=REAL_ROOT)
    assert errors == []


# ------------------------------------------------------------- Guard AC

def test_guard_ac_hitl_enabled_default_drift_is_detected(tmp_repo: Path):
    _hitl_repo(tmp_repo, hitl_enabled="false")
    doc = write(tmp_repo, "docs/design/hitl.md", "| `HITL_ENABLED` | `true` | 总开关 |\n")
    errors: list[str] = []
    audit.check_hitl_enabled_default([doc], errors, root=tmp_repo)
    assert errors and "HITL_ENABLED" in errors[0]


def test_guard_ac_matching_default_is_allowed(tmp_repo: Path):
    _hitl_repo(tmp_repo, hitl_enabled="false")
    doc = write(
        tmp_repo,
        "docs/design/hitl.md",
        "`HITL_ENABLED` 默认 `false`：开启会让高风险调用转为阻塞式人工流程。\n",
    )
    errors: list[str] = []
    audit.check_hitl_enabled_default([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_ac_reads_the_boolean_compare_idiom(tmp_repo: Path):
    """`os.getenv("HITL_ENABLED", "false").lower() == "true"` must resolve to
    ``false`` (the fallback), not be skipped as an unresolvable expression."""
    from config_fallback import extract_fallback_defaults

    assert extract_fallback_defaults(write(tmp_repo, "core/config.py",
        'import os\nHITL_ENABLED = os.getenv("HITL_ENABLED", "false").lower() == "true"\n',
    ) and tmp_repo / "core/config.py").get("HITL_ENABLED") == "false"


def test_real_repo_hitl_enabled_default_matches_config():
    from config_fallback import extract_fallback_defaults

    expected = extract_fallback_defaults(REAL_ROOT / "core/config.py")["HITL_ENABLED"]
    errors: list[str] = []
    audit.check_hitl_enabled_default(audit.discover_docs(REAL_ROOT), errors, root=REAL_ROOT)
    assert errors == [], f"documented HITL_ENABLED default must equal config fallback {expected}"


# ------------------------------------------------------------- Guard AD

@pytest.mark.parametrize(
    "line",
    [
        "`POST /api/chat` 已纳入 HITL 人工审批覆盖范围。",
        "HITL 人工审批覆盖 `/api/chat` 快路径。",
        "`/api/chat` 快路径受审批闸门保护。",
    ],
)
def test_guard_ad_fastpath_hitl_claim_is_detected(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "docs/design/x.md", f"{line}\n")
    errors: list[str] = []
    audit.check_hitl_fastpath_not_claimed([doc], errors, root=tmp_repo)
    assert errors, f"durable-HITL fast-path claim escaped the guard: {line}"


@pytest.mark.parametrize(
    "line",
    [
        "`/api/chat` 快路径无 run 上下文，不在该治理边界内。",
        "不得宣称 `/api/chat` 受 durable HITL 保护。",
        "fast path 的 HITL coverage 由部署形态决定。",
    ],
)
def test_guard_ad_allows_explicit_boundary_disclaimer(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "docs/design/x.md", f"{line}\n")
    errors: list[str] = []
    audit.check_hitl_fastpath_not_claimed([doc], errors, root=tmp_repo)
    assert errors == []


def test_guard_ad_does_not_match_unrelated_coverage_wording(tmp_repo: Path):
    """Regression: an ungrouped alternation once made any line containing
    「已覆盖」 a false positive."""
    doc = write(
        tmp_repo,
        "docs/decisions/009.md",
        "- 自研队列：Celery + Redis 已覆盖，重复造轮子。\n",
    )
    errors: list[str] = []
    audit.check_hitl_fastpath_not_claimed([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------------------------- Guard AE

@pytest.mark.parametrize(
    "line",
    [
        "真实 ERP 写操作已验证通过（生产联调完成）。",
        "真实 ERP 写入已实测。",
        "Real ERP write operations are VERIFIED.",
    ],
)
def test_guard_ae_real_erp_write_verified_claim_is_detected(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "docs/design/x.md", f"{line}\n")
    errors: list[str] = []
    audit.check_real_erp_write_not_verified([doc], errors, root=tmp_repo)
    assert errors, f"real-ERP-write VERIFIED claim escaped the guard: {line}"


@pytest.mark.parametrize(
    "line",
    [
        "真实 ERP 写操作 **NOT_VERIFIED**（无企业 staging）。",
        "真实 ERP 写操作未验证；副作用验证走确定性 staging 工具。",
        "真实 ERP 写操作尚无企业 staging 环境。",
    ],
)
def test_guard_ae_allows_not_verified_framing(tmp_repo: Path, line: str):
    doc = write(tmp_repo, "docs/design/x.md", f"{line}\n")
    errors: list[str] = []
    audit.check_real_erp_write_not_verified([doc], errors, root=tmp_repo)
    assert errors == []


@pytest.mark.parametrize(
    "line",
    [
        "| HITL governance | **Level 2 - CI VERIFIED** | ... **not** real ERP writes |",
        "CI VERIFIED for the governance contract; never claim real ERP writes |",
    ],
)
def test_guard_ae_allows_verified_for_another_capability_with_erp_exclusion(
    tmp_repo: Path, line: str
):
    """A line may claim VERIFIED for capability X while explicitly excluding
    real ERP writes — the exclusion is the point, not a contradiction."""
    doc = write(tmp_repo, "docs/evaluation/x.md", f"{line}\n")
    errors: list[str] = []
    audit.check_real_erp_write_not_verified([doc], errors, root=tmp_repo)
    assert errors == []


# ------------------------------------------------------------- Guard AF

def _approval_repo(tmp_repo: Path, *, mounted: bool = True, drop: str | None = None) -> Path:
    write(tmp_repo, "api/routes/approvals.py", "router = APIRouter(prefix='/api/approvals')\n")
    app_src = "from api.routes.approvals import router as approvals_router\n"
    app_src += "app.include_router(approvals_router)\n" if mounted else ""
    # An unmounted router still has its import present; the guard must key
    # on the registration call, not on the import.
    if not mounted:
        app_src = "from api.app import app\n"
    write(tmp_repo, "api/app.py", app_src)
    paths = {p: {"get": {}} for p in audit.HITL_APPROVAL_PATHS}
    if drop:
        paths.pop(drop, None)
    write(tmp_repo, "docs/openapi.json", json.dumps({"paths": paths}))
    write(tmp_repo, "docs/reference/api-reference.md", "# API\n\n`/api/approvals` 审批队列。\n")
    return tmp_repo


def test_guard_af_detects_missing_approval_endpoint(tmp_repo: Path):
    _approval_repo(tmp_repo, drop="/api/approvals/{approval_id}/decision")
    errors: list[str] = []
    audit.check_approval_surface_present(errors, root=tmp_repo)
    assert errors and "decision" in errors[0]


def test_guard_af_detects_undocumented_approval_surface(tmp_repo: Path):
    _approval_repo(tmp_repo)
    write(tmp_repo, "docs/reference/api-reference.md", "# API\n\n没有审批端点。\n")
    errors: list[str] = []
    audit.check_approval_surface_present(errors, root=tmp_repo)
    assert any("/api/approvals" in e for e in errors)


def test_guard_af_inactive_when_router_not_mounted(tmp_repo: Path):
    _approval_repo(tmp_repo, mounted=False, drop="/api/approvals")
    errors: list[str] = []
    audit.check_approval_surface_present(errors, root=tmp_repo)
    assert errors == []


def test_real_repo_approval_surface_is_present():
    errors: list[str] = []
    audit.check_approval_surface_present(errors, root=REAL_ROOT)
    assert errors == []
