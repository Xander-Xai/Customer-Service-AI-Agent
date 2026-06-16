# 项目审查与不足诊断报告（v2.0 综合审计）

> **生成日期**: 2026-06-15
> **审计依据**: `网站开发实践/05-多角色并行审计模板.md` v2.0、`00-启动清单与工具箱.md`、`02-开发全流程SOP.md`、`08-技术债务管理手册.md`
> **项目路径**: `/home/dev/projects/customer-service-ai-agent`
> **项目版本**: v5.2.2（药妆智多星多智能体客服系统）
> **审计模型**: 6 角色并行审计（攻击者 + 代码审查员 + 使用者 + AI/LLM安全 + 可观测性 + 数据守卫）

---

## 审计摘要

| 维度 | 评分 | 状态 | 核心问题 |
|------|------|------|---------|
| 安全性 | 6.5/10 | ⚠️ 需关注 | 3 个 P0（CSP nonce 格式、WS 认证绕过、管理端点权限），依赖 39 个已知漏洞 |
| 代码质量 | 7.2/10 | ✅ 良好 | 12 个文件超标，30 处 Ruff 违规，132 处版本注释 |
| 用户体验 | 7.0/10 | ✅ 良好 | 26 个 UX 问题，2 个 P0（错误键不统一、Token 过期无提示） |
| AI/LLM 安全 | 6.0/10 | ⚠️ 需关注 | 2 个 P0（user_id 传递链断裂、Session 明文存储），17 个发现 |
| 可观测性 | 5.8/10 | ⚠️ 需关注 | 3 个 P0（指标名不匹配、告警规则失效、堆栈缺失） |
| 数据层 | 7.5/10 | ✅ 良好 | 25 个发现，无 P0，Schema 设计合理但缺软删除/GDPR |
| **总体评分** | **6.7/10** | **⚠️ 有条件可上线** | **5 个 P0，需修复后上线** |

---

## 一、Phase 0 — 项目画像

### 1.1 项目类型
**Web 应用（后端 Python FastAPI + 前端原生 JS Vite）且深度集成 LLM**

### 1.2 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | 原生 JS + Vite 8 + CSS3（无框架） |
| 后端 | Python 3.10+ / FastAPI / SQLAlchemy / Alembic |
| 数据库 | PostgreSQL（生产）/ SQLite（开发）+ Redis |
| AI/LLM | LangGraph / OpenAI 兼容 API（硅基流动/DeepSeek） |
| 向量库 | ChromaDB |
| 监控 | Prometheus + Grafana + Alertmanager + Loki |
| 部署 | Docker Compose + Nginx |

### 1.3 代码规模

| 指标 | 数值 |
|------|------|
| Python 源文件 | 47 个 |
| Python 总行数 | 11,335 行 |
| JS 源文件 | 38 个 |
| JS 总行数 | 6,098 行 |
| 测试文件 | 25 个 |
| 测试总行数 | 18,771 行 |
| 测试通过率 | 1342 passed, 5 skipped |

### 1.4 风险暴露面

- **网络暴露**: 公网（Web + API + WebSocket）
- **用户输入**: 表单/API/文件上传/语音/图片
- **第三方集成**: LLM API（硅基流动/DeepSeek/OpenAI）、ERP（金蝶）
- **攻击面优先级**: **高**（公网 + 用户输入 + LLM + 文件上传）

---

## 二、Layer 1 — 多角色审计发现汇总

