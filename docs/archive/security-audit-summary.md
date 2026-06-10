# 安全审计摘要（2026-06-05）

> 审计范围：auth、api、agents、erp、tools、session、alerts、knowledge
> 方法：三面并行审计（认证安全 / 注入攻击 / 逻辑漏洞）
> 结果：23 项发现，**全部已修复**，319 tests passed

| 等级 | 数量 | 状态 |
|------|------|------|
| 🔴 Critical | 3 | ✅ 全部修复 |
| 🟠 High | 7 | ✅ 全部修复 |
| 🟡 Medium | 8 | ✅ 全部修复 |
| 🔵 Low | 5 | ✅ 全部修复 |

**关键修复：** JWT 密钥硬编码(C1) → 空默认值+启动校验 | 反向代理 localhost 绕过(C2) → DEV_MODE 环境变量 | Prompt Injection(C3) → 不可信数据边界标记 | 登录无限流(H1) → 独立认证限流 | Webhook SSRF(M1) → URL 安全校验

**影响文件：** config.py, auth/service.py, api/app.py, session_manager.py, agents/base_agent.py, agents/response_agent.py, alerts/notifier.py, knowledge/router.py, erp/kingdee_real_adapter.py

> 原始报告：`git log --all -- docs/archive/security-audit-2026-06-05.md` 可追溯完整 80 行原文。
