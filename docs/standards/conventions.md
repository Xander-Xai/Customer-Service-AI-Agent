# 项目工程约束 (AI 必须严格遵守)

## 技术栈
* 前端：Vanilla HTML/CSS/JS + Vite 8 + Vitest 3.x
* 后端：Python 3.10+ + FastAPI + SQLAlchemy 2.x + Alembic
* 缓存/队列：Redis 7
* 向量数据库：Qdrant（v6.3 起仅使用 Qdrant）
* 代码质量：Biome (前端 lint + format) + Ruff (后端 lint + format) + mypy (后端类型检查)
* 测试：Vitest (前端单元) + pytest (后端单元/集成) + Playwright (E2E)

## 目录结构
```
customer-service-ai-agent/
├── api/                  # FastAPI 路由与中间件
│   ├── routes/           # 端点路由（chat/sessions/feedback/monitoring/prompts/ws）
│   └── middleware/       # 认证/限流/CSRF/安全头/追踪（__init__.py + trace_middleware.py）
├── core/                 # 核心 DI 容器与 LangGraph 状态机构建
│   └── session/          # 会话管理（SessionManager/DriftDetector/TokenCounter）
├── agents/               # 9 个 AI Agent（7 领域专家 + ResponseAgent + ReAct）+ Evaluator
├── auth/                 # 认证与授权模块（JWT + Argon2id + RBAC）
├── db/                   # 数据库连接与 Schema（SQLAlchemy + Alembic）
├── router/               # 双层查询路由（LLM + 规则并行 + 复杂度评分）
├── collaboration/        # 5 种协作模式 + Orchestrator
├── rag/                  # Qdrant 知识库 + API Embedding（api_embedding.py）+ BM25 检索器（bm25_retriever.py）+ 重排 + RRF 融合 + 种子数据
├── cache/                # 三层缓存（L1 Redis + L2 Qdrant + L3 Jaccard）
├── erp/                  # 金蝶 ERP 适配器（Mock/Real + HMAC）
├── tools/                # Function Calling 工具注册（OpenAI 格式）
├── llm/                  # LLM 客户端（重试 + 熔断 + FC + SSE）+ 规则兜底
├── media/                # 多模态处理（图片/音频/视频/文档/TTS）
├── alerts/               # 告警通知（Webhook + SMTP）
├── knowledge/            # 知识库管理路由
├── web/                  # 前端静态站（vanilla JS + Vite 8）
│   ├── index.html        # 主聊天界面
│   ├── admin.html        # 管理员控制台
│   ├── login.html        # 登录与注册页
│   ├── widget.html       # 可嵌入聊天组件
│   ├── theme-comparison.html  # 主题对比页
│   ├── src/              # 前端 JS 逻辑
│   │   ├── api/          # 中央 API 统一拦截层（rest.js/sse.js/websocket.js/events.js）
│   │   ├── auth/         # 认证管理器（JWT 自动刷新）
│   │   ├── chat/         # 聊天 UI 组件（input.js/messages.js/sessions.js/voice.js/search.js/shortcuts.js/welcome.js）
│   │   ├── state/        # 中央状态管理器 (chatState.js)
│   │   └── utils/        # 工具函数 (theme/Toast/DOM/Markdown/format/agents/copy/monitor-render)
│   └── styles/           # CSS 样式表 (variables/layout/animations/components等 15 CSS)
├── deploy/               # 部署配置 (Docker Compose, Nginx, Prometheus)
├── scripts/              # 运维/benchmark/评测脚本
├── tests/                # 后端自动化测试（unit/integration/e2e/stress/eval 共 5000+ 条）
```

## 工程执行闭环

复杂度超过简单文案或机械修改的任务，遵循 [事实驱动的工程执行闭环](evidence-driven-engineering-loop.md)：

```text
事实 / 现象
→ 问题拆解
→ 可证伪假设
→ Baseline
→ 最小实现 / 实验
→ 验证证据
→ 结果验收
→ 复盘沉淀
```

至少保持：

- 事实、未知、假设和决策分开；
- `Code Complete != Problem Solved`；
- Benchmark 优化前后保持可比较口径；
- 未实测指标显式标为 `UNKNOWN / NOT_MEASURED`；
- Bug 修复优先留下最小复现与回归测试；
- RAG / Agent / 性能优化同时记录质量、时延、成本或失败模式中的相关权衡。

## 编码规范 (前端)
* **单文件代码限制**：单文件代码 ≤ 400 行，超出必须拆分（特别针对 UI 模块）。
* **单函数代码限制**：单函数代码 ≤ 50 行，超出必须重构拆分。
* **API 调用收口**：所有前端 API 调用必须通过 `web/src/api/` 或中央 REST 模块，禁止在业务组件中直接写裸的 `fetch` / `axios` 请求。
* **安全性**：禁止滥用 `innerHTML` 拼接未经过滤的用户输入；必须使用 `DOMPurify.sanitize()` 或 `escapeHtml()`。
* **状态一致性**：共享的状态（如 Token, SessionID）必须存储在 `state/chatState.js` 中，禁止在各业务模块定义散落的全局状态变量。
* **CSS 样式收口**：所有的颜色、阴影、圆角、间距必须使用 `variables.css` 中定义的 CSS 自定义属性（Variables），禁止直接在组件 CSS 中写入硬编码 Hex / RGB 颜色。
* **事件监听管理**：页面刷新或局部重渲染时需妥善清理已有的事件监听，或采用事件代理（Event Delegation）模式，避免内存泄漏。

## 安全与防线
* **API 输入校验**：所有 API 入参必须在边界进行类型与格式校验。
* **敏感信息保护**：禁止在前端代码中硬编码任何 API Key 或密码；禁止在前端控制台日志中输出 Token、密码等敏感信息。
* **跨域安全**：CORS 必须配置具体的可信域名，禁止在生产环境下使用通配符 `*`。

## 测试要求
* 核心用户路径（用户登录 -> AI 对话流式接收 -> 管理后台指标加载）必须编写 Playwright E2E 测试。
* 前端单元修改必须通过 `npm run test` (Vitest)。
* 后端单元修改必须通过 `pytest`。