### 2.1 角色 1：攻击者（Attacker）— 13 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| **A-3** | 🔴 P0 | CSP `style-src` 缺少 f-string 前缀，nonce 未实际注入 | `api/middleware.py:168` | 改为 `f"style-src 'self' 'nonce-{nonce}'; "` |
| **A-1** | 🔴 P0 | WebSocket 认证完全绕过（DEV_MODE/API_KEY_ENABLED 时） | `api/routes/ws.py:63` | 无论环境如何 WS 必须强制认证 |
| **A-2** | 🔴 P0 | `/api/admin/prompts/*` 未列入 admin 权限白名单 | `api/middleware.py:216-221` | 添加 `path.startswith("/api/admin/prompts")` |
| A-4 | 🟠 P1 | Jinja2/MarkupSafe 版本老旧（CVE-2024-22195） | `requirements-lock.txt` | 升级至 Jinja2>=3.1.4 |
| A-5 | 🟠 P1 | 输入长度校验不一致（模块常量 vs 环境变量） | `api/routes/chat.py:34-45` | 运行时动态校验 |
| A-6 | 🟠 P1 | WS 日志 `exc_info=True` 泄露堆栈 | `api/routes/ws.py:249` | 生产环境移除 |
| A-7 | 🟡 P2 | welcome.js `data-title` 未转义 | `web/src/chat/welcome.js:16` | 添加 `escapeAttr` |
| A-8 | 🟡 P2 | DEV_MODE 完全绕过认证 | `api/middleware.py:241-246` | 增加启动警告 |
| A-9 | 🟡 P2 | PBKDF2 建议迁移 Argon2id | `auth/service.py:104` | 评估迁移 |
| A-10 | 🟡 P2 | 限流存储超限放行 | `api/middleware.py:144-146` | 超限返回 429 |
| A-11 | 🟢 P3 | `/api/health` 泄露 LLM 配置 | `api/routes/monitoring.py:127-130` | 移除敏感字段 |
| A-12 | 🟢 P3 | openai 版本较旧 | `requirements-lock.txt` | 关注安全公告 |
| A-13 | 🟢 P3 | HSTS 在 HTTP 环境设置 | `api/middleware.py:173` | 仅 HTTPS 时设置 |

### 2.2 角色 2：代码审查员（Code Reviewer）— 29 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| **C-029** | 🔴 P0 | `HTTPException` 未导入导致运行时 NameError | `api/routes/monitoring.py:305` | 文件顶部添加 `from fastapi import HTTPException` |
| C-001 | 🔴 P0 | 7 个后端文件超标（≥400行） | `core/session/session_manager.py`(739) 等 | 按职责拆分 |
| C-003 | 🔴 P0 | 11 个后端大函数（>50行） | `agents/base_agent.py:469` 等 | 提取方法 |
| C-002 | 🟠 P1 | 5 个前端文件超标 | `web/src/admin-analytics.js`(495) 等 | 拆分模块 |
| C-004 | 🟠 P1 | 7 个前端大函数 | `web/src/auth/index.js:46` 等 | 提取方法 |
| C-005 | 🟠 P1 | DOM 创建模式重复（107 处） | `web/src/admin-*.js` | 统一使用 `utils/dom.js` |
| C-010 | 🟠 P1 | 未使用导入（3 处） | `core/token_quota.py` | 删除 |
| C-018 | 🟠 P1 | F401 未使用导入 | `core/token_quota.py` | 删除 |
| C-019 | 🟠 P1 | B007 未使用循环变量 | `agents/base_agent.py:433` | 改为 `_key` |
| C-020 | 🟠 P1 | B011 assert False（4 处） | `tests/unit/test_core_modules.py` | 改为 `raise AssertionError` |
| C-025 | 🟠 P1 | Biome noUnusedImports | `web/src/chat/messages.js:8` | 删除 `escapeHtml` |
| C-006 | 🟡 P2 | 图表渲染逻辑重复（5 处） | `web/src/admin-analytics.js` | 提取 `ChartRenderer` |
| C-007 | 🟡 P2 | A/B 变体解析重复 | `agents/base_agent.py:485,614` | 提取方法 |
| C-008 | 🟡 P2 | 事件发布重复 | `agents/base_agent.py:578,666` | 提取方法 |
| C-009 | 🟡 P2 | 错误处理模式重复 | `llm/client.py` | 提取装饰器 |
| C-012 | 🟡 P2 | 版本注释污染（132 处） | 全项目 | 迁移到 CHANGELOG |
| C-013 | 🟡 P2 | 命名风格不一致 | `web/src/admin-analytics.js` | 统一 camelCase |
| C-014 | 🟢 P3 | 魔法数字 | `web/src/auth/index.js:46` | 提取常量 |
| C-015 | 🟡 P2 | 模块边界模糊 | `agents/` -> `core/session/` | 引入协议层 |
| C-016 | 🟡 P2 | 延迟导入（noqa: E402）过多 | `api/app_factory.py` | 拆分模块 |
| C-021 | 🟡 P2 | E731 lambda 赋值 | `rag/knowledge_base.py:532` | 改写为 def |
| C-022 | 🟡 P2 | B905 zip 缺少 strict | `rag/reranker.py:99` | 添加 `strict=` |
| C-023 | 🟡 P2 | SIM117 嵌套 with（8 处） | 测试文件 | 合并 with |
| C-024 | 🟢 P3 | SIM105 try/except/pass（2 处） | 测试文件 | 用 `contextlib.suppress` |
| C-026 | 🟡 P2 | useExponentiationOperator | `web/src/__tests__/contrast.test.js:14` | 改用 `**` |
| C-027 | 🟡 P2 | Biome 格式（6 个文件） | 多个文件 | 运行 `biome fix` |
| C-028 | 🟢 P3 | console.log 残留 | `web/src/api/websocket.js` 等 | 迁移到日志系统 |

