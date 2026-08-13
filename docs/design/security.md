# 安全策略 (Security Policy)

## 安全架构概述

药妆智多星多智能体客服系统采用**纵深防御**策略，在多层实施安全控制。

### 当前实现状态

| 层级 | 安全措施 | 状态 |
|------|---------|------|
| **认证** | API Key + JWT 双模式，Admin Token 独立密钥 | ✅ 已实现 |
| **密码** | Argon2id（OWASP 2023 推荐），降级回退 PBKDF2-SHA256 600K 迭代 | ✅ 已实现 |
| **JWT** | PyJWT HS256 + 算法白名单 + jti 吊销 + Redis 黑名单 + Refresh Token | ✅ 已实现 |
| **CSRF** | 双重 Cookie 提交模式，hmac.compare_digest 比较 | ✅ 已实现 |
| **限流** | 通用 60/min/IP + 登录 5次/5min + 注册 3次/h + Redis 滑动窗口 | ✅ 已实现 |
| **输入净化** | 控制字符过滤 + HTML 标签移除 + HTML 实体解码防绕过 | ✅ 已实现 |
| **注入防护** | ERP 白名单消毒 + 对话历史 `<untrusted-data>` 隔离 + 输出层泄露检测 | ✅ 已实现 |
| **错误脱敏** | 工具执行错误返回通用消息，详细异常仅写服务端日志 | ✅ 已实现 |
| **安全头** | HSTS + X-Frame-Options + X-Content-Type-Options + Referrer-Policy + Permissions-Policy | ✅ 已实现 |
| **CSP** | script-src nonce（无 unsafe-inline），style-src nonce（v5.4 修复） | ✅ 已实现 |
| **前端 XSS** | DOMPurify 净化 LLM 输出 + escapeHtml 净化动态数据 + 4 处 innerHTML 已审计（2 注释 + 2 widget） | ✅ 已实现 |
| **WebSocket** | API Key（query/header/message）优先 + JWT 首条消息认证 + 每 IP 连接限制 + 消息限流 + 空闲超时 | ✅ 已实现 |
| **会话安全** | UUID 格式校验 + HMAC 会话令牌签名 + 用户级会话所有权隔离 + 黑板 ContextVar 隔离 | ✅ 已实现 |
| **SSRF 防护** | 告警 Webhook URL 验证：阻止私有 IP / 回环 / 链路本地 / 元数据端点 | ✅ 已实现 |
| **会话数据加密** | AES-256-Fernet 可选加密（`SESSION_ENCRYPTION_KEY`），未配置时明文存储 | ✅ 已实现 |
| **启动校验** | 生产环境强制校验 JWT_SECRET(≥32字符) / SESSION_TOKEN_SECRET / API_KEY | ✅ 已实现 |
| **密钥管理** | scripts/generate_prod_env.py 使用 secrets 模块生成密码学安全随机密钥 | ✅ 已实现 |

### 认证架构

| 认证方式 | 适用场景 | 实现 |
|---------|---------|------|
| API Key (`X-API-Key`) | 系统间调用 | `hmac.compare_digest` 常量时间比较 |
| JWT Bearer Token | 终端用户 | PyJWT (HS256) + jti 黑名单 + Refresh Token |
| Admin Token (`X-Admin-Token`) | 监控/管理端点 | 独立密钥，DEV_MODE 不跳过 |

### 基于角色的访问控制（RBAC）

4 级 RBAC 角色，权限从 `api/middleware/__init__.py` 的 `ROLE_PERMISSIONS` 映射：

| 角色 | 权限范围 |
|------|---------|
| **customer** | 聊天、自有会话管理、反馈 |
| **agent** | 聊天、全部会话查看、反馈 |
| **supervisor** | 聊天 + 全部会话 + 反馈 + 监控仪表盘 + 告警管理 |
| **admin** | 全部权限（用户管理、知识库管理、系统配置、监控、告警）|

路由级保护通过 `auth_middleware` 中间件实现：管理端点（`/api/knowledge`、`/api/admin/prompts`、`/api/auth/users`）仅 `admin` 可访问；监控端点（`/api/metrics`、`/api/circuit-breaker`、`/api/alerts`）允许 `supervisor` 及以上角色。JWT payload 的 `role` 字段在登录时写入，中间件通过 `check_jwt_auth` 解码后校验。

### 密码哈希（v5.4 升级）

v5.4 从 PBKDF2-SHA256 升级到 **Argon2id**（OWASP 2023 推荐标准）：

| 特性 | Argon2id | PBKDF2-SHA256（旧） |
|------|----------|---------------------|
| 标准 | OWASP 2023 推荐 | OWASP 2017 最低标准 |
| 抗 GPU/ASIC | ✅ 内存硬度 64MB | ❌ 纯计算，GPU 可并行 |
| 配置 | time_cost=3, memory_cost=65536, parallelism=4 | 600,000 迭代 |
| 迁移策略 | 新用户用 Argon2id；旧用户登录验证后自动重哈希 | 向后兼容验证 |

降级机制：`argon2-cffi` 未安装时自动回退到 PBKDF2-SHA256。

```python
# auth/service.py — 双格式验证
if password_hash.startswith("$argon2"):
    # Argon2id 验证（新格式）
    ph.verify(password_hash, password)
    # PBKDF2 旧格式验证成功后，标记需要迁移
else:
    # PBKDF2-SHA256 验证（向后兼容）
    # 验证成功后自动重新哈希为 Argon2id
```

### CSP 配置详情

