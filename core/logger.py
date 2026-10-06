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
import re
import time
import traceback
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
#: 纯新增键（不改动 timestamp/level/logger/message/module/func/line/trace_id/
#: exception 的既有形状），目的是让生产 JSON 日志可被日志系统**按字段**聚合 ——
#: 尤其是启动失败诊断字段（exception_type / root_exception_type / phase /
#: stage）。没有它们，这些信息只能埋在 message 字符串里，机器无法按根因分组告警。
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

#: 凭据值脱敏规则（``(正则, 替换模板)``），命中即把**值**替换为 ``***``。
#:
#: 为什么这里既不用 ``core/telemetry.py::scrub_attributes`` 的白名单，也不用
#: ``core/hitl/sanitize.py`` 的字段黑名单：那两个都以「结构化字段」为单位
#: （dict 的 key），而 traceback 与异常消息是**一整段文本**。
#:   - 白名单在这里等于把 traceback 整段丢掉 —— 而 traceback 正是启动失败唯一
#:     的证据，丢掉它等于回到 #50 的症状。
#:   - 字段黑名单在这里无处下手（没有 key 可匹配）。
#: 所以这里用「保留结构、脱敏值」的文本语义：帧号、文件路径、函数名、异常类型
#: 全部保留，只有形如 ``password=...`` 的凭据值被替换。
#:
#: 规则刻意保守（宁可漏也不误伤）：只打「键=值」「URL userinfo」「已知前缀
#: 的密钥」「Authorization 头」四类确定是凭据的写法，不做泛化的"看起来像密钥"的
#: 启发式匹配 —— 误改一个正常的 traceback 行比漏掉一个凭据更难排查。
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo： redis://:pw@host:6379 / postgres://user:pw@host/db
    # 用户名允许为空 —— `redis://:pw@host` 是本项目 REDIS_URL 的实际写法，
    # 要求非空用户名的正则会漏掉它（实测漏过）。
    (
        re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@\s]+)@"),
        r"\g<scheme>\g<user>:***@",
    ),
    # Authorization 头：整值替换。必须排在下面的通用键值规则**之前** ——
    # 否则通用规则只吃掉 "Bearer" 这个词，token 本身会留在原地
    # （`Authorization: Bearer <token>` → `Authorization: *** <token>`）。
    (re.compile(r"(?i)\b(authorization|proxy-authorization)(\"?\s*[:=]\s*\"?)?"), r"\1: ***"),
    # 键值对： password=xxx / api_key: xxx / "secret": "xxx"
    (
        re.compile(
            r"(?i)\b(passwo?r?d|passwd|pwd|secret|token|api[_-]?key|apikey"
            r"|access[_-]?key|secret[_-]?key|credential|credentials|auth[_-]?token)"
            r"(\"?\s*[:=]\s*\"?)([^\s,;&)\"']+)"
        ),
        r"\1\2***",
    ),
    # OpenAI 风格密钥前缀（长度阈值 16，避免误伤 "sk-" 这类普通词）
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "sk-***"),
    # Authorization: Bearer <token>（Authorization 规则之外的裸 token 形态）
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 ***"),
)

#: 环境变量名命中任一子串即视为「其值是凭据」。
_SENSITIVE_ENV_NAME_PARTS: tuple[str, ...] = (
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "SESSION_KEY",
)

#: 短于此长度的环境变量值不做整体替换 —— 像 "1" / "true" / "mock" 这类值
#: 会在正常文本里大量出现，替换它们会把日志改得无法阅读，却拦不住任何真实凭据。
_MIN_REDACTABLE_VALUE_LEN = 8


def _sensitive_env_values() -> tuple[str, ...]:
    """当前进程环境里「值本身就是凭据」的变量值，按长度降序。

    正则脱敏只能覆盖**已知书写形式**的凭据（``password=x``、DSN 里的口令）。
    真实故障里凭据常以别的键名出现，或被拼接进一句人话（"用 admin=... 认证失败"）。
    既然进程手里就有这些值的原文，直接把它们**逐个替换掉**比猜测书写形式可靠得多
    —— 键名可以千奇百怪，值不会。
    """
    values = {
        value
        for name, value in os.environ.items()
        if len(value) >= _MIN_REDACTABLE_VALUE_LEN
        and any(part in name.upper() for part in _SENSITIVE_ENV_NAME_PARTS)
    }
    # 长值优先：避免短值是长值子串时先被替换、留下残片
    return tuple(sorted(values, key=len, reverse=True))