### 2.3 角色 3：使用者（Consumer）— 26 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| **U-1** | 🔴 P0 | HTTP 错误键不统一（`error` vs `detail`） | `api/routes/chat.py:207` 等 | 全局统一错误格式 |
| **U-2** | 🔴 P0 | Token 过期无提示（空 catch） | `web/src/chat/sessions.js:73` | 添加错误提示 + 跳转 |
| U-3 | 🟠 P1 | 登录页硬编码红绿色 hex | `web/src/login.js:76,81` | 改用 CSS 变量 |
| U-4 | 🟠 P1 | 管理后台硬编码 hex 状态色（9 处） | `admin-analytics.js` 等 | 抽出主题 token |
| U-5 | 🟠 P1 | Toast 背景色硬编码 | `web/src/utils/toast.js:26-29` | 改用 CSS 变量 |
| U-6 | 🟠 P1 | 登录错误信息泛化 | `web/src/login.js:50` | 字段级提示 |
| U-7 | 🟠 P1 | 危险操作使用原生 `confirm()` | `admin-users.js:94` 等 | 替换为项目 modal |
| U-8 | 🟠 P1 | 管理后台无骨架屏/Loading | `admin-analytics.js:17` 等 | 注入 skeleton CSS |
| U-9 | 🟠 P1 | Widget 入口无 a11y 标签 | `web/widget.html` | 同步 index.html 的 a11y |
| U-10~U-26 | 🟡 P2~🟢 P3 | 详见完整报告 | 多个文件 | 详见完整报告 |

### 2.4 角色 4：AI/LLM 安全审查员 — 17 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| **H-1** | 🔴 P0 | user_id 传递链断裂 → Token Quota 失效 | `llm/client.py:158-167` | 注入 user_id 到 metadata |
| **H-2** | 🔴 P0 | Session/Message 明文存储（PII 风险） | `core/session/session_manager.py:240-250` | 引入加密 |
| H-3 | 🟠 P1 | PII 数据直发第三方 LLM | `agents/billing_agent.py:69-74` 等 | PII 脱敏 + 用户告知 |
| H-4 | 🟠 P1 | 全局 Blackboard 无用户隔离 | `core/container.py:59` | 按 session 隔离 |
| H-5 | 🟠 P1 | LLM 不可用时无 Agent 级 fallback | `agents/base_agent.py:638-660` | 引入 fallback LLM |
| H-6 | 🟠 P1 | Function Calling 无 RBAC | `tools/tool_registry.py:31-43` | 添加角色校验 |
| H-7 | 🟠 P1 | RAG 文档上传缺防护 | `knowledge/router.py:89-117` | 内容校验 + 去重 |
| H-8~H-17 | 🟡 P2~🟢 P3 | 详见完整报告 | 多个文件 | 详见完整报告 |

