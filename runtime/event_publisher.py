"""RunEventPublisher：worker 把 Agent 事件写入事件流。

Agent node 不直接 import redis；graph 继续使用 ``stream_callback`` 抽象，
由 worker 注入基于 Redis Streams 的 publisher。
"""

from __future__ import annotations

from typing import Any, Protocol

from core.logger import get_logger

from . import metrics
from .events import build_run_event

logger = get_logger("runtime.event_publisher")


class RunEventPublisher(Protocol):
    async def publish(self, event: dict[str, Any] | None) -> str | None: ...
    async def close(self) -> None: ...


class NullRunEventPublisher:
    """事件流关闭/不可用时的 no-op（不阻塞执行）。"""

    async def publish(self, event: dict[str, Any] | None) -> str | None:
        return None

    async def close(self) -> None:
        return None


class StreamRunEventPublisher:
    """基于事件流（Redis Streams）的 publisher。"""

    def __init__(
        self,
        stream: Any,
        run_id: str,
        thread_id: str,
        *,
        maxlen: int,
        ttl_seconds: int,
        max_bytes: int,
    ):
        self._stream = stream
        self._run_id = run_id
        self._thread_id = thread_id
        self._maxlen = maxlen
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._sequence = 0

    async def publish(self, event: dict[str, Any] | None) -> str | None:
        if not event or not isinstance(event, dict):
            return None
        self._sequence += 1
        event_type = str(event.get("type", "status"))
        payload = {k: v for k, v in event.items() if k != "type"}
        run_event = build_run_event(
            run_id=self._run_id,
            thread_id=self._thread_id,
            event_type=event_type,
            sequence=self._sequence,
            payload=payload,
            max_bytes=self._max_bytes,
        )
        try:
            event_id = await self._stream.append(
                self._run_id,
                run_event.to_fields(),
                maxlen=self._maxlen,
                ttl_seconds=self._ttl,
            )
        except Exception as e:
            metrics.record_publish_error()
            logger.debug("run event 发布失败 run=%s: %s", self._run_id, type(e).__name__)
            return None
        run_event.event_id = event_id
        metrics.record_publish()
        return event_id

    async def close(self) -> None:
        return None


def create_run_event_publisher(run_id: str, thread_id: str) -> RunEventPublisher:
    """按配置创建 publisher；禁用时返回 Null（执行不受影响）。"""
    from core.config import (
        RUN_EVENT_MAX_BYTES,
        RUN_EVENT_STREAM_ENABLED,
        RUN_EVENT_STREAM_MAXLEN,
        RUN_EVENT_STREAM_TTL_SECONDS,
    )

    if not RUN_EVENT_STREAM_ENABLED:
        return NullRunEventPublisher()

    from .event_stream import get_run_event_stream

    return StreamRunEventPublisher(
        get_run_event_stream(),
        run_id,
        thread_id,
        maxlen=RUN_EVENT_STREAM_MAXLEN,
        ttl_seconds=RUN_EVENT_STREAM_TTL_SECONDS,
        max_bytes=RUN_EVENT_MAX_BYTES,
    )


async def publish_run_event(
    run_id: str, thread_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> str | None:
    """Best-effort 单事件发布（API 侧 run_queued 等）。失败不抛出。"""
    publisher = create_run_event_publisher(run_id, thread_id)
    try:
        return await publisher.publish({"type": event_type, **(payload or {})})
    except Exception as e:  # pragma: no cover - best effort
        logger.debug("publish_run_event 失败 run=%s: %s", run_id, type(e).__name__)
        return None
    finally:
        await publisher.close()
