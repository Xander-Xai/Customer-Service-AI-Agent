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
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_fallback import extract_fallback_defaults  # noqa: E402
from eval_contract import (  # noqa: E402
    EXPERIMENT_NAMES,
    FAILURE_TAXONOMY,
    METRIC_FAMILIES,
    POPULATION_VIEWS,
    REPORT_SCHEMA_VERSION,
)
from rag_evidence_status import (  # noqa: E402
    claimed_doc_formal_status,
    derive_rag_formal_status,
    formal_status_agreement_problems,
)

BENCHMARK_PATH = PROJECT_ROOT / "tests" / "eval" / "rag_benchmark.json"
CONFIG_PATH = PROJECT_ROOT / "core" / "config.py"
EVAL_SCRIPT = PROJECT_ROOT / "scripts" / "evaluate_rag.py"
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"


def _llm_facts() -> dict:
    """Canonical runtime *fallback* facts (AST-extracted, env independent).

    ``core/config.py`` executes ``load_dotenv(override=True)`` on import, so
    reading the module attributes would leak the developer's local ``.env``
    into a doc guard that is documented as comparing against source fallback
    literals. The AST default is the canonical fallback.
    """
    fallback = extract_fallback_defaults(CONFIG_PATH)
    return {
        "runtime_version": fallback.get("VERSION"),
        "llm_provider_default": fallback.get("LLM_PROVIDER"),
        "llm_model": fallback.get("OPENAI_MODEL"),
        "llm_base_url": fallback.get("OPENAI_BASE_URL"),
        "embedding_model": fallback.get("EMBEDDING_MODEL"),
        "embedding_dim": fallback.get("EMBEDDING_DIM"),
        "reranker_model": fallback.get("RERANKER_MODEL"),
        "vector_db_mode": fallback.get("VECTOR_DB_MODE"),
        "hybrid_search_enabled": fallback.get("HYBRID_SEARCH_ENABLED"),
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
        for name in inspect.getsource(ServiceContainer._init_agents)
        .split("agent_classes = {")[1]
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
    # Evaluation-harness capabilities come from the shared canonical contract
    # (scripts/eval_contract.py), imported by the evaluator too. Deriving them
    # here by parsing evaluate_rag.py source was fragile (a renamed metric or a
    # new population could silently drift out of the guard).
    #
    # Formal-metric status is derived from provenance-bearing evidence
    # artifacts ONLY — never from Markdown text. Docs are verified against
    # this derived state by check_doc()/audit_doc_consistency.py.
    formal = derive_rag_formal_status(root)

    return {
        "evaluation_experiments": list(EXPERIMENT_NAMES),
        "evaluation_metric_names": list(METRIC_FAMILIES),
        "evaluation_metric_ks": [1, 3, 5, 8],
        "evaluation_populations": list(POPULATION_VIEWS),
        "evaluation_failure_taxonomy": list(FAILURE_TAXONOMY),
        "evaluation_report_schema_version": REPORT_SCHEMA_VERSION,
        **formal,
    }


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
        t
        for t in _makefile_targets()
        if t
        in {
            "eval-rag",
            "rag-eval-649",
            "rag-eval-649-preflight",
            "rag-eval-649-smoke",
            "rag-eval-import",
        }
    )
    return facts


# Field-to-field checks: each documented fact is matched by a pattern bound to
# its labeled line, and the captured value is compared to the runtime fact.
# A global token search is unsafe (e.g. expected agent count 10 found inside
# the embedding dimension 1024). Add a field here when current-state.md gains
# a runtime-derived number.
CHECK_FIELDS: dict[str, dict[str, Any]] = {
    "runtime_version": {
        "pattern": re.compile(r"Runtime version \(`core/config\.py::VERSION`\): \*\*`([^`]+)`\*\*"),
        "fact": "runtime_version",
    },
    "llm_model": {
        "pattern": re.compile(
            r"Default LLM \(`core/config\.py::OPENAI_MODEL`\): \*\*`([^`]+)`\*\*"
        ),
        "fact": "llm_model",
    },
    "embedding_model": {
        "pattern": re.compile(
            r"Default embedding \(`core/config\.py::EMBEDDING_MODEL`\): \*\*`([^`]+)`\*\*"
        ),
        "fact": "embedding_model",
    },
    "reranker_model": {
        "pattern": re.compile(
            r"Default reranker \(`core/config\.py::RERANKER_MODEL`\): \*\*`([^`]+)`\*\*"
        ),
        "fact": "reranker_model",
    },
    "vector_db_mode": {
        "pattern": re.compile(r"Vector DB \(`core/config\.py::VECTOR_DB_MODE`\): `([^`]+)`"),
        "fact": "vector_db_mode",
    },
    "hybrid_search_enabled": {
        "pattern": re.compile(
            r"Hybrid retrieval \(`core/config\.py::HYBRID_SEARCH_ENABLED`\): `([^`]+)`"
        ),
        "fact": "hybrid_search_enabled",
    },
    "agent_role_count": {
        "pattern": re.compile(r"Agent roles \(`core/container\.py::_init_agents`\): \*\*(\d+)\*\*"),
        "fact": "agent_role_count",
    },
    "openapi_path_count": {
        "pattern": re.compile(r"OpenAPI HTTP paths[^\n]*?: \*\*`?(\d+)`?\*\*"),
        "fact": "openapi_path_count",
    },
    "rag_benchmark_query_count": {
        "pattern": re.compile(r"RAG benchmark queries[^\n]*?: \*\*`?(\d+)`?\*\*"),
        "fact": "rag_benchmark_query_count",
    },
}