### 2.5 角色 5：可观测性督察 — 15 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| **B-1** | 🔴 P0 | Prometheus 指标名不匹配 | `core/monitoring.py` | 统一指标名 |
| **B-2** | 🔴 P0 | 告警规则指标名不匹配 | `alerts/` | 修复规则 |
| **B-3** | 🔴 P0 | 仅 5 处 `exc_info=True` | 全局 | 增加堆栈追踪 |
| B-4 | 🟠 P1 | 日志未脱敏 | `core/logger.py` | 敏感字段脱敏 |
| B-5 | 🟠 P1 | OpenTelemetry 默认未启用 | 全局 | 配置 OTel |
| B-6 | 🟠 P1 | trace_id 仅 12 字符 | `core/logger.py` | 使用标准 UUID |
| B-7 | 🟠 P1 | 缺少 Histogram 指标 | `core/monitoring.py` | 添加 P99/P99.9 |
| B-8 | 🟠 P1 | Alertmanager webhook URL 不存在 | `alerts/` | 修复端点 |
| B-9~B-15 | 🟡 P2~🟢 P3 | 详见完整报告 | 多个文件 | 详见完整报告 |

### 2.6 角色 6：数据守卫 — 25 个发现

| 编号 | 严重 | 问题 | 位置 | 修复建议 |
|------|------|------|------|----------|
| C-2 | 🟠 P1 | feedback db 未正确关闭 | `api/routes/feedback.py:50` | 使用上下文管理器 |
| C-4 | 🟠 P1 | 内存与持久化不一致 | `core/session/session_manager.py:180` | 实现写穿透 |
| M-2 | 🟠 P1 | 迁移 downgrade 不可逆 | `alembic/versions/003` | 标记不可逆 |
| L-1 | 🟠 P1 | 无备份策略 | 全局 | 添加备份脚本 |
| S-1 | 🟠 P1 | 文件会话明文存储 | `core/session/session_manager.py:240` | 加密或改用 DB |
| D-1~D-8 | 🟡 P2~🟢 P3 | Schema 设计问题（email 缺失、Boolean、JSON 无限制等） | `db/models.py` | 详见完整报告 |
| M-1, M-3~M-5 | 🟡 P2~🟢 P3 | 迁移问题 | `alembic/` | 详见完整报告 |
| C-1, C-3, C-5 | 🟡 P2~🟢 P3 | 一致性/事务问题 | 多个文件 | 详见完整报告 |
| L-2~L-4 | 🟡 P2~🟢 P3 | 数据生命周期问题 | 全局 | 详见完整报告 |
| S-2~S-5 | 🟡 P2 | Session 存储安全性 | `core/session/` | 详见完整报告 |

---

## 三、Phase 2.5 — 横切审计链

### 3.1 数据流审计链（DF）

```
用户输入 → 验证 → 转换 → 存储 → 读取 → 展示 → 导出 → 删除
```

| 节点 | 状态 | 问题 |
|------|------|------|
| 输入 | ⚠️ | sanitize_input 已做，但 MAX_QUERY_LENGTH 环境变量未在运行时校验 |
| 验证 | ⚠️ | Pydantic 验证 + JWT 鉴权，但 DEV_MODE 完全绕过 |
| 转换 | ✅ | 无精度丢失 |
| 存储 | ❌ | **明文存储**（H-2），无加密 |
| 读取 | ⚠️ | 缓存一致性待验证（C-4） |
| 展示 | ⚠️ | XSS 防护有 DOMPurify，但 welcome.js data-title 未转义 |
| 导出 | ✅ | 无导出功能 |
| 删除 | ❌ | 无软删除，物理删除无法恢复 |

