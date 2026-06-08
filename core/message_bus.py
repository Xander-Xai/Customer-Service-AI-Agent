"""
异步消息总线（v3.0）
基于 asyncio.Queue 的发布/订阅系统，支持 Mesh 拓扑的 Agent 间通信
新增：结构化日志
"""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from logger import get_logger

logger = get_logger("core.message_bus")

# 消息日志保留上限，防止内存无限增长
_MESSAGE_LOG_MAXLEN = 1000


class MessageType(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"


@dataclass
class Message:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    msg_type: MessageType = MessageType.BROADCAST
    topic: str = ""
    sender: str = ""
    receiver: str = ""
    payload: Any = None
    timestamp: float = field(default_factory=time.time)
    reply_to: str = ""


Handler = Callable[[Message], Coroutine]


class MessageBus:
    """异步消息总线（Mesh 拓扑，v3.7: asyncio.Lock 保护并发安全）"""

    def __init__(self):
        self._subscribers: dict[str, list[Handler]] = {}
        self._message_log: deque = deque(maxlen=_MESSAGE_LOG_MAXLEN)
        self._lock = asyncio.Lock()  # P1-2: 直接初始化，消除懒初始化竞态

    def _ensure_lock(self) -> asyncio.Lock:
        """P1-2: Lock 已在 __init__ 中初始化，直接返回"""
        return self._lock

    async def subscribe(self, topic: str, handler: Handler):
        async with self._ensure_lock():
            self._subscribers.setdefault(topic, []).append(handler)
            logger.debug(f"订阅: topic={topic}")

    async def unsubscribe(self, topic: str, handler: Handler):
        async with self._ensure_lock():
            if topic in self._subscribers:
                self._subscribers[topic] = [h for h in self._subscribers[topic] if h is not handler]

    async def publish(self, message: Message):
        async with self._ensure_lock():
            self._message_log.append(message)  # v3.8 fix: moved inside lock for atomicity
            handlers = list(self._subscribers.get(message.topic, []))
        if handlers:
            await asyncio.gather(*[h(message) for h in handlers], return_exceptions=True)
