"""共享黑板（v3.0）- Agent 间全局可读写状态，带结构化日志"""
import asyncio, time
from typing import Any, Dict, Optional
from logger import get_logger

logger = get_logger("core.blackboard")


class SharedBlackboard:
    """共享黑板：存放查询分类结果、中间推理、跨 Agent 协作上下文"""

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def write(self, key: str, value: Any, ttl: Optional[float] = None):
        async with self._lock:
            self._data[key] = value
            self._timestamps[key] = time.time() + (ttl if ttl else float("inf"))
            logger.debug(f"写入: key={key}")

    async def read(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            ts = self._timestamps.get(key, 0)
            if ts < time.time():
                self._data.pop(key, None)
                self._timestamps.pop(key, None)
                return default
            return self._data.get(key, default)

    async def read_prefix(self, prefix: str) -> Dict[str, Any]:
        async with self._lock:
            now = time.time()
            return {k: v for k, v in self._data.items()
                    if k.startswith(prefix) and self._timestamps.get(k, float("inf")) > now}

    async def delete(self, key: str):
        async with self._lock:
            self._data.pop(key, None)
            self._timestamps.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._data.clear()
            self._timestamps.clear()

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now = time.time()
            expired = [k for k, ts in self._timestamps.items() if ts < now]
            for k in expired:
                self._data.pop(k, None)
                self._timestamps.pop(k, None)
            return len(expired)

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        return {k: v for k, v in self._data.items()
                if self._timestamps.get(k, float("inf")) > now}
