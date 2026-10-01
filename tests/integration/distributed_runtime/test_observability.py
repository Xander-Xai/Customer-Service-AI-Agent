"""分布式 runtime 可观测性测试：trace 关联 + metrics + 隐私（不泄露 query）。

覆盖：
  - HTTP trace_id -> worker task -> graph 可关联；
  - 任务要求的 Prometheus 指标已注册；
  - metric label 与 run event payload 不包含用户 query（防高基数/隐私）。
"""

from __future__ import annotations

import json

import pytest

from runtime.event_stream import InMemoryRunEventStream, reset_run_event_stream_for_tests
from runtime.executor import execute_run
from runtime.statuses import RunStatus

from .harness import make_lock, make_run_env, noop_dispatcher

REQUIRED_METRICS = [
    "agent_run_total",
    "agent_run_retry_total",
    "agent_run_dead_total",
    "agent_run_duration_seconds",
    "agent_run_queue_wait_seconds",
    "agent_worker_active",
    "agent_worker_task_total",
    "thread_lock_wait_seconds",
    "checkpoint_operation_seconds",
    "run_event_lag_seconds",
]


def _provider(runtime):
    async def _p():
        return runtime

    return _p


@pytest.mark.integration
def test_required_runtime_metrics_registered():
    from core import monitoring

    for name in REQUIRED_METRICS:
        assert getattr(monitoring, name, None) is not None, f"missing metric: {name}"


@pytest.mark.integration
def test_metric_labels_do_not_include_query():
    from core import monitoring

    labelnames = getattr(monitoring.agent_run_total, "_labelnames", ())
    assert "query" not in labelnames
    assert "run_id" not in labelnames  # run_id 高基数，不应做 label


@pytest.mark.integration
async def test_trace_id_propagates_to_worker_and_graph():
    service, _sf = make_run_env()
    run = service.create_run(
        query="q", session_id="s", thread_id="T-trace", trace_id="trace-abc-123"
    )
    service.mark_queued(run["id"])

    captured: dict[str, str] = {}

    class RT:
        async def run(self, *, thread_id, query, user_id=None):
            from core.logger import get_trace_id

            captured["trace_id"] = get_trace_id()
            return {"response": "ok"}

    status = await execute_run(
        run["id"],
        service=service,
        runtime_provider=_provider(RT()),
        lock_manager=make_lock(),
        dispatcher=noop_dispatcher,
    )
    assert status == RunStatus.SUCCEEDED.value
    assert captured["trace_id"] == "trace-abc-123"


@pytest.mark.integration
async def test_worker_events_do_not_leak_query():
    service, _sf = make_run_env()
    stream = InMemoryRunEventStream()
    reset_run_event_stream_for_tests(stream)
    try:
        run = service.create_run(
            query="TOP_SECRET_QUERY", session_id="s", thread_id="T-priv"
        )
        service.mark_queued(run["id"])

        class RT:
            async def run(self, *, thread_id, query, user_id=None):
                return {"response": "ok"}

        await execute_run(
            run["id"],
            service=service,
            runtime_provider=_provider(RT()),
            lock_manager=make_lock(),
            dispatcher=noop_dispatcher,
        )
        entries = await stream.read(run["id"], after_id="0-0", count=50)
        blob = json.dumps([e.fields for e in entries], ensure_ascii=False)
        assert "TOP_SECRET_QUERY" not in blob
    finally:
        reset_run_event_stream_for_tests(None)


@pytest.mark.integration
async def test_checkpoint_operation_metric_is_recorded():
    from typing import TypedDict

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import StateGraph

    from core.checkpointer import timed_saver_class

    try:
        from prometheus_client import REGISTRY
    except ImportError:  # pragma: no cover
        pytest.skip("prometheus_client 未安装")

    class S(TypedDict, total=False):
        value: int

    saver = timed_saver_class(MemorySaver)()
    graph = StateGraph(S)
    graph.add_node("inc", lambda s: {"value": s.get("value", 0) + 1})
    graph.set_entry_point("inc")
    graph.set_finish_point("inc")
    app = graph.compile(checkpointer=saver)
    await app.ainvoke({"value": 0}, config={"configurable": {"thread_id": "m1"}})

    count = REGISTRY.get_sample_value("checkpoint_operation_seconds_count")
    assert count is not None and count >= 1
