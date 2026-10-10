"""Deterministic tests for the reviewed-gold RAG evaluation entry.

Everything here is offline: no Qdrant, provider, LLM, embedding or reranker.
The point of these tests is the **honesty semantics** of the metrics:

* only human-verified ``JUDGED`` records are scored;
* an unjudged retrieved document is never treated as irrelevant;
* Recall/NDCG are ``None`` unless the query's judgement set is declared closed;
* a query with no confirmed relevant doc is excluded, not scored as 0.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from scripts.evaluate_rag_reviewed_gold import (
    COMPLETENESS_CLOSED,
    EXCLUDE_NO_CONFIRMED_RELEVANT_DOC,
    EXCLUDE_NO_JUDGED_LABEL,
    GoldPopulation,
    QueryJudgment,
    build_population,
    compute_reviewed_metrics,
    main,
    summarize_reviewed_metrics,
)

CORPUS_HASH = "a" * 64


def _rec(
    *,
    query_id: str,
    doc_id: str | None,
    grade: int | None,
    status: str = "JUDGED",
    method: str = "human",
    completeness: str | None = COMPLETENESS_CLOSED,
    evidence: str = "verbatim span",
    annotator: str = "reviewer-1",
    reviewed_at: str | None = "2026-10-01T00:00:00Z",
) -> dict:
    record = {
        "schema_version": "rag-gold-label/v1",
        "query_id": query_id,
        "query": f"query for {query_id}",
        "corpus_version": "knowledge_base_5000@2026-10",
        "corpus_hash": CORPUS_HASH,
        "doc_id": doc_id,
        "evidence_span": evidence,
        "relevance_grade": grade,
        "annotator": annotator,
        "annotation_method": method,
        "reviewed_at": reviewed_at,
        "label_status": status,
        "exclusion_reason": None,
        "derivation_rule": None,
    }
    if completeness is not None:
        record["judgment_completeness"] = completeness
    return record


class TestBuildPopulation:
    def test_only_judged_records_scored(self):
        records = [
            _rec(query_id="q1", doc_id="d1", grade=3),
            _rec(query_id="q1", doc_id="d2", grade=0),
            _rec(
                query_id="q2",
                doc_id="d3",
                grade=None,
                status="DRAFT_UNVERIFIED",
                method="llm_suggested",
                evidence="",
                reviewed_at=None,
            ),
            _rec(
                query_id="q3",
                doc_id=None,
                grade=None,
                status="UNDETERMINABLE",
                method="category_random_match",
                evidence="",
                reviewed_at=None,
            ),
        ]
        population = build_population(records)
        assert {j.query_id for j in population.judgments} == {"q1"}
        assert population.status_counts == {
            "JUDGED": 2,
            "DRAFT_UNVERIFIED": 1,
            "UNDETERMINABLE": 1,
        }
        assert population.judged_label_count == 2

    def test_all_zero_grades_excluded_as_no_confirmed_relevant(self):
        population = build_population([_rec(query_id="q1", doc_id="d1", grade=0)])
        assert population.judgments == ()
        assert population.excluded == {"q1": EXCLUDE_NO_CONFIRMED_RELEVANT_DOC}

    def test_query_without_any_judged_is_excluded(self):
        population = build_population(
            [
                _rec(
                    query_id="q1",
                    doc_id="d1",
                    grade=None,
                    status="DRAFT_UNVERIFIED",
                    method="llm_suggested",
                    evidence="",
                    reviewed_at=None,
                ),
            ]
        )
        assert population.excluded == {"q1": EXCLUDE_NO_JUDGED_LABEL}

    def test_recall_eligible_requires_all_closed(self):
        records = [
            _rec(query_id="q1", doc_id="d1", grade=2, completeness=COMPLETENESS_CLOSED),
            _rec(query_id="q2", doc_id="d2", grade=2, completeness="partial"),
            _rec(query_id="q3", doc_id="d3", grade=2, completeness=None),
        ]
        population = build_population(records)
        eligible = {j.query_id for j in population.judgments if j.recall_eligible}
        assert eligible == {"q1"}
        assert population.recall_eligible_ids == ("q1",)


class TestComputeMetrics:
    def _judgment(self, grades, recall_eligible=True):
        return QueryJudgment(
            query_id="q1",
            query="q",
            grades=grades,
            recall_eligible=recall_eligible,
        )

    def test_unjudged_not_counted_as_irrelevant_in_precision(self):
        j = self._judgment({"d1": 2})
        m = compute_reviewed_metrics(["u1", "d1"], j, ks=(3,))
        # Only d1 is judged; it is relevant -> precision_judged denominator is 1.
        assert m["precision_judged@3"] == 1.0
        # Two docs were retrieved; one of them (u1) carries no judgement.
        assert m["unjudged_at_3"] == 1
        assert m["precision@3"] == pytest.approx(1 / 3, abs=1e-4)
        assert m["precision_is_lower_bound@3"] is True
        assert m["hit@3"] == 1.0

    def test_all_unjudged_topk_precision_undefined(self):
        j = self._judgment({"d1": 2})
        m = compute_reviewed_metrics(["u1", "u2"], j, ks=(3,))
        assert m["precision_judged@3"] is None
        assert m["unjudged_at_3"] == 2
        assert m["hit@3"] == 0.0

    def test_recall_ndcg_none_when_not_recall_eligible(self):
        j = self._judgment({"d1": 2}, recall_eligible=False)
        m = compute_reviewed_metrics(["d1"], j, ks=(3,))
        assert m["recall@3"] is None
        assert m["ndcg@3"] is None
        assert m["recall_applicable"] is False
        # Hit still computed (does not require a closed judgement set).
        assert m["hit@3"] == 1.0

    def test_recall_and_graded_ndcg_when_eligible(self):
        j = self._judgment({"d1": 3, "d2": 1}, recall_eligible=True)
        m = compute_reviewed_metrics(["d1", "d2"], j, ks=(3,))
        assert m["recall@3"] == 1.0
        assert m["ndcg@3"] == pytest.approx(1.0, abs=1e-4)

    def test_hit_requires_judged_relevant(self):
        j = self._judgment({"d1": 2}, recall_eligible=True)
        m = compute_reviewed_metrics(["d9"], j, ks=(3,))
        assert m["hit@3"] == 0.0
        assert m["mrr"] == 0.0


class TestSummarize:
    def test_none_excluded_from_means_with_own_denominator(self):
        rows = [
            {"hit@1": 1.0, "recall@1": None, "mrr": 1.0, "unjudged_at_1": 0},
            {"hit@1": 0.0, "recall@1": 1.0, "mrr": 0.0, "unjudged_at_1": 1},
        ]
        out = summarize_reviewed_metrics(rows, ks=(1,))
        assert out["hit@1"]["value"] == 0.5
        assert out["hit@1"]["denominator"] == 2
        # recall only measured on one row; it is not coerced to 0 on the other.
        assert out["recall@1"]["value"] == 1.0
        assert out["recall@1"]["denominator"] == 1
        assert out["recall@1"]["numerator_sum"] == 1.0

    def test_metric_not_measured_when_no_rows_carry_it(self):
        rows = [{"hit@1": 0.0, "recall@1": None, "mrr": 0.0, "unjudged_at_1": 0}]
        out = summarize_reviewed_metrics(rows, ks=(1,))
        assert out["recall@1"]["not_measured"] is True
        assert out["recall@1"]["value"] is None


class TestCli:
    def _write(self, path: Path, records: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )

    def test_not_measurable_without_judged(self, tmp_path):
        labels = tmp_path / "draft.jsonl"
        self._write(
            labels,
            [
                _rec(
                    query_id="q1",
                    doc_id="d1",
                    grade=None,
                    status="DRAFT_UNVERIFIED",
                    method="llm_suggested",
                    completeness=None,
                    evidence="",
                    reviewed_at=None,
                )
            ],
        )
        rc = main(
            [
                "--gold-labels",
                str(labels),
                "--no-corpus",
                "--summary-only",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        # summary-only is a status/QC command: always exits 0, but the report
        # must say NOT_MEASURABLE and must not run the ablation.
        assert rc == 0
        report = json.loads((tmp_path / "out").glob("*/report.json").__next__().read_text())
        assert report["status"] == "NOT_MEASURABLE"
        assert report["not_measurable_reason"] == "NO_JUDGED_LABELS"
        assert report["ablation_executed"] is False

    def test_formal_run_without_judged_fails_closed(self, tmp_path):
        labels = tmp_path / "draft.jsonl"
        self._write(
            labels,
            [
                _rec(
                    query_id="q1",
                    doc_id="d1",
                    grade=None,
                    status="DRAFT_UNVERIFIED",
                    method="llm_suggested",
                    completeness=None,
                    evidence="",
                    reviewed_at=None,
                )
            ],
        )
        rc = main(["--gold-labels", str(labels), "--no-corpus", "--output", str(tmp_path / "out")])
        assert rc == 2

    def test_summary_only_with_judged_exits_zero(self, tmp_path):
        labels = tmp_path / "reviewed.jsonl"
        self._write(
            labels,
            [
                _rec(query_id="q1", doc_id="d1", grade=3),
                _rec(query_id="q1", doc_id="d2", grade=0),
            ],
        )
        rc = main(
            [
                "--gold-labels",
                str(labels),
                "--no-corpus",
                "--summary-only",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        report = json.loads((tmp_path / "out").glob("*/report.json").__next__().read_text())
        assert report["status"] == "MEASURED"
        assert report["gold_labels"]["judged_query_count"] == 1
        assert report["gold_labels"]["judged_label_count"] == 2

    def test_schema_violation_exits_one(self, tmp_path):
        labels = tmp_path / "bad.jsonl"
        # JUDGED without evidence_span violates the contract.
        self._write(labels, [_rec(query_id="q1", doc_id="d1", grade=3, evidence="")])
        rc = main(
            [
                "--gold-labels",
                str(labels),
                "--no-corpus",
                "--summary-only",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 1

    def test_missing_gold_file_exits_two(self, tmp_path):
        rc = main(["--gold-labels", str(tmp_path / "nope.jsonl"), "--no-corpus"])
        assert rc == 2

    def test_missing_gold_file_summary_only_reports_not_measurable(self, tmp_path):
        # `make rag-gold-reviewed-status` must be runnable in the pre-annotation
        # state: a missing human gold file is NOT_MEASURABLE, not a crash.
        rc = main(
            [
                "--gold-labels",
                str(tmp_path / "nope.jsonl"),
                "--no-corpus",
                "--summary-only",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        report = json.loads((tmp_path / "out").glob("*/report.json").__next__().read_text())
        assert report["status"] == "NOT_MEASURABLE"
        assert report["not_measurable_reason"] == "GOLD_LABELS_FILE_ABSENT"
        assert report["ablation_executed"] is False


def test_gold_population_is_frozen_dataclass():
    population = GoldPopulation(
        judgments=(), excluded={}, status_counts={}, judged_label_count=0, total_queries_seen=0
    )
    with pytest.raises(FrozenInstanceError):
        population.judged_label_count = 5  # type: ignore[misc]
