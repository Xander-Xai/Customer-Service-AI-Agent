"""AgentRun 错误分类（transient / permanent / cancelled / timeout）。

用于 worker 决定是否 retry：
  - transient / timeout / lock_backend -> 允许 retry（受 max_attempts 限制）
  - permanent / cancelled -> 不 retry

默认未知异常按 transient 处理，但受 max_attempts 硬上限约束，不会无限重试。
"""

from __future__ import annotations

import asyncio

TRANSIENT = "transient"
PERMANENT = "permanent"
CANCELLED = "cancelled"
TIMEOUT = "timeout"
LOCK_BACKEND = "lock_backend"

RETRYABLE_ERROR_TYPES = frozenset({TRANSIENT, TIMEOUT, LOCK_BACKEND})


class AgentRunError(Exception):
    """AgentRun 执行错误基类。"""

    error_type: str = TRANSIENT


class TransientError(AgentRunError):
    """可重试：网络抖动、429、部分 5xx、临时依赖故障。"""

    error_type = TRANSIENT


class PermanentError(AgentRunError):
    """不可重试：参数错误、权限错误、业务拒绝、4xx 非 transient。"""

    error_type = PERMANENT


class RunCancelledError(AgentRunError):
    """运行被取消。"""

    error_type = CANCELLED


class RunTimeoutError(TransientError):
    """运行超时（可重试）。"""

    error_type = TIMEOUT


class ThreadLockBackendError(TransientError):
    """thread lock 后端不可用（可重试）。"""

    error_type = LOCK_BACKEND


_PERMANENT_BUILTINS = (
    ValueError,
    TypeError,
    KeyError,
    PermissionError,
    FileNotFoundError,
    NotImplementedError,
)

_TRANSIENT_NAMES = frozenset(
    {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "Timeout",
        "ServiceUnavailableError",
        "ConnectError",
        "ReadTimeout",
        "ConnectTimeout",
        "PoolTimeout",
    }
)
_PERMANENT_NAMES = frozenset(
    {
        "AuthenticationError",
        "BadRequestError",
        "NotFoundError",
        "PermissionDeniedError",
        "UnprocessableEntityError",
        "InvalidRequestError",
    }
)


def _http_status(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def classify_exception(exc: BaseException) -> str:
    """返回错误类别字符串。"""
    if isinstance(exc, AgentRunError):
        return exc.error_type
    if isinstance(exc, asyncio.CancelledError):
        return CANCELLED
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return TRANSIENT
    if isinstance(exc, _PERMANENT_BUILTINS):
        return PERMANENT

    status = _http_status(exc)
    if status is not None:
        if status == 429 or status >= 500:
            return TRANSIENT
        if 400 <= status < 500:
            return PERMANENT

    name = type(exc).__name__
    if name in _TRANSIENT_NAMES:
        return TRANSIENT
    if name in _PERMANENT_NAMES:
        return PERMANENT
    return TRANSIENT


def is_retryable(error_type: str) -> bool:
    return error_type in RETRYABLE_ERROR_TYPES


def safe_error_message(exc: BaseException, *, limit: int = 500) -> str:
    """脱敏错误消息：只保留类型与消息，绝不包含凭据。"""
    message = str(exc) or type(exc).__name__
    return message[:limit]
