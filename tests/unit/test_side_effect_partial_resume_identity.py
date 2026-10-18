"""Partial-resume logical operation identity (Issue #12 Part A residual).

Scenario under test
-------------------
A node executes two *parameter-identical* side-effect writes belonging to two
**independent business intents**::

    A = {"order_id": "ORD-001", "amount": 100}
    A1 = refund(A)   # first legitimate refund
    A2 = refund(A)   # second legitimate refund, same arguments

First attempt::

    A1 -> occurrence=0 -> K0 -> SUCCEEDED
    A2 -> occurrence=1 -> K1   (proposed; worker dies before A2 reaches the ledger)

Recovery: the LLM re-proposes only ``A2``::

    A2 -> occurrence=0 -> K0   # the in-node counter restarted from 0

The danger: the ledger finds ``K0 == SUCCEEDED`` and silently returns ``A1``'s
stored result, so the legitimate ``A2`` is **lost**.

These tests drive the **real** ``agents.base_agent.BaseAgent._process_with_tools``
tool loop against an independent SQLite side-effect ledger (same interface the
production path uses; no network, no real provider). They assert *actual write
execution counts*, ledger identity, and returned results -- never just hash
equality. A worker crash mid-loop is modelled by raising a ``BaseException``
from the registry *before* the second write reaches the ledger, which is exactly
what a SIGKILL between two tool executions looks like.

Cases
-----
A  true replay of A1 -> deduped (must keep working)
B  A1 succeeded, resume proposes only A2 -> ambiguous without a durable id ->
   fail closed (never silently swallow, never blindly execute A2)
C  full replay [A1, A2] -> both stable, no extra execution
D  reordered shortened recovery [A1, B, A2] -> [B, A2] -> ambiguous A ->
   fail closed (no collision, no arbitrary result reuse, no new write)
E  explicit stable ``logical_operation_id`` survives occurrence reset
F  crash / redelivery / result-replay / fingerprint-conflict boundaries

Rule: a shortened same-content proposal set against multiple durable
occurrences is never resolved by guessing. Only a full proposal set preserves
the persisted occurrence order; otherwise the caller must supply an explicit,
durable ``logical_operation_id``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _tc(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


class _Crash(BaseException):
    """Simulated worker death (not an ``Exception``: must escape tool-loop handlers)."""


class _ScriptedLLM:
    """Deterministic LLM returning a fixed tool-call sequence per round."""

    def __init__(self, turns: list[list[dict[str, Any]] | None]) -> None:
        self._turns = list(turns)
        self._calls = 0

    async def async_invoke(self, messages, timeout=None, tools=None):
        idx = self._calls
        self._calls += 1
        tcs = self._turns[idx] if idx < len(self._turns) else None
        if tcs is None:
            return type("_R", (), {"content": "已为您处理。", "tool_calls": None})()
        return type("_R", (), {"content": "", "tool_calls": tcs})()

    async def async_invoke_stream(self, messages, timeout=None):
        yield "已为您处理。"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from db.models import Base
    from runtime import side_effects as se

    engine = create_engine(
        f"sqlite:///{tmp_path / 'partial_resume.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    store = se.SideEffectStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(se, "get_side_effect_store", lambda: store)

    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context("run-partial-resume", "thread-partial-resume", "task-partial-resume")
    try:
        yield store
    finally:
        reset_run_context(tokens)


def _make_agent(llm, calls: list[dict], *, crash_on_write: int | None = None):
    """Build an agent whose write handler records real invocations.

    ``crash_on_write`` (1-based) raises :class:`_Crash` from ``execute_raw``
    **before** the N-th side effect reaches the ledger, modelling a worker that
    dies between two tool executions.
    """
    from agents.general_agent import GeneralAgent
    from tools.tool_registry import ToolCachePolicy, ToolRegistry

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    async def _read(arguments: dict) -> str:
        return "order-ok"

    registry = ToolRegistry()
    registry.register(
        name="staging_refund",
        description="写操作（退款）",
        parameters={"type": "object", "properties": {}},
        handler=_write,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )
    registry.register(
        name="query_order",
        description="只读查询",
        parameters={"type": "object", "properties": {}},
        handler=_read,
        side_effect=False,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    if crash_on_write is not None:
        original = registry.execute_raw
        seen = {"n": 0}

        async def _crashing_execute_raw(name, arguments, *args, **kwargs):
            if registry.is_side_effect(name) is True:
                seen["n"] += 1
                if seen["n"] == crash_on_write:
                    raise _Crash(f"simulated worker death before write #{seen['n']}")
            return await original(name, arguments, *args, **kwargs)

        registry.execute_raw = _crashing_execute_raw  # type: ignore[assignment]

    agent = GeneralAgent(llm=llm)
    agent.set_tool_registry(registry)
    return agent


def _state() -> dict[str, Any]:
    return {
        "customer_query": "处理退款",
        "session_id": "partial-resume-session",
        "user_id": "partial-resume-user",
        "query_type": "billing",
        "agents_used": [],
    }


def _all_rows(store) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from db.models import ToolSideEffect

    session = store._session_factory()
    try:
        rows = session.execute(select(ToolSideEffect)).scalars().all()
        return [
            {
                "tool_name": r.tool_name,
                "operation_key": r.operation_key,
                "status": r.status,
                "result_reference": r.result_reference,
                "request_fingerprint": r.request_fingerprint,
            }
            for r in rows
        ]
    finally:
        session.close()


A = {"order_id": "ORD-001", "amount": 100}
B = {"order_id": "ORD-002", "amount": 200}


# ---------------------------------------------------------------------------
# Case A -- true replay of A1 must stay deduped
# ---------------------------------------------------------------------------


async def test_case_a_true_replay_of_a1_is_deduped(ledger):
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("a1", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")
    assert calls == [A], "first attempt executes A1 exactly once"

    # Whole-node replay: identical proposal, new (LLM-generated) tool_call_id.
    await _make_agent(
        _ScriptedLLM([[_tc("a2", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 1, "true replay of the same logical operation must not re-execute"
    succeeded = [
        row
        for row in _all_rows(ledger)
        if row["tool_name"] == "staging_refund" and row["status"] == "SUCCEEDED"
    ]
    assert len(succeeded) == 1, "replay must reuse the single stored ledger result"
    assert succeeded[0]["result_reference"] == "written:1"


# ---------------------------------------------------------------------------
# Case B -- A1 succeeded, resume proposes only A2 (the defect)
# ---------------------------------------------------------------------------


async def test_case_b_resume_only_a2_is_not_silently_swallowed(ledger):
    """A1 succeeds; the worker dies before A2. Recovery proposes only A2.

    The lone A is ambiguous: it may be a true replay of A1 or the first
    execution of A2. Content and reservation slots cannot distinguish those
    business intents, so the safe automatic outcome is fail-closed. The
    caller must not see a silent success, and the resolver must not blindly
    execute A2 either.
    """
    from runtime.operation_identity import OperationIdentityUnavailable

    calls: list[dict] = []

    # Attempt 1: two identical proposals; write #1 (A1) commits, then crash
    # before write #2 (A2) reaches the ledger.
    with pytest.raises(_Crash):
        await _make_agent(
            _ScriptedLLM([[_tc("x1", "staging_refund", A), _tc("x2", "staging_refund", A)], None]),
            calls,
            crash_on_write=2,
        )._process_with_tools(_state(), "你是客服。")
    assert calls == [A], "only A1 executed before the crash"

    # Recovery: LLM proposes only A2 (same arguments). The in-node counter
    # restarts from 0, so a naive implementation maps A2 -> K0 (A1's key).
    with pytest.raises(OperationIdentityUnavailable, match="ambiguous shortened replay"):
        await _make_agent(
            _ScriptedLLM([[_tc("y1", "staging_refund", A)], None]), calls
        )._process_with_tools(_state(), "你是客服。")

    assert calls == [A], "ambiguous shortened replay must not execute another refund"
    statuses = [row["status"] for row in _all_rows(ledger) if row["tool_name"] == "staging_refund"]
    assert sorted(statuses) == ["PENDING", "SUCCEEDED"], (
        "A1 stays SUCCEEDED and the interrupted A2 reservation stays PENDING; "
        "neither may be turned into a second success by guessing"
    )


# ---------------------------------------------------------------------------
# Case C -- full replay [A1, A2]
# ---------------------------------------------------------------------------


async def test_case_c_full_replay_of_two_identical_ops(ledger):
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("p1", "staging_refund", A), _tc("p2", "staging_refund", A)], None]),
        calls,
    )._process_with_tools(_state(), "你是客服。")
    assert calls.count(A) == 2, "two intentional identical writes in one node both execute"

    # Full replay: same two proposals, new tool_call_ids.
    await _make_agent(
        _ScriptedLLM([[_tc("q1", "staging_refund", A), _tc("q2", "staging_refund", A)], None]),
        calls,
    )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 2, "full replay must not add any execution"


# ---------------------------------------------------------------------------
# Case D -- reordered recovery
# ---------------------------------------------------------------------------


async def test_case_d_reordered_recovery_does_not_collide(ledger):
    """A shortened reordered replay must fail closed, not reuse an arbitrary result.

    Attempt 1 completes ``[A1, B, A2]``. Recovery proposes ``[B, A2]``: B is a
    full one-for-one replay, but A is shortened from two occurrences to one.
    Nothing identifies which A result the caller means (A1 or A2), so the
    resolver must refuse rather than return an arbitrary stored result. No new
    write may execute.
    """
    from runtime.operation_identity import OperationIdentityUnavailable

    calls: list[dict] = []

    # Attempt 1 completes: [A1, B, A2]
    await _make_agent(
        _ScriptedLLM(
            [
                [
                    _tc("r1", "staging_refund", A),
                    _tc("r2", "staging_refund", B),
                    _tc("r3", "staging_refund", A),
                ],
                None,
            ]
        ),
        calls,
    )._process_with_tools(_state(), "你是客服。")
    assert calls.count(A) == 2 and calls.count(B) == 1

    # Recovery reorders / drops the already-executed first occurrence: [B, A2]
    with pytest.raises(OperationIdentityUnavailable, match="ambiguous shortened replay"):
        await _make_agent(
            _ScriptedLLM(
                [
                    [
                        _tc("s1", "staging_refund", B),
                        _tc("s2", "staging_refund", A),
                    ],
                    None,
                ]
            ),
            calls,
        )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 2, "ambiguous reordered recovery must not execute another A"
    assert calls.count(B) == 1, "ambiguous reordered recovery must not execute another B"


async def test_shortened_replay_after_two_completed_identical_ops_fails_closed(ledger):
    """Two completed identical writes + a lone A -> no arbitrary result reuse.

    This covers the all-SUCCEEDED variant of the ambiguity (the original
    guard only looked at ``executed + unexecuted`` mixed history). Reusing
    either stored result would silently answer for the wrong business
    operation, so the resolver must fail closed.
    """
    from runtime.operation_identity import OperationIdentityUnavailable

    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("m1", "staging_refund", A), _tc("m2", "staging_refund", A)], None]),
        calls,
    )._process_with_tools(_state(), "你是客服。")
    assert calls.count(A) == 2

    with pytest.raises(OperationIdentityUnavailable, match="ambiguous shortened replay"):
        await _make_agent(
            _ScriptedLLM([[_tc("m3", "staging_refund", A)], None]), calls
        )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 2, "a shortened replay must not execute a third refund"
    statuses = [row["status"] for row in _all_rows(ledger) if row["tool_name"] == "staging_refund"]
    assert statuses == ["SUCCEEDED", "SUCCEEDED"], "no extra reservation or success may appear"


# ---------------------------------------------------------------------------
# Case E -- explicit, durable logical_operation_id
# ---------------------------------------------------------------------------


async def test_case_e_explicit_operation_id_survives_occurrence_reset(ledger):
    """Explicit business ids (op-001 / op-002) are independent of occurrence."""
    from runtime.context import get_current_run_id
    from runtime.side_effects import build_replay_stable_operation_key
    from tools.tool_registry import LogicalCall

    calls: list[dict] = []

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return f"written:{len(calls)}"

    from tools.tool_registry import ToolCachePolicy, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        name="staging_refund",
        description="写操作",
        parameters={"type": "object", "properties": {}},
        handler=_write,
        side_effect=True,
        cache_policy=ToolCachePolicy(enabled=False),
    )

    run_id = get_current_run_id()

    def _key(op_id: str) -> str:
        return build_replay_stable_operation_key(
            run_id=run_id,
            node_id="general#0",
            agent="general",
            tool_name="staging_refund",
            logical_operation_id=op_id,
            arguments=A,
        )

    # First attempt: both intents execute under distinct durable ids.
    for op_id in ("op-001", "op-002"):
        call = LogicalCall(
            node_id="general#0", agent="general", ordinal=0, logical_operation_id=op_id
        )
        await registry.execute_raw("staging_refund", A, logical_call=call)
    assert calls.count(A) == 2

    # Recovery contains only op-002 (occurrence would reset to 0); identity is
    # driven by the explicit id, so it must dedupe against op-002, not re-run.
    await registry.execute_raw(
        "staging_refund",
        A,
        logical_call=LogicalCall(
            node_id="general#0", agent="general", ordinal=0, logical_operation_id="op-002"
        ),
    )
    assert calls.count(A) == 2, "explicit op-002 identity must be stable across resume"
    assert _key("op-001") != _key("op-002")


# ---------------------------------------------------------------------------
# Case F -- crash / redelivery / conflict boundaries
# ---------------------------------------------------------------------------


async def test_case_f1_crash_before_first_claim_leaves_no_false_success(ledger):
    """Worker dies before *any* write reaches the ledger -> recovery executes it once."""
    calls: list[dict] = []

    with pytest.raises(_Crash):
        await _make_agent(
            _ScriptedLLM([[_tc("z1", "staging_refund", A)], None]),
            calls,
            crash_on_write=1,
        )._process_with_tools(_state(), "你是客服。")
    assert calls == []

    await _make_agent(
        _ScriptedLLM([[_tc("z2", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")
    assert calls == [A], "a write that never committed must execute exactly once on recovery"


async def test_case_f2_same_args_distinct_business_ids_both_execute(ledger):
    """Same arguments, different durable business ids -> two executions."""
    from runtime.context import get_current_run_id, get_current_thread_id
    from runtime.side_effects import build_replay_stable_operation_key

    calls: list[dict] = []

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return "ok"

    from runtime.side_effects import execute_idempotent_operation

    run_id = get_current_run_id()
    thread_id = get_current_thread_id()
    for op_id in ("biz-1", "biz-2"):
        key = build_replay_stable_operation_key(
            run_id=run_id,
            node_id="general#0",
            agent="general",
            tool_name="staging_refund",
            logical_operation_id=op_id,
            arguments=A,
        )
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=key,
            run_id=run_id,
            thread_id=thread_id,
            arguments=A,
            operation=lambda: _write(A),
            store=ledger,
        )
    assert len(calls) == 2


async def test_case_f3_same_business_id_different_args_conflicts(ledger):
    """Same logical identity with different arguments -> PermanentError (fail-closed)."""
    from runtime.context import get_current_run_id, get_current_thread_id
    from runtime.errors import PermanentError
    from runtime.side_effects import (
        build_replay_stable_operation_key,
        execute_idempotent_operation,
    )

    calls: list[dict] = []

    async def _write(arguments: dict) -> str:
        calls.append(dict(arguments))
        return "ok"

    run_id = get_current_run_id()
    thread_id = get_current_thread_id()
    key = build_replay_stable_operation_key(
        run_id=run_id,
        node_id="general#0",
        agent="general",
        tool_name="staging_refund",
        logical_operation_id="biz-conflict",
        arguments=A,
    )
    await execute_idempotent_operation(
        tool_name="staging_refund",
        operation_key=key,
        run_id=run_id,
        thread_id=thread_id,
        arguments=A,
        operation=lambda: _write(A),
        store=ledger,
    )
    with pytest.raises(PermanentError):
        await execute_idempotent_operation(
            tool_name="staging_refund",
            operation_key=key,
            run_id=run_id,
            thread_id=thread_id,
            arguments={"order_id": "ORD-001", "amount": 999},
            operation=lambda: _write({"order_id": "ORD-001", "amount": 999}),
            store=ledger,
        )
    assert len(calls) == 1, "fingerprint conflict must never execute the second write"


async def test_case_f4_repeated_redelivery_after_success_is_stable(ledger):
    """Terminal SUCCEEDED entries are reused across repeated redelivery."""
    calls: list[dict] = []

    await _make_agent(
        _ScriptedLLM([[_tc("f-a", "staging_refund", A)], None]), calls
    )._process_with_tools(_state(), "你是客服。")
    for i in range(3):
        await _make_agent(
            _ScriptedLLM([[_tc(f"f-{i}", "staging_refund", A)], None]), calls
        )._process_with_tools(_state(), "你是客服。")
    assert calls.count(A) == 1


# ---------------------------------------------------------------------------
# Safety regression: an A1 replay and an A2 resume are indistinguishable
# without a durable logical business id.
# ---------------------------------------------------------------------------


async def test_ambiguous_single_proposal_after_two_reservations_fails_closed(ledger):
    """Never execute pending A2 just because a lone A could be its retry.

    Attempt 1 plans A1 and A2, completes A1, then crashes. A recovery
    proposing only A could be a replay of A1 *or* the first execution of A2.
    The stored content and occurrence rows cannot distinguish these intents.
    The only safe automatic result is an explicit identity refusal.
    """
    from runtime.operation_identity import OperationIdentityUnavailable

    calls: list[dict] = []
    with pytest.raises(_Crash):
        await _make_agent(
            _ScriptedLLM([[_tc("i1", "staging_refund", A), _tc("i2", "staging_refund", A)], None]),
            calls,
            crash_on_write=2,
        )._process_with_tools(_state(), "你是客服。")
    assert calls == [A]

    with pytest.raises(OperationIdentityUnavailable, match="ambiguous shortened replay"):
        await _make_agent(
            _ScriptedLLM([[_tc("ambiguous", "staging_refund", A)], None]), calls
        )._process_with_tools(_state(), "你是客服。")

    assert calls == [A], "ambiguous replay must not execute a second refund"
    statuses = [row["status"] for row in _all_rows(ledger) if row["tool_name"] == "staging_refund"]
    assert sorted(statuses) == ["PENDING", "SUCCEEDED"]


async def test_full_replay_after_partial_execution_preserves_order(ledger):
    """Full [A1,A2] recovery reuses occ0 before executing reserved occ1."""
    calls: list[dict] = []
    with pytest.raises(_Crash):
        await _make_agent(
            _ScriptedLLM([[_tc("j1", "staging_refund", A), _tc("j2", "staging_refund", A)], None]),
            calls,
            crash_on_write=2,
        )._process_with_tools(_state(), "你是客服。")
    assert calls == [A]

    await _make_agent(
        _ScriptedLLM([[_tc("k1", "staging_refund", A), _tc("k2", "staging_refund", A)], None]),
        calls,
    )._process_with_tools(_state(), "你是客服。")

    assert calls.count(A) == 2, "full replay must execute the missing write only once"
    statuses = [row["status"] for row in _all_rows(ledger) if row["tool_name"] == "staging_refund"]
    assert statuses.count("SUCCEEDED") == 2


async def test_run_resolver_unavailable_never_falls_back_to_memory_counter(ledger, monkeypatch):
    """A failed durable-identity store must fail closed even if an LLM proposes A."""
    from runtime import side_effects
    from runtime.operation_identity import OperationIdentityUnavailable

    def broken_store():
        raise OSError("durable identity store unavailable")

    monkeypatch.setattr(side_effects, "get_side_effect_store", broken_store)
    calls: list[dict] = []
    with pytest.raises(OperationIdentityUnavailable, match="解析器不可用"):
        await _make_agent(
            _ScriptedLLM([[_tc("bad", "staging_refund", A)], None]), calls
        )._process_with_tools(_state(), "你是客服。")
    assert calls == []
