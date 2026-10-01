"""真实 Redis Streams 事件流集成测试。

仅在提供 TEST_REDIS_URL 时运行：
    TEST_REDIS_URL=redis://localhost:6379/0 pytest tests/integration/test_run_events_redis.py -q

验证 XADD/XREAD/MAXLEN/TTL/续读，以及 worker 发布 -> API 读取的跨进程语义。
"""

import asyncio
import json
import os
import uuid

import pytest

REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="TEST_REDIS_URL 未设置；需要真实 Redis 才能运行",
)


async def _make_client():
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


async def test_redis_stream_order_resume_trim_ttl():
    from runtime.event_publisher import StreamRunEventPublisher
    from runtime.event_stream import RedisRunEventStream

    client = await _make_client()
    prefix = f"agent:test-events:{uuid.uuid4().hex}:"
    stream = RedisRunEventStream(client, key_prefix=prefix)
    run_id = "run-x"
    try:
        publisher = StreamRunEventPublisher(
            stream, run_id, "th", maxlen=100, ttl_seconds=60, max_bytes=4096
        )
        ids = []
        for i in range(3):
            ids.append(await publisher.publish({"type": "chunk", "content": str(i)}))

        entries = await stream.read(run_id, after_id="0-0", count=10)
        assert [json.loads(e.fields["payload"])["content"] for e in entries] == ["0", "1", "2"]

        resumed = await stream.read(run_id, after_id=ids[0], count=10)
        assert [json.loads(e.fields["payload"])["content"] for e in resumed] == ["1", "2"]

        # MAXLEN trim
        trim = StreamRunEventPublisher(
            stream, "run-trim", "th", maxlen=3, ttl_seconds=60, max_bytes=4096
        )
        for i in range(6):
            await trim.publish({"type": "chunk", "content": str(i)})
        assert await stream.length("run-trim") == 3

        # TTL：1 秒后过期
        ttl = StreamRunEventPublisher(
            stream, "run-ttl", "th", maxlen=10, ttl_seconds=1, max_bytes=4096
        )
        await ttl.publish({"type": "status", "content": "x"})
        assert await stream.length("run-ttl") == 1
        await asyncio.sleep(1.3)
        assert await stream.length("run-ttl") == 0
    finally:
        for rid in (run_id, "run-trim", "run-ttl"):
            await stream.delete(rid)
        await client.aclose()


async def test_worker_publish_api_read_cross_process():
    """两个独立 Redis 客户端（模拟 worker / API 进程）共享同一 Stream。"""
    from runtime.event_publisher import StreamRunEventPublisher
    from runtime.event_stream import RedisRunEventStream

    worker_client = await _make_client()
    api_client = await _make_client()
    prefix = f"agent:test-events:{uuid.uuid4().hex}:"
    worker_stream = RedisRunEventStream(worker_client, key_prefix=prefix)
    api_stream = RedisRunEventStream(api_client, key_prefix=prefix)
    run_id = "run-cross"
    try:
        publisher = StreamRunEventPublisher(
            worker_stream, run_id, "th", maxlen=100, ttl_seconds=60, max_bytes=4096
        )
        await publisher.publish({"type": "status", "content": "worker 开始"})
        await publisher.publish({"type": "run_completed", "status": "SUCCEEDED"})

        entries = await api_stream.read(run_id, after_id="0-0", count=10)
        assert [e.fields["type"] for e in entries] == ["status", "run_completed"]
    finally:
        await worker_stream.delete(run_id)
        await worker_client.aclose()
        await api_client.aclose()
