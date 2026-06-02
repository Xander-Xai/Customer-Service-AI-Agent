"""
异步消息总线（v3.0）
基于 asyncio.Queue 的发布/订阅系统，支持 Mesh 拓扑的 Agent 间通信
新增：结构化日志
"""
import asyncio, uuid, time
from typing import Any, Callable, Coroutine, Dict, List
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
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
    """异步消息总线（Mesh 拓扑）"""

    def __init__(self):
        self._subscribers: Dict[str, List[Handler]] = {}
        self._message_log: deque = deque(maxlen=_MESSAGE_LOG_MAXLEN)

    def subscribe(self, topic: str, handler: Handler):
        self._subscribers.setdefault(topic, []).append(handler)
        logger.debug(f"订阅: topic={topic}")

    def unsubscribe(self, topic: str, handler: Handler):
        if topic in self._subscribers:
            self._subscribers[topic] = [h for h in self._subscribers[topic] if h is not handler]

    async def publish(self, message: Message):
        self._message_log.append(message)
        handlers = self._subscribers.get(message.topic, [])
        if handlers:
            await asyncio.gather(*[h(message) for h in handlers], return_exceptions=True)
