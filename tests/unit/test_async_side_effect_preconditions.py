"""Issue #130: async Run side-effect writes fail closed when idempotency
preconditions are missing.

The async Run path is at-least-once (worker crash / redelivery). A declared
``side_effect=True`` tool therefore may only run through the durable
``runtime.side_effects`` ledger. If *any* precondition needed to construct that
protection is absent, the write must be refused before the handler runs — it
must never silently degrade to a raw, non-idempotent handler call.

This file is deterministic and offline; it does not depend on a live LLM,
PostgreSQL or Redis:

1. valid ``run_id`` + missing ``tool_call_id`` -> handler 0 calls;
2. side-effect ledger module unavailable        -> handler 0 calls;
3. idempotency helper (operation-key) failure   -> handler 0 calls;
4. ledger persistence failure                   -> handler 0 calls;
5. a valid async side effect executes exactly once;
6. a retry with the same operation key does not repeat the side effect;
7. a read-only tool is unaffected by all of the above.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from unittest.mock import MagicMock

import pytest

from tests.unit.runtime_helpers import dispose, make_sqlite_session_factory
from tools.hitl_staging_tools import (
    register_hitl_staging_tools,
    reset_staging_ledger,
    staging_call_count,
)
from tools.tool_registry import ToolCachePolicy, ToolRegistry


@contextlib.contextmanager
def _run_context(run_id: str = "run-130", thread_id: str = "thread-130"):
    from runtime.context import reset_run_context, set_run_context

    tokens = set_run_context(run_id, thread_id, "task-130")
    try:
        yield
    finally:
        reset_run_context(tokens)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_hitl_staging_tools(reg)
    return reg


@pytest.fixture(autouse=True)
def _isolated_ledger():
    """Clean staging ledger + a SQLite-backed side-effect ledger per test."""
    reset_staging_ledger()

    import runtime.side_effects as se

    reset = getattr(se, "reset_side_effect_store_for_tests", None)

    factory, engine, path = make_sqlite_session_factory()
    store = se.SideEffectStore(session_factory=factory)
    original = se._default_store
    se._default_store = store

    @contextlib.contextmanager
    def _patch_module(module_name: str):
        """Make ``import module_name`` fail (simulate an unavailable dependency)."""
        saved = sys.modules.get(module_name, _SENTINEL)
        sys.modules[module_name] = None
        try:
            yield
        finally:
            if saved is _SENTINEL:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = saved

    try:
        yield {"store": store, "patch_module": _patch_module}
    finally:
        se._default_store = original
        if reset is not None:
            reset()
        reset_staging_ledger()
        dispose(engine, path)


_SENTINEL = object()


class TestAsyncSideEffectPreconditions:
    def test_missing_tool_call_id_fails_closed(self, registry):
        """Case 1: valid run_id but no stable tool_call_id -> refuse, 0 calls."""
        with _run_context():
            result = asyncio.run(
                registry.execute_raw("staging_refund", {"order_id": "A-1", "amount": 100})
            )
        assert "拒绝执行" in str(result)
        assert staging_call_count("A-1") == 0

    def test_ledger_module_unavailable_fails_closed(self, registry, _isolated_ledger):
        """Case 2: runtime.side_effects import failure -> refuse, 0 calls."""
        with _isolated_ledger["patch_module"]("runtime.side_effects"), _run_context():
            result = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "A-2", "amount": 100},
                    tool_call_id="tc-2",
                )
            )
        assert "拒绝执行" in str(result)
        assert staging_call_count("A-2") == 0

    def test_helper_operation_key_failure_fails_closed(self, registry, monkeypatch):
        """Case 3: operation-key helper raises -> refuse, 0 calls."""
        import runtime.side_effects as se

        def _boom(*args, **kwargs):
            raise RuntimeError("operation-key helper unavailable")

        monkeypatch.setattr(se, "build_tool_idempotency_key", _boom)
        with _run_context():
            result = asyncio.run(
                registry.execute_raw(
                    "staging_order_change",
                    {"order_id": "A-3", "new_status": "CANCELLED"},
                    tool_call_id="tc-3",
                )
            )
        assert "拒绝执行" in str(result)
        assert staging_call_count("A-3:CANCELLED") == 0

    def test_persistence_failure_keeps_handler_at_zero(self, registry, _isolated_ledger):
        """Persistence failure inside the ledger must not fall back to the handler."""
        import runtime.side_effects as se

        def _unavailable_session_factory():
            raise RuntimeError("ledger database unavailable")

        se._default_store = se.SideEffectStore(session_factory=_unavailable_session_factory)
        with _run_context(), pytest.raises(RuntimeError):
            asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "A-4", "amount": 100},
                    tool_call_id="tc-4",
                )
            )
        assert staging_call_count("A-4") == 0

    def test_valid_async_side_effect_executes_once(self, registry, _isolated_ledger):
        """Case 4: a governed async write runs exactly once."""
        with _run_context():
            result = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "B-1", "amount": 100},
                    tool_call_id="tc-b1",
                )
            )
        assert "拒绝执行" not in str(result)
        assert "staging-refund-ok" in str(result)
        assert staging_call_count("B-1") == 1
        row = _isolated_ledger["store"].get("staging_refund", "run-130:tc-b1")
        assert row is not None and row["status"] == "SUCCEEDED"

    def test_retry_does_not_repeat_side_effect(self, registry, _isolated_ledger):
        """Case 5: same run_id + tool_call_id -> ledger dedupes the redelivery."""
        with _run_context():
            first = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "B-2", "amount": 100},
                    tool_call_id="tc-b2",
                )
            )
            second = asyncio.run(
                registry.execute_raw(
                    "staging_refund",
                    {"order_id": "B-2", "amount": 100},
                    tool_call_id="tc-b2",
                )
            )
        assert "拒绝执行" not in str(first)
        assert "拒绝执行" not in str(second)
        assert staging_call_count("B-2") == 1

    def test_readonly_tool_unaffected(self, registry, _isolated_ledger):
        """Case 6: read-only tools are never blocked by the side-effect gate."""
        with _isolated_ledger["patch_module"]("runtime.side_effects"), _run_context():
            result = asyncio.run(
                registry.execute_raw(
                    "staging_readonly_lookup",
                    {"order_id": "C-1"},
                    tool_call_id="tc-c1",
                )
            )
        assert "staging-order-info" in str(result)
        assert "拒绝执行" not in str(result)


class TestReadOnlyWithoutRunContextUnaffected:
    def test_readonly_without_run_context_runs(self, registry, _isolated_ledger):
        result = asyncio.run(registry.execute_raw("staging_readonly_lookup", {"order_id": "C-2"}))
        assert "staging-order-info" in str(result)


class TestDeclaredSideEffectWithoutRunContextContract:
    def test_no_run_context_does_not_enter_async_precondition_path(self):
        """The no-run-context path is Issue #123's scope; this fix must not
        silently change it (it is asserted here only to pin the boundary)."""
        reg = ToolRegistry()
        reg.register(
            name="write_no_ctx",
            description="declared write",
            parameters={"type": "object", "properties": {}},
            handler=MagicMock(),
            cache_policy=ToolCachePolicy(enabled=False),
            side_effect=True,
            risk_level="high",
        )
        op, refusal = reg._idempotent_operation(
            reg._tools["write_no_ctx"], {}, "tc-x", stream_callback=None
        )
        assert op is None
        assert refusal is None  # realtime gate (#123) handles it, not this path
