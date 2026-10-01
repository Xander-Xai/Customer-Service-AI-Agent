"""RunEvent：跨进程 Agent 事件流的事件模型。

事件通过 Redis Streams 从 worker 流向 API SSE，是**临时流式通道**，不是真相源：
  - Redis Stream = ephemeral streaming channel
  - PostgreSQL AgentRun = durable run state
  - PostgreSQL LangGraph Checkpoint = durable graph execution state

保留现有 SSE 前端依赖的事件语义（status/thinking/rag_status/agent_switch/
tool_call/tool_result/chunk/content_complete）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: 敏感字段（大小写不敏感，精确匹配），发布前过滤
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "jwt",
        "session_token",
        "client_secret",
    }
)


class RunEventType(str, Enum):
    RUN_QUEUED = "run_queued"
    RUN_STARTED = "run_started"
    STATUS = "status"
    THINKING = "thinking"
    RAG_STATUS = "rag_status"
    AGENT_SWITCH = "agent_switch"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CHUNK = "chunk"
    CONTENT_COMPLETE = "content_complete"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


TERMINAL_EVENT_TYPES = frozenset(
    {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
    }
)

#: 保留现有前端 SSE 语义的事件类型（legacy stream_callback 事件）
LEGACY_EVENT_TYPES = frozenset(
    {
        "status",
        "thinking",
        "rag_status",
        "agent_switch",
        "tool_call",
        "tool_result",
        "chunk",
        "content_complete",
    }
)


def is_terminal_type(event_type: str) -> bool:
    try:
        return RunEventType(event_type) in TERMINAL_EVENT_TYPES
    except ValueError:
        return False


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _sanitize_value(v)
            for k, v in value.items()
            if str(k).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """递归过滤敏感字段（不修改原对象）。"""
    return _sanitize_value(dict(event or {}))


def _truncate_payload(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """事件大小限制：超限时截断 content/text 字段。"""
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return payload
    truncated = dict(payload)
    for key in ("content", "text", "message"):
        if key in truncated and isinstance(truncated[key], str):
            over = len(encoded.encode("utf-8")) - max_bytes
            keep = max(0, len(truncated[key]) - over)
            truncated[key] = truncated[key][:keep] + "…[truncated]"
            encoded = json.dumps(truncated, ensure_ascii=False, default=str)
            if len(encoded.encode("utf-8")) <= max_bytes:
                return truncated
    return {"type": truncated.get("type", "status"), "truncated": True}


def normalize_event_type(event_type: str) -> str:
    """把 legacy stream_callback 事件类型映射为 RunEventType 值。"""
    if event_type in LEGACY_EVENT_TYPES:
        return event_type
    try:
        return RunEventType(event_type).value
    except ValueError:
        return RunEventType.STATUS.value


@dataclass
class RunEvent:
    run_id: str
    thread_id: str
    type: str
    sequence: int
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "type": self.type,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_sse_dict(self) -> dict[str, Any]:
        """SSE data：保留 legacy 顶层字段（content/agent/...），叠加 envelope。

        前端依赖 ``data.type`` / ``data.content`` 等顶层语义，因此不能把 payload
        整体嵌到子对象里。
        """
        merged: dict[str, Any] = dict(self.payload)
        merged.update(
            {
                "event_id": self.event_id,
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "type": self.type,
                "sequence": self.sequence,
                "timestamp": self.timestamp,
            }
        )
        return merged

    def to_fields(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "type": self.type,
            "sequence": str(self.sequence),
            "timestamp": str(self.timestamp),
            "payload": json.dumps(self.payload, ensure_ascii=False, default=str),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, Any], event_id: str) -> RunEvent:
        payload_raw = fields.get("payload", "{}")
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (json.JSONDecodeError, TypeError):
            payload = {"raw": str(payload_raw)}
        try:
            sequence = int(fields.get("sequence", 0))
        except (TypeError, ValueError):
            sequence = 0
        try:
            timestamp = float(fields.get("timestamp", 0.0))
        except (TypeError, ValueError):
            timestamp = 0.0
        return cls(
            run_id=str(fields.get("run_id", "")),
            thread_id=str(fields.get("thread_id", "")),
            type=str(fields.get("type", "status")),
            sequence=sequence,
            timestamp=timestamp,
            payload=payload if isinstance(payload, dict) else {"value": payload},
            event_id=event_id or str(fields.get("event_id", "")),
        )


def build_run_event(
    *,
    run_id: str,
    thread_id: str,
    event_type: str,
    sequence: int,
    payload: dict[str, Any] | None = None,
    max_bytes: int = 16384,
    timestamp: float | None = None,
) -> RunEvent:
    clean = _truncate_payload(sanitize_event(payload or {}), max_bytes)
    return RunEvent(
        run_id=run_id,
        thread_id=thread_id,
        type=normalize_event_type(event_type),
        sequence=sequence,
        timestamp=timestamp if timestamp is not None else time.time(),
        payload=clean,
    )
