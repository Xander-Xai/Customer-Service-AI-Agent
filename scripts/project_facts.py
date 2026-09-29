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
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BENCHMARK_PATH = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"


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


def collect() -> dict:
    facts = _llm_facts()
    facts["openapi_path_count"] = _openapi_path_count()
    facts["agent_role_count"] = _agent_role_count()
    declared, actual = _benchmark_query_count()
    facts["rag_benchmark_query_count"] = declared
    facts["rag_benchmark_metadata_consistent"] = declared == actual
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
