#!/usr/bin/env python3
"""P1-03 migration tool: discover / rebuild legacy Qdrant Point IDs.

Default is ``--dry-run`` (read-only). The destructive ``--execute`` path
requires ``--i-understand-this-is-destructive`` and never deletes a whole
collection — it only removes orphaned legacy points after the stable points
are in place (see :func:`rag.point_id_migration.rebuild_collection_point_ids`).

Usage
-----
    # read-only: classify every point in product_knowledge
    python -m scripts.migrate_point_ids --collection product_knowledge

    # destructive rebuild (requires explicit acknowledgement)
    python -m scripts.migrate_point_ids --collection product_knowledge \
        --execute --i-understand-this-is-destructive

Exit codes: 0 success, 1 collection/connection error, 2 argument error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

# Static knowledge collections in this project (see rag/seed_data.py,
# knowledge/router.py). Used by --all.
DEFAULT_COLLECTIONS = (
    "product_knowledge",
    "faq",
    "tech_support",
    "complaint_knowledge",
    "image_knowledge",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover / rebuild legacy Qdrant Point IDs (P1-03).",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--collection",
        action="append",
        metavar="NAME",
        help="target collection (may be repeated)",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="target all known static knowledge collections",
    )
    # Mode group: neither flag (default) => dry-run (read-only).
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="read-only discovery (default when neither flag is given)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="destructive in-place rebuild (requires --i-understand-this-is-destructive)",
    )
    parser.add_argument(
        "--i-understand-this-is-destructive",
        action="store_true",
        help="explicit acknowledgement required to use --execute",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Enforce the migration-safety contract.

    Raises SystemExit(2) if --execute is requested without the explicit
    acknowledgement flag. No flag (or --dry-run) is the safe default.
    """
    if args.execute and not args.i_understand_this_is_destructive:
        parser_error("--execute is destructive: also pass --i-understand-this-is-destructive")


def parser_error(message: str) -> None:
    print(f"migrate_point_ids: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _target_collections(args: argparse.Namespace) -> list[str]:
    if args.all:
        return list(DEFAULT_COLLECTIONS)
    return list(args.collection or [])


def _connect_client():
    """Build a QdrantClient from core.config (env-overridable)."""
    from qdrant_client import QdrantClient

    from core.config import (
        QDRANT_API_KEY,
        QDRANT_GRPC_PORT,
        QDRANT_HOST,
        QDRANT_PORT,
        QDRANT_PREFER_GRPC,
    )

    return QdrantClient(
        host=QDRANT_HOST,
        port=QDRANT_PORT,
        grpc_port=QDRANT_GRPC_PORT,
        prefer_grpc=QDRANT_PREFER_GRPC,
        api_key=QDRANT_API_KEY or None,
        timeout=10.0,
    )


def _serialize_report(report) -> dict:
    return {
        "collection": report.collection,
        "dry_run": getattr(report, "dry_run", True),
        "total_points": getattr(report, "total_points", 0),
        "stable_points": getattr(report, "stable_points", 0),
        "legacy_points": getattr(report, "legacy_points", 0),
        "unmappable_points": getattr(report, "unmappable_points", 0),
        "duplicate_logical_ids": getattr(report, "duplicate_logical_ids", []),
        "potential_conflicts": getattr(report, "potential_conflicts", []),
        "scrolled": getattr(report, "scrolled", 0),
        "upserted": getattr(report, "upserted", 0),
        "deleted_legacy": getattr(report, "deleted_legacy", 0),
        "skipped_unmappable": getattr(report, "skipped_unmappable", 0),
        # BF-P1-03-02/04: surface the execute-preflight blockers so an
        # operator/CI can see WHAT blocked the migration, not just that it
        # aborted (the exit code already reflects the failure).
        "aborted": getattr(report, "aborted", False),
        "blocking_duplicates": getattr(report, "blocking_duplicates", []),
        "blocking_conflicts": getattr(report, "blocking_conflicts", []),
        "blocking_unmappable_occupied": getattr(report, "blocking_unmappable_occupied", []),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    from rag.point_id_migration import (
        discover_legacy_points,
        rebuild_collection_point_ids,
    )

    try:
        client = _connect_client()
        client.get_collections()
    except Exception as e:  # noqa: BLE001 - surface connection failure
        print(f"migrate_point_ids: cannot connect to Qdrant: {e}", file=sys.stderr)
        return 1

    reports = []
    failed = 0
    for name in _target_collections(args):
        try:
            if args.execute:
                report = rebuild_collection_point_ids(client, name, dry_run=False)
                # BF-P1-03-02: a blocking-conflict abort is a failure, not a
                # silent skip — surface it and propagate a non-zero exit.
                if getattr(report, "aborted", False):
                    failed += 1
                    reports.append(_serialize_report(report))
                    print(
                        f"migrate_point_ids: {name}: aborted — blocking conflicts "
                        f"(see report). Resolve before re-running.",
                        file=sys.stderr,
                    )
                    continue
            else:
                report = discover_legacy_points(client, name)
            reports.append(_serialize_report(report))
        except Exception as e:  # noqa: BLE001 - per-collection isolation
            failed += 1
            print(f"migrate_point_ids: {name}: {e}", file=sys.stderr)
            reports.append({"collection": name, "error": str(e)})

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for r in reports:
            print(json.dumps(r, indent=2, default=str, ensure_ascii=False))
    # BF-P1-03-03: any per-collection failure (exception OR blocking-conflict
    # abort) must return non-zero — no migration false-green for CI/operators.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