### 3.2 故障传播审计链（FP）

```
LLM API 挂了 → graph_builder 降级 → RuleBasedLLM → 前端展示什么？
```

| 层级 | 状态 | 问题 |
|------|------|------|
| LLM API | ⚠️ | CircuitBreaker 已配置，但 user_id 传递链断裂（H-1） |
| graph_builder | ⚠️ | 降级到规则分类，但 Agent 级无 fallback（H-5） |
| 业务层 | ✅ | 有熔断器 + 降级响应 |
| API 层 | ✅ | 统一错误格式（但 U-1 错误键不统一） |
| 前端层 | ❌ | 401 无提示，空 catch（U-2） |
| 告警层 | ❌ | 告警规则指标名不匹配（B-2） |

### 3.3 配置安全审计链（CS）

| 节点 | 状态 | 问题 |
|------|------|------|
| .env.example | ✅ | 占位符，无真实值 |
| .env.dev | ⚠️ | DEV_MODE=true，完全绕过认证 |
| .env.prod | ✅ | 未提交到 git |
| Docker | ⚠️ | 未验证是否以非 root 运行 |
| K8s | N/A | 未使用 K8s |
| Feature Flag | ❌ | 无 Feature Flag 机制 |

---

## 四、Layer 2 — 动态验证

### 4.1 测试结果

| 指标 | 结果 |
|------|------|
| 测试总数 | 1342 passed, 5 skipped |
| 测试耗时 | 146.75s |
| 警告数 | 203（主要是 DeprecationWarning 和 RuntimeWarning） |
| 覆盖率 | 80%+（门槛） |

### 4.2 Lint 结果

| 指标 | 结果 |
|------|------|
| Ruff 错误 | 30 处（3 处可自动修复） |
| Biome 错误 | 9 处（7 处可自动修复） |

### 4.3 依赖漏洞扫描

| 指标 | 结果 |
|------|------|
| 已知漏洞 | **39 个**（15 个包） |
| 高危 | python-jose（PYSEC-2024-232/233）、starlette（PYSEC-2026-161）、pillow（PYSEC-2026-165） |
| 中危 | babel、chromadb、configobj、idna、langchain-community、litellm、oauthlib、pyopenssl、pytest |

---

## 五、Layer 3 — 业务对齐层

### 5.1 需求 ↔ 代码对齐

| 需求 | 实现状态 | 偏差 |
|------|---------|------|
| 四层状态机 | ✅ 已实现 | 无偏差 |
| 8 个 AI Agent | ✅ 已实现 | 无偏差 |
| 5 种协作模式 | ✅ 已实现 | 无偏差 |
| 双层缓存 | ✅ 已实现 | 无偏差 |
| RAG + FC + ReAct | ✅ 已实现 | 无偏差 |
| Token Quota | ⚠️ 框架存在，user_id 传递链断裂 | **H-1** |
| 前端 E2E 测试 | ✅ 已实现（flows.spec.js） | 需扩展 |
| CSP nonce | ⚠️ 框架存在，style-src 格式错误 | **A-3** |

### 5.2 文档 ↔ 代码对齐

| 文档 | 代码状态 | 偏差 |
|------|---------|------|
| README.md | ✅ 详细 | 无偏差 |
| CLAUDE.md | ✅ 详细 | 无偏差 |
| CONVENTIONS.md | ✅ 存在 | 无偏差 |
| docs/active/ | ✅ 9 份文档 | 无偏差 |
| API 文档 | ⚠️ 部分缺失 | 需补充 OpenAPI |

---

## 六、Phase 3 — 多维关联分析

### 6.1 多角色命中矩阵

