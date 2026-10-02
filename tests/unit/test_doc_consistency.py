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
