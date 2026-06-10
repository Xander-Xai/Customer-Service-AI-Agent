# 安全策略 (Security Policy)

## 安全架构概述

药妆智多星多智能体客服系统采用**纵深防御**策略，在多层实施安全控制。

### 当前实现状态

| 层级 | 安全措施 | 状态 |
|------|---------|------|
| **认证** | API Key + JWT 双模式，Admin Token 独立密钥 | ✅ 已实现 |
| **密码** | PBKDF2-SHA256, 600K 迭代，hmac.compare_digest 常量时间比较 | ✅ 已实现 |
| **JWT** | PyJWT HS256 + 算法白名单 + jti 吊销 + Redis 黑名单 + Refresh Token | ✅ 已实现 |
| **CSRF** | 双重 Cookie 提交模式，hmac.compare_digest 比较 | ✅ 已实现 |
| **限流** | 通用 60/min/IP + 登录 5次/5min + 注册 3次/h + Redis 滑动窗口 | ✅ 已实现 |
| **输入净化** | 控制字符过滤 + HTML 标签移除 + HTML 实体解码防绕过 | ✅ 已实现 |
| **注入防护** | ERP 白名单消毒 + 对话历史 `<untrusted-data>` 隔离 + 输出层泄露检测 | ✅ 已实现 |
| **错误脱敏** | 工具执行错误返回通用消息，详细异常仅写服务端日志 | ✅ 已实现 |
| **安全头** | HSTS + X-Frame-Options + X-Content-Type-Options + Referrer-Policy + Permissions-Policy | ✅ 已实现 |
| **CSP** | script-src nonce（无 unsafe-inline），style-src unsafe-inline（已知限制） | ⚠️ 部分 |
| **前端 XSS** | DOMPurify 净化 LLM 输出 + escapeHtml 净化动态数据 + 53/54 处 innerHTML 已审计安全 | ✅ 已实现 |
| **WebSocket** | 首条消息 JWT 认证 + 每 IP 连接限制 + 消息限流 + 空闲超时 | ✅ 已实现 |
| **会话安全** | UUID 格式校验 + HMAC 会话令牌签名 + 用户级会话所有权隔离 | ✅ 已实现 |
| **SSRF 防护** | 告警 Webhook URL 验证：阻止私有 IP / 回环 / 链路本地 / 元数据端点 | ✅ 已实现 |
| **启动校验** | 生产环境强制校验 JWT_SECRET(≥32字符) / SESSION_TOKEN_SECRET / API_KEY | ✅ 已实现 |
| **密钥管理** | scripts/generate_prod_env.py 使用 secrets 模块生成密码学安全随机密钥 | ✅ 已实现 |

### 认证架构

| 认证方式 | 适用场景 | 实现 |
|---------|---------|------|
| API Key (`X-API-Key`) | 系统间调用 | `hmac.compare_digest` 常量时间比较 |
| JWT Bearer Token | 终端用户 | PyJWT (HS256) + jti 黑名单 |
| Admin Token (`X-Admin-Token`) | 监控/管理端点 | 独立密钥，DEV_MODE 不跳过 |

### CSP 配置详情

```python
# api/middleware.py:146-153
Content-Security-Policy:
  default-src 'self';
  script-src 'self' 'nonce-{random}' 'unsafe-hashes';
  style-src 'self' 'unsafe-inline';    # ← 已知限制
  connect-src 'self';
  img-src 'self' data:;
  frame-ancestors 'none'
```

**script-src**：使用 nonce，无 `unsafe-inline`，每次请求生成随机 nonce。
**style-src**：使用 `unsafe-inline`，原因是前端 65 处内联 `style=` 属性和 32 处 `.style.` 赋值。移除非线样式需要大规模重构。

### 前端 innerHTML 安全审计

54 处 innerHTML 使用审计结果（2026-06-10）：

| 分类 | 数量 | 说明 |
|------|------|------|
| 纯静态模板（无动态数据） | ~20 | 安全，无需处理 |
| 动态数据经 escapeHtml 净化 | ~30 | 安全，已有保护 |
| LLM 输出经 DOMPurify 净化 | 2 | 安全，renderMarkdown → DOMPurify.sanitize() |
| **已修复** | 1 | main.js voice ID 已添加 escapeHtml |

LLM 输出处理链路：`marked.parse()` → `DOMPurify.sanitize()` → 插入 DOM

### 速率限制

| 端点 | 限制 | 窗口 |
|------|------|------|
| 通用 API | 60 次/IP | 1 分钟 |
| 登录 `/api/auth/login` | 5 次/IP | 5 分钟 |
| 注册 `/api/auth/register` | 3 次/IP | 1 小时 |
| WebSocket 消息 | 50 条/连接 | 1 分钟 |

实现：Redis 滑动窗口优先，内存降级回退。

### WebSocket 安全

- 每 IP 连接数限制（`WS_MAX_CONNECTIONS_PER_IP`）
- 空闲超时断开（`WS_IDLE_TIMEOUT`）
- JWT 认证通过首条消息传递（非 URL 参数）
- 会话所有权令牌（HMAC-SHA256）

### LLM 安全

- **Prompt 注入防护**：用户输入标记为不可信数据，与系统提示隔离
- **响应清洗**：移除 LLM 输出中的调试代码、注入泄露、XSS 内容
- **熔断器保护**：连续 5 次失败后降级到规则引擎
- **Token 预算**：会话级 token 上限防止滥用

### 基础设施安全

- **Docker**：多阶段构建、非 root 用户 (`appuser`)、资源限制
- **密码哈希**：PBKDF2-SHA256, 600,000 次迭代 (OWASP 推荐)
- **密钥管理**：`.env` 文件不入版本控制，生产环境使用 `.env.prod.generated`
- **审计日志**：认证事件（登录/注册）记录 IP 和时间戳到 `AuditLog` 表
- **SSRF 防护**：Webhook URL 校验私网/回环/链路本地地址

---

## 已知限制与改进计划

| 限制 | 当前状态 | 改进方案 | 优先级 |
|------|---------|---------|--------|
| style-src 使用 unsafe-inline | 前端 65 处内联样式 | 迁移内联样式到 CSS 类或使用 style nonce | P2 |
| 密码哈希使用 PBKDF2 | OWASP 最低推荐 | 升级到 Argon2id | P2 |
| 同步 JWT decode 无法检查 Redis 黑名单 | 已文档化 | 迁移到全异步认证路径 | P1 |
| ERP 集成为 Mock 模式 | 接口抽象已完成 | 真实 ERP 对接需企业配合 | P3 |
| DEV_MODE 跳过所有认证 | 仅限开发环境 | 生产环境自动禁用（已实现启动校验） | — |

---

## 安全相关配置

| 环境变量 | 说明 | 生产建议 |
|---------|------|---------|
| `JWT_SECRET` | JWT 签名密钥 | ≥32 字符随机值 |
| `SESSION_TOKEN_SECRET` | 会话令牌密钥 | ≥32 字符随机值 |
| `API_KEY_ENABLED` | API Key 认证开关 | `true` |
| `DEV_MODE` | 开发模式 | 生产环境自动禁用 |
| `MONITORING_ADMIN_TOKEN` | 管理端点令牌 | 随机强密码 |

---

## 报告安全漏洞

如发现安全漏洞，请通过以下方式报告：
- 提交 GitHub Issue（标记 `security` 标签）
- 或发送邮件至项目维护者

请**不要**通过公开 Issue 报告未修复的漏洞。
