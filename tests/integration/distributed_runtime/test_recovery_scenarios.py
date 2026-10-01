"""分布式 LangGraph Runtime 故障恢复场景测试。

覆盖：
  1. worker crash -> checkpoint 恢复
  2. API restart 不丢 run
  3. duplicate delivery 不重复执行
  4. same thread 串行 / different thread 并行
  5. side-effect tool 崩溃重放不重复执行
  6. SSE disconnect + Last-Event-ID reconnect

默认用进程内 lock/stream + MemorySaver；真实跨进程（Postgres checkpoint /
Redis stream）见 gated 集成测试。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from api.app import create_app
from api.routes.runs import _run_event_sse_generator
from runtime.event_publisher import create_run_event_publisher
from runtime.event_stream import InMemoryRunEventStream, reset_run_event_stream_for_tests
from runtime.executor import execute_run
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus
from runtime.thread_lock import NullThreadLock

from .harness import (
    BlockingRuntime,
    CheckpointedRuntime,
    FakeRefundTool,
    make_lock,
    make_run_env,
    noop_dispatcher,
    queued,
)


@pytest.fixture(autouse=True)
def _in_memory_event_stream():
    stream = InMemoryRunEventStream()
    reset_run_event_stream_for_tests(stream)
    yield stream
    reset_run_event_stream_for_tests(None)


def _provider(runtime):
    async def _p():
        return runtime

    return _p


def _make_app(service: RunService):
    sm = MagicMock()
    sm.validate_session_token = MagicMock(return_value=True)
    sm.generate_session_token = MagicMock(return_value="tok")
    app = create_app(None, session_manager=sm)
    app.state.session_manager = sm
    app.state.dev_mode = True
    app.state.run_service = service
    app.state._current_jwt_payload = None
    return app


# ---------------------------------------------------------------------------
# 场景 1：Worker crash -> checkpoint 恢复
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_worker_crash_resumes_from_checkpoint():
    service, _sf = make_run_env()
    checkpointer = MemorySaver()
    node_log: list[str] = []
    crash_flag = {"crash": True}
    runtime = CheckpointedRuntime(
        checkpointer, node_log=node_log, crash_flag=crash_flag
    )
    rid = queued(service, thread="T-crash")

    first = await execute_run(
        rid,
        service=service,
        runtime_provider=_provider(runtime),
        lock_manager=make_lock(),
        dispatcher=noop_dispatcher,
    )
    assert first == RunStatus.QUEUED.value  # 崩溃 -> retry 排队
    assert service.get_run(rid)["attempt"] == 1

    # 新 worker（新 graph 实例，共享 checkpointer）resume
    second = await execute_run(
        rid,
        service=service,
        runtime_provider=_provider(runtime),
        lock_manager=make_lock(),
        dispatcher=noop_dispatcher,
    )
    assert second == RunStatus.SUCCEEDED.value
    # 已完成节点不重跑：node1 只执行一次
    assert node_log.count("node1") == 1
    assert node_log.count("node3") == 1

    # checkpoint 可被全新 graph 读取（持久化证明）
    state = await checkpointer.aget_tuple({"configurable": {"thread_id": "T-crash"}})
    assert state is not None


# ---------------------------------------------------------------------------
# 场景 2：API restart 不丢 run
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_restart_does_not_lose_run():
    import httpx

    service, session_factory = make_run_env()
    rid = queued(service, thread="T-api")
    runtime = BlockingRuntime()
    worker = asyncio.create_task(
        execute_run(
            rid,
            service=service,
            runtime_provider=_provider(runtime),
            lock_manager=make_lock(),
            dispatcher=noop_dispatcher,
        )
    )
    await runtime.wait_entered("T-api")

    # "停止 API"：worker 独立继续执行
    runtime.release.set()
    await asyncio.wait_for(worker, timeout=5)

    # "重启 API"：全新 app/service 实例，仅共享数据库
    new_service = RunService(AgentRunRepository(session_factory))
    app = _make_app(new_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/runs/{rid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == RunStatus.SUCCEEDED.value
    assert resp.json()["result"]["response"] == "ok"


# ---------------------------------------------------------------------------
# 场景 3：Duplicate delivery 不重复执行
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_delivery_does_not_repeat_execution():
    service, _sf = make_run_env()
    rid = queued(service, thread="T-dup")
    runtime = BlockingRuntime()

    # 使用 NullThreadLock：直接考验 RUNNING + lease 的投递幂等
    first = asyncio.create_task(
        execute_run(
            rid,
            service=service,
            runtime_provider=_provider(runtime),
            lock_manager=NullThreadLock(),
            dispatcher=noop_dispatcher,
            worker_id="worker-1",
        )
    )
    await runtime.wait_entered("T-dup")

    # 第二次投递（不同 worker）：lease 有效，不得执行
    second = await execute_run(
        rid,
        service=service,
        runtime_provider=_provider(runtime),
        lock_manager=NullThreadLock(),
        dispatcher=noop_dispatcher,
        worker_id="worker-2",
    )
    assert second == RunStatus.RUNNING.value
    assert runtime.calls == 1

    runtime.release.set()
    assert await asyncio.wait_for(first, timeout=5) == RunStatus.SUCCEEDED.value
    assert runtime.calls == 1


# ---------------------------------------------------------------------------
# 场景 4：Same thread 串行 / Different thread 并行
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_same_thread_serial_different_thread_parallel():
    service, _sf = make_run_env()
    lock = make_lock()
    runtime = BlockingRuntime()

    rid_a1 = queued(service, thread="A", session="sa1")
    rid_a2 = queued(service, thread="A", session="sa2")
    rid_b = queued(service, thread="B", session="sb")

    task_a1 = asyncio.create_task(
        execute_run(
            rid_a1, service=service, runtime_provider=_provider(runtime),
            lock_manager=lock, dispatcher=noop_dispatcher,
        )
    )
    await runtime.wait_entered("A")

    # 同 thread 第二个 run：被锁占用 -> 保持 QUEUED（不并发修改 graph）
    status_a2 = await execute_run(
        rid_a2, service=service, runtime_provider=_provider(runtime),
        lock_manager=lock, dispatcher=noop_dispatcher,
    )
    assert status_a2 == RunStatus.QUEUED.value
    assert runtime.calls == 1  # A2 未执行

    # 不同 thread B：无需等待 A，可并行进入
    task_b = asyncio.create_task(
        execute_run(
            rid_b, service=service, runtime_provider=_provider(runtime),
            lock_manager=lock, dispatcher=noop_dispatcher,
        )
    )
    await runtime.wait_entered("B")
    assert runtime.calls == 2
    assert runtime.max_concurrent == 2

    runtime.release.set()
    results = await asyncio.wait_for(
        asyncio.gather(task_a1, task_b), timeout=5
    )
    assert results == [RunStatus.SUCCEEDED.value, RunStatus.SUCCEEDED.value]


# ---------------------------------------------------------------------------
# 场景 5：Side-effect replay 不重复执行
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_side_effect_not_repeated_after_crash_replay():
    service, session_factory = make_run_env()
    checkpointer = MemorySaver()
    node_log: list[str] = []
    crash_flag = {"crash": True}
    refund = FakeRefundTool(session_factory)
    runtime = CheckpointedRuntime(
        checkpointer,
        node_log=node_log,
        crash_flag=crash_flag,
        refund_registry=refund.registry,
    )
    rid = queued(service, thread="T-refund", max_attempts=3)

    first = await execute_run(
        rid, service=service, runtime_provider=_provider(runtime),
        lock_manager=make_lock(), dispatcher=noop_dispatcher,
    )
    assert first == RunStatus.QUEUED.value
    assert refund.calls == 1  # 第一次已退款

    second = await execute_run(
        rid, service=service, runtime_provider=_provider(runtime),
        lock_manager=make_lock(), dispatcher=noop_dispatcher,
    )
    assert second == RunStatus.SUCCEEDED.value
    assert refund.calls == 1  # 重放不重复退款


# ---------------------------------------------------------------------------
# 场景 6：SSE disconnect + Last-Event-ID reconnect
# ---------------------------------------------------------------------------


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _collect(gen, limit: int):
    frames = []
    async for frame in gen:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


def _parse(frames):
    return [
        json.loads([ln for ln in f.splitlines() if ln.startswith("data: ")][0][6:])
        for f in frames
    ]


@pytest.mark.integration
async def test_sse_reconnect_with_last_event_id(_in_memory_event_stream):
    service, _sf = make_run_env()
    rid = queued(service, thread="T-sse")
    publisher = create_run_event_publisher(rid, "T-sse")
    for i in range(4):
        await publisher.publish({"type": "chunk", "content": str(i)})
    await publisher.publish({"type": "run_completed", "status": "SUCCEEDED"})

    # 连接 1：接收 2 个事件后 disconnect
    first = await _collect(
        _run_event_sse_generator(_FakeRequest(), service, rid, None), limit=2
    )
    assert [d["sequence"] for d in _parse(first)] == [1, 2]
    last_id = [ln for ln in first[-1].splitlines() if ln.startswith("id: ")][0][4:]

    # 连接 2：Last-Event-ID 续读
    second = await _collect(
        _run_event_sse_generator(_FakeRequest(), service, rid, last_id), limit=10
    )
    parsed2 = _parse(second)
    assert [d["sequence"] for d in parsed2] == [3, 4, 5]

    # 合并完整且无重复/倒序
    all_seqs = [d["sequence"] for d in _parse(first) + parsed2]
    assert all_seqs == [1, 2, 3, 4, 5]
    assert parsed2[-1]["type"] == "run_completed"