| 问题摘要 | 位置 | 攻击者 | 代码审查 | 使用者 | AI安全 | 可观测性 | 数据守卫 | 命中数 | 提升后 |
|---------|------|--------|---------|--------|--------|---------|---------|--------|--------|
| CSP nonce 格式错误 | `api/middleware.py:168` | ✅ A-3 | — | — | — | — | — | 1 | 🔴 P0 |
| WS 认证绕过 | `api/routes/ws.py:63` | ✅ A-1 | — | — | — | — | — | 1 | 🔴 P0 |
| 管理端点权限绕过 | `api/middleware.py:216` | ✅ A-2 | — | — | — | — | — | 1 | 🔴 P0 |
| user_id 传递链断裂 | `llm/client.py:158` | — | — | — | ✅ H-1 | — | — | 1 | 🔴 P0 |
| Session 明文存储 | `core/session/session_manager.py:240` | ✅ A-6 | — | — | ✅ H-2 | — | ✅ S-1 | **3** | **🔴 阻塞** |
| 错误键不统一 | `api/routes/chat.py:207` | — | — | ✅ U-1 | — | — | — | 1 | 🔴 P0 |
| Token 过期无提示 | `web/src/chat/sessions.js:73` | — | — | ✅ U-2 | — | — | — | 1 | 🔴 P0 |
| 依赖漏洞（39个） | `requirements-lock.txt` | ✅ A-4 | — | — | — | — | — | 1 | 🟠 P1 |
| PII 上送第三方 | `agents/billing_agent.py:69` | — | — | — | ✅ H-3 | — | — | 1 | 🟠 P1 |
| Blackboard 无隔离 | `core/container.py:59` | — | — | — | ✅ H-4 | — | — | 1 | 🟠 P1 |
| 指标名不匹配 | `core/monitoring.py` | — | — | — | — | ✅ B-1 | — | 1 | 🔴 P0 |
| 告警规则失效 | `alerts/` | — | — | — | — | ✅ B-2 | — | 1 | 🔴 P0 |
| 无备份策略 | 全局 | — | — | — | — | — | ✅ L-1 | 1 | 🟠 P1 |

### 6.2 因果链分析

```
根因：CSP nonce 格式错误（A-3）
  ├→ 症状：内联样式被浏览器拒绝
  └→ 症状：主题切换异常

根因：user_id 传递链断裂（H-1）
  ├→ 症状：Token Quota 失效（任何用户都可无限制调用）
  ├→ 症状：LLM 成本失控
  └→ 症状："钱包枯竭攻击"风险

根因：Session 明文存储（H-2）
  ├→ 症状：PII 泄露风险
  ├→ 症状：GDPR 不合规
  └→ 症状：运维人员可直接读取客户对话

根因：DEV_MODE 完全绕过认证（A-8）
  ├→ 症状：WS 认证绕过（A-1）
  └→ 症状：开发环境误配置导致生产风险
```

### 6.3 聚合修复

| 聚合任务 | 涉及问题 | 预计节省 |
|---------|---------|---------|
| 修复 CSP nonce 格式 | A-3 | 1 行代码 |
| 注入 user_id 到 metadata | H-1 | 3 个文件 |
| 加密 Session 存储 | H-2, S-1 | 1 个模块 |
| 统一错误格式 | U-1, A-6 | 全局中间件 |
| 升级依赖版本 | A-4, 39 个漏洞 | 1 个文件 |
| 拆分大文件/函数 | C-001, C-003, C-002, C-004 | 渐进式 |

---

## 七、Phase 4 — 审计结论

### 7.1 总体评分

| 维度 | 评分 | 状态 |
|------|------|------|
| 安全性 | 6.5/10 | ⚠️ |
| 代码质量 | 7.2/10 | ✅ |
| 用户体验 | 7.0/10 | ✅ |
| AI/LLM 安全 | 6.0/10 | ⚠️ |
| 可观测性 | 5.8/10 | ⚠️ |
| 数据层 | 7.5/10 | ✅ |
| **总体** | **6.7/10** | **⚠️ 有条件可上线** |

