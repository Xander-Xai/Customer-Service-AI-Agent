"""Framework-free acceptance reference for a bounded customer-service tool loop.

This module is deliberately NOT wired into the online LangGraph request path.
It demonstrates server-owned tool policy without claiming durable execution,
provider cost caps, or real ERP refund guarantees.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Tool:
    name: str
    schema: Mapping[str, type]
    execute: Callable[[dict[str, Any]], Any]
    side_effect: bool = False
    high_risk: bool = False


@dataclass(frozen=True)
class Decision:
    kind: Literal["tool", "finish"]
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    response: str = ""
    # Only verified provider usage belongs here; scripted controllers pass zero.
    tokens: int | None = 0


@dataclass(frozen=True)
class Limits:
    model_steps: int = 8
    tool_calls: int = 5
    tokens: int = 5000
    read_attempts: int = 2

    def __post_init__(self) -> None:
        if any(
            type(x) is not int or x <= 0
            for x in (self.model_steps, self.tool_calls, self.tokens, self.read_attempts)
        ):
            raise ValueError("Limits must be positive integers")


@dataclass(frozen=True)
class Result:
    status: Literal["completed", "cancelled", "approval_required", "write_uncertain", "blocked"]
    reason: str
    response: str
    model_steps: int
    tool_calls: int
    tokens: int
    trace: tuple[str, ...]


def run_agent_loop(
    task: str,
    model: Callable[[str, list[dict[str, Any]]], Decision],
    tools: Mapping[str, Tool],
    *,
    limits: Limits = Limits(),
    cancelled: Callable[[], bool] = lambda: False,
    authorize_write: Callable[[Tool, dict[str, Any]], bool] = lambda _tool, _args: False,
    backoff: Callable[[int], None] = lambda _attempt: None,
) -> Result:
    """Run a minimal safe loop; never infer backend authorization from model text.

    Read operations may retry on transient errors. Writes never retry on a
    timeout because the downstream result is unknown. Cancellation is
    cooperative: an in-flight external call needs independent reconciliation.
    """
    history: list[dict[str, Any]] = []
    trace: list[str] = []
    steps = calls = tokens = 0

    def finish(
        status: Literal["completed", "cancelled", "approval_required", "write_uncertain", "blocked"],
        reason: str,
        response: str = "",
    ) -> Result:
        return Result(status, reason, response, steps, calls, tokens, tuple(trace))

    for _ in range(limits.model_steps):
        if cancelled():
            return finish("cancelled", "cancel_requested")
        choice = model(task, list(history))
        steps += 1
        if type(choice.tokens) is not int or choice.tokens < 0:
            return finish("blocked", "provider_usage_unknown")
        tokens += choice.tokens
        if tokens > limits.tokens:
            return finish("blocked", "token_budget_exceeded")
        if choice.kind == "finish":
            trace.append("finish")
            return finish("completed", "model_finished", choice.response)
        if choice.kind != "tool":
            return finish("blocked", "invalid_decision")
        if calls >= limits.tool_calls:
            return finish("blocked", "tool_budget_exceeded")
        spec = tools.get(choice.name)
        if spec is None:
            trace.append("tool_unknown")
            history.append({"tool": choice.name, "error": "unknown_tool"})
            continue
        if (
            set(choice.arguments) != set(spec.schema)
            or any(type(choice.arguments[key]) is not expected for key, expected in spec.schema.items())
        ):
            trace.append("invalid_args")
            history.append({"tool": spec.name, "error": "schema_validation_failed"})
            continue
        if spec.side_effect and not authorize_write(spec, choice.arguments):
            trace.append("write_rejected" if not spec.high_risk else "approval_pending")
            return finish(
                "approval_required" if spec.high_risk else "blocked", "server_policy_denied"
            )
        if cancelled():
            return finish("cancelled", "cancel_before_tool")
        calls += 1
        attempts = 1 if spec.side_effect else limits.read_attempts
        for attempt in range(1, attempts + 1):
            try:
                value = spec.execute(dict(choice.arguments))
                trace.append("tool_ok:" + spec.name)
                history.append({"tool": spec.name, "result": value})
                break
            except (TimeoutError, ConnectionError):
                if spec.side_effect:
                    trace.append("write_outcome_unknown:" + spec.name)
                    return finish("write_uncertain", "reconcile_external_state")
                if attempt >= attempts:
                    trace.append("read_failed:" + spec.name)
                    history.append({"tool": spec.name, "error": "transient_failure_exhausted"})
                else:
                    trace.append("read_retry:" + spec.name)
                    backoff(attempt)
            except Exception:
                trace.append("tool_failed:" + spec.name)
                history.append({"tool": spec.name, "error": "tool_execution_failed"})
                break
    return finish("blocked", "model_step_limit_exceeded")
