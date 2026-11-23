"""P0: failed durable side effects must never become a successful AgentRun."""

from __future__ import annotations

import asyncio

import pytest

from collaboration.modes import ConsultationMode, HierarchicalMode, ParallelMode, SequentialMode
from runtime.context import reset_run_context, set_run_context
from runtime.executor import _approved_write_failed, execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus
from runtime.thread_lock import InMemoryThreadLock
from tests.unit.runtime_helpers import (
    FakeRuntime,
    RecordingDispatcher,
    dispose,
    make_sqlite_session_factory,
    runtime_provider,
)
from tools.tool_registry import SideEffectExecutionError

pytestmark = pytest.mark.unit


class _FailingAgent:
    async def process_with_retry(self, state):
        raise SideEffectExecutionError("staging_refund")


class _GoodAgent:
    async def process_with_retry(self, state):
        return {"response": "normal read-only response"}


@pytest.mark.parametrize(
    ("mode", "agents", "context"),
    [
        (ParallelMode(), {"billing_agent": _FailingAgent()}, {"agent_list": ["billing_agent"]}),
        (
            ConsultationMode(),
            {"general_agent": _GoodAgent(), "billing_agent": _FailingAgent()},
            {"primary_agent": "general_agent", "consult_agents": ["billing_agent"]},
        ),
        (
            HierarchicalMode(),
            {"general_agent": _GoodAgent(), "billing_agent": _FailingAgent()},
            {"coordinator": "general_agent", "sub_tasks": {"billing_agent": "refund"}},
        ),
    ],
)
async def test_modes_propagate_write_failure(mode, agents, context):
    with pytest.raises(SideEffectExecutionError):
        await mode.execute(agents, {"customer_query": "refund"}, context)


async def test_durable_sequential_timeout_is_not_successful_fallback():
    class _Slow:
        async def process_with_retry(self, state):
            await asyncio.sleep(0.2)

    mode = SequentialMode()
    mode._SEQUENTIAL_TIMEOUT = 0.01
    tokens = set_run_context("run-test", "thread-test")
    try:
        with pytest.raises(asyncio.TimeoutError):
            await mode.execute(
                {"general_agent": _Slow()},
                {"customer_query": "unknown write timeout"},
                {"primary_agent": "general_agent"},
            )
    finally:
        reset_run_context(tokens)


def test_approval_execution_failure_detection():
    assert _approved_write_failed({"approval_results": [{"status": "error"}]})
    assert not _approved_write_failed({"approval_results": [{"status": "executed"}]})
    assert not _approved_write_failed({"approval_results": [{"status": "rejected"}]})
    assert not _approved_write_failed({"response": "ok"})


async def test_approved_write_error_marks_agent_run_failed_not_succeeded():
    factory, engine, path = make_sqlite_session_factory()
    service = RunService(repository=AgentRunRepository(session_factory=factory))
    run = service.create_run(query="refund", session_id="T-write-failed")
    runtime = FakeRuntime(
        lambda **kwargs: {
            "response": "not executed",
            "approval_results": [
                {"tool": "staging_refund", "status": "error", "error": "downstream"}
            ],
        }
    )
    dispatcher = RecordingDispatcher()
    try:
        status = await execute_run(
            run["id"],
            service=service,
            runtime_provider=runtime_provider(runtime),
            dispatcher=dispatcher,
            lock_manager=InMemoryThreadLock(),
            worker_id="write-error-worker",
        )
        assert status == RunStatus.FAILED.value
        record = service.get_run(run["id"])
        assert record["status"] == RunStatus.FAILED.value
        assert record["error_code"] == "ApprovedSideEffectFailed"
        assert record["error_type"] == "permanent"
        assert dispatcher.calls == []
    finally:
        dispose(engine, path)


async def test_approval_rejection_can_complete_without_write():
    factory, engine, path = make_sqlite_session_factory()
    service = RunService(repository=AgentRunRepository(session_factory=factory))
    run = service.create_run(query="refund", session_id="T-write-rejected")
    runtime = FakeRuntime(
        lambda **kwargs: {
            "response": "approval rejected",
            "approval_results": [{"tool": "staging_refund", "status": "rejected"}],
        }
    )
    try:
        status = await execute_run(
            run["id"],
            service=service,
            runtime_provider=runtime_provider(runtime),
            dispatcher=RecordingDispatcher(),
            lock_manager=InMemoryThreadLock(),
            worker_id="rejected-worker",
        )
        assert status == RunStatus.SUCCEEDED.value
    finally:
        dispose(engine, path)
