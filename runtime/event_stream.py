"""RunEvent 事件流后端：Redis Streams（生产）+ 进程内（开发/测试）。

Redis Streams 是临时流式通道（ephemeral streaming channel）：
  - XADD + MAXLEN 限制长度；
  - EXPIRE 限制生命周期；
  - XREAD 支持从 event_id 之后继续（Last-Event-ID 断线重连）。

它不是 Run 状态真相源：Stream 过期后最终状态仍从 PostgreSQL AgentRun 读取。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

from core.logger import get_logger

logger = get_logger("runtime.event_stream")

KEY_PREFIX = "agent:run-events:"


@dataclass
class StreamEntry:
    event_id: str
    fields: dict[str, Any]


class RunEventStream(Protocol):
    async def append(
        self, run_id: str, fields: dict[str, str], *, maxlen: int, ttl_seconds: int
    ) -> str: ...
    async def read(
        self,
        run_id: str,
        *,
        after_id: str = "0-0",
        block_ms: int = 0,
        count: int = 100,
    ) -> list[StreamEntry]: ...
    async def length(self, run_id: str) -> int: ...
    async def expire(self, run_id: str, ttl_seconds: int) -> None: ...
    async def delete(self, run_id: str) -> None: ...
    async def close(self) -> None: ...


class NullRunEventStream:
    """事件流关闭时的 no-op（SSE 端退化为 DB 轮询）。"""

    is_blocking = False

    async def append(self, run_id, fields, *, maxlen, ttl_seconds) -> str:
        return ""

    async def read(self, run_id, *, after_id="0-0", block_ms=0, count=100) -> list[StreamEntry]:
        return []

    async def length(self, run_id: str) -> int:
        return 0

    async def expire(self, run_id: str, ttl_seconds: int) -> None:
        return None

    async def delete(self, run_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _id_key(event_id: str) -> tuple[int, int]:
    try:
        ms, _, seq = str(event_id).partition("-")
        return int(ms), int(seq or 0)
    except (TypeError, ValueError):
        return (0, 0)


class InMemoryRunEventStream:
    """进程内事件流（开发/测试），语义对齐 Redis Streams 的 MAXLEN/TTL/续读。"""

    is_blocking = False

    def __init__(self, clock=None):
        import time as _time

        self._clock = clock or _time.monotonic
        self._streams: dict[str, list[StreamEntry]] = {}
        self._expiry: dict[str, float] = {}
        self._counter = 0

    def _purge(self, run_id: str) -> None:
        expiry = self._expiry.get(run_id)
        if expiry is not None and expiry <= self._clock():
            self._streams.pop(run_id, None)
            self._expiry.pop(run_id, None)

    async def append(self, run_id, fields, *, maxlen, ttl_seconds) -> str:
        self._purge(run_id)
        self._counter += 1
        event_id = f"{self._counter}-0"
        entries = self._streams.setdefault(run_id, [])
        entries.append(StreamEntry(event_id=event_id, fields=dict(fields)))
        if maxlen and len(entries) > maxlen:
            self._streams[run_id] = entries[-maxlen:]
        self._expiry[run_id] = self._clock() + ttl_seconds
        return event_id

    async def read(self, run_id, *, after_id="0-0", block_ms=0, count=100) -> list[StreamEntry]:
        self._purge(run_id)
        entries = self._streams.get(run_id, [])
        cursor = _id_key(after_id) if after_id and after_id != "0-0" else (0, 0)
        result = [e for e in entries if _id_key(e.event_id) > cursor]
        return result[: max(1, count)]

    async def length(self, run_id: str) -> int:
        self._purge(run_id)
        return len(self._streams.get(run_id, []))

    async def expire(self, run_id: str, ttl_seconds: int) -> None:
        if run_id in self._streams:
            self._expiry[run_id] = self._clock() + ttl_seconds

    async def delete(self, run_id: str) -> None:
        self._streams.pop(run_id, None)
        self._expiry.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
        self._expiry.clear()


class RedisRunEventStream:
    """Redis Streams 后端（生产）。"""

    is_blocking = True

    def __init__(self, client: Any, key_prefix: str = KEY_PREFIX):
        self._client = client
        self._prefix = key_prefix

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}{run_id}"

    async def append(self, run_id, fields, *, maxlen, ttl_seconds) -> str:
        key = self._key(run_id)
        event_id = await self._client.xadd(
            key, dict(fields), maxlen=max(1, int(maxlen)), approximate=False
        )
        await self._client.expire(key, max(1, int(ttl_seconds)))
        return str(event_id)

    async def read(self, run_id, *, after_id="0-0", block_ms=0, count=100) -> list[StreamEntry]:
        key = self._key(run_id)
        start = after_id or "0-0"
        result = await self._client.xread(
            {key: start}, block=block_ms if block_ms and block_ms > 0 else None, count=count
        )
        if not result:
            return []
        entries: list[StreamEntry] = []
        for _stream_key, items in result:
            for entry_id, fields in items:
                entries.append(StreamEntry(event_id=str(entry_id), fields=dict(fields)))
        return entries

    async def length(self, run_id: str) -> int:
        return int(await self._client.xlen(self._key(run_id)))

    async def expire(self, run_id: str, ttl_seconds: int) -> None:
        await self._client.expire(self._key(run_id), max(1, int(ttl_seconds)))

    async def delete(self, run_id: str) -> None:
        await self._client.delete(self._key(run_id))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


_default_stream: RunEventStream | None = None


def build_run_event_stream(backend: str, redis_url: str) -> RunEventStream:
    backend = (backend or "redis").strip().lower()
    if backend == "memory":
        return InMemoryRunEventStream()
    if backend == "redis":
        import redis.asyncio as aioredis

        client = aioredis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        return RedisRunEventStream(client)
    raise ValueError(f"未知 RUN_EVENT_STREAM_BACKEND: {backend!r}（仅支持 redis | memory）")


def get_run_event_stream() -> RunEventStream:
    """进程内单例事件流（worker 发布 / API 读取共用配置）。"""
    global _default_stream
    if _default_stream is not None:
        return _default_stream

    from core.config import (
        REDIS_URL,
        RUN_EVENT_STREAM_BACKEND,
        RUN_EVENT_STREAM_ENABLED,
    )

    if not RUN_EVENT_STREAM_ENABLED:
        _default_stream = NullRunEventStream()
    else:
        _default_stream = build_run_event_stream(RUN_EVENT_STREAM_BACKEND, REDIS_URL)
    return _default_stream


async def shutdown_run_event_stream() -> None:
    global _default_stream
    stream = _default_stream
    _default_stream = None
    if stream is not None:
        await stream.close()


def reset_run_event_stream_for_tests(stream: RunEventStream | None = None) -> None:
    """仅测试使用：注入/清空单例。"""
    global _default_stream
    _default_stream = stream
