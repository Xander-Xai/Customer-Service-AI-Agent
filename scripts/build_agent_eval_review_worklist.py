#!/usr/bin/env python3
"""Build an auditable, balanced human-review worklist for Agent route labels.

A model-generated route is a *candidate*, never ground truth. This script does
not approve, relabel, or score any case. An independent reviewer must use the
existing approve_agent_eval_annotations.py workflow after reading each query.

Example:
    python3 scripts/build_agent_eval_review_worklist.py --per-route 3 --limit 24
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation.agent_eval.cases import AgentCase, load_dataset  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "agent-eval" / "route-review-worklist.json"
CONTRACT = "agent-route-review-worklist/v1"


def select_candidates(cases: tuple[AgentCase, ...], *, per_route: int = 3, limit: int = 24) -> list[AgentCase]:
    """Deterministic round-robin across candidate routes, excluding confirmed labels."""
    if per_route <= 0 or limit <= 0:
        raise ValueError("per_route and limit must be positive")
    groups: dict[str, list[AgentCase]] = defaultdict(list)
    for case in cases:
        if case.expected_route is not None and not case.annotation.is_human_confirmed:
            groups[case.expected_route].append(case)
    for group in groups.values():
        group.sort(key=lambda c: c.case_id)
    selected: list[AgentCase] = []
    for index in range(per_route):
        for route in sorted(groups):
            if len(selected) >= limit:
                return selected
            if len(groups[route]) > index:
                selected.append(groups[route][index])
    return selected


def build_worklist(dataset, *, per_route: int = 3, limit: int = 24) -> dict:
    selected = select_candidates(dataset.cases, per_route=per_route, limit=limit)
    return {
        "schema_version": CONTRACT,
        "dataset_sha256": dataset.sha256,
        "dataset_total": len(dataset),
        "selected_count": len(selected),
        "review_status": "DRAFT_UNVERIFIED",
        "formal_route_accuracy": "NOT_MEASURED_UNTIL_HUMAN_CONFIRMED",
        "cases": [
            {
                "case_id": case.case_id,
                "query": case.input,
                "candidate_route_not_ground_truth": case.expected_route,
                "tags": list(case.tags),
                "reviewer_route": None,
                "reviewer_id": None,
                "review_notes": "",
                "review_status": "PENDING_HUMAN_REVIEW",
            }
            for case in selected
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--per-route", type=int, default=3)
    parser.add_argument("--limit", type=int, default=24)
    args = parser.parse_args(argv)
    if args.per_route <= 0 or args.limit <= 0:
        parser.error("--per-route and --limit must be positive")
    dataset = load_dataset(args.dataset)
    report = build_worklist(dataset, per_route=args.per_route, limit=args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"draft human-review worklist: {report['selected_count']} cases -> {args.out}")
    print("No labels approved. Formal route accuracy remains NOT_MEASURED until independent review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