# Canonical make targets the RAG evidence workflow documents.
CANONICAL_RAG_TARGETS = (
    "rag-eval-import",
    "rag-eval-649-preflight",
    "rag-eval-649-smoke",
    "rag-eval-649",
)


def check_doc(doc_path: Path, root: Path = PROJECT_ROOT) -> int:
    facts = collect(root)
    text = doc_path.read_text(encoding="utf-8")
    problems: list[str] = []

    # 1. Field-to-field: documented value must equal the runtime fact.
    for field, spec in CHECK_FIELDS.items():
        match = spec["pattern"].search(text)
        if match is None:
            problems.append(f"missing field marker `{field}` in {doc_path.name}")
            continue
        documented = match.group(1)
        expected = str(facts[spec["fact"]])
        if documented != expected:
            problems.append(
                f"stale field `{field}`: {doc_path.name} renders `{documented}` "
                f"but runtime fallback fact is `{expected}`"
            )

    # 2. LLM base URL + provider are documented on one line; validate both.
    base_match = re.search(r"Default LLM base URL: `([^`]+)`（provider: `([^`]+)`）", text)
    if base_match is None:
        problems.append(f"missing field marker `llm_base_url` in {doc_path.name}")
    else:
        if base_match.group(1) != str(facts["llm_base_url"]):
            problems.append(
                f"stale field `llm_base_url`: {doc_path.name} renders "
                f"`{base_match.group(1)}` but runtime fallback is `{facts['llm_base_url']}`"
            )
        if base_match.group(2) != str(facts["llm_provider_default"]):
            problems.append(
                f"stale field `llm_provider_default`: {doc_path.name} renders "
                f"`{base_match.group(2)}` but runtime fallback is "
                f"`{facts['llm_provider_default']}`"
            )

    if not facts["rag_benchmark_metadata_consistent"]:
        problems.append("rag benchmark metadata inconsistent (total_queries != len(queries))")

    # 3. Canonical evaluation surfaces (derived, so adding/renaming a fact in
    # the contract forces this guard to require the new value).
    for experiment in facts["evaluation_experiments"]:
        if experiment not in text:
            problems.append(
                f"stale evaluation fact: canonical experiment `{experiment}` "
                f"missing from {doc_path.name}"
            )
    for population in facts["evaluation_populations"]:
        if population not in text:
            problems.append(
                f"stale evaluation fact: canonical population `{population}` "
                f"missing from {doc_path.name}"
            )
    for target in CANONICAL_RAG_TARGETS:
        if target not in text:
            problems.append(
                f"stale evaluation fact: canonical make target `{target}` "
                f"missing from {doc_path.name}"
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

    The two-directional agreement rule itself lives in
    ``rag_evidence_status.formal_status_agreement_problems`` so that this
    single-doc entry point and ``scripts/audit_doc_consistency.py``'s
    repo-wide guard cannot drift into disagreeing about what agreement means
    (issue #53). The extra provenance-binding rule below is specific to this
    path and stays here.

    - docs must NOT claim VERIFIED without a valid formal artifact
      (docs cannot self-promote: artifact, not Markdown, owns the state);
    - when a formal artifact IS VERIFIED, docs still claiming NOT_VERIFIED
      are stale and must be re-rendered (guard drives docs, not vice versa).
    """
    problems: list[str] = []
    facts_evaluation = _evaluation_facts(root)
    actual = facts_evaluation["rag_formal_metrics_status"]
    claimed = claimed_doc_formal_status(text)
    problems.extend(formal_status_agreement_problems(claimed, actual, str(doc_path)))
    if (
        actual == "VERIFIED"
        and claimed == "VERIFIED"
        and facts_evaluation["rag_formal_artifact_path"] not in text
    ):
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
