"""Run 事件流（Redis Streams）—— worker 与 API 之间的解耦事件通道。

为什么不是 worker 直接推 SSE：浏览器 socket 绑在 API 进程上，worker 与 API 是
不同进程（甚至不同主机）。让 worker 维持浏览器连接会带来粘性会话与重启丢连问题。
正确做法是 worker 只**写事件**，API 从 Redis Stream 读并转发::

    worker -> XADD agent:run:{run_id}:events -> API XREAD -> SSE -> client

投递语义（由 ``tests/integration/runtime/test_event_delivery_semantics.py`` 实测锁定）::

    best-effort resumable event stream; NOT exactly-once;
    duplicates or gaps may occur around reconnect/replay;
    trimmed historical events may become unrecoverable.

具体：

  - **单条连接内不重复**：cursor 单调前进，``XREAD <key> <cursor>`` 严格返回 cursor
    之后的条目，因此一次连续连接里每个 entry 只被转发一次。
  - **重连可能重复**：客户端带**较旧的** ``Last-Event-ID``（或不带）重连时，服务端会
    重放该 ID 之后的全部条目 —— 包括客户端**已经处理过**的。所以重复投递是可能的，
    本模块**不是** at-most-once（把 Stream 说成 at-most-once 是错的：重连 replay
    明明会重复）。
  - **可能丢**（gap）：``replay=false``（cursor ``$``）跳过历史；idle 超时会结束流。
  - **裁剪后不可恢复**：``XADD ... MAXLEN ~ N`` 是**近似**裁剪：只在 macro-node
    边界触发，短流完全不裁剪，长流裁剪后仍可能**多于** N 条。因此不能声称"最多保留
    N 条"，只能说"按近似上限裁剪，被裁掉的历史不可恢复"。
  - **不是业务真相源**：run 的权威状态是 ``agent_runs`` 表
    （``GET /api/runs/{run_id}``）。事件流全丢也不影响 run 终态正确性。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from core.logger import get_logger

logger = get_logger("runtime.events")

KEY_PREFIX = "agent:run:"
KEY_SUFFIX = ":events"

#: 事件类型（与 spec 对齐）
EVENT_QUEUED = "queued"
EVENT_STARTED = "started"
EVENT_STATUS = "status"
EVENT_THINKING = "thinking"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_CHUNK = "chunk"
EVENT_RETRY = "retry"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"
EVENT_CANCELLED = "cancelled"

#: 单个 run 事件保留条数上限（控制 Redis 内存；超出后旧事件被裁剪）
DEFAULT_MAXLEN = int(os.getenv("AGENT_RUN_EVENT_MAXLEN", "500"))

#: 允许写入事件负载的 key 白名单，避免把 query / 用户输入等敏感内容塞进 Redis。
#: label/查询文本不进入事件流是刻意的安全约束。
_SAFE_FIELDS = frozenset(
    {
        "run_id",
        "thread_id",
        "status",
        "attempt",
        "max_attempts",
        "error_code",
        "error_type",
        "agent",
        "stage",
        "duration_seconds",
        "reason",
        "event",
    }
)


def events_key(run_id: str) -> str:
    return f"{KEY_PREFIX}{run_id}{KEY_SUFFIX}"


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """只保留白名单字段（防止 query/凭据/用户标识进入 Redis 与 SSE 输出）。"""
    if not payload:
        return {}
    return {k: v for k, v in payload.items() if k in _SAFE_FIELDS}


class RunEventPublisher:
    """把 run 生命周期事件写入 Redis Stream（worker 侧）。

    Redis 不可用时静默降级为 no-op：事件流是观测通道，不能因为它让业务失败。

    刻意使用**同步** redis 客户端并通过 ``asyncio.to_thread`` 派发：async 客户端会
    绑定创建它的事件循环，一旦该 loop 结束（例如测试里每个场景各自
    ``asyncio.run``），连接在 GC 时会抛 "Event loop is closed"。同步客户端与 loop
    无关，worker 的长生命周期 loop 和测试的短生命周期 loop 都能安全复用。
    """

    def __init__(self, client: Any = None, *, maxlen: int = DEFAULT_MAXLEN):
        self._client = client
        self._maxlen = maxlen

    def _ensure_client(self):
        if self._client is None:
            import redis as redis_lib

            from core.config import REDIS_URL

            self._client = redis_lib.Redis.from_url(
                REDIS_URL, decode_responses=True, socket_timeout=2
            )
        return self._client

    async def publish(
        self, run_id: str, event: str, payload: dict[str, Any] | None = None
    ) -> str | None:
        """写入一条事件，返回 Redis 生成的 event id（失败返回 None）。"""
        fields = {"event": event, **sanitize_payload(payload), "ts": _now_iso()}
        key = events_key(run_id)
        try:
            client = self._ensure_client()
            return await asyncio.to_thread(
                client.xadd, key, fields, maxlen=self._maxlen, approximate=True
            )
        except Exception as e:  # pragma: no cover - 观测通道不得影响业务
            logger.debug("run 事件写入失败 run_id=%s event=%s: %s", run_id, event, type(e).__name__)
            return None

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.close)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def read_events(
    client: Any, run_id: str, *, last_event_id: str = "0-0", count: int = 100, block_ms: int = 0
) -> list[tuple[str, dict[str, Any]]]:
    """从 stream 读取事件（``last_event_id`` 之后）。

    block_ms > 0 时使用 XREAD BLOCK 等待新事件；``last_event_id`` 为 ``"$"`` 表示
    只看新事件（不重放历史）。
    """
    kwargs: dict[str, Any] = {"count": count}
    if block_ms > 0:
        kwargs["block"] = block_ms
    result = await client.xread({events_key(run_id): last_event_id or "0-0"}, **kwargs)
    events: list[tuple[str, dict[str, Any]]] = []
    for _key, entries in result or []:
        for entry_id, fields in entries:
            events.append((entry_id, fields))
    return events


def format_sse(event_id: str, fields: dict[str, Any]) -> str:
    """把一条 stream entry 渲染成 SSE 帧（带 id 以支持 Last-Event-ID 续读）。"""
    event = fields.get("event", "status")
    data = json.dumps(fields, ensure_ascii=False, default=str)
    return f"id: {event_id}\nevent: {event}\ndata: {data}\n\n"


__all__ = [
    "RunEventPublisher",
    "events_key",
    "read_events",
    "format_sse",
    "sanitize_payload",
    "KEY_PREFIX",
    "KEY_SUFFIX",
    "EVENT_QUEUED",
    "EVENT_STARTED",
    "EVENT_STATUS",
    "EVENT_THINKING",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_CHUNK",
    "EVENT_RETRY",
    "EVENT_COMPLETED",
    "EVENT_FAILED",
    "EVENT_CANCELLED",
]
