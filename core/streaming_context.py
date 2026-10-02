"""流式回调的请求级传递（不进 LangGraph checkpoint）。

背景（真实缺陷）：

``stream_callback`` 是请求内的 async 可调用对象，被声明为 ``core/state.py`` 的一个
LangGraph channel。LangGraph 会把**每个 channel 的值**交给 checkpointer 序列化，
于是任何 checkpointer（``MemorySaver`` 与官方 ``AsyncPostgresSaver`` 同样）在写
checkpoint 时都会失败::

    TypeError: Type is not msgpack serializable: function

即 ``POST /api/chat/stream`` 只要开启了 checkpoint 就必然失败——生产默认
``LANGGRAPH_CHECKPOINT_BACKEND=postgres``，所以 SSE 在生产是坏的。

做法：把回调移出 channel，改用 contextvar（请求/任务级），图节点通过
:func:`get_stream_callback` 读取。

- 为什么不丢功能：contextvar 在 ``ainvoke`` 之前设置，LangGraph 内部创建的 asyncio
  task 会**继承**当时的 context，因此节点仍能拿到同一个回调；
  ``asyncio.to_thread`` 也会复制 context。
- 为什么不破坏兼容：``get_stream_callback`` 先看 state（旧调用方式），再回退到
  contextvar。任何仍把回调放进 state 的调用方（测试、脚本）行为不变。

关键约束：state 里**不能**再放函数，否则 checkpoint 写入会再次失败。
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable
from typing import Any

_stream_callback: contextvars.ContextVar[Callable | None] = contextvars.ContextVar(
    "csai_stream_callback", default=None
)


def set_stream_callback(cb: Callable | None) -> contextvars.Token:
    """绑定当前请求/任务的流式回调，返回用于复原的 token。"""
    return _stream_callback.set(cb)


def reset_stream_callback(token: contextvars.Token) -> None:
    with contextlib.suppress(ValueError):
        _stream_callback.reset(token)


def get_stream_callback(state: Any = None) -> Callable | None:
    """取流式回调：state 优先（兼容旧调用），否则取 contextvar。"""
    if isinstance(state, dict):
        cb = state.get("stream_callback")
        if cb is not None:
            return cb
    return _stream_callback.get()
