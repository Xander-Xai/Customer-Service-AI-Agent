"""Event Stream 真实投递语义（不是 prose 声明，而是可执行断言）。

本文件把"事件流到底保证什么"钉成可执行事实，避免文档里出现自相矛盾的措辞
（例如把 Redis Stream 说成 at-most-once，但重连 replay 明明会重复投递）。

从实现读出的真实语义（``runtime/events.py`` + ``api/routes/runs.py``）：

  - writer:``XADD agent:run:{run_id}:events``（``maxlen`` + approximate 裁剪）
  - reader:``XREAD <key> <cursor>``，**严格返回 cursor 之后**的条目，按序
  - SSE 每帧带 ``id: <entry_id>``，客户端可带 ``Last-Event-ID`` 续读

由此可断言：

  1. **单条连接内不重复**：cursor 单调前进，每个 entry 在一次连接里只被转发一次。
     （既不是 at-most-once 的"绝不重复"，也不是 exactly-once 的"绝不重复且不丢"——
     它在"不丢"这一侧。）
  2. **重连会重复**：客户端若用**较旧的** ``Last-Event-ID`` 重连（或不带），服务端会
     重放该 ID 之后的全部条目 —— 包括它**已经处理过**的。所以 duplicates 可能出现。
  3. **可能丢**：``replay=false``（cursor ``$``）直接跳过历史；idle 超时会结束流。
  4. **裁剪后不可恢复**：``maxlen`` 之外的历史已从 Redis 删除。

结论措辞（统一口径）：
    best-effort resumable event stream; not exactly-once;
    duplicates or gaps may occur around reconnect/replay;
    trimmed historical events may become unrecoverable.

运行::

    TEST_REDIS_URL=redis://localhost:6379 pytest tests/integration/runtime/test_event_delivery_semantics.py -q
"""

from __future__ import annotations

import asyncio
import os

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL 未设置；需要真实 Redis"),
]


def _async_client():
    import redis.asyncio as aioredis

    from core.config import REDIS_URL as url

    return aioredis.from_url(url, decode_responses=True)


@pytest.fixture
def run_id(redis_client):
    import uuid

    rid = f"evt-sem-{uuid.uuid4().hex[:10]}"
    yield rid
    redis_client.delete(f"agent:run:{rid}:events")


@pytest.mark.timeout(120)
def test_cursor_is_monotonic_and_no_duplicate_within_one_connection(run_id):
    """断言 1：单条连接内 cursor 单调前进，同一 entry 不被转发两次。"""
    from runtime.events import RunEventPublisher, read_events

    async def scenario():
        pub = RunEventPublisher()
        ids = [await pub.publish(run_id, "status", {"status": f"S{i}"}) for i in range(5)]
        await pub.close()

        client = _async_client()
        try:
            seen: list[str] = []
            cursor = "0-0"
            while len(seen) < len(ids):
                batch = await read_events(client, run_id, last_event_id=cursor)
                if not batch:
                    break
                for eid, _fields in batch:
                    seen.append(eid)
                    cursor = eid  # 单调前进：下一批只读 cursor 之后
        finally:
            await client.aclose()
        return ids, seen

    ids, seen = asyncio.run(scenario())

    assert seen == ids, f"应按序且不重复读到全部 entry: {seen} vs {ids}"
    assert len(set(seen)) == len(seen), "单连接内出现重复投递"


@pytest.mark.timeout(120)
def test_reconnect_with_older_id_replays_already_seen_events(run_id):
    """断言 2：**重连会重复**。这是"at-most-once"措辞错误的直接证据。

    客户端已经处理到 ``ids[2]``，但重连时只带 ``ids[0]``（例如浏览器重连丢失了
    进度），服务端会重放 ``ids[1..]`` —— 其中 ``ids[1]`` / ``ids[2]`` 是它已经
    处理过的。
    """
    from runtime.events import RunEventPublisher, read_events

    async def scenario():
        pub = RunEventPublisher()
        ids = [await pub.publish(run_id, "status", {"status": f"S{i}"}) for i in range(4)]
        await pub.close()
        client = _async_client()
        try:
            stale_cursor = ids[0]
            replayed = await read_events(client, run_id, last_event_id=stale_cursor)
        finally:
            await client.aclose()
        return ids, [eid for eid, _ in replayed]

    ids, replayed = asyncio.run(scenario())

    assert (
        ids[1] in replayed and ids[2] in replayed
    ), "用较旧 Last-Event-ID 重连应重放已处理过的 entry（这正是重复投递的来源）"
    assert len(set(replayed)) == len(replayed), "单次 replay 内部不应自相重复"


