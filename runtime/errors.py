"""AgentRun 错误分类（transient / permanent / timeout）。

用于 worker 决定是否 retry：
  - transient / timeout -> 允许 retry（受 max_attempts 限制）；
  - permanent -> 直接 FAILED，不重试。

默认未知异常按 transient 处理，但受 max_attempts 硬上限约束，不会无限重试。
"""

from __future__ import annotations

import asyncio

TRANSIENT = "transient"
PERMANENT = "permanent"
TIMEOUT = "timeout"

RETRYABLE_ERROR_TYPES = frozenset({TRANSIENT, TIMEOUT})


class AgentRunError(Exception):
    """AgentRun 执行错误基类。"""

    error_type: str = TRANSIENT


class TransientError(AgentRunError):
    """可重试：网络抖动、429、部分 5xx、临时依赖故障。"""

    error_type = TRANSIENT


class PermanentError(AgentRunError):
    """不可重试：参数错误、权限错误、业务拒绝、4xx 非 transient。"""

    error_type = PERMANENT


class RunTimeoutError(TransientError):
    """运行超时（可重试）。"""

    error_type = TIMEOUT


class ThreadLockBackendError(TransientError):
    """thread lock 后端不可用（可重试）。"""

    error_type = TRANSIENT


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
    # 工具层超时（``tools.tool_registry.ToolExecutionTimeout``）。按**属性**识别而非
    # isinstance：``tools`` 与 ``runtime`` 是两个独立的包边界，``runtime`` 不应
    # 反向 import ``tools`` 造成单向依赖。它归入 TIMEOUT（可重试）而不是 PERMANENT：
    # 超时意味着外部系统的执行结果**未知**，重试必须经幂等 ledger 去重
    # （``operation_key = run_id:tool_call_id``），而不是直接放行第二次写入。
    if getattr(exc, "is_tool_timeout", False):
        return TIMEOUT
    if isinstance(exc, asyncio.CancelledError):
        return TRANSIENT
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return TIMEOUT
    if isinstance(exc, ConnectionError | OSError):
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
