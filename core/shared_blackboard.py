"""共享黑板（v3.0）- Agent 间全局可读写状态，带结构化日志，隔离 session 数据"""

import asyncio
import contextvars
import time
from typing import Any, Optional

from core.logger import get_logger

logger = get_logger("core.blackboard")

# context variable to hold the active session_id
_blackboard_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "blackboard_session_id", default=None
)

def set_blackboard_session_id(session_id: str | None):
    """设置当前上下文的 blackboard session_id"""
    _blackboard_session_id.set(session_id)

def get_blackboard_session_id() -> Optional[str]:
    """获取当前上下文的 blackboard session_id"""
    return _blackboard_session_id.get()


class SharedBlackboard:
    """共享黑板：存放查询分类结果、中间推理、跨 Agent 协作上下文，按 session 进行隔离"""

    def __init__(self):
        # 结构：{session_id: {key: value}}
        self._session_data: dict[str, dict[str, Any]] = {}
        # 结构：{session_id: {key: timestamp}}
        self._session_timestamps: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()  # P1-2: 直接初始化，消除懒初始化竞态

    def _ensure_lock(self):
        """P1-2: Lock 已在 __init__ 中初始化，直接返回"""
        return self._lock

    def _get_session_id(self, session_id: str | None = None) -> str:
        if session_id is not None:
            return session_id
        ctx_sid = _blackboard_session_id.get()
        return ctx_sid if ctx_sid is not None else "default"

    async def write(self, key: str, value: Any, ttl: float | None = None, session_id: str | None = None):
        async with self._ensure_lock():
            sid = self._get_session_id(session_id)
            if sid not in self._session_data:
                self._session_data[sid] = {}
                self._session_timestamps[sid] = {}
            self._session_data[sid][key] = value
            self._session_timestamps[sid][key] = time.time() + (ttl if ttl else float("inf"))
            logger.debug(f"写入: session_id={sid}, key={key}")

    async def read(self, key: str, default: Any = None, session_id: str | None = None) -> Any:
        async with self._ensure_lock():
            sid = self._get_session_id(session_id)
            data = self._session_data.get(sid, {})
            timestamps = self._session_timestamps.get(sid, {})
            ts = timestamps.get(key, 0)
            if ts < time.time():
                data.pop(key, None)
                timestamps.pop(key, None)
                return default
            return data.get(key, default)

    async def read_prefix(self, prefix: str, session_id: str | None = None) -> dict[str, Any]:
        async with self._ensure_lock():
            sid = self._get_session_id(session_id)
            data = self._session_data.get(sid, {})
            timestamps = self._session_timestamps.get(sid, {})
            now = time.time()
            return {
                k: v
                for k, v in data.items()
                if k.startswith(prefix) and timestamps.get(k, float("inf")) > now
            }
