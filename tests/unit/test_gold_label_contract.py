"""Deterministic tests for the provenance-enforced RAG gold-label contract (#119).

The validator is the gate that stops "same-category random match" pseudo-gold and
unreviewed LLM candidates from ever being treated as formal retrieval evidence.
Everything here is offline and makes no provider/LLM/embedding/reranker call.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.gold_label_contract import (
    LABEL_STATUS_CONSTRUCTED,
    LABEL_STATUS_DRAFT,
    LABEL_STATUS_EXCLUDED,
    LABEL_STATUS_JUDGED,
    LABEL_STATUS_UNDETERMINABLE,
    METHOD_CATEGORY_RANDOM,
    METHOD_DERIVED,
    METHOD_HUMAN,
    METHOD_LLM_SUGGESTED,
    SCHEMA_VERSION,
    load_jsonl,
    summarize,
    validate_records,
)

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "tests" / "eval" / "rag_benchmark.json"
TEMPLATE = ROOT / "tests" / "eval" / "gold_labels" / "template.jsonl"

CORPUS_HASH = "a81ea7f3347b45b3adefe474790fabd0b2277eada05327422134d3ca0737502d"


def _record(**overrides):
    base = {
        "schema_version": SCHEMA_VERSION,
        "query_id": "bench_0000",
        "query": "这款产品适合敏感肌吗？",
        "corpus_version": "knowledge_base_5000@2026-06",
        "corpus_hash": CORPUS_HASH,
        "doc_id": "derm_000001",
        "evidence_span": "本产品专为敏感肌设计",
        "relevance_grade": 2,
        "annotator": "reviewer-7",
        "annotation_method": METHOD_HUMAN,
        "reviewed_at": "2026-07-01T00:00:00Z",
        "label_status": LABEL_STATUS_JUDGED,
        "exclusion_reason": None,
        # 仅 CONSTRUCTED 需要非空；其它状态为 null。字段必须**显式出现**，
        # 这样"缺失"与"刻意留空"在审计时是可区分的两件事。
        "derivation_rule": None,
    }
    base.update(overrides)
    return base


def _errors(record, **kwargs):
    return validate_records([record], **kwargs)


class TestValid:
    def test_valid_judged_passes(self):
        assert _errors(_record()) == []

    def test_valid_draft_llm_candidate_passes(self):
        assert (
            _errors(
                _record(
                    label_status=LABEL_STATUS_DRAFT,
                    annotation_method=METHOD_LLM_SUGGESTED,
                    annotator="llm-candidate",
                    relevance_grade=None,
                    reviewed_at=None,
                    evidence_span="",
                )
            )
            == []
        )

    def test_valid_excluded_with_reason_passes(self):
        assert (
            _errors(
                _record(
                    doc_id=None,
                    label_status=LABEL_STATUS_EXCLUDED,
                    relevance_grade=None,
                    exclusion_reason="all gold ids absent from corpus",
                )
            )
            == []
        )

    def test_valid_undeterminable_with_reason_passes(self):
        assert (
            _errors(
                _record(
                    label_status=LABEL_STATUS_UNDETERMINABLE,
                    relevance_grade=None,
                    exclusion_reason="ambiguous query; annotators disagree",
                )
            )
            == []
        )


class TestRejections:
    def test_missing_field_rejected(self):
        record = _record()
        del record["evidence_span"]
        assert any("missing required field" in e for e in _errors(record))

    def test_duplicate_id_rejected(self):
        errors = validate_records([_record(), _record()])
        assert any("duplicate" in e for e in errors)

    def test_invalid_doc_id_rejected(self):
        errors = _errors(_record(doc_id="does_not_exist"), corpus_ids={"derm_000001"})
        assert any("does not exist in the corpus" in e for e in errors)

    def test_judged_without_evidence_span_rejected(self):
        assert any("evidence_span" in e for e in _errors(_record(evidence_span="")))

    def test_judged_without_review_rejected(self):
        errors = _errors(_record(reviewed_at=None))
        assert any("reviewed_at" in e for e in errors)

    def test_judged_without_annotator_rejected(self):
        errors = _errors(_record(annotator=""))
        assert any("annotator" in e for e in errors)

    def test_llm_suggested_cannot_be_judged(self):
        errors = _errors(_record(annotation_method=METHOD_LLM_SUGGESTED))
        assert any("llm_suggested" in e for e in errors)

    def test_category_random_match_cannot_be_judged(self):
        errors = _errors(_record(annotation_method=METHOD_CATEGORY_RANDOM))
        assert any("category_random_match" in e for e in errors)

    def test_category_random_match_cannot_carry_grade(self):
        errors = _errors(
            _record(
                annotation_method=METHOD_CATEGORY_RANDOM,
                label_status=LABEL_STATUS_DRAFT,
                reviewed_at=None,
            )
        )
        assert any("must not carry a relevance_grade" in e for e in errors)

    def test_non_judged_cannot_carry_grade(self):
        errors = _errors(
            _record(
                label_status=LABEL_STATUS_DRAFT,
                annotation_method=METHOD_LLM_SUGGESTED,
                annotator="llm",
                reviewed_at=None,
            )
        )
        assert any("must be null unless" in e for e in errors)

    def test_excluded_without_reason_rejected(self):
        errors = _errors(
            _record(
                doc_id=None,
                label_status=LABEL_STATUS_EXCLUDED,
                relevance_grade=None,
                exclusion_reason=None,
            )
        )
        assert any("exclusion_reason" in e for e in errors)

    def test_invalid_status_rejected(self):
        assert any("label_status" in e for e in _errors(_record(label_status="MAYBE")))

    def test_invalid_method_rejected(self):
        assert any("annotation_method" in e for e in _errors(_record(annotation_method="vibes")))

    def test_corpus_hash_mismatch_rejected(self):
        errors = _errors(_record(), expected_corpus_hash="0" * 64)
        assert any("does not match evaluated corpus" in e for e in errors)

    def test_bad_corpus_hash_format_rejected(self):
        assert any("sha256" in e for e in _errors(_record(corpus_hash="not-a-hash")))

    def test_bad_reviewed_at_format_rejected(self):
        assert any("ISO-8601" in e for e in _errors(_record(reviewed_at="2026/07/01")))


class TestTemplate:
    def test_template_is_admissible(self):
        records = load_jsonl(TEMPLATE)
        assert records, "template must contain at least one example"
        errors = validate_records(records, corpus_ids={"derm_000001", "derm_000002"})
        assert errors == [], errors

    def test_template_hash_matches_pinned_corpus_hash(self):
        records = load_jsonl(TEMPLATE)
        assert all(r["corpus_hash"] == CORPUS_HASH for r in records)


class TestShippedBenchmarkUntouched:
    def test_validation_does_not_modify_benchmark(self):
        before = hashlib.sha256(BENCHMARK.read_bytes()).hexdigest()
        validate_records([_record(), _record(query_id="q2")])
        after = hashlib.sha256(BENCHMARK.read_bytes()).hexdigest()
        assert before == after, "validator must never mutate the shipped benchmark"


class TestSummarize:
    def test_counts_by_status(self):
        records = [
            _record(),
            _record(query_id="q2"),
            _record(
                query_id="q3",
                label_status=LABEL_STATUS_DRAFT,
                annotation_method=METHOD_LLM_SUGGESTED,
                annotator="llm",
                relevance_grade=None,
                reviewed_at=None,
                evidence_span="",
            ),
        ]
        counts = summarize(records)
        assert counts[LABEL_STATUS_JUDGED] == 2
        assert counts[LABEL_STATUS_DRAFT] == 1


class TestValidatorAgainstCorpusFileIfPresent:
    """When the (gitignored, generated) corpus exists, verify real doc ids."""

    def test_pinned_doc_id_exists_in_generated_corpus(self):
        corpus = ROOT / "data" / "knowledge_base" / "knowledge_base_5000.jsonl"
        if not corpus.is_file():
            pytest.skip("generated corpus not present; run `make generate-knowledge-base`")
        import json

        ids = set()
        with open(corpus, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    ids.add(json.loads(line).get("id"))
        assert "derm_000001" in ids


class TestConstructedGold:
    """CONSTRUCTED：机械推导的 gold，与 JUDGED **分属不同 population**。"""

    def test_valid_constructed_passes(self):
        assert (
            _errors(
                _record(
                    relevance_grade=3,
                    annotator="derive_from_title_v1",
                    annotation_method=METHOD_DERIVED,
                    reviewed_at=None,
                    label_status=LABEL_STATUS_CONSTRUCTED,
                    derivation_rule="query := document.title (verbatim)",
                )
            )
            == []
        )

    def test_constructed_requires_derivation_rule(self):
        errors = _errors(
            _record(
                annotation_method=METHOD_DERIVED,
                reviewed_at=None,
                label_status=LABEL_STATUS_CONSTRUCTED,
                derivation_rule=None,
            )
        )
        assert any("derivation_rule" in e for e in errors), errors

    def test_constructed_must_not_claim_human_review(self):
        """CONSTRUCTED 声称有人审过 = 把机械推导伪装成人工判定。"""
        errors = _errors(
            _record(
                annotation_method=METHOD_DERIVED,
                label_status=LABEL_STATUS_CONSTRUCTED,
                derivation_rule="query := document.title",
                reviewed_at="2026-07-01T00:00:00Z",
            )
        )
        assert any("must NOT claim a human review" in e for e in errors), errors

    def test_derived_method_requires_constructed_status(self):
        errors = _errors(_record(annotation_method=METHOD_DERIVED))
        assert any("only valid with" in e for e in errors), errors

    def test_is_human_verified_is_false_for_constructed(self):
        """核心断言：CONSTRUCTED 永远不算人工确认。

        任何把 CONSTRUCTED 与 JUDGED 混进同一个分母的统计都是在偷换概念 ——
        "索引能不能按标题找回文档"和"搜索结果相不相关"是两件事。
        """
        from scripts.gold_label_contract import is_human_verified

        constructed = _record(
            annotation_method=METHOD_DERIVED,
            reviewed_at=None,
            label_status=LABEL_STATUS_CONSTRUCTED,
            derivation_rule="query := document.title (verbatim)",
        )
        judged = _record()
        assert is_human_verified(constructed) is False
        assert is_human_verified(judged) is True
