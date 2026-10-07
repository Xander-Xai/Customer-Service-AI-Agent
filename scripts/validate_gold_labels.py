#!/usr/bin/env python3
"""Offline validator CLI for the provenance-enforced gold-label contract (#119).

    python3 scripts/validate_gold_labels.py tests/eval/gold_labels/template.jsonl
    python3 scripts/validate_gold_labels.py labels.jsonl \
        --corpus data/knowledge_base/knowledge_base_5000.jsonl

Exit codes: 0 = valid (all records admissible), 1 = invalid, 2 = input error.
No network, LLM, embedding or reranker call is made. The shipped 649 benchmark
is never read or modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gold_label_contract import (
    SCHEMA_VERSION,
    corpus_sha256,
    load_jsonl,
    summarize,
    validate_records,
)


def _load_corpus_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            doc_id = obj.get("id")
            if isinstance(doc_id, str) and doc_id:
                ids.add(doc_id)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate RAG gold labels against the rag-gold-label/v1 contract."
    )
    parser.add_argument("labels", help="JSONL file with labelled records")
    parser.add_argument(
        "--corpus",
        default="data/knowledge_base/knowledge_base_5000.jsonl",
        help="corpus JSONL (each line has an `id`); used for doc-id existence + hash",
    )
    parser.add_argument(
        "--no-corpus",
        action="store_true",
        help="skip corpus doc-id existence checks and corpus hash verification",
    )
    parser.add_argument("--json-out", default=None, help="write the report as JSON")
    args = parser.parse_args(argv)

    try:
        records = load_jsonl(args.labels)
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    corpus_ids: set[str] | None = None
    expected_hash: str | None = None
    doc_id_check = "NOT_RUN"
    if not args.no_corpus:
        corpus_path = Path(args.corpus)
        if not corpus_path.is_file():
            # The corpus is generated (gitignored), not committed. Missing corpus
            # must not silently pass the doc-id check -- surface it as NOT_RUN.
            print(
                f"warning: corpus not found at {corpus_path}; doc-id existence and "
                f"corpus-hash checks are NOT_RUN (use --no-corpus to silence). "
                f"Generate it with `make generate-knowledge-base`.",
                file=sys.stderr,
            )
        else:
            try:
                corpus_ids = _load_corpus_ids(corpus_path)
            except ValueError as exc:
                print(f"input error: {exc}", file=sys.stderr)
                return 2
            expected_hash = corpus_sha256(corpus_path)
            doc_id_check = "RAN"

    errors = validate_records(records, corpus_ids=corpus_ids, expected_corpus_hash=expected_hash)
    report = {
        "schema_version": SCHEMA_VERSION,
        "labels_file": str(args.labels),
        "corpus": None if (args.no_corpus or corpus_ids is None) else str(args.corpus),
        "corpus_hash": expected_hash,
        "doc_id_check": "SKIPPED" if args.no_corpus else doc_id_check,
        "records": len(records),
        "status_counts": summarize(records),
        "valid": not errors,
        "errors": errors,
    }

    print(f"gold-label contract {SCHEMA_VERSION}: {len(records)} record(s)")
    print(f"  doc-id/corpus-hash check: {report['doc_id_check']}")
    for status, count in sorted(report["status_counts"].items()):
        print(f"  {status}: {count}")
    if errors:
        print(f"INVALID — {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
    else:
        print("OK: all records satisfy the contract")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written: {out}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
