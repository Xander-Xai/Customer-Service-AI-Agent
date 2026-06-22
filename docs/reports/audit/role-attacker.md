# 角色审计报告 — 攻击者（Attacker）

> **审计日期**: 2026-06-15
> **审计范围**: 全栈（Python FastAPI + 前端 JS + 配置）
> **发现总数**: 13 个

---

## 发现列表

| 编号 | 严重 | 类别 | 问题描述 | 位置 | 修复建议 |
|------|------|------|----------|------|----------|
| **A-3** | 🔴 P0 | CSP 配置 | `style-src` 指令中 `nonce-{nonce}` 缺少 `f` 前缀，导致 CSP 头中 `style-src` 包含字面量 `'nonce-{nonce}'` 而非实际 nonce 值，CSS 注入防护失效 | `api/middleware.py:168` | 改为 `f"style-src 'self' 'nonce-{nonce}'; "` |
| **A-1** | 🔴 P0 | 认证绕过 | WebSocket `/ws/chat` 认证完全绕过：当 `API_KEY_ENABLED=false` 或 `DEV_MODE=true` 时，`_ws_authenticate` 直接返回而不执行任何认证 | `api/routes/ws.py:63` | 无论环境如何，WS 连接必须强制认证 |
| **A-2** | 🔴 P0 | 认证绕过 | `/api/admin/prompts/*` 管理端点未在 `auth_middleware` 中列入 `admin` 路径白名单，任何 JWT 认证用户均可访问 | `api/middleware.py:216-221` | 添加 `path.startswith("/api/admin/prompts")` 到 `admin` 权限路径 |
| A-4 | 🟠 P1 | 依赖漏洞 | `Jinja2==3.0.3` 和 `MarkupSafe==2.0.1` 版本老旧，存在已知安全漏洞（CVE-2024-22195 等） | `requirements-lock.txt` | 升级至 `Jinja2>=3.1.4` 和 `MarkupSafe>=2.1.5` |
| A-5 | 🟠 P1 | 输入验证 | `ChatRequest.query` 和 `ChatStreamRequest.query` 使用 `max_length=CHAT_QUERY_MAX_LENGTH` 但 `CHAT_QUERY_MAX_LENGTH` 在模块级别定义为 `2000`，未在运行时校验 `MAX_QUERY_LENGTH` 环境变量 | `api/routes/chat.py:34-45` | 在请求处理中动态校验 `MAX_QUERY_LENGTH` 并拒绝超限输入 |
| A-6 | 🟠 P1 | 日志泄露 | `api/routes/ws.py:249` 使用 `exc_info=True` 记录完整堆栈，可能泄露内部路径/实现细节 | `api/routes/ws.py:249` | 生产环境移除 `exc_info=True` 或限制日志级别 |
| A-7 | 🟡 P2 | XSS 防护 | `welcome.js` 中 `QUICK_PROMPTS` 的 `p.title` 和 `p.desc` 直接嵌入 HTML 模板，虽 `setSafeHtml` 使用 DOMParser，但 `data-title` 属性未转义 | `web/src/chat/welcome.js:16` | 对动态属性值使用 `escapeHtml` 转义 |
| A-8 | 🟡 P2 | 认证绕过 | `DEV_MODE=true` 时 `auth_middleware` 完全跳过认证，且 `DEV_MODE` 仅通过 `.env` 文件控制，缺乏二次确认 | `api/middleware.py:241-246` | 增加启动时强制确认 `DEV_MODE` 的日志警告，或要求额外环境变量确认 |
| A-9 | 🟡 P2 | 密码安全 | PBKDF2-SHA256 600K 迭代虽符合 OWASP 最低要求，但建议迁移到 Argon2id 以抵抗 GPU/ASIC 攻击 | `auth/service.py:104` | 评估迁移到 `argon2-cffi` |
| A-10 | 🟡 P2 | 限流绕过 | 内存限流存储 `_rate_limit_store` 在 Redis 不可用时使用内存字典，当 IP 数量超过 `_RATE_LIMIT_STORE_MAX=100_000` 时直接放行，可能导致限流绕过 | `api/middleware.py:144-146` | 超限时应返回 429 而非放行 |
| A-11 | 🟢 P3 | 信息泄露 | `/api/health` 返回 `llm_key_valid` 和 `provider`，泄露 LLM 配置状态 | `api/routes/monitoring.py:127-130` | 移除或模糊化 LLM 配置信息 |
| A-12 | 🟢 P3 | 依赖漏洞 | `openai==2.40.0` 版本较旧，建议关注官方安全公告并定期更新 | `requirements-lock.txt` | 升级到最新稳定版 |
| A-13 | 🟢 P3 | 安全头 | `Strict-Transport-Security` 在开发环境也设置，可能导致 HTTPS 强制跳转问题 | `api/middleware.py:173` | 仅在 HTTPS 启用时设置 HSTS |

