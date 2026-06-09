# 安全策略 (Security Policy)

## 安全架构概述

药妆智多星多智能体客服系统采用**纵深防御**策略，在多层实施安全控制：

### 认证架构

| 认证方式 | 适用场景 | 实现 |
|---------|---------|------|
| API Key (`X-API-Key`) | 系统间调用 | `hmac.compare_digest` 常量时间比较 |
| JWT Bearer Token | 终端用户 | PyJWT (HS256) + jti 黑名单 |
| Admin Token (`X-Admin-Token`) | 监控/管理端点 | 独立密钥，DEV_MODE 不跳过 |

### 输入净化

- **控制字符过滤**：移除 `\x00-\x08\x0b\x0c\x0e-\x1f\x7f`
- **HTML 实体解码 + 标签移除**：防止 `&lt;script&gt;` 绕过
- **Prompt 注入防御**：对话历史包裹「不可信数据边界标记」(`base_agent.py:311`)
- **ERP 注入防护**：白名单方案 + 单引号转义 (`erp/__init__.py`)
- **XSS 防护**：CSP nonce + 前端 `escapeHtml()` + `addEventListener`

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

### 响应安全头

```
Content-Security-Policy: script-src 'self' 'nonce-{random}'; ...
Strict-Transport-Security: max-age=31536000; includeSubDomains
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 0 (CSP 取代)
Permissions-Policy: camera=(), microphone=(), geolocation=()
```

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

## 已知限制

1. **密码哈希**：使用 PBKDF2-SHA256（OWASP 最低推荐），生产环境建议升级到 Argon2id
2. **JWT 实现**：已迁移到 PyJWT 成熟库，算法白名单限制为 HS256
3. **多 Worker 部署**：JWT 黑名单内存回退不支持跨 Worker 同步，生产环境必须配置 Redis
4. **前端 Markdown**：使用正则实现，生产环境建议替换为 DOMPurify + marked

## 报告安全漏洞

如发现安全漏洞，请通过以下方式报告：
- 提交 GitHub Issue（标记 `security` 标签）
- 或发送邮件至项目维护者

请**不要**通过公开 Issue 报告未修复的漏洞。

## 安全相关配置

| 环境变量 | 说明 | 生产建议 |
|---------|------|---------|
| `JWT_SECRET` | JWT 签名密钥 | ≥32 字符随机值 |
| `SESSION_TOKEN_SECRET` | 会话令牌密钥 | ≥32 字符随机值 |
| `API_KEY_ENABLED` | API Key 认证开关 | `true` |
| `DEV_MODE` | 开发模式 | 生产环境自动禁用 |
| `MONITORING_ADMIN_TOKEN` | 管理端点令牌 | 随机强密码 |
| `SESSION_TOKEN_SECRET` | 会话 HMAC 密钥 | 生产必须配置 |
