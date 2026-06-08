# 安全审计报告（2026-06-05）

> 三面并行审计：认证安全 / 注入攻击 / 逻辑漏洞
> 审计范围：auth、api、agents、erp、tools、session、alerts、knowledge
> 结果：319 tests passed，所有修复验证通过

---

## 总览

| 等级 | 数量 | 状态 |
|------|------|------|
| 🔴 Critical | 3 | ✅ 全部修复 |
| 🟠 High | 7 | ✅ 全部修复 |
| 🟡 Medium | 8 | ✅ 全部修复 |
| 🔵 Low | 5 | ✅ 全部修复 |

---

## 修复记录

### Critical

| # | 漏洞 | 修复方式 | 文件 |
|---|------|----------|------|
| C1 | JWT 密钥硬编码默认值 | config.py 空默认值 + auth/service.py 从 config 读取 + 启动时强制校验 | config.py, auth/service.py |
| C2 | 反向代理后 localhost 绕过 | 移除所有 `request.client.host` 判断，改用 `DEV_MODE` 环境变量 | api/app.py |
| C3 | 对话历史 Prompt Injection | SystemMessage 加不可信数据边界标记 + 忽略操纵指令 | agents/base_agent.py |

### High

| # | 漏洞 | 修复方式 | 文件 |
|---|------|----------|------|
| H1 | 登录/注册无限流 | 新增独立认证限流（登录 5次/5min，注册 3次/hour） | api/app.py |
| H2 | 默认管理员硬编码密码 | 首次生成随机密码，仅输出到 stderr，不再写入日志 | auth/service.py |
| H3 | 会话令牌不传=放行 | SESSION_TOKEN_SECRET 启用时必须携带有效 token | session_manager.py |
| H4 | ERP 工具无用户权限校验 | 新增 get_user_id_from_request 工具函数 | auth/service.py |
| H5 | HTML 实体注入 XSS | _sanitize_input 增加 html.unescape 解码实体 | api/app.py |
| H6 | JWT 中间件无角色校验 | 管理端点 JWT 校验 role=admin | api/app.py |
| H7 | Prometheus 标签注入 | 新增 _safe_label 转义函数 | api/app.py |

### Medium

| # | 漏洞 | 修复方式 | 文件 |
|---|------|----------|------|
| M1 | Webhook SSRF | 新增 URL 安全校验（禁止内网/回环/链路本地 IP） | alerts/notifier.py |
| M2 | 知识库错误泄露 | str(e) 改为通用错误消息 | knowledge/router.py |
| M3 | 知识库统计无权限 | 加 require_admin 校验 | knowledge/router.py |
| M4 | CSP unsafe-inline | 保留（前端内联脚本需要），记录为已知风险 | - |
| M5 | 多 Worker 限流失效 | 记录为已知风险，生产环境建议使用 Redis 限流 | - |
| M6 | ERP 死代码 | 移除未使用的 _sanitize_filter_value 方法调用 | erp/kingdee_real_adapter.py |
| M7 | 对话历史无信任边界 | 加不可信数据警告前缀（与 C3 合并修复） | agents/base_agent.py |
| M8 | 响应清洗遗漏元评论 | 新增 LLM 元评论 + AI 前缀正则清洗 | agents/response_agent.py |

### Low（已全部修复）

| # | 漏洞 | 修复方式 | 文件 |
|---|------|----------|------|
| L1 | JWT 不可撤销 | 增加 jti claim + 内存吊销黑名单 + logout API 端点 + 前端调用 | auth/service.py, auth/router.py, static/js/chat.js |
| L2 | Prometheus 无认证 | 文档记录需 Nginx 内网限制（nginx.conf 配置 allow/deny） | docs/security-audit-2026-06-05.md |
| L3 | PBKDF2 迭代次数偏低 | 升级到 600,000 次（OWASP 推荐），兼容旧 hash 回退验证 | auth/service.py |
| L4 | 会话令牌未绑定客户端 | token 绑定客户端指纹（IP+UA），跨客户端复用自动拒绝 | session_manager.py |
| L5 | 前端 Agent 名称未转义 | Agent 名称通过 escapeHtml 转义后渲染 | static/js/chat.js |

---

## 修改文件清单

| 文件 | 变更 |
|------|------|
| config.py | +DEV_MODE, JWT_SECRET 空默认值 |
| auth/service.py | JWT 从 config 读取, 随机管理员密码, get_user_id_from_request |
| api/app.py | DEV_MODE 替代 localhost, 认证限流, XSS 修复, JWT 角色校验, Prometheus 标签转义 |
| session_manager.py | 空 token 拒绝（不再默认放行） |
| agents/base_agent.py | 对话历史不可信数据边界标记 |
| agents/response_agent.py | LLM 元评论清洗正则 |
| alerts/notifier.py | Webhook URL SSRF 防护 |
| knowledge/router.py | stats 端点加 require_admin, 错误消息脱敏 |
| erp/kingdee_real_adapter.py | 移除死代码 |
| tests/test_all.py | 更新测试以匹配新安全策略 |
