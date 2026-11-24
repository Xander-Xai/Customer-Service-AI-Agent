"""Acceptance paths for the framework-free Agent Loop learning baseline."""

from core.standalone_agent_loop import Decision, Limits, Tool, run_agent_loop


def scripted(*choices):
    queue = iter(choices)
    return lambda _task, _history: next(queue)


def test_01_no_tool_answer():
    result = run_agent_loop("greet", scripted(Decision("finish", response="hello")), {})
    assert (result.status, result.tool_calls, result.response) == ("completed", 0, "hello")


def test_02_single_read_tool():
    result = run_agent_loop(
        "lookup",
        scripted(Decision("tool", "order", {"id": 7}), Decision("finish", response="found")),
        {"order": Tool("order", {"id": int}, lambda args: {"id": args["id"]})},
    )
    assert result.status == "completed" and result.tool_calls == 1
    assert "tool_ok:order" in result.trace


def test_03_dependent_tools_use_previous_observation():
    def model(_task, history):
        if not history:
            return Decision("tool", "order", {"id": 7})
        if len(history) == 1:
            shipment_id = history[0]["result"]["shipment_id"]
            return Decision("tool", "shipping", {"shipment_id": shipment_id})
        return Decision("finish", response=history[1]["result"])

    tools = {
        "order": Tool("order", {"id": int}, lambda _args: {"shipment_id": 22}),
        "shipping": Tool("shipping", {"shipment_id": int}, lambda _args: "delivered"),
    }
    result = run_agent_loop("track", model, tools)
    assert (result.status, result.response, result.tool_calls) == ("completed", "delivered", 2)


def test_04_invalid_arguments_allow_safe_correction():
    seen = []

    def decide(_task, history):
        seen.extend(history[-1:])
        if not history:
            return Decision("tool", "order", {"id": "7"})
        if len(history) == 1:
            return Decision("tool", "order", {"id": 7})
        return Decision("finish", response="ok")

    result = run_agent_loop(
        "order", decide, {"order": Tool("order", {"id": int}, lambda _args: "ok")}
    )
    assert result.status == "completed" and result.tool_calls == 1
    assert seen[0]["error"] == "schema_validation_failed"


def test_05_read_retry_but_no_blind_write_retry():
    attempts = []

    def read(_args):
        attempts.append("read")
        if len(attempts) == 1:
            raise TimeoutError()
        return "ok"

    result = run_agent_loop(
        "read",
        scripted(Decision("tool", "read", {}), Decision("finish", response="ok")),
        {"read": Tool("read", {}, read)},
    )
    assert result.status == "completed" and attempts == ["read", "read"]
    writes = []

    def write(_args):
        writes.append("write")
        raise TimeoutError()

    result = run_agent_loop(
        "refund",
        scripted(Decision("tool", "refund", {})),
        {"refund": Tool("refund", {}, write, side_effect=True)},
        authorize_write=lambda _tool, _args: True,
    )
    assert result.status == "write_uncertain"
    assert result.reason == "reconcile_external_state" and len(writes) == 1


def test_06_loop_and_token_budgets_stop():
    def repeating(_task, _history):
        return Decision("tool", "read", {}, tokens=1)

    tools = {"read": Tool("read", {}, lambda _args: "same")}
    step = run_agent_loop("loop", repeating, tools, limits=Limits(model_steps=3, tool_calls=5))
    token = run_agent_loop(
        "loop", repeating, tools, limits=Limits(model_steps=9, tool_calls=5, tokens=2)
    )
    assert (step.status, step.reason) == ("blocked", "model_step_limit_exceeded")
    assert (token.status, token.reason) == ("blocked", "token_budget_exceeded")


def test_07_cancellation_stops_next_step():
    count = []

    def read(_args):
        count.append(1)
        return "found"

    model = scripted(Decision("tool", "read", {}), Decision("tool", "read", {}))
    result = run_agent_loop(
        "cancel", model, {"read": Tool("read", {}, read)}, cancelled=lambda: bool(count)
    )
    assert result.status == "cancelled" and len(count) == 1