@pytest.mark.timeout(120)
def test_dollar_cursor_skips_history_creating_gaps(run_id):
    """断言 3：``replay=false``（cursor ``$``）直接跳过历史 —— 缺口的一种来源。"""
    from runtime.events import RunEventPublisher, read_events

    async def scenario():
        pub = RunEventPublisher()
        for i in range(3):
            await pub.publish(run_id, "status", {"status": f"S{i}"})
        await pub.close()
        client = _async_client()
        try:
            only_new = await read_events(client, run_id, last_event_id="$")
            fresh = await read_events(client, run_id, last_event_id="0-0")
        finally:
            await client.aclose()
        return only_new, fresh

    only_new, fresh = asyncio.run(scenario())
    assert len(fresh) == 3
    assert only_new == [], "``$`` 应只等待新事件，不重放既有历史"


@pytest.mark.timeout(180)
def test_approximate_trimming_has_no_hard_bound_and_drops_old_history(run_id):
    """断言 4：裁剪后旧事件不可恢复，但 ``approximate=True`` **不是硬上界**。

    实测语义（Redis Stream macro-node 裁剪）：
      - ``XADD ... MAXLEN ~ N`` 只在超过 macro-node 边界时才裁剪，因此短流上
        ``maxlen=5`` 完全不会裁剪；长流上裁剪后仍可能**多于** N 条；
      - 一旦裁剪，被裁掉的 entry **无法再取回**（不可恢复的缺口）。
    所以文档不能说"最多保留 N 条"，只能说"按近似上限裁剪，历史可能不可恢复"。
    """
    from runtime.events import RunEventPublisher, read_events

    async def scenario():
        pub = RunEventPublisher(maxlen=5)
        ids = [await pub.publish(run_id, "status", {"status": f"S{i}"}) for i in range(300)]
        await pub.close()
        client = _async_client()
        try:
            surviving = await read_events(client, run_id, last_event_id="0-0")
        finally:
            await client.aclose()
        return ids, [eid for eid, _ in surviving]

    ids, surviving = asyncio.run(scenario())

    # 近似裁剪不是硬上界：条数可能超过 maxlen
    assert len(surviving) > 5, (
        f"approximate=True 不保证 <= maxlen（实测 {len(surviving)} 条）；"
        "文档不得声称'最多保留 maxlen 条'"
    )
    # 但确实裁掉了历史：最早的事件不可恢复，且保留下来的都是较新的
    assert len(surviving) < len(ids), "大量写入后应发生裁剪"
    assert ids[0] not in surviving, "最早的 entry 应已被裁剪且不可恢复"
    assert surviving == ids[-len(surviving) :], "应保留最近的连续尾部"


@pytest.mark.timeout(120)
def test_event_stream_is_not_the_source_of_truth(run_id):
    """事件流是观测通道；权威状态是 ``agent_runs``。断流不改变 run 终态。"""
    if not DB_URL:
        pytest.skip("TEST_DISTRIBUTED_DB_URL 未设置；跳过真相源对照")

    import uuid as _uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from db.models import AgentDeadLetter, AgentRun, ToolSideEffect
    from runtime.repository import AgentRunRepository
    from runtime.run_service import RunService

    engine = create_engine(DB_URL)
    for table in (AgentRun, AgentDeadLetter, ToolSideEffect):
        table.__table__.create(engine, checkfirst=True)
    svc = RunService(
        repository=AgentRunRepository(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False)
        )
    )
    run = svc.create_run(query="evt-truth", session_id=f"T-evt-{_uuid.uuid4().hex[:8]}")
    svc.mark_running(run["id"], worker_id="w-evt", lease_seconds=60)
    svc.mark_succeeded(run["id"], {"response": "done"})

    # 该 run 完全没有写任何事件（模拟事件流丢失 / Redis 抖动）
    import redis as redis_lib

    sync = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    assert sync.exists(f"agent:run:{run['id']}:events") == 0, "本用例不应写事件"
    sync.close()

    fetched = svc.require_run(run["id"])
    assert fetched["status"] == "SUCCEEDED", "即使事件流为空，run 终态仍然正确"

    with engine.connect() as conn:
        conn.execute(text("DELETE FROM agent_runs WHERE id=:id"), {"id": run["id"]})
    engine.dispose()
