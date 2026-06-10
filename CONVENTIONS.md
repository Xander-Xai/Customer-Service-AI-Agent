# 项目工程约束 (AI 必须严格遵守)

## 技术栈
* 前端：Vanilla HTML/CSS/JS + Vite 8 + Vitest 3.x
* 后端：Python 3.10+ + FastAPI + SQLAlchemy 2.x + Alembic
* 缓存/队列：Redis 7
* 向量数据库：ChromaDB
* 代码质量：Biome (前端 lint + format) + Ruff (后端 lint + format) + mypy (后端类型检查)
* 测试：Vitest (前端单元) + pytest (后端单元/集成) + Playwright (E2E)

## 目录结构
```
customer-service-ai-agent/
├── api/                  # FastAPI 路由与中间件
├── core/                 # 核心 DI 容器与 LangGraph 状态机构建
├── agents/               # 8个 AI 专家 Agent 逻辑
├── auth/                 # 认证与授权模块
├── db/                   # 数据库连接与 Schema
├── web/                  # 前端静态站
│   ├── index.html        # 主聊天界面
│   ├── admin.html        # 管理员控制台
│   ├── login.html        # 登录与注册页
│   ├── src/              # 前端 JS 逻辑
│   │   ├── api/          # 中央 API 统一拦截层
│   │   ├── auth/         # 认证管理器
│   │   ├── chat/         # 聊天 UI 组件
│   │   ├── state/        # 中央状态管理器 (chatState.js)
│   │   └── utils/        # 工具函数 (格式化、提示框等)
│   └── styles/           # CSS 样式表 (变量、布局、动画等)
├── deploy/               # 部署配置 (Docker Compose, Nginx, Prometheus)
└── tests/                # 后端自动化测试
```

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
