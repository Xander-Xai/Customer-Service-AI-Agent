"""
统一日志模块（v4.1 — 生产就绪版 + 分布式追踪）
- 文件日志轮转（10MB/文件，保留 5 份）
- JSON 结构化格式（生产环境可选）
- gzip 压缩历史日志
- 同时输出到 stderr + 文件
- 分布式追踪：contextvars 自动注入 trace_id
"""

import contextvars
import gzip
import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

from core.config import LOG_CONFIG

# ===== 分布式追踪上下文变量 =====
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_trace_id(trace_id: str):
    """设置当前请求的 trace_id"""
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    """获取当前请求的 trace_id"""
    return _trace_id_var.get()


class _GzipRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler + gzip 压缩历史日志"""

    def rotate(self, source, dest):
        """压缩已轮转的日志文件"""
        super().rotate(source, dest)
        if os.path.exists(dest) and not dest.endswith(".gz"):
            try:
                with open(dest, "rb") as f_in:
                    compressed = gzip.compress(f_in.read())
                with open(dest + ".gz", "wb") as f_out:
                    f_out.write(compressed)
                os.remove(dest)
            except OSError:
                pass


#: JSON 结构化输出中额外透出的 ``extra`` 字段白名单。
#: 纯新增键（不改 timestamp/level/logger/message/module/func/line/trace_id/
#: exception 的既有形状），让生产 JSON 日志可被日志系统直接按字段查询 ——
#: 尤其是失败诊断字段（exception_type / root_exception_type / phase / stage），
#: 否则它们只能埋在 message 字符串里，机器无法聚合告警。
_EXTRA_JSON_FIELDS = (
    "user_id",
    "session_id",
    "response_time",
    "agent",
    "mode",
    "event",
    "phase",
    "stage",
    "exception_type",
    "root_exception_type",
)


class _JSONFormatter(logging.Formatter):
    """JSON 结构化日志格式器（生产环境）"""

    def format(self, record):
        log_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }

        # 添加 trace_id（如果存在，用于分布式追踪）
        if hasattr(record, "trace_id") and record.trace_id:
            log_data["trace_id"] = record.trace_id

        if record.exc_info and record.exc_info[0]:
            log_data["exception"] = self.formatException(record.exc_info)

        # Business context extraction
        extra_fields = {}
        for attr in _EXTRA_JSON_FIELDS:
            val = getattr(record, attr, None)
            if val is not None:
                extra_fields[attr] = val
        if extra_fields:
            log_data["extra"] = extra_fields

        return json.dumps(log_data, ensure_ascii=False)


class _TraceFilter(logging.Filter):
    """自动注入 trace_id 到每条日志记录。

    仅当记录未携带显式 trace_id（空）时才从 ContextVar 注入，避免覆盖
    调用方通过 extra={"trace_id": ...} 显式提供的值（如安全事件在无请求
    上下文的直接调用场景下回退的 "no-trace"）。P0-03 AC16。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", ""):
            record.trace_id = _trace_id_var.get()
        return True


# 日志目录
_LOG_DIR = os.getenv("LOG_DIR", "logs")


def _setup_file_handler(logger_instance: logging.Logger, level: int):
    """配置文件日志轮转（静默失败：目录不可写时仅 stderr）"""
    try:
        Path(_LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_file = os.path.join(_LOG_DIR, "app.log")

        file_handler = _GzipRotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.addFilter(_TraceFilter())

        # 生产环境使用 JSON 格式，开发环境使用文本格式
        use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"
        file_handler.setFormatter(
            _JSONFormatter()
            if use_json
            else logging.Formatter(
                LOG_CONFIG.get(
                    "format", "%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s"
                )
            )
        )

        logger_instance.addHandler(file_handler)
    except OSError as e:
        # 目录不可写时静默降级，仅输出到 stderr
        logger_instance.warning(f"无法创建日志文件，降级为仅 stderr 输出: {e}")


def get_logger(name: str) -> logging.Logger:
    """
    获取指定模块的 logger（v4.1: 生产就绪 + 分布式追踪）
    - stderr 输出（开发友好）
    - 文件轮转 + gzip 压缩（生产持久化）
    - JSON 结构化（可选，LOG_FORMAT=json 启用）
    - 自动注入 trace_id（分布式追踪）
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        level = getattr(logging, LOG_CONFIG.get("level", "INFO").upper(), logging.INFO)
        logger.setLevel(level)

        # 分布式追踪过滤器（自动注入 trace_id）
        trace_filter = _TraceFilter()
        logger.addFilter(trace_filter)

        # 1) stderr 输出（始终启用）
        stderr_handler = logging.StreamHandler()
        stderr_handler.setLevel(level)
        stderr_handler.addFilter(trace_filter)
        stderr_handler.setFormatter(
            logging.Formatter(
                LOG_CONFIG.get(
                    "format", "%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s"
                )
            )
        )
        logger.addHandler(stderr_handler)

        # 2) 文件轮转 + gzip（生产环境）
        _setup_file_handler(logger, level)

        logger.propagate = False
    return logger


# ===== 进程启动失败的可诊断日志契约 =====


def _root_cause(exc: BaseException) -> BaseException:
    """返回异常链尾端的根因异常（沿 ``__cause__`` / ``__context__`` 下钻）。

    没有下层异常时返回自身；链上出现环（``a.__cause__ is b`` 且
    ``b.__cause__ is a``）时返回第一个被重复访问到的节点而不是死循环 ——
    本函数只在失败路径上被调用，它自己抛异常会把真实根因彻底掩盖。
    """
    seen = {id(exc)}
    current = exc
    while True:
        nxt = current.__cause__ or current.__context__
        if nxt is None or id(nxt) in seen:
            return current
        seen.add(id(nxt))
        current = nxt


def log_startup_failure(
    stage: str,
    exc: BaseException,
    remediation: str,
    *,
    logger: logging.Logger,
    phase: str = "startup",
) -> None:
    """记录一条"启动/初始化失败、进程即将退出"的 CRITICAL 日志。

    进程启动失败时，取证窗口只有这一条日志 —— 日志不落地等于故障不可诊断。
    因此契约集中在这里实现，调用点无法各写一版而漏掉其中某一项。每条记录
    固定携带四类信息：

    1. **定位**：``event`` / ``phase`` / ``stage``（失败在生命周期的哪一步）；
    2. **类型**：失败异常与**根因**异常各自的类名与消息（沿 ``__cause__`` 链）；
    3. **证据**：完整 traceback —— 用显式 ``exc_info`` 三元组而非 ``True``，
       因此即使调用点已跳出 ``except`` 块也能拿到真实 traceback；
    4. **动作**：调用方给定的 ``remediation`` 处置建议原文。

    **本函数只记录、不处理。** 调用方必须在记录后原样 ``raise``；吞掉异常
    等于把 fail-closed 降级成"带病启动"，那正是这里要防的事故。
    """
    root = _root_cause(exc)
    event = "lifespan_startup_failed" if phase == "startup" else "lifespan_shutdown_failed"
    logger.critical(
        "🚨 应用生命周期 %s 失败并将退出 event=%s stage=%s exc=%s: %s | 根因=%s: %s | 处置建议：%s",
        phase,
        event,
        stage,
        type(exc).__name__,
        exc,
        type(root).__name__,
        root,
        remediation,
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={
            "event": event,
            "phase": phase,
            "stage": stage,
            "exception_type": type(exc).__name__,
            "root_exception_type": type(root).__name__,
        },
    )
