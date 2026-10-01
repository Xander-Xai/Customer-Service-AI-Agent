"""Run 执行上下文（contextvars）。

worker 在执行图前设置 ``run_id`` / ``thread_id``，工具层（side-effect 幂等）
通过 contextvar 读取，无需把 run_id 塞进每个函数签名。contextvars 在 asyncio
任务内自动传播。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_run_id: ContextVar[str | None] = ContextVar("agent_run_id", default=None)
_current_thread_id: ContextVar[str | None] = ContextVar("agent_thread_id", default=None)
_current_task_id: ContextVar[str | None] = ContextVar("agent_task_id", default=None)


def set_run_context(
    run_id: str | None, thread_id: str | None = None, task_id: str | None = None
) -> tuple[Token, Token, Token]:
    return (
        _current_run_id.set(run_id),
        _current_thread_id.set(thread_id),
        _current_task_id.set(task_id),
    )


def reset_run_context(tokens: tuple[Token, Token, Token]) -> None:
    run_token, thread_token, task_token = tokens
    _current_run_id.reset(run_token)
    _current_thread_id.reset(thread_token)
    _current_task_id.reset(task_token)


def get_current_run_id() -> str | None:
    return _current_run_id.get()


def get_current_thread_id() -> str | None:
    return _current_thread_id.get()


def get_current_task_id() -> str | None:
    return _current_task_id.get()
