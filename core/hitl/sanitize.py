"""审批提案的脱敏（sanitized audit record）。

为什么不能用 ``runtime/events.py`` 的 ``sanitize_payload``
--------------------------------------------------------
run 事件流是**白名单**语义：只保留 run_id/status/attempt 这类低基数运维字段，
其余一律丢弃（防止 query 与凭据进入 Redis / SSE）。但审批要留存的恰恰是
``proposal``——审批人必须看到「要退多少、哪个订单号、改成什么」，白名单会把
提案整个抹掉，审批就退化成盲批。

因此这里是**黑名单 + 定长截断**语义：保留业务可判定的字段，把凭据类字段替换
成掩码。既满足留痕可审计，又不让审批表变成凭据泄漏通道。

刻意不做的事：
  - 不做「脱敏后就当安全」——掩码是纵深防御，不是加密。``proposal`` 列不含
    完整卡号/密钥，但含订单号与金额，仍按敏感业务数据对待（表访问受控、
    日志不打印原文）。
  - 不静默改写业务语义：被脱敏的字段保留 key 并标记，审批人能看出「这里原本有值
    但已隐藏」，而不是误以为调用方没传。
"""

from __future__ import annotations

from typing import Any

#: 命中即掩码的字段名（小写子串匹配）。宁可多脱一点：漏掉一个凭据的代价
#: 远高于审批人多看一个 ``***``。
SENSITIVE_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "credential",
        "private_key",
        "authorization",
        "cookie",
        "session_token",
        "cvv",
        "cvc",
        "card_no",
        "card_number",
        "bank_card",
        "id_card",
        "idcard",
        "id_number",
        "passport",
        "ssn",
        "phone",
        "mobile",
        "telephone",
        "email",
    }
)

#: 掩码后写入的值。
REDACTED = "***REDACTED***"

#: 单个字符串值的最大保留长度（业务字段如备注/地址通常远小于此）。
MAX_STRING_LENGTH = 512

#: 嵌套深度上限；超出的子结构整体替换为截断标记，避免深递归与超大载荷。
MAX_DEPTH = 6

#: 字典 / 列表的最大条目数。
MAX_ITEMS = 64


def is_sensitive_key(key: str) -> bool:
    """字段名是否应被脱敏（小写子串匹配）。"""
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        return value[:MAX_STRING_LENGTH] + f"...[+{len(value) - MAX_STRING_LENGTH}chars]"
    return value


def sanitize_proposal(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏任意 JSON-ish 结构，返回可安全持久化的副本。

    - 凭据类 key 的值替换为 ``REDACTED``（保留 key 以便审批人看出有隐藏字段）；
    - 字符串按 ``MAX_STRING_LENGTH`` 截断；
    - 深度超限返回 ``"[TRUNCATED:max_depth]"``，条目超限追加截断标记；
    - 非 JSON 类型退化为 ``repr`` 截断，保证结果始终可序列化。
    """
    if _depth >= MAX_DEPTH:
        return "[TRUNCATED:max_depth]"

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return _clip(value)

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                out["__truncated__"] = f"+{len(value) - MAX_ITEMS} more keys"
                break
            key = str(raw_key)
            if is_sensitive_key(key):
                # 保留 key 与掩码，审批人据此知道「原本有值、已被隐藏」
                out[key] = REDACTED
            else:
                out[key] = sanitize_proposal(raw_value, _depth=_depth + 1)
        return out

    if isinstance(value, list | tuple | set):
        items = list(value)
        clipped = [sanitize_proposal(item, _depth=_depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            clipped.append(f"[TRUNCATED:+{len(items) - MAX_ITEMS} items]")
        return clipped

    return _clip(repr(value))


def sanitize_text(value: str | None, *, limit: int = 2000) -> str | None:
    """审批理由等自由文本的定长截断（``Text`` 列仍需上限，避免日志/响应膨胀）。"""
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[+{len(text) - limit}chars]"


__all__ = [
    "MAX_DEPTH",
    "MAX_ITEMS",
    "MAX_STRING_LENGTH",
    "REDACTED",
    "SENSITIVE_KEY_PARTS",
    "is_sensitive_key",
    "sanitize_proposal",
    "sanitize_text",
]
