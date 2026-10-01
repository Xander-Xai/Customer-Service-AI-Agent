"""跨进程 Agent 事件流测试（RunEvent / Redis Streams 抽象 / SSE）。

覆盖任务指定用例：
  event_order / event_resume_from_last_id / event_stream_ttl / event_trim /
  unauthorized_run_stream / worker_to_api_cross_process / client_disconnect /
  run_completed_after_stream_disconnect / final_result_available_after_event_expired

本地单元测试使用 InMemoryRunEventStream（语义对齐 Redis Streams）；
真实 Redis Streams 见 tests/integration/test_run_events_redis.py。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.config as config
from api.app import create_app
from api.routes.runs import _run_event_sse_generator
from db.models import Base
from runtime.event_publisher import create_run_event_publisher
from runtime.event_stream import InMemoryRunEventStream, reset_run_event_stream_for_tests
from runtime.events import RunEvent, RunEventType
from runtime.repository import AgentRunRepository
from runtime.run_service import RunService
from runtime.statuses import RunStatus


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    service = RunService(AgentRunRepository(session_factory))
    stream = InMemoryRunEventStream()
    reset_run_event_stream_for_tests(stream)

    sm = MagicMock()
    sm.validate_session_token = MagicMock(return_value=True)
    sm.generate_session_token = MagicMock(return_value="tok")
    app = create_app(None, session_manager=sm)
    app.state.session_manager = sm
    app.state.dev_mode = True
    app.state.run_service = service
    app.state._current_jwt_payload = None
    yield service, stream, app, session_factory
    reset_run_event_stream_for_tests(None)


def _queued(service, *, thread="T1", session="s1"):
    run = service.create_run(query="q", session_id=session, thread_id=thread)
    service.mark_queued(run["id"])
    return run["id"]


async def _collect(gen, limit=50):
    frames = []
    async for frame in gen:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


def _parse_frames(frames):
    parsed = []
    for frame in frames:
        data_line = [ln for ln in frame.splitlines() if ln.startswith("data: ")][0]
        parsed.append(json.loads(data_line[len("data: "):]))
    return parsed


class _FakeRequest:
    def __init__(self, disconnect_after: int = 0):
        self._calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after


# ---------------------------------------------------------------------------
# 1. 事件顺序 / 续读
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_event_order(ctx):
    _service, stream, _app, _sf = ctx
    publisher = create_run_event_publisher("run-1", "th-1")
    for i in range(3):
        await publisher.publish({"type": "chunk", "content": f"c{i}"})

    entries = await stream.read("run-1", after_id="0-0", count=10)
    events = [RunEvent.from_fields(e.fields, e.event_id) for e in entries]
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.payload.get("content") for e in events] == ["c0", "c1", "c2"]


@pytest.mark.unit
async def test_event_resume_from_last_id(ctx):
    _service, stream, _app, _sf = ctx
    publisher = create_run_event_publisher("run-2", "th-2")
    ids = []
    for i in range(4):
        ids.append(await publisher.publish({"type": "status", "content": str(i)}))

    resumed = await stream.read("run-2", after_id=ids[1], count=10)
    events = [RunEvent.from_fields(e.fields, e.event_id) for e in resumed]
    # 只返回 last_id 之后的事件
    assert [e.sequence for e in events] == [3, 4]


# ---------------------------------------------------------------------------
# 2. TTL / trim
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_event_stream_ttl(ctx):
    _service, _stream, _app, _sf = ctx
    now = {"t": 1000.0}
    stream = InMemoryRunEventStream(clock=lambda: now["t"])
    from runtime.event_publisher import StreamRunEventPublisher

    publisher = StreamRunEventPublisher(
        stream, "run-ttl", "th", maxlen=10, ttl_seconds=5, max_bytes=1024
    )
    await publisher.publish({"type": "status", "content": "x"})
    assert await stream.length("run-ttl") == 1

    now["t"] += 10  # 超过 TTL
    assert await stream.length("run-ttl") == 0
    assert await stream.read("run-ttl", after_id="0-0", count=10) == []


@pytest.mark.unit
async def test_event_trim(ctx):
    _service, _stream, _app, _sf = ctx
    stream = InMemoryRunEventStream()
    from runtime.event_publisher import StreamRunEventPublisher

    publisher = StreamRunEventPublisher(
        stream, "run-trim", "th", maxlen=3, ttl_seconds=3600, max_bytes=1024
    )
    for i in range(6):
        await publisher.publish({"type": "chunk", "content": str(i)})

    assert await stream.length("run-trim") == 3
    entries = await stream.read("run-trim", after_id="0-0", count=10)
    contents = [RunEvent.from_fields(e.fields, e.event_id).payload["content"] for e in entries]
    assert contents == ["3", "4", "5"]  # 最早的被裁剪


@pytest.mark.unit
async def test_event_sensitive_fields_filtered_and_size_capped(ctx):
    _service, stream, _app, _sf = ctx
    publisher = create_run_event_publisher("run-sec", "th")
    await publisher.publish(
        {"type": "tool_call", "content": "x", "api_key": "SECRET", "nested": {"password": "p"}}
    )
    entry = (await stream.read("run-sec", after_id="0-0", count=10))[0]
    payload = entry.fields["payload"]
    assert "api_key" not in payload
    assert "password" not in payload

    # 大小上限：超长 content 被截断（用较小 max_bytes 直接构造 publisher）
    from runtime.event_publisher import StreamRunEventPublisher

    small = StreamRunEventPublisher(
        stream, "run-big", "th", maxlen=10, ttl_seconds=3600, max_bytes=100
    )
    await small.publish({"type": "chunk", "content": "a" * 5000})
    entry2 = (await stream.read("run-big", after_id="0-0", count=10))[0]
    assert "truncated" in entry2.fields["payload"]


# ---------------------------------------------------------------------------
# 3. SSE：顺序 / 续读 / 终态 / 断线
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_sse_emits_ordered_events(ctx):
    service, stream, _app, _sf = ctx
    rid = _queued(service)
    publisher = create_run_event_publisher(rid, "T1")
    await publisher.publish({"type": "status", "content": "a"})
    await publisher.publish({"type": "chunk", "content": "b"})
    await publisher.publish({"type": "run_completed", "status": "SUCCEEDED"})

    frames = await _collect(
        _run_event_sse_generator(_FakeRequest(disconnect_after=999), service, rid, None)
    )
    parsed = _parse_frames(frames)
    assert [d["type"] for d in parsed] == ["status", "chunk", "run_completed"]
    assert [d["sequence"] for d in parsed] == [1, 2, 3]
    assert parsed[1]["content"] == "b"  # legacy 顶层字段保留
    # 每帧带 id（Last-Event-ID 依据）
    assert all(f.startswith("id: ") for f in frames)


@pytest.mark.unit
async def test_sse_resume_from_last_id(ctx):
    service, stream, _app, _sf = ctx
    rid = _queued(service)
    publisher = create_run_event_publisher(rid, "T1")
    ids = []
    for i in range(3):
        ids.append(await publisher.publish({"type": "chunk", "content": str(i)}))
    await publisher.publish({"type": "run_completed", "status": "SUCCEEDED"})

    frames = await _collect(
        _run_event_sse_generator(_FakeRequest(disconnect_after=999), service, rid, ids[1])
    )
    parsed = _parse_frames(frames)
    assert [d["content"] for d in parsed if d["type"] == "chunk"] == ["2"]
    assert parsed[-1]["type"] == "run_completed"


@pytest.mark.unit
async def test_unauthorized_run_stream(ctx):
    _service, _stream, app, _sf = ctx
    # 非 dev 模式 + run 归属他人
    app.state.dev_mode = False
    run = _service.create_run(query="q", session_id="s", thread_id="T", user_id="owner-1")
    client = TestClient(app)
    resp = client.get(f"/api/runs/{run['id']}/stream")
    assert resp.status_code == 403


@pytest.mark.unit
async def test_client_disconnect(ctx):
    service, _stream, _app, _sf = ctx
    rid = _queued(service)
    # 第一轮后断开，且无事件 -> 生成器应尽快返回
    frames = await asyncio.wait_for(
        _collect(_run_event_sse_generator(_FakeRequest(disconnect_after=0), service, rid, None)),
        timeout=3,
    )
    assert frames == []


@pytest.mark.unit
async def test_run_completed_after_stream_disconnect(ctx, monkeypatch):
    service, _stream, _app, _sf = ctx
    monkeypatch.setattr(config, "RUN_EVENT_SSE_POLL_DB_SECONDS", 0.02)
    monkeypatch.setattr(config, "RUN_EVENT_SSE_IDLE_TIMEOUT_SECONDS", 1.0)
    rid = _queued(service)

    # 客户端先断开（没有终态事件）
    frames = await asyncio.wait_for(
        _collect(_run_event_sse_generator(_FakeRequest(disconnect_after=1), service, rid, None)),
        timeout=3,
    )
    assert frames == []

    # worker 稍后完成
    service.mark_running(rid)
    service.mark_succeeded(rid, {"response": "late"})

    # 新连接读取：从 DB 得到最终状态
    frames2 = await asyncio.wait_for(
        _collect(_run_event_sse_generator(_FakeRequest(disconnect_after=999), service, rid, None)),
        timeout=3,
    )
    parsed = _parse_frames(frames2)
    assert parsed[-1]["type"] == "run_completed"
    assert parsed[-1]["source"] == "db_final"


# ---------------------------------------------------------------------------
# 4. Stream 过期后最终结果仍可用（真相源是 DB）
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_final_result_available_after_event_expired(ctx):
    service, stream, app, _sf = ctx
    rid = _queued(service)
    service.mark_running(rid)
    service.mark_succeeded(rid, {"response": "final answer"})

    # 事件流被删除/过期
    await stream.delete(rid)
    assert await stream.length(rid) == 0

    # GET 仍从 PostgreSQL 读到最终结果
    client = TestClient(app)
    got = client.get(f"/api/runs/{rid}")
    assert got.status_code == 200
    assert got.json()["status"] == RunStatus.SUCCEEDED.value
    assert got.json()["result"]["response"] == "final answer"

    # SSE 也回退到 DB 终态
    frames = await _collect(
        _run_event_sse_generator(_FakeRequest(disconnect_after=999), service, rid, None)
    )
    parsed = _parse_frames(frames)
    assert parsed[-1]["type"] == "run_completed"
    assert parsed[-1]["source"] == "db_final"


# ---------------------------------------------------------------------------
# 5. worker -> API 跨组件（同 stream）
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_worker_to_api_cross_process(ctx):
    service, _stream, _app, _sf = ctx
    rid = _queued(service)

    # worker 侧：graph stream_callback -> publisher -> stream
    publisher = create_run_event_publisher(rid, "T1")
    await publisher.publish({"type": "status", "content": "worker 开始"})
    await publisher.publish({"type": "chunk", "content": "worker 产出"})
    await publisher.publish({"type": "run_completed", "status": "SUCCEEDED", "content": "worker 产出"})

    # API 侧：读取 stream -> SSE
    frames = await _collect(
        _run_event_sse_generator(_FakeRequest(disconnect_after=999), service, rid, None)
    )
    parsed = _parse_frames(frames)
    assert [d["type"] for d in parsed] == ["status", "chunk", "run_completed"]
    assert parsed[1]["content"] == "worker 产出"


# ---------------------------------------------------------------------------
# 6. run_queued 事件（API 创建时发布）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_run_publishes_run_queued(ctx):
    _service, stream, app, _sf = ctx
    with patch("runtime.dispatch.dispatch_run", new=AsyncMock(return_value="celery")):
        client = TestClient(app)
        resp = client.post("/api/runs", json={"query": "q", "session_id": "s"})
    assert resp.status_code == 202
    rid = resp.json()["run_id"]
    entries = asyncio.run(stream.read(rid, after_id="0-0", count=10))
    types = [RunEvent.from_fields(e.fields, e.event_id).type for e in entries]
    assert RunEventType.RUN_QUEUED.value in types
