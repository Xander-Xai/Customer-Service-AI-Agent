#!/usr/bin/env python3
"""Emit machine-readable current runtime facts (JSON).

All values are extracted from code/metadata at execution time — nothing is
hardcoded. Use for CI guards, release preparation, and documentation audits.

Usage:
    python3 scripts/project_facts.py            # JSON to stdout
    python3 scripts/project_facts.py --check docs/reference/current-state.md
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BENCHMARK_PATH = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
EVAL_SCRIPT = PROJECT_ROOT / "scripts" / "evaluate_rag.py"
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"


def _llm_facts() -> dict:
    import core.config as cfg

    return {
        "runtime_version": cfg.VERSION,
        "llm_provider_default": cfg.LLM_PROVIDER,
        "llm_model": cfg.OPENAI_MODEL,
        "llm_base_url": cfg.OPENAI_BASE_URL,
        "embedding_model": cfg.EMBEDDING_MODEL,
        "embedding_dim": cfg.EMBEDDING_DIM,
        "reranker_model": cfg.RERANKER_MODEL,
        "vector_db_mode": cfg.VECTOR_DB_MODE,
        "hybrid_search_enabled": cfg.HYBRID_SEARCH_ENABLED,
    }


def _openapi_path_count() -> int:
    from api.app_factory import app

    return len(app.openapi()["paths"])


def _agent_role_count() -> int:
    """Count concrete runtime agent roles registered by ServiceContainer.

    7 domain agents come from the agent_classes dict in ``_init_agents``;
    ReActAgent and ResponseAgent are registered separately, so the total is
    domain + 2. BaseAgent is an abstract base and ResponseEvaluator is a
    quality evaluator — neither is a runtime role.
    """
    from core.container import ServiceContainer

    source = inspect.getsource(ServiceContainer._init_agents)
    dict_keys = [
        name
        for name in inspect.getsource(ServiceContainer._init_agents).split("agent_classes = {")[1]
        .split("}")[0]
        .splitlines()
        if '"' in name
    ]
    domain = len([k for k in dict_keys if '":' in k])
    has_react = "react_agent" in source
    has_response = "response_agent" in source
    return domain + (1 if has_react else 0) + (1 if has_response else 0)


def _benchmark_query_count() -> tuple[int, int]:
    data = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    declared = data["metadata"]["total_queries"]
    actual = len(data["queries"])
    return declared, actual


# Canonical evaluation-harness facts, extracted at runtime from the evidence
# pipeline source (single source of truth: scripts/evaluate_rag.py).
# These are harness *capabilities* (experiment names / metric families /
# populations / failure taxonomy), NOT evaluation results — no metric number
# is ever emitted here.


def _evaluation_facts() -> dict:
    src = EVAL_SCRIPT.read_text(encoding="utf-8")

    experiments = re.findall(
        r'^\s{4}"(\w+)":\s*\{\s*"(?:disable_hybrid|disable_embedding|rerank)"', src, re.MULTILINE
    )
    seen: set[str] = set()
    experiment_names = [e for e in experiments if not (e in seen or seen.add(e))]

    metric_names = ["hit@k", "recall@k", "precision@k", "ndcg@k", "mrr@k"]

    pop_match = re.search(r"POPULATION_VIEWS\s*=\s*\(([^)]*)\)", src, re.DOTALL)
    populations = re.findall(r'"([a-z_]+)"', pop_match.group(1)) if pop_match else []

    ks_match = re.search(r"DEFAULT_KS\s*=\s*\(([^)]*)\)", src)
    ks = re.findall(r"\d+", ks_match.group(1)) if ks_match else []

    schema_match = re.search(r'REPORT_SCHEMA_VERSION\s*=\s*"([^"]+)"', src)
    schema_version = schema_match.group(1) if schema_match else None

    failure_match = re.search(r"FAILURE_TAXONOMY\s*=\s*\(([^)]*)\)", src, re.DOTALL)
    failure_taxonomy = (
        re.findall(r'"([A-Z_]+)"', failure_match.group(1)) if failure_match else []
    )

    canonical_doc = PROJECT_ROOT / "docs" / "reference" / "rag-evaluation.md"
    formal_status_marker = (
        current_formal_status(canonical_doc) if canonical_doc.exists() else None
    )

    return {
        "evaluation_experiments": experiment_names,
        "evaluation_metric_names": metric_names,
        "evaluation_metric_ks": [int(k) for k in ks],
        "evaluation_populations": populations,
        "evaluation_failure_taxonomy": failure_taxonomy,
        "evaluation_report_schema_version": schema_version,
        "rag_formal_metrics_status": formal_status_marker,
    }


def current_formal_status(canonical_doc: Path) -> str:
    """Derive the formal-metric status marker from the canonical review doc.

    The only accepted explicit states are NOT_VERIFIED / VERIFIED; the marker
    line must stay explicit so drift between doc and truth is detectable.
    Absence of the marker is surfaced as UNKNOWN (not guessed), which the
    --check contract treats as drift while formal evaluation is pending.
    """
    text = canonical_doc.read_text(encoding="utf-8", errors="replace")
    if re.search(r"当前 649-query 正式指标[：:]\s*(?:\*\*)?NOT_VERIFIED", text):
        return "NOT_VERIFIED"
    if re.search(r"当前 649-query 正式指标[：:]\s*(?:\*\*)?VERIFIED", text):
        return "VERIFIED"
    return "UNKNOWN"


def _makefile_targets() -> list[str]:
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    return re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", makefile, re.MULTILINE)


def collect() -> dict:
    facts = _llm_facts()
    facts["openapi_path_count"] = _openapi_path_count()
    facts["agent_role_count"] = _agent_role_count()
    declared, actual = _benchmark_query_count()
    facts["rag_benchmark_query_count"] = declared
    facts["rag_benchmark_metadata_consistent"] = declared == actual
    facts.update(_evaluation_facts())
    facts["makefile_rag_eval_targets_present"] = sorted(
        t for t in _makefile_targets()
        if t in {"eval-rag", "rag-eval-649", "rag-eval-649-preflight", "rag-eval-649-smoke", "rag-eval-import"}
    )
    return facts


# Canonical static lines inside docs/reference/current-state.md.
# Values are re-rendered dynamically; a mismatch means the doc drifted.
CHECK_LINES = {
    "runtime_version": "Runtime version (`core/config.py::VERSION`)",
    "llm_model": "Default LLM (`core/config.py::OPENAI_MODEL`)",
    "embedding_model": "Default embedding (`core/config.py::EMBEDDING_MODEL`)",
    "reranker_model": "Default reranker (`core/config.py::RERANKER_MODEL`)",
}


def check_doc(doc_path: Path) -> int:
    facts = collect()
    text = doc_path.read_text(encoding="utf-8")
    problems: list[str] = []
    for marker in CHECK_LINES.values():
        if marker not in text:
            problems.append(f"missing line: {marker}")
    for token in (
        facts["runtime_version"],
        facts["llm_model"],
        facts["embedding_model"],
        facts["reranker_model"],
        str(facts["openapi_path_count"]),
        str(facts["rag_benchmark_query_count"]),
        str(facts["agent_role_count"]),
    ):
        if token not in text:
            problems.append(f"stale fact: expected `{token}` in {doc_path.name}")
    if not facts["rag_benchmark_metadata_consistent"]:
        problems.append("rag benchmark metadata inconsistent (total_queries != len(queries))")
    # Evaluation-harness drift guards (machine-derived from evaluate_rag.py):
    for experiment in facts["evaluation_experiments"]:
        if experiment not in text:
            problems.append(
                f"stale evaluation fact: canonical experiment `{experiment}` missing from {doc_path.name}"
            )
    for population in ("all_queries", "retrieval_eligible", "full_gold_covered"):
        if population not in text:
            problems.append(
                f"stale evaluation fact: population `{population}` missing from {doc_path.name}"
            )
    for target in ("rag-eval-import", "rag-eval-649-preflight", "rag-eval-649-smoke", "rag-eval-649"):
        if target not in text:
            problems.append(
                f"stale evaluation fact: canonical make target `{target}` missing from {doc_path.name}"
            )
    if facts.get("rag_formal_metrics_status") != "NOT_VERIFIED":
        problems.append(
            f"rag formal metrics status drifted: doc state is "
            f"{facts.get('rag_formal_metrics_status')} (expected NOT_VERIFIED until a "
            "provenance-bearing formal artifact exists)"
        )
    for problem in problems:
        print(f"ERROR: {problem}")
    if problems:
        return 1
    print(f"OK: {doc_path} matches current runtime facts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        type=Path,
        metavar="DOC",
        help="verify a markdown doc contains the current runtime facts",
    )
    args = parser.parse_args()
    if args.check:
        return check_doc(args.check)
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
