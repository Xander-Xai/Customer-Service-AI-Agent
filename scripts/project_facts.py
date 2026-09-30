#!/usr/bin/env python3
"""Emit machine-readable current runtime facts (JSON).

All values are extracted from code/metadata/evidence at execution time —
nothing is hardcoded. Use for CI guards, release preparation, and
documentation audits.

Evidence direction: artifact/evidence -> facts -> docs -> guard. In
particular ``rag_formal_metrics_status`` derives ONLY from provenance-bearing
artifacts under ``artifacts/evaluation/rag-649/**`` (see
``scripts/rag_evidence_status.py``); docs are verified against it, never the
reverse.

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
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from rag_evidence_status import claimed_doc_formal_status, derive_rag_formal_status  # noqa: E402

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


def _benchmark_query_count(root: Path = PROJECT_ROOT) -> tuple[int, int]:
    data = json.loads((root / "tests" / "eval" / "rag_benchmark.json").read_text(encoding="utf-8"))
    declared = data["metadata"]["total_queries"]
    actual = len(data["queries"])
    return declared, actual


# Canonical evaluation-harness facts, extracted at runtime from the evidence
# pipeline source (single source of truth: scripts/evaluate_rag.py).
# These are harness *capabilities* (experiment names / metric families /
# populations / failure taxonomy), NOT evaluation results — no metric number
# is ever emitted here.


def _evaluation_facts(root: Path = PROJECT_ROOT) -> dict:
    eval_script = root / "scripts" / "evaluate_rag.py"
    src = eval_script.read_text(encoding="utf-8", errors="replace") if eval_script.exists() else ""

    seen: set[str] = set()
    experiment_names = [
        e for e in _EXPERIMENT_NAME_RE.findall(src) if not (e in seen or seen.add(e))
    ]

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

    # Formal-metric status is derived from provenance-bearing evidence
    # artifacts ONLY — never from Markdown text. Docs are verified against
    # this derived state by check_doc()/audit_doc_consistency.py.
    formal = derive_rag_formal_status(root)

    return {
        "evaluation_experiments": experiment_names,
        "evaluation_metric_names": metric_names,
        "evaluation_metric_ks": [int(k) for k in ks],
        "evaluation_populations": populations,
        "evaluation_failure_taxonomy": failure_taxonomy,
        "evaluation_report_schema_version": schema_version,
        **formal,
    }


_EXPERIMENT_NAME_RE = re.compile(
    r'^\s{4}"(\w+)":\s*\{\s*"(?:disable_hybrid|disable_embedding|rerank)"', re.MULTILINE
)


def _makefile_targets() -> list[str]:
    makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
    return re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", makefile, re.MULTILINE)


def collect(root: Path = PROJECT_ROOT) -> dict:
    facts = _llm_facts()
    facts["openapi_path_count"] = _openapi_path_count()
    facts["agent_role_count"] = _agent_role_count()
    declared, actual = _benchmark_query_count(root)
    facts["rag_benchmark_query_count"] = declared
    facts["rag_benchmark_metadata_consistent"] = declared == actual
    facts.update(_evaluation_facts(root))
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


def check_doc(doc_path: Path, root: Path = PROJECT_ROOT) -> int:
    facts = collect(root)
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
    problems.extend(check_formal_status_claims(text, doc_path, root))
    canonical_rag_doc = root / "docs" / "reference" / "rag-evaluation.md"
    if canonical_rag_doc.exists() and canonical_rag_doc.resolve() != doc_path.resolve():
        problems.extend(
            check_formal_status_claims(
                canonical_rag_doc.read_text(encoding="utf-8", errors="replace"),
                canonical_rag_doc,
                root,
            )
        )
    for problem in problems:
        print(f"ERROR: {problem}")
    if problems:
        return 1
    print(f"OK: {doc_path} matches current runtime facts")
    return 0


def check_formal_status_claims(
    text: str,
    doc_path: Path,
    root: Path = PROJECT_ROOT,
) -> list[str]:
    """Both check-doc directions against the ARTIFACT-derived formal status.

    - docs must NOT claim VERIFIED without a valid formal artifact
      (docs cannot self-promote: artifact, not Markdown, owns the state);
    - when a formal artifact IS VERIFIED, docs still claiming NOT_VERIFIED
      are stale and must be re-rendered (guard drives docs, not vice versa).
    """
    problems: list[str] = []
    facts_evaluation = _evaluation_facts(root)
    actual = facts_evaluation["rag_formal_metrics_status"]
    claimed = claimed_doc_formal_status(text)
    if claimed == "CONFLICT":
        problems.append(
            f"formal metrics status conflict: {doc_path} claims both NOT_VERIFIED and VERIFIED"
        )
    if claimed == "VERIFIED" and actual != "VERIFIED":
        problems.append(
            f"docs must not self-promote formal metrics: {doc_path} claims VERIFIED "
            f"but evidence artifacts derive {actual} (artifact owns the state; "
            f"regenerate a formal artifact via `make rag-eval-649` first)"
        )
    if actual == "VERIFIED" and claimed != "VERIFIED":
        problems.append(
            f"stale formal-status claim: {doc_path} does not claim VERIFIED "
            f"but the evidence artifact derives VERIFIED; re-render the doc "
            f"and bind it to {facts_evaluation['rag_formal_artifact_path']}"
        )
    if actual == "VERIFIED" and claimed == "VERIFIED" and facts_evaluation[
        "rag_formal_artifact_path"
    ] not in text:
        problems.append(
            f"formal-metric claim must bind provenance: {doc_path} claims VERIFIED "
            f"without referencing the evidence artifact "
            f"{facts_evaluation['rag_formal_artifact_path']}"
        )
    return problems


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
