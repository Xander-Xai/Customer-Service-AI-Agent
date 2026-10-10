#!/usr/bin/env python3
"""RAG **reviewed-gold** evaluation entry (``rag-reviewed-gold-eval/v1``).

Why this exists
---------------
``scripts/evaluate_rag.py`` computes Hit@K / Recall@K / NDCG over
``tests/eval/rag_benchmark.json``'s ``expected_doc_ids``. The static audit
(``scripts/rag_gold_label_provenance.py``) proved those ids are **not relevance
judgements** (``DATASET_DEFECT``), so that path can never yield formal retrieval
quality. ``scripts/gold_label_contract.py`` defines the provenance-enforced
replacement (``rag-gold-label/v1``) and ``scripts/validate_gold_labels.py``
validates it offline — but until this script existed, **nothing actually turned
JUDGED labels into metrics**. A contract nothing consumes is theatre.

This is the missing consumer. It is a *separate* entry (explicitly allowed by
the remediation plan) so the shipped 649 benchmark is never read or modified.

Non-negotiable semantics
------------------------
1. **Only human-verified ``JUDGED`` records enter formal metrics.** ``DRAFT_*``,
   ``llm_suggested``, ``category_random_match``, ``EXCLUDED`` and
   ``UNDETERMINABLE`` are counted and reported, never scored.
2. **Unjudged is not irrelevant.** A retrieved document with no judgement is
   tracked as ``unjudged_at_k`` and removed from the precision denominator; it
   is never silently counted as a miss.
3. **Recall / NDCG require a closed judgement set.** A query is
   ``recall_eligible`` only when every one of its JUDGED records carries
   ``judgment_completeness == "closed"`` (an explicit annotator declaration that
   the relevant set for that query is complete). Partial queries still receive
   Hit@K / precision (which need no closure) but Recall/NDCG are reported as
   ``null`` with reason ``JUDGMENT_SET_NOT_CLOSED``.
4. **Missing confirmed relevant docs are explicit.** A query whose JUDGED
   records are all grade 0 has no confirmed relevant document; it is excluded
   and recorded under ``NO_CONFIRMED_RELEVANT_DOC`` rather than scored as 0.
5. **No JUDGED labels -> ``NOT_MEASURABLE``, fail closed.** The 4-config
   ablation is only executed once a real reviewed-gold dataset exists.

Metrics computed here (binary relevance for hit/precision/recall/MRR, graded
gain ``2^grade - 1`` for NDCG):
``hit@K`` / ``precision_judged@K`` (unjudged removed from denominator) /
``recall@K`` / ``ndcg@K`` / ``mrr``.

Usage::

    # offline summary / contract + provenance artifact (no Qdrant / provider):
    python3 scripts/evaluate_rag_reviewed_gold.py \
        --gold-labels tests/eval/gold_labels/reviewed_gold.jsonl --summary-only

    # formal reviewed-gold ablation (needs Qdrant + provider + closed judgments):
    python3 scripts/evaluate_rag_reviewed_gold.py \
        --gold-labels tests/eval/gold_labels/reviewed_gold.jsonl

Output: ``artifacts/evaluation/rag-reviewed-gold/<run-id>/report.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gold_label_contract import (  # noqa: E402
    LABEL_STATUS_JUDGED,
    corpus_sha256,
    load_jsonl,
    validate_records,
)
from gold_label_contract import SCHEMA_VERSION as GOLD_SCHEMA_VERSION  # noqa: E402

REPORT_SCHEMA_VERSION = "rag-reviewed-gold-eval/v1"

#: Explicit query-level marker meaning "the relevant set for this query is fully
#: enumerated by these JUDGED records". Absent/other values => partial.
COMPLETENESS_CLOSED = "closed"

DEFAULT_CORPUS = REPO_ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "evaluation" / "rag-reviewed-gold"
DEFAULT_KS: tuple[int, ...] = (1, 3, 5, 8)
DEFAULT_EXPERIMENTS: tuple[str, ...] = (
    "vector_only",
    "bm25_only",
    "hybrid_no_rerank",
    "hybrid_rerank",
)

#: Reasons a query is excluded from the formal judged population.
EXCLUDE_NO_JUDGED_LABEL = "NO_JUDGED_LABEL"
EXCLUDE_NO_CONFIRMED_RELEVANT_DOC = "NO_CONFIRMED_RELEVANT_DOC"


# ---------------------------------------------------------------------------
# Pure data model + metric functions (deterministic, offline, unit-testable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryJudgment:
    """Per-query relevance judgement derived from ``rag-gold-label/v1``."""

    query_id: str
    query: str
    grades: dict[str, int]  # doc_id -> grade (0..3), JUDGED only
    recall_eligible: bool  # every JUDGED record declared completeness == closed

    @property
    def relevant_ids(self) -> set[str]:
        return {doc_id for doc_id, grade in self.grades.items() if grade >= 1}

    @property
    def has_confirmed_relevant(self) -> bool:
        return bool(self.relevant_ids)

    @property
    def external_ids(self) -> set[str]:
        """Documents the search space is *known* to contain (all judged docs)."""
        return set(self.grades)


@dataclass(frozen=True)
class GoldPopulation:
    """The reviewed gold, split into the formal population and exclusions."""

    judgments: tuple[QueryJudgment, ...]
    excluded: dict[str, str]  # query_id -> reason
    status_counts: dict[str, int]
    judged_label_count: int
    total_queries_seen: int
    recall_eligible_ids: tuple[str, ...] = field(default_factory=tuple)


def _completeness_of(record: dict[str, Any]) -> str:
    value = record.get("judgment_completeness")
    return value.strip() if isinstance(value, str) else ""


def build_population(records: list[dict[str, Any]]) -> GoldPopulation:
    """Turn validated ``rag-gold-label/v1`` records into a scored population.

    Only ``JUDGED`` records with a non-empty ``doc_id`` and an integer grade are
    scored. Every other status is tallied in ``status_counts`` and ignored for
    metrics. No record is ever mutated.
    """
    status_counts: Counter[str] = Counter()
    judged_by_query: dict[str, dict[str, int]] = {}
    query_text: dict[str, str] = {}
    completeness_by_query: dict[str, list[bool]] = {}
    all_query_ids: set[str] = set()
    judged_label_count = 0

    for record in records:
        status = str(record.get("label_status", "<missing>"))
        status_counts[status] += 1
        query_id = record.get("query_id")
        if not isinstance(query_id, str) or not query_id:
            continue
        all_query_ids.add(query_id)
        if status != LABEL_STATUS_JUDGED:
            continue
        doc_id = record.get("doc_id")
        grade = record.get("relevance_grade")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        if isinstance(grade, bool) or not isinstance(grade, int):
            continue
        judged_label_count += 1
        judged_by_query.setdefault(query_id, {})[doc_id] = grade
        query_text.setdefault(query_id, str(record.get("query", "")))
        completeness_by_query.setdefault(query_id, []).append(
            _completeness_of(record) == COMPLETENESS_CLOSED
        )

    judgments: list[QueryJudgment] = []
    excluded: dict[str, str] = {}
    for query_id in sorted(judged_by_query):
        grades = judged_by_query[query_id]
        relevant = any(grade >= 1 for grade in grades.values())
        if not relevant:
            excluded[query_id] = EXCLUDE_NO_CONFIRMED_RELEVANT_DOC
            continue
        closed = all(completeness_by_query.get(query_id, []))
        judgments.append(
            QueryJudgment(
                query_id=query_id,
                query=query_text.get(query_id, ""),
                grades=grades,
                recall_eligible=closed,
            )
        )

    # Queries the dataset mentions but that carry no JUDGED label at all.
    for query_id in sorted(all_query_ids - set(judged_by_query)):
        excluded.setdefault(query_id, EXCLUDE_NO_JUDGED_LABEL)

    return GoldPopulation(
        judgments=tuple(judgments),
        excluded=excluded,
        status_counts=dict(sorted(status_counts.items())),
        judged_label_count=judged_label_count,
        total_queries_seen=len(all_query_ids),
        recall_eligible_ids=tuple(j.query_id for j in judgments if j.recall_eligible),
    )


def compute_reviewed_metrics(
    retrieved_ids: list[str],
    judgment: QueryJudgment,
    ks: tuple[int, ...] = DEFAULT_KS,
) -> dict[str, Any]:
    """Per-query metrics with honest unjudged handling.

    * ``hit@K`` / ``mrr``: a hit requires a *judged relevant* doc in top-K.
      Unjudged docs are never treated as relevant.
    * ``precision_judged@K``: relevant / (judged docs in top-K). If a top-K has
      no judged doc the value is ``None`` and ``precision_undefined=True`` —
      reporting 0 there would equate "unjudged" with "irrelevant".
    * ``precision@K``: standard lower bound (relevant / K); flagged
      ``precision_is_lower_bound`` whenever unjudged docs appear in top-K.
    * ``recall@K`` / ``ndcg@K``: ``None`` unless ``recall_eligible``.
    """
    grades = judgment.grades
    relevant = judgment.relevant_ids
    max_k = max(ks)
    top = list(retrieved_ids[:max_k])

    first_rank = 0
    for rank, doc_id in enumerate(top, 1):
        if doc_id in relevant:
            first_rank = rank
            break

    out: dict[str, Any] = {
        "mrr": round(1.0 / first_rank, 4) if first_rank else 0.0,
        "first_relevant_rank": float(first_rank),
        "recall_applicable": judgment.recall_eligible,
        "recall_ndcg_reason": None if judgment.recall_eligible else "JUDGMENT_SET_NOT_CLOSED",
    }
    for k in ks:
        top_k = top[:k]
        relevant_hits = sum(1 for d in top_k if d in relevant)
        judged_in_topk = sum(1 for d in top_k if d in grades)
        unjudged_in_topk = len(top_k) - judged_in_topk

        out[f"hit@{k}"] = 1.0 if relevant_hits > 0 else 0.0
        out[f"precision@{k}"] = round(relevant_hits / k, 4)
        out[f"precision_is_lower_bound@{k}"] = unjudged_in_topk > 0
        out[f"unjudged_at_{k}"] = unjudged_in_topk
        out[f"precision_judged@{k}"] = (
            round(relevant_hits / judged_in_topk, 4) if judged_in_topk else None
        )

        if judgment.recall_eligible:
            n_relevant = len(relevant)
            out[f"recall@{k}"] = round(relevant_hits / n_relevant, 4)
            dcg = sum(
                (2 ** grades[d] - 1) / math.log2(rank + 1)
                for rank, d in enumerate(top_k, 1)
                if d in grades and grades[d] >= 1
            )
            ideal_grades = sorted((g for g in grades.values() if g >= 1), reverse=True)[:k]
            idcg = sum((2**g - 1) / math.log2(i + 1) for i, g in enumerate(ideal_grades, 1))
            out[f"ndcg@{k}"] = round(dcg / idcg, 4) if idcg > 0 else 0.0
        else:
            out[f"recall@{k}"] = None
            out[f"ndcg@{k}"] = None
    return out


def summarize_reviewed_metrics(rows: list[dict[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    """Mean over rows, reporting numerator/denominator per metric.

    ``None`` values are excluded from their own mean (never coerced to 0). Each
    metric records its own denominator so a partial-judgment run cannot be read
    as if Recall/NDCG covered the whole population.
    """
    metric_keys = ["mrr"] + [
        f"{family}@{k}"
        for k in ks
        for family in ("hit", "precision", "precision_judged", "recall", "ndcg")
    ]
    out: dict[str, Any] = {}
    for key in metric_keys:
        values = [r[key] for r in rows if r.get(key) is not None]
        out[key] = {
            "value": round(sum(values) / len(values), 4) if values else None,
            "numerator_sum": round(sum(values), 4) if values else None,
            "denominator": len(values),
            "not_measured": not values,
        }
    out["unjudged_at_k"] = {
        f"k{k}": sum(int(r.get(f"unjudged_at_{k}", 0)) for r in rows) for k in ks
    }
    return out


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_provenance() -> dict[str, Any]:
    from core.code_provenance import collect_code_provenance

    return collect_code_provenance(REPO_ROOT).to_dict()


def _execution_environment() -> dict[str, Any]:
    from core import config as cfg

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "vector_db_mode": cfg.VECTOR_DB_MODE,
        "embedding_model": cfg.EMBEDDING_MODEL,
        "embedding_dim": cfg.EMBEDDING_DIM,
        "reranker_model": cfg.RERANKER_MODEL,
        "hybrid_search_enabled": cfg.HYBRID_SEARCH_ENABLED,
    }


def _load_corpus(path: Path) -> tuple[set[str], dict[str, dict[str, Any]]]:
    ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            obj = json.loads(line)
            doc_id = obj.get("id")
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError(f"{path}:{lineno}: corpus row missing a string `id`")
            ids.add(doc_id)
            by_id[doc_id] = obj
    return ids, by_id


def _not_measurable_reason(population: GoldPopulation, judged_query_count: int) -> str | None:
    if judged_query_count == 0:
        return "NO_JUDGED_LABELS"
    return None


# ---------------------------------------------------------------------------
# Formal ablation (only reached once a reviewed-gold dataset exists)
# ---------------------------------------------------------------------------


async def _run_ablation(args, population: GoldPopulation, ks: tuple[int, ...]) -> dict[str, Any]:
    """Run the 4-config ablation over JUDGED queries against real Qdrant.

    Reuses ``evaluate_rag``'s retrieval overrides so the production defaults are
    untouched. Only executed when the population is real (JUDGED labels exist).
    """
    from evaluate_rag import Override, _build_request, apply_override

    from rag.qdrant_knowledge_base import QdrantKnowledgeBase

    exp_specs = {
        "vector_only": Override(disable_hybrid=True, disable_embedding=False),
        "bm25_only": Override(disable_hybrid=False, disable_embedding=True),
        "hybrid_no_rerank": Override(disable_hybrid=False, disable_embedding=False),
        "hybrid_rerank": Override(disable_hybrid=False, disable_embedding=False),
    }
    kb = QdrantKnowledgeBase(host=args.host, port=args.port)
    if not kb.available:
        raise RuntimeError("Qdrant unreachable")

    results: dict[str, Any] = {}
    for name in args.experiments:
        override = exp_specs[name]
        rerank_flag = name == "hybrid_rerank"
        restore = apply_override(kb, override)
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        try:
            for judgment in population.judgments[: args.limit or None]:
                try:
                    result = await kb.retrieve(
                        _build_request(judgment.query, args.top_k, rerank_flag, args.timeout)
                    )
                    retrieved = [d.get("id", "") for d in result]
                    rows.append(
                        {
                            "query_id": judgment.query_id,
                            **compute_reviewed_metrics(retrieved, judgment, ks),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - record and continue
                    failures.append(
                        {
                            "query_id": judgment.query_id,
                            "error": f"{type(exc).__name__}: {exc}"[:200],
                        }
                    )
        finally:
            restore()
        results[name] = {
            "status": "MEASURED",
            "query_count": len(rows),
            "failure_count": len(failures),
            "metrics": summarize_reviewed_metrics(rows, ks),
            "failures": failures,
        }
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--gold-labels", required=True, help="rag-gold-label/v1 JSONL with JUDGED records"
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument(
        "--no-corpus",
        action="store_true",
        help="skip doc-id existence / corpus-hash checks (schema-only; never formal)",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--experiments", default=",".join(DEFAULT_EXPERIMENTS))
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--ks", default="1,3,5,8")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="validate + summarize the gold labels offline; never touch Qdrant",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ks = tuple(int(k) for k in str(args.ks).split(",") if k.strip())
    args.experiments = [e for e in str(args.experiments).split(",") if e.strip()]

    gold_path = Path(args.gold_labels)
    if not gold_path.is_file():
        if args.summary_only:
            # A status/QC command must be runnable *before* any human labelling
            # exists. "No reviewed-gold file yet" is the expected pre-annotation
            # state, not a crash: report NOT_MEASURABLE and exit 0. The formal
            # path still fails closed (exit 2) below.
            reason = "GOLD_LABELS_FILE_ABSENT"
            run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = Path(args.output) / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            report = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "run_id": run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "NOT_MEASURABLE",
                "not_measurable_reason": reason,
                "gold_labels": {
                    "path": str(gold_path),
                    "sha256": None,
                    "contract": GOLD_SCHEMA_VERSION,
                    "exists": False,
                },
                "ablation_executed": False,
                "notes": [
                    "No reviewed-gold file exists yet: the pre-annotation state.",
                    "Formal reviewed-gold metrics stay NOT_MEASURABLE until a human "
                    "produces JUDGED labels (see scripts/build_rag_gold_review_worklist.py).",
                    "The shipped 649 benchmark is never read or modified.",
                ],
            }
            (out_dir / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"summary-only: status=NOT_MEASURABLE ({reason})")
            print("  0 judged queries (no gold file); Recall/NDCG stay NOT_MEASURABLE.")
            print(f"  artifact: {out_dir / 'report.json'}")
            return 0
        print(f"[FATAL] gold labels not found: {gold_path}", file=sys.stderr)
        return 2

    try:
        records = load_jsonl(gold_path)
    except (OSError, ValueError) as exc:
        print(f"[FATAL] cannot read gold labels: {exc}", file=sys.stderr)
        return 2

    # Corpus + hash (fail closed unless explicitly waived).
    corpus_ids: set[str] | None = None
    corpus_hash: str | None = None
    corpus_path: Path | None = Path(args.corpus)
    if args.no_corpus:
        corpus_path = None
    elif not corpus_path.is_file():
        print(
            f"[FATAL] corpus not found at {corpus_path}; generate it or pass --no-corpus "
            "(schema-only mode cannot be formal evidence).",
            file=sys.stderr,
        )
        return 2
    else:
        corpus_ids, _ = _load_corpus(corpus_path)
        corpus_hash = corpus_sha256(corpus_path)

    errors = validate_records(records, corpus_ids=corpus_ids, expected_corpus_hash=corpus_hash)
    if errors:
        print(
            f"[FATAL] gold labels violate {GOLD_SCHEMA_VERSION} ({len(errors)} error(s)):",
            file=sys.stderr,
        )
        for err in errors[:20]:
            print(f"  - {err}", file=sys.stderr)
        return 1

    population = build_population(records)
    judged_query_count = len(population.judgments)
    reason = _not_measurable_reason(population, judged_query_count)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "NOT_MEASURABLE" if reason else "MEASURED",
        "not_measurable_reason": reason,
        "gold_labels": {
            "path": str(gold_path.relative_to(REPO_ROOT))
            if gold_path.is_relative_to(REPO_ROOT)
            else str(gold_path),
            "sha256": _sha256_file(gold_path),
            "contract": GOLD_SCHEMA_VERSION,
            "status_counts": population.status_counts,
            "judged_label_count": population.judged_label_count,
            "judged_query_count": judged_query_count,
            "recall_eligible_query_count": len(population.recall_eligible_ids),
            "total_queries_seen": population.total_queries_seen,
        },
        "corpus": {
            "path": str(corpus_path) if corpus_path else None,
            "sha256": corpus_hash,
            "doc_count": len(corpus_ids) if corpus_ids is not None else None,
            "checked": corpus_ids is not None,
        },
        "provenance": {
            "code": _code_provenance(),
            "environment": _execution_environment(),
        },
        "excluded_queries": population.excluded,
        "exclusion_reasons": {
            EXCLUDE_NO_JUDGED_LABEL: "query has no JUDGED label",
            EXCLUDE_NO_CONFIRMED_RELEVANT_DOC: "all JUDGED labels are grade 0 (no confirmed relevant doc)",
        },
        "metric_denominators": {
            "formal_route": "queries with a human-verified JUDGED relevant doc",
            "recall_ndcg": (
                "recall_eligible queries only (all JUDGED records declare "
                "judgment_completeness=closed)"
            ),
            "unjudged_policy": "unjudged documents are excluded from the precision denominator, never counted irrelevant",
        },
        "experiments": {},
        "notes": [
            "This entry never reads or modifies tests/eval/rag_benchmark.json.",
            "Only human-verified JUDGED labels enter formal metrics; all other statuses are counted, not scored.",
            "Recall/NDCG are null unless the query's judgement set is declared closed.",
            "NOT_MEASURABLE is a real state: with no JUDGED labels the 4-config ablation is not run.",
        ],
    }

    if reason and not args.summary_only:
        report["ablation_executed"] = False
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"status: NOT_MEASURABLE ({reason})")
        print(f"  gold records: {len(records)} | status counts: {population.status_counts}")
        print(f"  artifact: {out_dir / 'report.json'}")
        print("  -> 4-config ablation NOT run; a reviewed (JUDGED) dataset is required first.")
        return 2

    if args.summary_only:
        report["ablation_executed"] = False
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        label = "NOT_MEASURABLE" if reason else "MEASURED"
        print(
            f"summary-only: status={label} "
            f"({judged_query_count} judged queries, "
            f"{len(population.recall_eligible_ids)} recall-eligible, "
            f"{len(population.excluded)} excluded)"
        )
        print(f"  artifact: {out_dir / 'report.json'}")
        return 0

    import asyncio

    report["ablation_executed"] = True
    report["experiments"] = asyncio.run(_run_ablation(args, population, ks))
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"status: MEASURED ({judged_query_count} judged queries)")
    print(f"  artifact: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
