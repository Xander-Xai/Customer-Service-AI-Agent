"""Offline, deterministic checks for balanced Agent route-label review selection.

This is an annotation worklist, not an automatic gold-label generator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evaluation.agent_eval.cases import AgentCase, Annotation
from scripts import approve_agent_eval_annotations as approvals
from scripts.build_agent_eval_review_worklist import main, select_candidates
from scripts.generate_agent_eval_cases import build_cases

pytestmark = pytest.mark.unit


def _case(name: str, route: str, *, reviewed: bool = False) -> AgentCase:
    annotation = (
        Annotation(
            provenance="human_confirmed", method="human_reviewed", confirmed_by="reviewer"
        )
        if reviewed else Annotation()
    )
    return AgentCase(
        case_id=name,
        input=f"synthetic query {name}",
        expected_terminal_state="SUCCEEDED",
        max_steps=5,
        expected_route=route,
        annotation=annotation,
    )


def test_balanced_selection_is_deterministic_and_excludes_human_confirmed():
    cases = (
        _case("b2", "billing"),
        _case("a2", "product_info"),
        _case("a1", "product_info"),
        _case("b1", "billing"),
        _case("a0", "product_info", reviewed=True),
        _case("c1", "technical_support"),
    )
    first = select_candidates(cases, per_route=2, limit=5)
    second = select_candidates(tuple(reversed(cases)), per_route=2, limit=5)
    assert [case.case_id for case in first] == ["b1", "a1", "c1", "b2", "a2"]
    assert [case.case_id for case in second] == [case.case_id for case in first]
    assert all(not case.annotation.is_human_confirmed for case in first)


@pytest.mark.parametrize("per_route,limit", [(0, 2), (2, 0), (-1, 1)])
def test_invalid_sampling_budget_refused(per_route, limit):
    with pytest.raises(ValueError, match="must be positive"):
        select_candidates(
            (_case("a1", "product_info"),), per_route=per_route, limit=limit
        )


def test_generated_worklist_does_not_self_approve_gold_labels(tmp_path):
    out = tmp_path / "route-review.json"
    assert main(["--out", str(out), "--per-route", "2", "--limit", "10"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "agent-route-review-worklist/v1"
    assert report["review_status"] == "DRAFT_UNVERIFIED"
    assert report["blind_review"] is True
    assert report["formal_route_accuracy"] == "NOT_MEASURED_UNTIL_HUMAN_CONFIRMED"
    assert len(report["cases"]) == report["selected_count"]
    assert report["selected_count"] <= 10
    assert all(
        "candidate_route_not_ground_truth" not in row
        and row["review_status"] == "PENDING_HUMAN_REVIEW"
        and row["reviewer_route"] is None
        and row["reviewer_id"] is None
        for row in report["cases"]
    )


def test_independent_reviewer_can_correct_wrong_candidate_label(tmp_path, monkeypatch):
    """A wrong model label must remain in the scored population, not be discarded."""
    monkeypatch.setattr(approvals, "APPROVALS_PATH", tmp_path / "review.json")
    args = SimpleNamespace(dataset=None, reviewer="independent-reviewer")
    case_id = "route_产品咨询_001"

    assert approvals._apply_reviewed_labels(args, [f"{case_id}=general"]) == 0
    saved = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    record = saved["approvals"][case_id]
    assert record["candidate_route"] == "product_info"
    assert record["expected_route"] == "general"
    assert record["confirmed_by"] == "independent-reviewer"

    generated = build_cases(saved["approvals"])
    corrected = next(item for item in generated if item["case_id"] == case_id)
    assert corrected["expected_route"] == "general"
    assert corrected["annotation"]["provenance"] == "human_confirmed"

    # Malformed or unsupported labels must never silently alter the approval.
    assert approvals._apply_reviewed_labels(args, [f"{case_id}=not_a_real_route"]) == 2
    assert json.loads((tmp_path / "review.json").read_text(encoding="utf-8")) == saved


def test_stale_candidate_label_invalidates_old_approval():
    payload = {"case_id": "route_产品咨询_001", "expected_route": "billing"}
    from scripts.generate_agent_eval_cases import _apply_approvals

    _apply_approvals(
        [payload],
        {
            payload["case_id"]: {
                "confirmed_by": "independent-reviewer",
                "candidate_route": "product_info",
                "expected_route": "general",
            }
        },
    )
    assert payload["expected_route"] == "billing"
    assert "annotation" not in payload
