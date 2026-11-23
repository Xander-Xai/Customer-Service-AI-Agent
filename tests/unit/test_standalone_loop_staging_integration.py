"""Cross-module safety acceptance: native Loop with actual local HITL staging tool.

This executes the repository's deterministic staging refund handler and its
ledger, not a real ERP/payment operation, and does not persist across processes.
The deliberate lost-response fault is injected AFTER the staging side effect.
"""

from __future__ import annotations

import asyncio

from core.standalone_agent_loop import Decision, Tool, run_agent_loop
from tools.hitl_staging_tools import (
    _staging_refund,
    reset_staging_ledger,
    staging_call_count,
    staging_ledger,
)


def test_staging_refund_response_lost_reconciles_without_duplicate_execution():
    reset_staging_ledger()
    operation = {"order_id": "OP-RECONCILE-1", "amount": 100.0}
    attempts: list[str] = []
    authoritative_checks: list[str] = []

    def call_staging_and_lose_response(arguments):
        attempts.append(arguments["order_id"])
        reply = asyncio.run(_staging_refund(arguments))
        assert reply.startswith("staging-refund-ok")
        raise TimeoutError("simulated network response lost after staging commit")

    def check_staging_ledger(_tool, arguments):
        authoritative_checks.append(arguments["order_id"])
        entries = staging_ledger()
        entry = entries.get(arguments["order_id"], {})
        return (
            entry.get("calls") == 1
            and entry.get("last") == arguments
        )

    requests = iter(
        [
            Decision("tool", "staging_refund", operation),
            Decision("finish", response="confirmed in staging ledger"),
        ]
    )
    result = run_agent_loop(
        "refund request",
        lambda _task, _history: next(requests),
        {
            "staging_refund": Tool(
                "staging_refund",
                {"order_id": str, "amount": float},
                call_staging_and_lose_response,
                side_effect=True,
                high_risk=True,
            )
        },
        authorize_write=lambda _tool, _args: True,  # Simulated prior approval.
        reconcile_write=check_staging_ledger,
    )
    assert result.status == "completed"
    assert result.tool_calls == 1
    assert attempts == authoritative_checks == [operation["order_id"]]
    assert staging_call_count(operation["order_id"]) == 1
    assert "write_applied_verified:staging_refund" in result.trace


def test_staging_refund_status_unknown_is_not_retried_or_acknowledged():
    reset_staging_ledger()
    operation = {"order_id": "OP-RECONCILE-UNKNOWN", "amount": 20.0}
    attempts: list[str] = []

    def call_and_lose_response(arguments):
        attempts.append(arguments["order_id"])
        asyncio.run(_staging_refund(arguments))
        raise TimeoutError("staging result ambiguous to caller")

    result = run_agent_loop(
        "refund request",
        lambda _task, _history: Decision("tool", "staging_refund", operation),
        {
            "staging_refund": Tool(
                "staging_refund",
                {"order_id": str, "amount": float},
                call_and_lose_response,
                side_effect=True,
                high_risk=True,
            )
        },
        authorize_write=lambda _tool, _args: True,
        reconcile_write=lambda _tool, _args: None,  # Reconciliation unavailable.
    )
    assert result.status == "write_uncertain"
    assert result.reason == "reconcile_external_state"
    assert attempts == [operation["order_id"]]
    assert staging_call_count(operation["order_id"]) == 1
    assert not result.response
