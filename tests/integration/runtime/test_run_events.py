"""Gate 13：run 事件流（Redis Streams）+ SSE 转发 + 断点续读。

覆盖：
  - worker 把生命周期事件写入 ``agent:run:{run_id}:events``；
  - SSE 帧格式正确（``id``/``event``/``data``），可被 ``Last-Event-ID`` 续读；
  - 事件负载**不含** query / 用户输入等敏感内容（白名单强制）；
  - 重连不会重复投递已消费的事件。

运行::

    TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \\
    TEST_REDIS_URL=redis://localhost:6379 \\
    pytest tests/integration/runtime/test_run_events.py -q
"""

from __future__ import annotations

import os

import pytest

DB_URL = os.getenv("TEST_DISTRIBUTED_DB_URL", "").strip()
REDIS_URL = os.getenv("TEST_REDIS_URL", "").strip()

INFRA_REASON = "TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL 未设置；需要真实 PG + Redis"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (DB_URL and REDIS_URL), reason=INFRA_REASON),
]


def test_gate13_events_are_written_to_redis_stream(redis_client, run_service, unique):
    """执行一个 run 后，stream 里必须出现 started + completed 事件。"""
    import asyncio

    from runtime.events import (
        EVENT_COMPLETED,
        EVENT_STARTED,
        events_key,
        read_events,
    )

    run = run_service.create_run(query="events-test", session_id=unique("T-ev"))

    class _RT:
        async def run(self, *, thread_id, query, user_id=None, **kw):
            return {"response": "ok", "thread_id": thread_id}

    async def _provider():
        return _RT()

    from runtime.executor import execute_run

    async def _dispatch(run_id, countdown=None):
        return "stub"

    status = asyncio.run(
        execute_run(
            run["id"],
            service=run_service,
            runtime_provider=_provider,
            dispatcher=_dispatch,
            worker_id="w-events",
        )
    )
    assert status == "SUCCEEDED"

    async def _read():
        import redis.asyncio as aioredis

        from core.config import REDIS_URL as url

        client = aioredis.from_url(url, decode_responses=True)
        try:
            return await read_events(client, run["id"], last_event_id="0-0")
        finally:
            await client.aclose()

    events = asyncio.run(_read())
    names = [fields.get("event") for _eid, fields in events]
    assert EVENT_STARTED in names, f"缺少 started 事件: {names}"
    assert EVENT_COMPLETED in names, f"缺少 completed 事件: {names}"
    assert names.index(EVENT_STARTED) < names.index(EVENT_COMPLETED), (
        f"事件顺序不对: {names}"
    )

    key = events_key(run["id"])
    assert key == f"agent:run:{run['id']}:events"

    # 事件不得携带 query / 用户输入
    blob = str(events)
    assert "events-test" not in blob, "事件流泄露了 query 内容"


def test_gate13_last_event_id_supports_resume(redis_client, unique):
    """带 Last-Event-ID 续读：只返回该 id 之后的事件，不重复已消费部分。"""
    import asyncio
    import json as _json

    from runtime.events import (
        RunEventPublisher,
        format_sse,
        read_events,
    )

    run_id = unique("run")
    redis_client.delete(f"agent:run:{run_id}:events")

    async def scenario():
        import redis.asyncio as aioredis

        from core.config import REDIS_URL as url

        pub = RunEventPublisher()
        first_id = await pub.publish(run_id, "started", {"thread_id": "T1", "attempt": 1})
        second_id = await pub.publish(run_id, "status", {"status": "RUNNING"})
        third_id = await pub.publish(run_id, "completed", {"status": "SUCCEEDED"})
        await pub.close()

        client = aioredis.from_url(url, decode_responses=True)
        try:
            all_events = await read_events(client, run_id, last_event_id="0-0")
            resumed = await read_events(client, run_id, last_event_id=second_id)
        finally:
            await client.aclose()
        return first_id, second_id, third_id, all_events, resumed

    first_id, second_id, third_id, all_events, resumed = asyncio.run(scenario())

    assert [f.get("event") for _i, f in all_events] == ["started", "status", "completed"]
    # 续读只返回第二条之后
    assert [f.get("event") for _i, f in resumed] == ["completed"], resumed

    # SSE 帧可解析且带 id（客户端据此发送 Last-Event-ID）
    frame = format_sse(third_id, {"event": "completed", "status": "SUCCEEDED"})
    lines = frame.strip().split("\n")
    assert lines[0] == f"id: {third_id}"
    assert lines[1] == "event: completed"
    data = _json.loads(lines[2][len("data: ") :])
    assert data["status"] == "SUCCEEDED"
    assert frame.endswith("\n\n")

    redis_client.delete(f"agent:run:{run_id}:events")


def test_gate13_payload_sanitizer_drops_sensitive_fields():
    """白名单强制：query / 用户标识 / 凭据不得进入事件流。"""
    from runtime.events import sanitize_payload

    payload = sanitize_payload(
        {
            "thread_id": "T1",
            "attempt": 2,
            "status": "RETRYING",
            "query": "我的身份证号是...",
            "customer_query": "敏感",
            "user_id": "u-123",
            "api_key": "sk-secret",
            "jwt": "eyJhbGciOi...",
        }
    )
    assert set(payload) == {"thread_id", "attempt", "status"}
    blob = str(payload)
    for bad in ("身份证", "u-123", "sk-secret", "eyJhbGciOi"):
        assert bad not in blob


def test_gate13_sse_endpoint_is_registered():
    """SSE 端点必须在 OpenAPI surface 上存在（防止被误删）。"""
    from api.routes.runs import router

    paths = {
        getattr(r, "path", None)
        for r in router.routes
        if getattr(r, "path", None)
    }
    assert "/api/runs/{run_id}/events" in paths, sorted(paths)
