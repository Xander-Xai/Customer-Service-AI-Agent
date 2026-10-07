#!/usr/bin/env python3
"""Provenance-enforced RAG gold-label contract + offline validator (Issue #119).

Why this exists
---------------
The formal 649-query RAG metrics are computed against
``tests/eval/rag_benchmark.json``'s ``expected_doc_ids``. The static audit in
``scripts/rag_gold_label_provenance.py`` established that those ids are **not
relevance judgements** (``DATASET_DEFECT``): the only recorded generator is
``random.sample`` over same-category documents. Publishing Hit@K / MRR / NDCG
against them would produce numbers that look like formal evidence but are
semantically meaningless.

This module defines a **versioned, provenance-enforced** replacement label
contract and a deterministic, offline validator. It does **not** modify the
shipped 649 benchmark, does not compute metrics, and makes no network / LLM /
embedding / reranker call.

Schema (``rag-gold-label/v1``)
------------------------------
One JSON object per line (JSONL). Required keys on every record::

    schema_version      "rag-gold-label/v1"
    query_id            benchmark query id (e.g. "bench_0000")
    query               the user query text
    corpus_version      human-readable corpus version/tag
    corpus_hash         sha256 of the evaluated corpus
    doc_id              corpus document id (null only for EXCLUDED rows)
    evidence_span       verbatim snippet justifying relevance
    relevance_grade     0..3 (null unless label_status == JUDGED)
    annotator           who produced the judgement
    annotation_method   human | llm_judge_human_verified | llm_suggested |
                        category_random_match
    reviewed_at         ISO-8601 UTC timestamp of the *human* review (or null)
    label_status        DRAFT_UNVERIFIED | JUDGED | EXCLUDED | UNDETERMINABLE
    exclusion_reason    required (non-empty) for EXCLUDED / UNDETERMINABLE

Trust rules enforced by :func:`validate_records`
-----------------------------------------------
1. required fields present and well typed;
2. duplicate ``(query_id, doc_id)`` labels rejected;
3. ``doc_id`` must exist in the corpus when corpus ids are supplied;
4. a ``JUDGED`` label must carry a non-empty ``evidence_span``;
5. a ``JUDGED`` label must have been **human-reviewed** (``reviewed_at`` +
   ``annotator`` + a human-verified ``annotation_method``);
6. a ``category_random_match`` label can never be ``JUDGED`` (pseudo-gold);
7. an ``llm_suggested`` label can only be ``DRAFT_UNVERIFIED`` (LLM candidates
   are never trusted gold until independently confirmed);
8. only ``JUDGED`` may carry a non-null ``relevance_grade``; ``EXCLUDED`` and
   ``UNDETERMINABLE`` must state an ``exclusion_reason``.

Usage::

    python3 scripts/validate_gold_labels.py tests/eval/gold_labels/template.jsonl
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "rag-gold-label/v1"

LABEL_STATUS_DRAFT = "DRAFT_UNVERIFIED"
LABEL_STATUS_JUDGED = "JUDGED"
LABEL_STATUS_EXCLUDED = "EXCLUDED"
LABEL_STATUS_UNDETERMINABLE = "UNDETERMINABLE"
LABEL_STATUSES = frozenset(
    {
        LABEL_STATUS_DRAFT,
        LABEL_STATUS_JUDGED,
        LABEL_STATUS_EXCLUDED,
        LABEL_STATUS_UNDETERMINABLE,
    }
)

METHOD_HUMAN = "human"
METHOD_LLM_VERIFIED = "llm_judge_human_verified"
METHOD_LLM_SUGGESTED = "llm_suggested"
METHOD_CATEGORY_RANDOM = "category_random_match"
ANNOTATION_METHODS = frozenset(
    {
        METHOD_HUMAN,
        METHOD_LLM_VERIFIED,
        METHOD_LLM_SUGGESTED,
        METHOD_CATEGORY_RANDOM,
    }
)
HUMAN_VERIFIED_METHODS = frozenset({METHOD_HUMAN, METHOD_LLM_VERIFIED})

RELEVANCE_GRADES = frozenset({0, 1, 2, 3})

REQUIRED_FIELDS = (
    "schema_version",
    "query_id",
    "query",
    "corpus_version",
    "corpus_hash",
    "doc_id",
    "evidence_span",
    "relevance_grade",
    "annotator",
    "annotation_method",
    "reviewed_at",
    "label_status",
    "exclusion_reason",
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


def corpus_sha256(path: str | Path) -> str:
    """Deterministic sha256 of a corpus file (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load JSONL records; blank lines ignored, malformed lines raise."""
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{lineno}: record must be a JSON object")
            obj.setdefault("__line__", lineno)
            records.append(obj)
    return records


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _record_ref(record: dict[str, Any], index: int) -> str:
    line = record.get("__line__")
    qid = record.get("query_id")
    doc = record.get("doc_id")
    if line is not None:
        return f"line {line} (query_id={qid!r}, doc_id={doc!r})"
    return f"record #{index} (query_id={qid!r}, doc_id={doc!r})"


def validate_records(
    records: Iterable[dict[str, Any]],
    *,
    corpus_ids: set[str] | None = None,
    expected_corpus_hash: str | None = None,
) -> list[str]:
    """Return a list of human-readable errors; empty list means valid."""
    errors: list[str] = []
    records = list(records)
    seen: set[tuple[Any, Any]] = set()

    for index, record in enumerate(records):
        ref = _record_ref(record, index)

        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            errors.append(f"{ref}: missing required field(s): {', '.join(missing)}")
            # Cannot meaningfully check further without the fields.
            continue

        if record["schema_version"] != SCHEMA_VERSION:
            errors.append(
                f"{ref}: schema_version must be {SCHEMA_VERSION!r}, "
                f"got {record['schema_version']!r}"
            )

        for field in ("query_id", "query", "corpus_version", "corpus_hash"):
            if not _is_nonempty_str(record[field]):
                errors.append(f"{ref}: {field} must be a non-empty string")

        corpus_hash = record["corpus_hash"]
        if _is_nonempty_str(corpus_hash) and not _HASH_RE.match(corpus_hash):
            errors.append(f"{ref}: corpus_hash must be a 64-char lowercase sha256 hex")
        if expected_corpus_hash and corpus_hash != expected_corpus_hash:
            errors.append(
                f"{ref}: corpus_hash {corpus_hash!r} does not match evaluated corpus "
                f"{expected_corpus_hash!r}"
            )

        status = record["label_status"]
        if status not in LABEL_STATUSES:
            errors.append(
                f"{ref}: label_status must be one of {sorted(LABEL_STATUSES)}, got {status!r}"
            )
            continue

        method = record["annotation_method"]
        if method not in ANNOTATION_METHODS:
            errors.append(
                f"{ref}: annotation_method must be one of "
                f"{sorted(ANNOTATION_METHODS)}, got {method!r}"
            )
            continue

        grade = record["relevance_grade"]
        if grade is not None and (isinstance(grade, bool) or grade not in RELEVANCE_GRADES):
            errors.append(
                f"{ref}: relevance_grade must be null or one of "
                f"{sorted(RELEVANCE_GRADES)}, got {grade!r}"
            )

        doc_id = record["doc_id"]
        if doc_id is not None and not _is_nonempty_str(doc_id):
            errors.append(f"{ref}: doc_id must be a non-empty string or null")

        # Duplicate (query_id, doc_id) — same judgement declared twice.
        key = (record["query_id"], doc_id)
        if key in seen:
            errors.append(f"{ref}: duplicate (query_id, doc_id) label {key!r}")
        else:
            seen.add(key)

        # Document existence.
        if corpus_ids is not None and _is_nonempty_str(doc_id) and doc_id not in corpus_ids:
            errors.append(f"{ref}: doc_id {doc_id!r} does not exist in the corpus")

        # Rule 6: category-random matches are never relevance gold.
        if method == METHOD_CATEGORY_RANDOM:
            if status == LABEL_STATUS_JUDGED:
                errors.append(
                    f"{ref}: category_random_match cannot be JUDGED "
                    "(pseudo-gold: same-category random match is not a relevance judgement)"
                )
            if grade is not None:
                errors.append(f"{ref}: category_random_match must not carry a relevance_grade")

        # Rule 7: LLM suggestions are never trusted gold.
        if method == METHOD_LLM_SUGGESTED and status != LABEL_STATUS_DRAFT:
            errors.append(
                f"{ref}: llm_suggested candidates must be {LABEL_STATUS_DRAFT}, got {status!r}"
            )

        if status == LABEL_STATUS_JUDGED:
            if not _is_nonempty_str(doc_id):
                errors.append(f"{ref}: JUDGED requires a doc_id")
            if not _is_nonempty_str(record["evidence_span"]):
                errors.append(f"{ref}: JUDGED requires a non-empty evidence_span")
            if grade is None:
                errors.append(f"{ref}: JUDGED requires a relevance_grade")
            if not _is_nonempty_str(record["annotator"]):
                errors.append(f"{ref}: JUDGED requires an annotator")
            if method not in HUMAN_VERIFIED_METHODS:
                errors.append(
                    f"{ref}: JUDGED requires a human-verified annotation_method "
                    f"({sorted(HUMAN_VERIFIED_METHODS)}), got {method!r}"
                )
            reviewed_at = record["reviewed_at"]
            if not _is_nonempty_str(reviewed_at):
                errors.append(f"{ref}: JUDGED requires reviewed_at (unreviewed != JUDGED)")
            elif not _ISO_UTC_RE.match(reviewed_at):
                errors.append(f"{ref}: reviewed_at must be ISO-8601 UTC, got {reviewed_at!r}")

        elif grade is not None:
            errors.append(
                f"{ref}: relevance_grade must be null unless label_status is "
                f"{LABEL_STATUS_JUDGED} (got status={status!r})"
            )

        if status in (LABEL_STATUS_EXCLUDED, LABEL_STATUS_UNDETERMINABLE) and not _is_nonempty_str(
            record["exclusion_reason"]
        ):
            errors.append(f"{ref}: {status} requires a non-empty exclusion_reason")

    return errors


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Count records per label_status (for the validator's report)."""
    counts: dict[str, int] = {}
    for record in records:
        status = record.get("label_status", "<missing>")
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


__all__ = [
    "ANNOTATION_METHODS",
    "HUMAN_VERIFIED_METHODS",
    "LABEL_STATUSES",
    "LABEL_STATUS_DRAFT",
    "LABEL_STATUS_EXCLUDED",
    "LABEL_STATUS_JUDGED",
    "LABEL_STATUS_UNDETERMINABLE",
    "METHOD_CATEGORY_RANDOM",
    "METHOD_HUMAN",
    "METHOD_LLM_SUGGESTED",
    "METHOD_LLM_VERIFIED",
    "RELEVANCE_GRADES",
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "corpus_sha256",
    "load_jsonl",
    "summarize",
    "validate_records",
]