def redact_secrets(text: str) -> str:
    """把文本中确定是凭据的部分替换为 ``***``，其余字符原样保留。

    两层，缺一不可：
      1. **本进程已知凭据原文** —— 逐值替换（见 :func:`_sensitive_env_values`）；
      2. **书写形式** —— 上面的几类正则，覆盖枚举不到来源的凭据。

    用于「必须把异常原文（含 traceback）落盘」的场景：可诊断性与不泄漏凭据
    在这里同时成立，而不是二选一。输入非字符串时原样返回，避免在失败路径上
    自身抛异常（那会把真实根因彻底埋掉）。
    """
    if not isinstance(text, str) or not text:
        return text
    for value in _sensitive_env_values():
        if value in text:
            text = text.replace(value, "***")
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_exception_message(exc: BaseException) -> BaseException:
    """就地把异常消息脱敏，返回同一个异常对象（供调用方继续 ``raise``）。

    为什么需要这一步
    --------------
    应用自己的 CRITICAL 记录已经脱敏，但 **ASGI 服务器还会把原始 traceback 再打
    一遍**（uvicorn 的 ``ERROR: Traceback (most recent call last)``）。那条路径由
    服务器的 formatter 渲染，我们无法插手 —— 结果是脱敏形同虚设：我们的日志干净，
    紧接着的服务器日志把口令原样写回 stderr。

    进程马上就要退出，这个异常对象不会再被业务代码读取，所以把 ``args`` 换成
    脱敏文本是安全的：它让**所有**下游打印方（服务器、崩溃报告器、日志收集器）
    只能看到脱敏后的文本。返回原对象而不是新异常，是为了让调用方照常
    ``raise`` 原异常 —— 异常类型与 traceback 形状不变，只换消息。
    """
    try:
        if exc.args and any(isinstance(a, str) for a in exc.args):
            exc.args = tuple(redact_secrets(a) if isinstance(a, str) else a for a in exc.args)
    except Exception:  # noqa: BLE001 - 只读失败路径的 best-effort，绝不能因此二次抛出
        pass
    return exc


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

    没有下层异常时返回自身。链上出现环（``a.__cause__ is b`` 且
    ``b.__cause__ is a``）时返回第一个被重复访问到的节点，而不是死循环 ——
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
) -> None:
    """记录一条「启动失败、进程即将退出」的 CRITICAL 日志，然后**由调用方抛出**。

    为什么需要它（issue #50）
    ----------------------
    lifespan 初始化失败时，进程在服务任何请求之前就退出。取证的窗口只有启动
    阶段的日志 —— 而此前应用**自己**一条记录都不写：唯一能看到的是 ASGI 服务器
    打的 ``Application startup failed. Exiting.``。那一行不属于本项目的 logger
    （无 trace_id、非 JSON 结构、不指名失败阶段、不给处置建议），并且完全取决于
    服务器版本与其日志配置：换成别的服务器、把它自己的 logger 关掉或调高 level，
    这条唯一线索就没了。症状正是「进程 exit 3，日志里什么都没有」。

    契约集中在这里实现，调用点无法各写一版而漏掉其中某一项。每条记录固定携带：

    1. **定位** —— ``event`` / ``phase`` / ``stage``：失败在生命周期的哪一步；
    2. **类型** —— 失败异常与**根因**异常各自的类名与消息（沿 ``__cause__`` 链下钻）；
    3. **证据** —— 完整 traceback；
    4. **动作** —— 调用方给定的 ``remediation``，必须是可核对的具体项。

    **本函数只记录、不处理。** 调用方必须在记录后原样 ``raise``：吞掉异常等于把
    fail-closed 降级成「带病启动」，而带病启动的进程会开始对外服务 —— 那比启动
    失败危险得多，也正是这里要防的事故。

    **刻意不传 ``exc_info``。** logging 的 Formatter 会用**原始** ``exc_info``
    渲染 traceback，那条路径绕过脱敏，异常消息里带的凭据（如连接串口令）就会
    原样落盘。因此这里自己 ``format_exception`` → :func:`redact_secrets` → 拼进
    message，并且**不**再传 ``exc_info``，避免未脱敏的原文被第二次打印。

    同时调用 :func:`scrub_exception_message` 就地脱敏异常本身：ASGI 服务器随后
    还会把原始 traceback 自己打一遍，那条路径不受本函数控制，只能靠异常对象
    本身已经是脱敏的来保证不泄漏。
    """
    root = _root_cause(exc)
    try:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001 - 失败路径上再失败必须被吞掉，否则根因被埋掉
        rendered = f"<traceback unavailable for {type(exc).__name__}>"
    safe_trace = redact_secrets(rendered)
    scrub_exception_message(exc)

    logger.critical(
        "🚨 应用启动失败并将退出 event=%s phase=startup stage=%s | "
        "异常=%s: %s | 根因=%s: %s | traceback(已脱敏):\n%s | 处置建议：%s",
        "lifespan_startup_failed",
        stage,
        type(exc).__name__,
        redact_secrets(str(exc)),
        type(root).__name__,
        redact_secrets(str(root)),
        safe_trace,
        redact_secrets(remediation),
        extra={
            "event": "lifespan_startup_failed",
            "phase": "startup",
            "stage": stage,
            "exception_type": type(exc).__name__,
            "root_exception_type": type(root).__name__,
        },
    )