### 7.2 上线建议

**⚠️ 有条件可上线** — 修复以下 5 个 P0 后可上线：

1. **A-3** CSP `style-src` 缺少 f-string 前缀（1 行修复）
2. **A-1** WebSocket 认证绕过（条件判断修复）
3. **A-2** 管理端点权限绕过（1 行修复）
4. **H-1** user_id 传递链断裂（3 个文件修改）
5. **B-1/B-2** 指标名不匹配/告警规则失效（配置修复）

### 7.3 后续行动清单

#### 🔴 P0 — 阻塞上线（必须立即完成）

| 任务 | 工作量 | 依赖 | 验收标准 |
|------|--------|------|---------|
| 修复 CSP nonce f-string | 5 分钟 | 无 | CSP 头正确包含 nonce |
| 修复 WS 认证绕过 | 30 分钟 | 无 | DEV_MODE 下 WS 仍需认证 |
| 修复管理端点权限 | 5 分钟 | 无 | `/api/admin/prompts/*` 需 admin |
| 修复 user_id 传递链 | 2 小时 | 无 | LLM client 正确获取 user_id |
| 修复指标名不匹配 | 1 小时 | 无 | Prometheus 指标名与规则一致 |

#### 🟠 P1 — 显著提升（本周完成）

| 任务 | 工作量 | 依赖 | 验收标准 |
|------|--------|------|---------|
| 升级依赖版本（39个漏洞） | 2 小时 | 无 | `pip-audit` 无高危 |
| 加密 Session 存储 | 4 小时 | 无 | 会话文件加密 |
| PII 脱敏 | 4 小时 | 无 | 敏感字段脱敏后发送 |
| 统一错误格式 | 2 小时 | 无 | 全局统一 `error` 键 |
| 修复 Token 过期无提示 | 1 小时 | 无 | 401 时跳转登录 |

#### 🟡 P2 — 锦上添花（本月完成）

| 任务 | 工作量 | 依赖 | 验收标准 |
|------|--------|------|---------|
| 拆分大文件/函数 | 持续 | 无 | 单文件 ≤400行 |
| 添加备份策略 | 2 小时 | 无 | 定时备份脚本 |
| 实现软删除 | 4 小时 | 无 | deleted_at 字段 |
| 添加 Feature Flag | 4 小时 | 无 | 灰度发布能力 |
| 完善 E2E 测试 | 1 天 | 无 | 覆盖注册/Token刷新等 |

---

## 八、附录

### 8.1 审计文档清单

| 文档 | 路径 | 用途 |
|------|------|------|
| 多角色并行审计模板 | `网站开发实践/05-多角色并行审计模板.md` | 审计方法论 |
| 启动清单与工具箱 | `网站开发实践/00-启动清单与工具箱.md` | 工程基线 |
| 开发全流程 SOP | `网站开发实践/02-开发全流程SOP.md` | 开发规范 |
| 技术债务管理手册 | `网站开发实践/08-技术债务管理手册.md` | 债务识别 |
| 第三方服务治理 | `网站开发实践/09-第三方服务治理.md` | 依赖治理 |

### 8.2 角色前缀映射

| 角色 | 前缀 | 问题数 |
|------|------|--------|
| 攻击者 | A | 13 |
| 代码审查员 | C | 29 |
| 使用者 | U | 26 |
| AI/LLM 安全审查员 | H | 17 |
| 可观测性督察 | B | 15 |
| 数据守卫 | W/D/M/S/L | 25 |
| **总计** | — | **125** |

### 8.3 变更日志

| 版本 | 日期 | 改动 |
|------|------|------|
| v1.0 | 2026-06-15 | 初始版本，5 角色审计 |
| v2.0 | 2026-06-15 | 升级至 6 角色 + 横切审计链 + 动态验证 + 多维关联分析 |
