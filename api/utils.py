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
from core.config import API_KEY, API_KEY_ENABLED, CORS_ORIGINS, MONITORING_ADMIN_TOKEN

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


#: ``Authorization: Bearer <token>`` 的 scheme 前缀（大小写不敏感）。
_BEARER_PREFIX = "bearer "


def _bearer_token(request) -> str:
    """从 ``Authorization: Bearer <token>`` 取出 token；不是 Bearer 形式返回 ""。"""
    authorization = request.headers.get("Authorization", "")
    if authorization[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return ""
    return authorization[len(_BEARER_PREFIX) :].strip()


def check_admin_token(request) -> bool:
    """检查监控/管理端点的 Admin Token。

    接受两种**等价**的携带形式，都是同一个 ``MONITORING_ADMIN_TOKEN`` 密钥：

    - ``X-Admin-Token: <token>`` —— 浏览器 / curl / 自研采集器的既有形式；
    - ``Authorization: Bearer <token>`` —— Prometheus 的标准抓取凭据形式
      （``bearer_token_file`` / ``authorization.credentials_file`` 只会发这个头）。
      没有它，``monitoring/prometheus.yml`` 无论怎么配都拿不到凭据，
      指标端点对抓取器永远 401 —— 这正是 ``agent_run_dead_letter_total``
      告警链断掉的第二个环节。

    安全边界不变：只比对**同一个**密钥，不接受任何其它凭据，也不放宽 JWT / API Key
    的既有要求。显式给了 ``X-Admin-Token`` 且不匹配时**不再**回退到 Bearer
    （fail-closed：不能用第二种形式绕过第一种形式的显式拒绝）。

    与 ``check_jwt_auth`` 共用 ``Authorization`` 头不冲突：JWT 会被拿去和
    ``MONITORING_ADMIN_TOKEN`` 做常量时间比较，不等即失败，随后由调用方走 JWT 分支。
    """
    if not MONITORING_ADMIN_TOKEN:
        # 未配置 token = 没有监控凭据。绝不因为「没配」就放行。
        return False
    admin_token = request.headers.get("X-Admin-Token", "")
    if admin_token:
        return hmac.compare_digest(admin_token, MONITORING_ADMIN_TOKEN)
    bearer = _bearer_token(request)
    if bearer:
        return hmac.compare_digest(bearer, MONITORING_ADMIN_TOKEN)
    return False


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
