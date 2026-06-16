# 审计修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复项目审计中发现的 12 个 P0 关键问题 + 31 个 P1 高优先级问题，使项目达到生产就绪标准（总分 ≥ 7.5/10）。

**Architecture:** 按优先级分 5 个阶段实施：P0 安全修复 → P1 安全/质量修复 → P2 代码质量 → P3 优化项 → 最终验收。每个阶段独立可验证。

**Tech Stack:** Python 3.10+ / FastAPI / LangGraph / WebSocket / Prometheus / Redis / JWT / 原生 JS

---

## 阶段 1：P0 关键安全修复（预计 4-6 小时）

### Task 1.1: 修复 CSP `style-src` nonce f-string 格式错误（A-3）

**问题:** `api/middleware.py:168` 的 CSP 头中 `style-src` 使用了普通字符串 `nonce-{nonce}` 而非 f-string，导致 nonce 值未实际注入，CSP 策略失效。

**Files:**
- Modify: `api/middleware.py:168`
- Test: `tests/unit/test_middleware.py`（已有）

- [ ] **Step 1: 定位并修复 CSP 头**

```python
# 当前代码（第165-172行）:
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    f"script-src 'self' 'nonce-{nonce}' 'unsafe-hashes'; "
    "style-src 'self' 'nonce-{nonce}'; "  # ← BUG: 不是 f-string
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
```

修复为：
```python
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    f"script-src 'self' 'nonce-{nonce}' 'unsafe-hashes'; "
    f"style-src 'self' 'nonce-{nonce}'; "  # ← FIX: 添加 f 前缀
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
```

- [ ] **Step 2: 运行相关测试**

Run: `pytest tests/unit/test_middleware.py -v -k csp`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add api/middleware.py
git commit -m "fix(security): 修复 CSP style-src nonce f-string 格式错误

- style-src 指令中的 nonce 使用了普通字符串而非 f-string
- 导致 CSP nonce 值未实际注入，样式加载被浏览器阻止
- 修复后 CSP 策略正确生效"
```

---

### Task 1.2: 修复 WebSocket 认证绕过（A-1）

**问题:** `api/routes/ws.py:63` 中 `_ws_authenticate` 函数的逻辑 `if not (API_KEY_ENABLED and not DEV_MODE and not ws_api_key):` 导致当 `API_KEY_ENABLED=false` 或 `DEV_MODE=true` 时完全跳过认证。

**Files:**
- Modify: `api/routes/ws.py:61-64`
- Test: `tests/unit/test_ws.py`（已有）

- [ ] **Step 1: 重写 WebSocket 认证逻辑**

```python
# 当前代码（第61-64行）:
async def _ws_authenticate(ws: WebSocket, ws_api_key: str) -> tuple:
    """WebSocket 首条消息认证..."""
    if not (API_KEY_ENABLED and not DEV_MODE and not ws_api_key):
        return ws_api_key, "", None
```

修复为：
```python
async def _ws_authenticate(ws: WebSocket, ws_api_key: str) -> tuple:
    """WebSocket 首条消息认证。返回 (ws_api_key, session_token, ws_jwt_payload) 或抛出异常。"""
    # v5.3 安全修复: 不再因 API_KEY_ENABLED=false 或 DEV_MODE=true 跳过认证
    # 始终要求有效认证（API Key 或 JWT），除非已提供有效的 ws_api_key
    if ws_api_key and API_KEY_ENABLED:
        import hmac
        if hmac.compare_digest(ws_api_key, API_KEY):
            return ws_api_key, "", None

    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        msg_api_key = auth_msg.get("api_key", "")
        if msg_api_key and API_KEY_ENABLED:
            import hmac
            if hmac.compare_digest(msg_api_key, API_KEY):
                return msg_api_key, "", None

        ws_jwt = auth_msg.get("token", "")
        session_token = auth_msg.get("session_token", "")
        if not ws_jwt:
            await ws.send_json({"type": "error", "message": "认证失败: 缺少 token 或 api_key"})
            await ws.close(code=4001, reason="Unauthorized")
            raise ValueError("Missing credentials")

        from auth.service import decode_token

        payload = decode_token(ws_jwt)
        if not payload:
            await ws.send_json({"type": "error", "message": "认证失败: 无效的 token"})
            await ws.close(code=4001, reason="Invalid token")
            raise ValueError("Invalid token")

        return ws_api_key, session_token, payload
    except asyncio.TimeoutError:
        await ws.send_json({"type": "error", "message": "认证超时"})
        await ws.close(code=4002, reason="Auth timeout")
        raise
    except ValueError:
        raise
    except Exception as e:
        logger.debug(f"[WS] 认证异常: {e}")
        await ws.send_json({"type": "error", "message": "认证失败"})
        await ws.close(code=4003, reason="Auth error")
        raise
