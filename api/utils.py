"""
API 共享工具函数：输入净化、认证辅助、CORS 配置
从 api/app.py 提取，供路由模块和中间件共同使用。
"""

import hmac
import json
import os
import re
import uuid

from auth.service import decode_token as _decode_jwt_token
from config import API_KEY, API_KEY_ENABLED, CORS_ORIGINS, MONITORING_ADMIN_TOKEN

# ── 输入净化 ──
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_input(text: str) -> str:
    """净化用户输入：移除控制字符和 HTML 标签（v4.0: 增加 HTML 实体解码防 XSS）"""
    import html as _html_mod

    text = _CONTROL_CHAR_RE.sub("", text)
    text = _html_mod.unescape(text)
    text = _HTML_TAG_RE.sub("", text)
    return text.strip()


# ── Session ID 校验 ──
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_session_id(sid: str) -> str:
    """校验并清理 session_id，不合法则自动生成 UUID"""
    if sid and _SESSION_ID_RE.match(sid):
        return sid
    return str(uuid.uuid4())


# ── 认证辅助函数 ──


def check_api_key(request) -> bool:
    """检查 API Key 认证"""
    api_key = request.headers.get("X-API-Key", "")
    return API_KEY_ENABLED and API_KEY and hmac.compare_digest(api_key, API_KEY)


def check_jwt_auth(request) -> dict | None:
    """检查 JWT 认证，返回 payload 或 None"""
    auth_header = request.headers.get("Authorization", "")
    jwt_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if jwt_token:
        return _decode_jwt_token(jwt_token)
    return None


def extract_user_id(request) -> str | None:
    """从请求中提取当前用户 ID（JWT sub 字段）"""
    payload = check_jwt_auth(request)
    if payload:
        return payload.get("sub")
    return None


def check_admin_token(request) -> bool:
    """检查 Admin Token"""
    admin_token = request.headers.get("X-Admin-Token", "")
    return bool(MONITORING_ADMIN_TOKEN and hmac.compare_digest(admin_token, MONITORING_ADMIN_TOKEN))


def is_authenticated(request) -> bool:
    """综合认证检查：API Key 或 JWT"""
    return check_api_key(request) or bool(check_jwt_auth(request))


# ── CORS 配置 ──


def resolve_cors_origins() -> list:
    """优先从 ALLOWED_ORIGINS 环境变量读取，回退到 config.py 的 CORS_ORIGINS。"""
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    if raw:
        raw = raw.strip()
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if origins:
            return origins
    return CORS_ORIGINS