def test_08_high_risk_write_requires_server_approval():
    was_called = []
    spec = Tool(
        "refund",
        {"id": int},
        lambda _args: was_called.append(True),
        side_effect=True,
        high_risk=True,
    )
    result = run_agent_loop("refund", scripted(Decision("tool", "refund", {"id": 7})), {"refund": spec})
    assert (result.status, result.reason) == ("approval_required", "server_policy_denied")
    assert was_called == []


def test_09_authoritative_write_reconciliation_does_not_repeat_effect():
    executed = []
    checked = []

    def write(args):
        executed.append(args["operation_id"])
        raise TimeoutError("response lost after downstream commit")

    def verify(_tool, args):
        checked.append(args["operation_id"])
        return True  # Simulated authoritative external ledger reports applied.

    result = run_agent_loop(
        "refund",
        scripted(
            Decision("tool", "refund", {"operation_id": "op-1"}),
            Decision("finish", response="verified"),
        ),
        {"refund": Tool("refund", {"operation_id": str}, write, side_effect=True)},
        authorize_write=lambda _tool, _args: True,
        reconcile_write=verify,
    )
    assert result.status == "completed" and result.response == "verified"
    assert executed == checked == ["op-1"]
    assert "write_applied_verified:refund" in result.trace


def test_10_reconciliation_unknown_stays_uncertain_without_retry():
    executed = []

    def write(_args):
        executed.append("one")
        raise ConnectionError("ambiguous transport failure")

    def unavailable(_tool, _args):
        raise TimeoutError("downstream status lookup unavailable")

    result = run_agent_loop(
        "refund",
        scripted(Decision("tool", "refund", {})),
        {"refund": Tool("refund", {}, write, side_effect=True)},
        authorize_write=lambda _tool, _args: True,
        reconcile_write=unavailable,
    )
    assert result.status == "write_uncertain"
    assert result.reason == "reconcile_external_state"
    assert executed == ["one"]


def test_11_untyped_model_response_is_fail_closed():
    result = run_agent_loop(
        "malformed provider output", lambda _task, _history: {"kind": "finish"}, {}
    )
    assert (result.status, result.reason) == ("blocked", "invalid_model_response")


def test_12_malformed_tool_payload_does_not_crash_and_is_not_executed():
    executed = []
    result = run_agent_loop(
        "malformed tool response",
        scripted(Decision("tool", name="read", arguments=["not-a-json-object"])),
        {"read": Tool("read", {}, lambda _args: executed.append("called"))},
    )
    assert (result.status, result.reason) == ("blocked", "invalid_tool_decision")
    assert executed == []


def test_13_high_risk_tool_cannot_hide_behind_read_only_risk_flag():
    try:
        Tool("refund", {}, lambda _args: "irrelevant", high_risk=True, side_effect=False)
    except ValueError as exc:
        assert "side_effect" in str(exc)
    else:
        raise AssertionError("Conflicting tool risk metadata must be rejected")


def test_duplicate_write_after_verified_success_is_blocked():
    writes = []
    choice = Decision("tool", "refund", {"id": 7})
    result = run_agent_loop(
        "refund",
        scripted(choice, choice),
        {"refund": Tool("refund", {"id": int}, lambda args: writes.append(args), side_effect=True)},
        authorize_write=lambda _tool, _args: True,
    )
    assert (result.status, result.reason) == ("blocked", "duplicate_write_attempt")
    assert writes == [{"id": 7}]


def test_unknown_write_exception_cannot_be_reclassified_as_success_by_model():
    writes = []

    def write(args):
        writes.append(args)
        raise RuntimeError("unknown downstream commit status")

    result = run_agent_loop(
        "refund",
        scripted(Decision("tool", "refund", {}), Decision("finish", response="success")),
        {"refund": Tool("refund", {}, write, side_effect=True)},
        authorize_write=lambda _tool, _args: True,
    )
    assert (result.status, result.reason) == ("write_uncertain", "reconcile_external_state")
    assert writes == [{}]
    assert not result.response


def test_truthy_non_boolean_authorization_does_not_grant_write():
    writes = []
    result = run_agent_loop(
        "refund",
        scripted(Decision("tool", "refund", {})),
        {"refund": Tool("refund", {}, lambda args: writes.append(args), side_effect=True)},
        authorize_write=lambda _tool, _args: "approved",
    )
    assert (result.status, result.reason) == ("blocked", "server_policy_denied")
    assert writes == []