```

- [ ] **Step 2: 运行相关测试**

Run: `pytest tests/unit/test_ws.py -v -k auth`
Expected: PASS（可能需要更新测试用例）

- [ ] **Step 3: Commit**

```bash
git add api/routes/ws.py
git commit -m "fix(security): 修复 WebSocket 认证绕过漏洞

- 移除因 API_KEY_ENABLED=false 或 DEV_MODE=true 导致的认证跳过
- 现在 WebSocket 始终要求有效认证（API Key 或 JWT）
- 防止未认证用户通过 WebSocket 访问系统"
```

---

### Task 1.3: 修复 Admin Prompt 端点权限绕过（A-2）

**问题:** `api/middleware.py:216-221` 的 admin 路径白名单未包含 `/api/admin/prompts/*`，导致任何 JWT 认证用户都能访问 prompt 管理端点。

**Files:**
- Modify: `api/middleware.py:216-221`
- Test: `tests/unit/test_middleware.py`

- [ ] **Step 1: 将 `/api/admin/prompts` 加入 admin 路径保护**

```python
# 当前代码（第216-221行）:
elif (
    path.startswith("/api/auth/users")
    or path.startswith("/api/auth/audit")
    or path.startswith("/api/knowledge")
):
    required_auth = "admin"
```

修复为：
```python
elif (
    path.startswith("/api/auth/users")
    or path.startswith("/api/auth/audit")
    or path.startswith("/api/knowledge")
    or path.startswith("/api/admin/prompts")  # v5.3 安全修复: 添加 admin prompt 端点保护
):
    required_auth = "admin"
```

- [ ] **Step 2: 运行相关测试**

Run: `pytest tests/unit/test_middleware.py -v -k admin`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add api/middleware.py
git commit -m "fix(security): 修复 admin prompt 端点权限绕过

- /api/admin/prompts/* 端点未在 admin 路径白名单中
- 导致任何 JWT 认证用户均可访问 prompt 管理功能
- 已将该路径加入 admin 权限校验"
```

---

### Task 1.4: 修复 user_id 传播链断裂（H-1）

**问题:** `BaseAgent._prepare_llm_messages` 从未设置消息的 `metadata` 字段，`chat.py:215` 未将 JWT user_id 传入 state，导致 `check_quota` 始终使用 `user_id=None`。

**Files:**
- Modify: `api/routes/chat.py:215` 附近（run_graph 调用处）
- Modify: `agents/base_agent.py:390-459`（_prepare_llm_messages）
- Modify: `llm/client.py:149-170`（token quota 检查）
- Test: `tests/unit/test_token_quota.py`

- [ ] **Step 1: 修改 chat.py REST 端点，将 user_id 传入 state**

```python
# 在 api/routes/chat.py 的 rest_chat 函数中（第215行附近）:
# 当前:
result = await run_graph(session.sid, query)

# 修复为：
user_id = extract_user_id(request)
result = await run_graph(session.sid, query, user_id=user_id)
```

- [ ] **Step 2: 修改 BaseAgent._prepare_llm_messages，设置 metadata**

```python
# 在 agents/base_agent.py 的 _prepare_llm_messages 方法中（第390-459行）:
# 在构建 messages 后，添加 metadata 到第一个 HumanMessage:

# 获取 user_id（从 state 传入）
user_id = state.get("user_id")

# 在 HumanMessage 创建后设置 metadata
human_msg = HumanMessage(content=user_content)
if user_id:
    human_msg.metadata = {"user_id": user_id}
messages.append(human_msg)

# 注意：需要修改返回值，将 user_id 也返回或确保 state 中有 user_id
```

更完整的修复方案（修改 _prepare_llm_messages 的签名和调用）：

```python
# 修改 _prepare_llm_messages 方法签名（第390行）:
async def _prepare_llm_messages(
    self, state: dict[str, Any], system_prompt: str, extra_context: str = "", mode: str = "llm"
) -> tuple:
    # ... 现有代码 ...

    # 在构建 HumanMessage 时设置 metadata
    user_id = state.get("user_id")
    human_msg = HumanMessage(content=user_content)
    if user_id:
        human_msg.metadata = {"user_id": user_id}
    messages.append(human_msg)

    return session_id, messages, drift
```

- [ ] **Step 3: 修改 llm/client.py，从消息 metadata 读取 user_id**

当前代码（第158-163行）:
```python
user_id = None
for msg in messages:
    if hasattr(msg, 'metadata') and msg.metadata:
        user_id = msg.metadata.get('user_id')
        break
```

这段代码逻辑正确，但需要确保消息确实设置了 metadata。如果 LangChain 的 HumanMessage 不支持 metadata 属性，需要改用其他方式传递 user_id。

备选方案（在 state 中传递 user_id）：
```python
# 在 llm/client.py 的 async_invoke 中，直接从调用参数获取 user_id:
# 修改签名: async def async_invoke(self, messages, timeout: float | None = None, tools: list | None = None, user_id: str = None):
# 然后在调用时从 state 传入
```

- [ ] **Step 4: 运行相关测试**

Run: `pytest tests/unit/test_token_quota.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/routes/chat.py agents/base_agent.py llm/client.py
git commit -m "fix(security): 修复 user_id 传播链断裂导致 Token Quota 失效

- BaseAgent._prepare_llm_messages 现在正确设置 HumanMessage.metadata.user_id
- chat.py REST 端点将 JWT user_id 传入 run_graph
- llm/client.py 从消息 metadata 读取 user_id 进行配额检查
- 修复后 Token Quota 按用户正确生效"
```

---

### Task 1.5: 修复 Prometheus 指标名不匹配（B-1）+ 告警规则引用不存在指标（B-2）

**问题:** `core/monitoring.py` 中的指标名称与告警规则中的名称不一致，导致告警永远不会触发。

**Files:**
- Modify: `core/monitoring.py`（指标定义）
- Modify: `alerts/` 目录下的告警规则配置
- Test: `tests/unit/test_monitoring.py`

- [ ] **Step 1: 检查当前指标名称和告警规则**

Run: `grep -r "sla_" core/monitoring.py`
Run: `grep -r "metric_name\|alert_rule" alerts/`

- [ ] **Step 2: 统一指标名称**

根据审计报告，需要检查以下指标名称是否一致：
- `core/monitoring.py` 中定义的指标名
- `alerts/` 目录中引用的指标名
- Prometheus 告警规则中的指标名

修复方案（示例，需根据实际文件内容调整）：
```python
# 在 core/monitoring.py 中确保指标名称一致
# 例如，如果使用了 "request_duration_seconds" 但告警规则引用 "http_request_duration_seconds"
# 需要统一为同一个名称
```

- [ ] **Step 3: 运行相关测试**

Run: `pytest tests/unit/test_monitoring.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add core/monitoring.py alerts/
git commit -m "fix(observability): 修复 Prometheus 指标名不匹配导致告警失效

- 统一 core/monitoring.py 中的指标名称与告警规则引用
- 确保告警规则引用的指标确实存在
- 修复后告警系统正确触发"
```

---

## 阶段 2：P1 高优先级修复（预计 8-12 小时）

### Task 2.1: 修复依赖安全漏洞（39 个已知漏洞）

**问题:** 审计发现 39 个已知依赖漏洞，其中 3 个高危（python-jose, starlette, pillow）。

**Files:**
- Modify: `requirements.txt` 或 `pyproject.toml`
- Test: 运行安全扫描

- [ ] **Step 1: 更新高危依赖**

```bash
# 查看当前依赖版本
cat requirements.txt | grep -E "python-jose|starlette|pillow|jinja2|markupsafe"

# 更新到安全版本
pip install --upgrade "python-jose>=3.4.0" "starlette>=0.45.0" "pillow>=11.1.0" "jinja2>=3.1.6" "markupsafe>=2.1.5"
```

- [ ] **Step 2: 更新 requirements.txt**

```bash
pip freeze > requirements.txt
```

- [ ] **Step 3: 运行安全扫描验证**

```bash
pip install safety
safety check -r requirements.txt
```
Expected: 高危漏洞数为 0

- [ ] **Step 4: Commit**

```bash
git add requirements.txt pyproject.toml
git commit -m "fix(security): 修复 39 个已知依赖安全漏洞

- 升级 python-jose, starlette, pillow 等高危依赖
- 升级 jinja2, markupsafe 等中危依赖
- 通过 safety 扫描验证"
```

---

### Task 2.2: 修复会话明文存储（H-2）

**问题:** Session 数据在文件后端（`_save_to_file`）和 Redis 后端均以明文 JSON 存储，存在 PII 泄露风险。

**Files:**
- Modify: `core/session/session_manager.py`（_save_to_file, _create_memory_backend, Redis 保存逻辑）
- Modify: `core/config.py`（添加加密密钥配置）
- Test: `tests/unit/test_session_manager.py`

- [ ] **Step 1: 添加加密工具函数**

在 `core/session/session_manager.py` 中添加：
```python
import base64
from cryptography.fernet import Fernet

def _get_encryption_key() -> bytes:
    """获取加密密钥（从环境变量或生成）"""
    from core.config import SESSION_ENCRYPTION_KEY
    if SESSION_ENCRYPTION_KEY:
        return base64.urlsafe_b64encode(SESSION_ENCRYPTION_KEY.encode()[:32].ljust(32, b'0'))
    return None

def _encrypt_data(data: str, key: bytes) -> str:
    """加密数据"""
    if not key:
        return data
    f = Fernet(key)
    return f.encrypt(data.encode()).decode()

def _decrypt_data(data: str, key: bytes) -> str:
    """解密数据"""
    if not key:
        return data
    f = Fernet(key)
    return f.decrypt(data.encode()).decode()
```

- [ ] **Step 2: 修改文件后端存储，添加加密**

```python
# 修改 _save_to_file 方法:
def _save_to_file(self, session_id: str, messages: list):
    """文件后端持久化（v5.3: 支持加密存储）"""
    if self.storage_backend == "file":
        fp = os.path.join(
            self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json"
        )
        try:
            data = json.dumps(messages, ensure_ascii=False)
            key = _get_encryption_key()
            if key:
                data = _encrypt_data(data, key)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception as e:
            logger.warning(f"文件保存失败: {e}")
```

- [ ] **Step 3: 修改文件后端读取，添加解密**

```python
# 修改 _create_memory_backend 方法:
def _create_memory_backend(self, session_id: str) -> list:
    if self.storage_backend == "file":
        fp = os.path.join(
            self.storage_config.get("storage_dir", "./chat_sessions"), f"{session_id}.json"
        )
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    data = f.read()
                key = _get_encryption_key()
                if key:
                    data = _decrypt_data(data, key)
                return json.loads(data)
            except Exception as e:
                logger.warning(f"加载会话文件失败 session={session_id}: {e}")
    return []
```

- [ ] **Step 4: 修改 Redis 后端存储，添加加密**

```python
# 在 add_message 方法的 Redis 保存逻辑中:
def _redis_save():
    data = json.dumps(session["messages"], ensure_ascii=False)
    key = _get_encryption_key()
    if key:
        data = _encrypt_data(data, key)
    r.setex(
        f"{_CFG_REDIS_PREFIX}{session_id}:messages",
        ttl,
        data,
    )
    # ... meta 同样加密
```

- [ ] **Step 5: 添加环境变量配置**

在 `core/config.py` 中添加：
```python
SESSION_ENCRYPTION_KEY = os.environ.get("SESSION_ENCRYPTION_KEY", "")
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/unit/test_session_manager.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add core/session/session_manager.py core/config.py
git commit -m "fix(security): 加密会话存储防止 PII 泄露

- 文件后端和 Redis 后端均添加 AES 加密
- 支持通过 SESSION_ENCRYPTION_KEY 环境变量配置密钥
- 向后兼容：未配置密钥时保持明文存储"
```

---

### Task 2.3: 修复前端错误键不一致（U-1）

**问题:** API 返回的错误格式不一致，有的用 `{"error": ...}`，有的用 FastAPI 默认的 `{"detail": ...}`，导致前端 `rest.js` 的 `err.detail || err.error` 在某些情况下获取不到错误信息。

**Files:**
- Modify: `api/routes/chat.py`（统一错误格式）
- Modify: `api/middleware.py`（统一错误格式）
- Modify: `web/src/api/rest.js`（增强错误处理）

- [ ] **Step 1: 统一后端错误响应格式**

在 `api/middleware.py` 中，将所有错误响应统一为：
```python
return JSONResponse({"error": "错误消息"}, status_code=xxx)
```

检查并修改以下位置：
- 第124行: `{"error": "登录尝试过于频繁..."}` ✅ 已正确
- 第132行: `{"error": "注册过于频繁..."}` ✅ 已正确
- 第141行: `{"error": "Rate limit exceeded"}` ✅ 已正确
- 第152行: `{"error": "Rate limit exceeded"}` ✅ 已正确
- 第230行: `{"error": "Unauthorized"}` ✅ 已正确
- 第239行: `{"error": "Unauthorized"}` ✅ 已正确
- 第254行: `{"error": "Unauthorized: Invalid API Key or Token"}` ✅ 已正确

检查 `api/routes/chat.py`：
```python
# 第207行:
return JSONResponse({"error": e.detail}, status_code=e.status_code)
# 应改为:
return JSONResponse({"error": e.detail}, status_code=e.status_code)
# ✅ 已正确

# 第212行:
return JSONResponse({"error": "query 不能为空"}, status_code=400)
# ✅ 已正确

# 第232行:
return JSONResponse({"error": "服务内部错误，请稍后重试"}, status_code=500)
# ✅ 已正确
```

- [ ] **Step 2: 增强前端错误处理**

修改 `web/src/api/rest.js`：
```javascript
// 当前（第19-22行）:
const err = await resp.json().catch(() => ({ error: resp.statusText }));
throw new Error(err.detail || err.error || `HTTP ${resp.status}`);

// 修复为：
const err = await resp.json().catch(() => ({ error: resp.statusText }));
throw new Error(err.error || err.detail || err.message || `HTTP ${resp.status}`);
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/unit/test_chat.py -v -k error`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add api/routes/chat.py api/middleware.py web/src/api/rest.js
git commit -m "fix(api): 统一错误响应格式并增强前端错误处理

- 确保所有后端错误响应使用统一的 {error: ...} 格式
- 前端 rest.js 增强错误解析，支持 error/detail/message 多种键
- 修复因错误键不一致导致的静默失败问题"
```

---

### Task 2.4: 修复 Token 过期无用户通知（U-2）

**问题:** `loadSessionList()` 的 catch 块为空，`fetchWithAuth` 在 401 重试失败后直接返回 resp，用户看不到"登录过期"提示。

**Files:**
- Modify: `web/src/auth/index.js`（fetchWithAuth）
- Modify: 使用 loadSessionList 的文件

- [ ] **Step 1: 修改 fetchWithAuth，401 时触发全局事件**

```javascript
// 在 web/src/auth/index.js 的 fetchWithAuth 函数中（第71-90行）:
export async function fetchWithAuth(url, options = {}) {
  const token = localStorage.getItem('token');
  if (token && !options.headers?.Authorization) {
    options.headers = { ...options.headers, Authorization: `Bearer ${token}` };
  }

  let resp = await fetch(url, options);

  if (resp.status === 401 && localStorage.getItem('refresh_token')) {
    const refreshed = await refreshToken();
    if (refreshed) {
      const newToken = localStorage.getItem('token');
      options.headers = { ...options.headers, Authorization: `Bearer ${newToken}` };
      resp = await fetch(url, options);
    } else {
      // 刷新失败，触发全局认证过期事件
      window.dispatchEvent(new CustomEvent('auth:expired', { detail: '登录已过期，请重新登录' }));
      logout();
      return Promise.reject(new Error('登录已过期'));
    }
  }

  return resp;
}
```

- [ ] **Step 2: 在应用入口监听认证过期事件**

在 `web/src/main.js` 或类似入口文件中添加：
```javascript
// 监听认证过期事件
window.addEventListener('auth:expired', (e) => {
  alert(e.detail || '登录已过期，请重新登录');
  window.location.href = '/login.html';
});
```

- [ ] **Step 3: Commit**

```bash
git add web/src/auth/index.js web/src/main.js
git commit -m "fix(ui): 修复 Token 过期无用户通知

- fetchWithAuth 在 401 重试失败后触发全局 auth:expired 事件
- 应用入口监听该事件并显示登录过期提示
- 修复用户看到空会话列表而非登录过期提示的问题"
```

---

### Task 2.5: 修复 PII 数据直接发送到第三方 LLM（H-3）

**问题:** `agents/billing_agent.py:69` 将用户的电话、地址、消费总额等 PII 数据直接发送到第三方 LLM。

**Files:**
- Modify: `agents/billing_agent.py`
- Test: `tests/unit/test_billing_agent.py`

- [ ] **Step 1: 在发送前脱敏 PII 数据**

```python
# 在 agents/billing_agent.py 中，构建发送到 LLM 的上下文前：
def _sanitize_pii(data: dict) -> dict:
    """脱敏敏感个人信息"""
    import copy
    sanitized = copy.deepcopy(data)
    # 脱敏手机号: 138****8888
    if 'phone' in sanitized and sanitized['phone']:
        phone = str(sanitized['phone'])
        if len(phone) >= 7:
            sanitized['phone'] = phone[:3] + '****' + phone[-4:]
    # 脱敏地址: 保留到区/县级别
    if 'address' in sanitized and sanitized['address']:
        address = str(sanitized['address'])
        parts = address.split()
        if len(parts) > 2:
            sanitized['address'] = ' '.join(parts[:2]) + ' ...'
    # 消费总额保留范围不保留精确值
    if 'total_spent' in sanitized and isinstance(sanitized['total_spent'], (int, float)):
        amount = sanitized['total_spent']
        if amount < 1000:
            sanitized['total_spent'] = '< 1,000'
        elif amount < 5000:
            sanitized['total_spent'] = '1,000 - 5,000'
        elif amount < 10000:
            sanitized['total_spent'] = '5,000 - 10,000'
        else:
            sanitized['total_spent'] = '> 10,000'
    return sanitized
```

- [ ] **Step 2: 在调用 LLM 前应用脱敏**

```python
# 在构建 LLM 消息前:
erp_data = await self.erp.query_customer(customer_id)
if erp_data:
    sanitized_data = _sanitize_pii(erp_data)
    extra_context += f"\n\n[客户信息] {sanitized_data}"
```

- [ ] **Step 3: Commit**

```bash
git add agents/billing_agent.py
git commit -m "fix(privacy): 脱敏发送到 LLM 的 PII 数据

- 手机号脱敏为 138****8888 格式
- 地址仅保留到区/县级别
- 消费总额转为范围描述
- 防止用户敏感信息泄露到第三方 LLM"
```

---

## 阶段 3：P2 代码质量修复（预计 6-8 小时）

### Task 3.1: 修复 Ruff lint 错误（30 个）

**Files:**
- Modify: 多个 Python 文件
- Test: `make lint`

- [ ] **Step 1: 运行 Ruff 并查看错误**

```bash
make lint
# 或
ruff check . --output-format=full
```

- [ ] **Step 2: 修复 P0 F821 undefined name**

```bash
# 根据审计报告，core/monitoring.py 中有 F821 错误
ruff check core/monitoring.py --select F821
# 修复未定义的名称
```

- [ ] **Step 3: 修复其余 lint 错误**

```bash
# 自动修复可安全修复的问题
ruff check . --fix

# 手动修复剩余问题
ruff check . --output-format=full
```

- [ ] **Step 4: Commit**

```bash
git add .
git commit -m "style: 修复 Ruff lint 错误

- 修复 F821 undefined name (P0)
- 修复 6 个 P1 级别错误
- 修复 15 个 P2 级别错误
- 修复 6 个 P3 级别错误
- 通过 make lint 验证"
```

---

### Task 3.2: 修复 Biome lint 错误（9 个）

**Files:**
- Modify: 多个 JS 文件
- Test: `cd web && npx biome check .`

- [ ] **Step 1: 运行 Biome 检查**

```bash
cd web && npx biome check .
```

- [ ] **Step 2: 自动修复**

```bash
cd web && npx biome check . --apply
```

- [ ] **Step 3: 手动修复剩余问题**

```bash
cd web && npx biome check . --verbose
```

- [ ] **Step 4: Commit**

```bash
git add web/
git commit -m "style: 修复 Biome lint 错误

- 修复 1 个 P1 级别错误（未使用导入）
- 修复 1 个 P2 级别错误
- 修复 6 个格式化问题
- 移除 console.log 残留"
```

---

### Task 3.3: 清理死代码

**Files:**
- Modify: `core/token_quota.py`（未使用导入）
- Modify: `web/src/chat/messages.js`（未使用 escapeHtml）

- [ ] **Step 1: 移除未使用导入**

```python
# core/token_quota.py 中:
# 移除未使用的导入（如果有的话）
```

- [ ] **Step 2: 移除未使用的 escapeHtml**

```javascript
// web/src/chat/messages.js 中:
// 如果 escapeHtml 未使用，移除其定义或导入
```

- [ ] **Step 3: Commit**

```bash
git add core/token_quota.py web/src/chat/messages.js
git commit -m "refactor: 清理死代码和未使用导入

- 移除 core/token_quota.py 中的未使用导入
- 移除 web/src/chat/messages.js 中的未使用 escapeHtml
- 通过 lint 检查验证"
```

---

### Task 3.4: 添加 exc_info=True 到错误日志

**问题:** 审计发现只有 5 处错误日志使用了 `exc_info=True`，大部分缺少堆栈跟踪。

**Files:**
- Modify: 多个文件中的 logger.error 调用

- [ ] **Step 1: 查找所有缺少 exc_info 的 logger.error**

```bash
grep -rn "logger.error(" --include="*.py" . | grep -v "exc_info" | head -50
```

- [ ] **Step 2: 添加 exc_info=True**

```python
# 将:
logger.error(f"错误描述: {e}")

# 改为:
logger.error(f"错误描述: {e}", exc_info=True)
```

- [ ] **Step 3: Commit**

```bash
git add .
git commit -m "fix(logging): 为错误日志添加 exc_info=True

- 审计发现仅 5 处错误日志包含堆栈跟踪
- 为所有 logger.error 添加 exc_info=True
- 提升故障排查能力"
```

---

## 阶段 4：P3 优化项（预计 4-6 小时）

### Task 4.1: 添加前端 E2E 测试扩展

**Files:**
- Create/Modify: `web/src/__e2e__/*.spec.js`

- [ ] **Step 1: 添加注册流程测试**

```javascript
// web/src/__e2e__/registration.spec.js
describe('注册流程', () => {
  it('应能成功注册新用户', async () => {
    // 实现注册测试
  });
});
```

- [ ] **Step 2: 添加 Token 刷新测试**

```javascript
// web/src/__e2e__/token_refresh.spec.js
describe('Token 刷新', () => {
  it('应在 Token 过期前自动刷新', async () => {
    // 实现 Token 刷新测试
  });
});
```

- [ ] **Step 3: Commit**

```bash
git add web/src/__e2e__/
git commit -m "test(e2e): 添加前端 E2E 测试

- 添加注册流程测试
- 添加 Token 刷新测试
- 添加管理后台导航测试"
```

---

### Task 4.2: 完善文档

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新测试数量**

```bash
# 将 README.md 中的测试数量从 1191+ 更新为实际数量（1342）
```

- [ ] **Step 2: 添加安全修复记录**

```bash
# 在 CHANGELOG 或相关文档中添加安全修复记录
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: 更新文档和测试数量

- 更新 README 中的测试数量（1191+ → 1342）
- 添加安全修复记录"
```

---

## 阶段 5：最终验收（预计 2 小时）

### Task 5.1: 运行全量测试

- [ ] **Step 1: 运行所有测试**

```bash
make test
```
Expected: 1342+ passed, 0 failed

- [ ] **Step 2: 运行 Lint 检查**

```bash
make lint
```
Expected: 0 errors

- [ ] **Step 3: 运行安全扫描**

```bash
safety check -r requirements.txt
```
Expected: 0 high/critical vulnerabilities

- [ ] **Step 4: 运行类型检查**

```bash
mypy . --ignore-missing-imports
```
Expected: 0 errors

---

### Task 5.2: 更新验收清单

- [ ] **Step 1: 检查所有 P0 修复**

| P0 问题 | 状态 | 验证方式 |
|---------|------|---------|
| A-3 CSP nonce f-string | ⬜ | 检查响应头 |
| A-1 WS 认证绕过 | ⬜ | 测试未认证 WS 连接被拒绝 |
| A-2 Admin prompt 绕过 | ⬜ | 测试非 admin 用户 403 |
| H-1 user_id 传播 | ⬜ | 检查 Token Quota 按用户生效 |
| B-1/B-2 指标名不匹配 | ⬜ | 检查告警触发 |

- [ ] **Step 2: 更新 docs/audit/acceptance_checklist.md**

```bash
# 将所有 P0 项标记为已完成
```

- [ ] **Step 3: Commit**

```bash
git add docs/audit/acceptance_checklist.md
git commit -m "docs: 更新验收清单，标记 P0 修复完成"
```

---

## 附录：修复优先级矩阵

### P0（阻断生产）- 必须立即修复

| ID | 问题 | 文件 | 预计时间 | 依赖 |
|----|------|------|---------|------|
| A-3 | CSP style-src f-string | `api/middleware.py:168` | 5 分钟 | 无 |
| A-1 | WS 认证绕过 | `api/routes/ws.py:63` | 30 分钟 | 无 |
| A-2 | Admin prompt 绕过 | `api/middleware.py:216` | 5 分钟 | 无 |
| H-1 | user_id 传播断裂 | `agents/base_agent.py`, `llm/client.py`, `chat.py` | 2 小时 | 无 |
| B-1/B-2 | 指标名不匹配 | `core/monitoring.py`, `alerts/` | 1 小时 | 无 |

### P1（高优先级）- 修复后达到生产标准

| ID | 问题 | 文件 | 预计时间 |
|----|------|------|---------|
| H-3 | PII 发送到 LLM | `agents/billing_agent.py` | 1 小时 |
| H-2 | 会话明文存储 | `core/session/session_manager.py` | 2 小时 |
| U-1 | 错误键不一致 | `api/routes/chat.py`, `web/src/api/rest.js` | 30 分钟 |
| U-2 | Token 过期无通知 | `web/src/auth/index.js` | 30 分钟 |
| - | 39 个依赖漏洞 | `requirements.txt` | 1 小时 |

### P2（中等优先级）- 提升代码质量

| ID | 问题 | 文件 | 预计时间 |
|----|------|------|---------|
| - | Ruff 30 个 lint 错误 | 多个文件 | 2 小时 |
| - | Biome 9 个 lint 错误 | `web/` | 1 小时 |
| - | 死代码清理 | `core/token_quota.py`, `web/src/chat/messages.js` | 30 分钟 |
| - | exc_info 缺失 | 多个文件 | 1 小时 |

### P3（低优先级）- 锦上添花

| ID | 问题 | 文件 | 预计时间 |
|----|------|------|---------|
| - | E2E 测试扩展 | `web/src/__e2e__/` | 2 小时 |
| - | 文档更新 | `README.md`, `CLAUDE.md` | 30 分钟 |

---

## 执行检查清单

- [ ] 阶段 1 完成（P0 安全修复）
- [ ] 阶段 2 完成（P1 高优先级修复）
- [ ] 阶段 3 完成（P2 代码质量）
- [ ] 阶段 4 完成（P3 优化项）
- [ ] 阶段 5 完成（最终验收）
- [ ] 全量测试通过（1342+ passed）
- [ ] Lint 检查通过（0 errors）
- [ ] 安全扫描通过（0 high/critical）
- [ ] 验收清单更新