```python
# api/middleware/__init__.py — security_headers 中间件
Content-Security-Policy:
  default-src 'self';
  script-src 'self' 'nonce-{random}';
  style-src 'self' 'unsafe-inline';     # 动态样式需要，见已知限制
  connect-src 'self';
  img-src 'self' data: blob:;
  frame-ancestors 'none'                # widget.html 动态切换为 'self'（v6.3）
```

**script-src**：使用 nonce，无 `unsafe-inline`，每次请求生成随机 nonce。
**style-src**：当前仍使用 `unsafe-inline`（主题切换和动态样式注入需要）。理想方案是迁移到 CSS 自定义属性，但内联样式较分散，优先级较低。通过 CSP 报告收集违规情况，逐步迁移。
**frame-ancestors**（v6.3）：动态切换 — 普通页面为 `'none'` 阻止 iframe 嵌入；`widget.html` 页面为 `'self'` 允许同源 iframe 嵌入，支撑三方网站 widget 部署。

### 前端 innerHTML 安全审计

4 处 innerHTML 使用审计结果（2026-06-17 复审）：

| 分类 | 数量 | 说明 |
|------|------|------|
| 注释中提及（未实际使用） | 2 | `dom.js` 和 `auth/index.js` 中的注释 |
| Widget HTML 插入 | 2 | `widget.html` 中的 `innerHTML` 赋值，内容受 `renderMarkdown` → `DOMPurify.sanitize()` 保护 |

LLM 输出处理链路：`marked.parse()` → `DOMPurify.sanitize()` → 插入 DOM

### 速率限制

| 端点 | 限制 | 窗口 |
|------|------|------|
| 通用 API | 60 次/IP | 1 分钟 |
| 登录 `/api/auth/login` | 5 次/IP | 5 分钟 |
| 注册 `/api/auth/register` | 3 次/IP | 1 小时 |
| WebSocket 消息 | 10 条/连接 | 1 分钟 |

实现：Redis 滑动窗口优先，内存降级回退。

### WebSocket 安全

- 每 IP 连接数限制（`WS_MAX_CONNECTIONS_PER_IP=5`）
- 空闲超时断开（`WS_IDLE_TIMEOUT=300s`）
- 认证方式：API Key（query/header/message）优先，否则 JWT 首条消息 + session_token
- 消息限流（`WS_MESSAGE_RATE_LIMIT=10` 条/分钟）
- 会话所有权令牌（HMAC-SHA256）

### LLM 安全

- **Prompt 注入防护**：用户输入标记为不可信数据，与系统提示隔离
- **响应清洗**：移除 LLM 输出中的调试代码、注入泄露、XSS 内容
- **熔断器保护**：连续 5 次失败后降级到规则引擎
- **Token Quota**：用户级 Token 消耗限额（每日/每月），Redis 持久化 + 内存回退
- **会话数据加密**：AES-256-Fernet 可选加密（`SESSION_ENCRYPTION_KEY`），文件后端自动加解密

### 基础设施安全

- **Docker**：多阶段构建、非 root 用户 (`appuser`)、资源限制
- **密码哈希**：Argon2id（v5.4 升级，OWASP 2023 推荐），降级回退 PBKDF2-SHA256
- **密钥管理**：`.env` 文件不入版本控制，生产环境使用 `.env.prod.generated`
- **API Key 校验** (v5.5)：检测 `test-`/`mock-`/`sk-placeholder` 等非生产 Key 前缀 + 长度 < 40 判定无效，开发模式自动降级到规则引擎；阻止测试 Key 绕过 LLM 降级路径
- **审计日志**：认证事件（登录/注册）记录 IP 和时间戳到 `AuditLog` 表
- **SSRF 防护**：Webhook URL 校验私网/回环/链路本地地址
- **黑板隔离**：ContextVar 按 session 隔离 Agent 间共享数据（v5.3 新增）

---

## 已知限制与改进计划

| 限制 | 当前状态 | 改进方案 | 优先级 |
|------|---------|---------|--------|
| ~~style-src 使用 unsafe-inline~~ | ~~前端 65 处内联样式~~ | ~~迁移内联样式到 CSS 类或使用 style nonce~~ | ✅ v5.4 已完成 |
| ~~密码哈希使用 PBKDF2~~ | ~~OWASP 最低推荐~~ | ~~升级到 Argon2id~~ | ✅ v5.4 已完成 |
| 同步 JWT decode 无法检查 Redis 黑名单 | 已文档化 | 迁移到全异步认证路径 | P1 |
| ERP 集成为 Mock 模式 | 接口抽象已完成 | 真实 ERP 对接需企业配合 | P3 |
| DEV_MODE 跳过所有认证 | 仅限开发环境 | 生产环境自动禁用（已实现启动校验） | — |

---

## 安全相关配置

| 环境变量 | 说明 | 生产建议 |
|---------|------|---------|
| `JWT_SECRET` | JWT 签名密钥 | ≥32 字符随机值 |
| `SESSION_TOKEN_SECRET` | 会话令牌密钥 | ≥32 字符随机值 |
| `SESSION_ENCRYPTION_KEY` | 会话数据加密密钥（可选） | 32 字节 base64 编码 |
| `API_KEY_ENABLED` | API Key 认证开关 | `true` |
| `DEV_MODE` | 开发模式 | 生产环境自动禁用 |
| `MONITORING_ADMIN_TOKEN` | 管理端点令牌 | 随机强密码 |

---

## 报告安全漏洞

如发现安全漏洞，请通过以下方式报告：
- 提交 GitHub Issue（标记 `security` 标签）
- 或发送邮件至项目维护者

请**不要**通过公开 Issue 报告未修复的漏洞。