---

## 详细分析

### A-3: CSP nonce 未实际应用到 style-src (P0)

**位置**: `api/middleware.py:165-172`

**问题代码**:
```python
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    f"script-src 'self' 'nonce-{nonce}' 'unsafe-hashes'; "
    "style-src 'self' 'nonce-{nonce}'; "  # 缺少 f-string 前缀
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
```

**分析**: `style-src` 行缺少 `f` 前缀，导致 CSP 头中实际值为字面量 `'nonce-{nonce}'`，而非实际的 nonce 值。这意味着所有内联样式都会被浏览器拒绝（因为 nonce 不匹配），或者如果浏览器忽略无效的 nonce 格式，则样式注入防护完全失效。

**修复建议**:
```python
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    f"script-src 'self' 'nonce-{nonce}' 'unsafe-hashes'; "
    f"style-src 'self' 'nonce-{nonce}'; "  # 添加 f-string 前缀
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
```

### A-1: WebSocket 认证完全绕过 (P0)

**位置**: `api/routes/ws.py:63`

**问题代码**:
```python
if not (API_KEY_ENABLED and not DEV_MODE and not ws_api_key):
    return ws_api_key, "", None
```

**攻击路径**:
1. 攻击者连接 `ws://host/ws/chat`
2. 当 `API_KEY_ENABLED=false`（测试环境默认）或 `DEV_MODE=true`（开发环境）时，`_ws_authenticate` 函数在首行即返回，不执行任何 JWT 或 API Key 验证
3. 攻击者可直接发送聊天消息，完全绕过认证

**修复建议**:
```python
# 修复方案：无论环境如何，WS 连接必须强制认证
async def _ws_authenticate(ws: WebSocket, ws_api_key: str) -> tuple:
    if ws_api_key and API_KEY_ENABLED and hmac.compare_digest(ws_api_key, API_KEY):
        return ws_api_key, "", None
    
    # 强制要求 JWT 认证
    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        # ... JWT 验证逻辑
```

### A-2: 管理端点权限绕过 (P0)

**位置**: `api/middleware.py:216-221`

**问题代码**:
```python
elif (
    path.startswith("/api/auth/users")
    or path.startswith("/api/auth/audit")
    or path.startswith("/api/knowledge")
):
    required_auth = "admin"
```

**分析**: `/api/admin/prompts/*` 端点未在 `admin` 路径白名单中，因此任何通过 JWT 认证的用户（包括 `customer` 角色）均可访问这些管理端点。

**修复建议**:
```python
elif (
    path.startswith("/api/auth/users")
    or path.startswith("/api/auth/audit")
    or path.startswith("/api/knowledge")
    or path.startswith("/api/admin/prompts")  # 添加此行
):
    required_auth = "admin"
```

---

## 总结

| 严重程度 | 数量 |
|----------|------|
| 🔴 P0 (Critical) | 3 |
| 🟠 P1 (High) | 3 |
| 🟡 P2 (Medium) | 3 |
| 🟢 P3 (Low) | 3 |
| **总计** | **13** |

**优先修复顺序**: A-3 → A-1 → A-2 → A-4 → A-5 → A-6 → 其余
