# 变更日志 (CHANGELOG)

> 本文档记录药妆智多星多智能体客服系统的所有版本变更。

---

## v5.2.2 (2026-06-11) — 会话列表标题字段修正 + CI 覆盖率修复

### 修复
- **会话列表标题字段错位**：`web/src/chat/sessions.js` 的 `loadSessionList()` 渲染时会话项标题从 `s.summary` 改为 `s.title`。后端 `list_sessions_brief` 接口（`/api/sessions` 与 `/api/history`）返回的字段是 `title`（取自首条消息内容前 50 字符），`summary` 字段只存在于详细会话接口 `getSession`（`/api/sessions/{id}`）中。此前列表请求读取 `summary` 始终为 `undefined`，导致所有会话项均回退到”对话 {session_id 前 8 位}”占位符，用户无法看到真实标题。修正后会话列表正确显示首条消息截取的可读标题，详情面板仍保留 `session.summary` 长摘要的展示。

### CI 覆盖率修复
- **pytest.ini `addopts` 配置错误**：`--cov` 从 `addopts` 移除。此前每次 pytest 运行都触发 `fail_under=80` 检查，导致 integration/e2e/stress 单独运行时因覆盖率不足 80% 而失败。现在只有显式传入 `--cov` 时才检查覆盖率门槛（CI 覆盖率报告步骤已显式传入 `--cov=.`）
- **.coveragerc 排除不可测文件**：`erp/kingdee_real_adapter.py`（需要真实 ERP 后端，覆盖率 0%）从覆盖率统计中排除；移除 `source` 中冗余的 `.` catch-all
- **新增 151 个测试用例（6 个文件）**：
  - `test_collaboration_modes.py`（35 tests）：Sequential/Parallel/Consultation/Hierarchical/ReAct 全分支覆盖（22% → 97%）
  - `test_collaboration_orchestrator.py`（25 tests）：模式选择 + 运行时模式升级全分支（48% → 99%）
  - `test_query_router_coverage.py`（34 tests）：规则分类 + LLM 分类 + 双层路由（0% → 100%）
  - `test_alert_notifier_coverage.py`（20 tests）：Webhook + 邮件 + SSRF 防护
  - `test_base_agent_billing_coverage.py`（32 tests）：A/B 变体 + 漂移修复 + 账单 Agent
  - `test_graph_builder_coverage.py`（5 tests）：工具函数 + 向后兼容包装器

---

## v5.2.1 (2026-06-11) — 混合主题特异性修复 + 面板状态同步

### 对比度修复补充
- **OS 深色模式污染修复**：将 `@media (prefers-color-scheme: dark)` 媒体查询从 `variables.css` 移至 `theme-dark.css` 末尾，彻底解决深色系统模式下切换浅色主题时出现深色侧边栏+浅色主区+深色字体的混合主题 Bug
- **浅色主题侧边栏补全**：`pure`/`soft`/`cream` 主题补齐缺失的 `--color-surface-base-glass` Token，防止错误继承系统深色玻璃态

### 交互修复
- **面板状态同步**：修改 `theme.js` 中的 `_bindControls()` 函数，在切换配色模式/字号/行高时调用 `_syncPanelState()` 确保面板 UI 与设置状态同步

---

## v5.2 (2026-06-10) — 无障碍合规 + 交互增强 + 对比度全量修复 + CI 升级

### 无障碍合规（WCAG AA/AAA）
- **全局交互色 AAA 达标**：push 所有 `--color-*-hover/active` token 到 ≥7.0 对比度
- **禁用态对比度 AA 修复**：`btn-send:disabled` 改用 `--color-disabled` token，补齐 `.btn-secondary`
- **弱化文字 AA 加深**：`--text-muted` / `--text-secondary` / `--progress-bg` 色值加深至 ≥4.5:1
- **Disabled token 统一修正**：所有 disabled 状态色彩通过语义 token 控制

### 对比度全量修复（8+ 处）
- **混合主题对比度灾难修复 (Root Cause)**：修复了由于 CSS `@media (prefers-color-scheme: dark)` 特异性污染导致的“深色系统模式下切换浅色主题会产生深色侧边栏+浅色主区+深色字体”的 Bug。现已将 OS 深色媒体查询抽离并置于 `theme-dark.css` 末尾，确保不同颜色模式的变量隔离。
- **浅色主题侧边栏一致性**：在 `theme-light.css` 中的 `pure`/`soft`/`cream` 主题补齐了缺失的 `--color-surface-base-glass`，防止其错误继承默认的暖色玻璃态或系统的深色玻璃态。
- **侧边栏文本对比度**：`layout.css` 中将会话标题强制绑定至高对比语义 Token `--color-text-primary` 和 `--color-text-secondary`，解决背景深色覆盖浅色文本的不可读问题。
- **消息气泡对比度**：`components.css` 中为用户消息气泡和 AI 气泡显式提供具有高对比度保证的 `background` 与 `color` Token 对（如 `--color-action-primary` 配合 `--color-surface-base`），避免出现深背景+深字体或无法看清内容的问题。
- **btn-send:disabled**：opacity 方案→专用 disabled token（2.07→4.61:1）
- **btn-primary:disabled**：bg-hover→disabled token（3.9→4.61:1）
- **btn-secondary**：补全缺失的样式定义（4.61:1 AA）
- **btn-reset**：加深默认边框 border→border-hover
- **#logoutBtn**：加深边框 + hover 背景（3.28→5.2:1）
- **cream 主题**：text-secondary #6c5f4f→#5d5240（6.0:1 AA+）
- **cream 主题**：text-muted #756a58→#645840（5.5:1 AA）
- **暖深色主题**：新增 disabled token（4.90:1 AA）
- **自动化测试**：contrast.test.js 18 个 token 对比度回归测试（全部通过）

### 交互增强
- **TTS 语音选择器**：`index.html` 添加 `#selectTTSVoice` 元素，激活 `main.js` 已有逻辑
- **会话详情侧面板**：点击会话项时自动打开侧面板，显示会话元信息（Agent / 模式 / 时间）
- **Esc 键关闭面板**：侧面板支持 Esc 键 dismiss + 新会话自动关闭面板

### 死代码清理
- **删除 `getHistory()` / `getHistoryMessages()`**：`web/src/api/rest.js` 中 2 个无调用点的函数
- **删除 `monitor/index.js`**：223 行死代码文件，admin-analytics.js 已完全替代
- **清理对应 re-export**：`api/index.js` 移除已删除函数的重导出

### CI 升级
- **GitHub Actions v6**：actions/checkout、setup-python、upload-artifact 升级到 v6（Node.js 24）
- **Node.js 20 弃用修复**：解决 2026-09-16 移除前的弃用警告

### 测试改进
- **Ruff lint 清理**：346 → 73 warning（删除无用导入 + 断言优化）
- **Flaky 测试修复**：修复异步 mock + 类型适配问题
- **对比度回归测试**：18 个 token 对比度自动验证（覆盖 6 种主题）

---

## v5.1 (2026-06-10) — 全量清理与文档同步

- **清理 320 个临时文件**：删除 docs/superpowers/specs/、旧计划文档、临时脚本
- **新增模块文档同步**：theme-panel.css / theme-a11y.css / admin-analytics.js 等新模块补入项目结构说明
- **隐私检查通过**：无遗留 API Key / Secret / Token 泄露
- **文档结构优化**：active/ 存放当前有效文档，archive/ 存放历史归档，decisions/ 存放 ADR

---

## v5.0 (2026-06-08) — 前端重构 + 前后端对齐 + 版本号统一

### 前端重构（Phase 1-4）
- **Vite 8 构建管线**：34 个源文件（22 JS + 12 CSS），原生 ES Module + Tree-shaking + Hashed 产物
- **marked.js + DOMPurify**：替换正则 Markdown 解析，双重 XSS 防护
- **SSE 流式打字框架**：逐 token 推送 + 进度条 + Agent 流转轨迹显示
- **Toast 通知 + 消息搜索**：用户交互增强
- **无障碍**：ARIA 标签 + 焦点环 + 对比度修复 + 跳转链接
- **移动端**：抽屉式导航 + 响应式布局
- **主题系统**：亮色（pure/warm/soft/cream）+ 暗色（classic/warm）+ 字号/行高控制 + 减弱动效切换
- **冗余清理（-4269 行）**：删除旧前端 templates/ + static/，迁移 LLM 客户端

### 前后端匹配修复（15 项）
- **P0（4 项）**：ReAct 模式标签补全 / SSE done 完整数据传递 / 反馈 message_index 精确定位 / Token 自动刷新
- **P1（3 项）**：语音按钮 + voice.js / 文件上传扩展（PDF/DOCX/视频/文本）/ SSE Agent 轨迹
- **P2（3 项）**：版本号对齐 5.0.0 / admin 显示 ChromaDB+DB 健康状态 / complaint_knowledge 统计
- **P3（2 项）**：escapeHtml 统一 / rest.js API 层替代直接 fetch

### 安全审查修复（7 项）
- admin.js innerHTML 数据转义 / WebSocket API Key 防日志泄露 / DB 日志移除凭据 / LoginRequest max_length / 非 DEV 空 secret 拒绝

### 测试与工具链
- **测试用例**：1151 条（新增 middleware / prompt_manager / protocols_di / rag_reranker / token_tracker_db 等测试文件）
- **Ruff 工具链**：`pyproject.toml` 统一配置 + `make lint` / `make format`
- **覆盖率门槛**：`fail_under=80`（从 60 提升）
- **mypy 渐进严格**：`setup.cfg` 配置 + CI 集成（continue-on-error）
- **pre-commit hooks**：防止 .env 文件提交 + Ruff lint/format

---

## v4.6 (2026-06-08) — 文档扫描与优化实施

- **扫描 20/20 项全部完成**：docs/superpowers/specs/ 下 4 份设计文档的优化建议
- **pre-commit 配置**：`.pre-commit-config.yaml` 防止 .env 文件提交
- **覆盖率门槛提升**：`.coveragerc` fail_under 60→80
- **未用导入清理**：api/app.py / core/container.py / agents/evaluator.py 移除多余导入
- **REACT 配置对齐**：`.env` / `.env.dev` REACT_MAX_ITERATIONS 5→3 与 config.py 默认值一致
- **api/app.py 路由拆分**：1359 行单体 → 7 个模块（249 + 91 + 189 + 553 + 256 + 105 + 218 + 138 行）

---

## v4.5 (2026-06-08) — 图构建统一 + 测试加固 + mypy CI

- **图构建代码冗余消除**：删除 `make_graph()` 及 4 个全局锁，`build_graph(container)` 成为唯一入口，净减 317 行
- **测试断言加固**：49 个宽泛断言 → 具体值/类型/属性断言（7 类修复）
- **mypy CI 集成**：新增 `setup.cfg` mypy 配置 + CI Type checking 步骤
- **create_session await 修复**：修复 v4.3 遗留的 async 调用 bug
- **Orchestrator 初始化优化**：移到 ServiceContainer.__init__() 同步创建

---

## v4.4 (2026-06-08) — 安全加固 + 代码重构 + 测试覆盖率 + 文档完善

### 安全加固 (6 项)
- **PyJWT 替换自研 JWT**：使用 PyJWT 成熟库 + 算法白名单 HS256，防止 `alg:none` 攻击
- **JWT denylist 大小限制**：内存回退上限 10,000 条，生产无 Redis 时打印警告
- **WebSocket JWT 认证修复**：JWT 不再通过 URL 参数传递，改用首条消息认证，防止 token 泄露到日志
- **WebSocket 连接计数清理**：定期清理零连接 IP 记录，防止内存泄漏
- **CSP 安全加固**：`script-src` 移除 `unsafe-inline`，仅保留 nonce + `unsafe-hashes`
- **清理 .env.dev 真实 API Key**：替换为占位符，防止意外泄露

### 代码质量重构 (4 项)
- **session_manager.py 拆分**：→ `token_counter.py`（Token 计数 + jieba 分词）+ `drift_detector.py`（4 种漂移检测）+ 核心会话管理
- **AgentState 统一定义**：新建 `core/state.py` 单一定义点，消除 3 处重复 TypedDict
- **图构建优化**：`build_graph()` 与 `make_graph()` 共享节点逻辑，消除代码重复
- **API Key 验证增强**：占位符值更全面的检测（覆盖已知旧格式）

### 测试与 CI (3 项)
- **pytest 覆盖率门槛**：`.coveragerc` 配置 `fail_under=60`，强制覆盖率达标
- **CI 覆盖率报告**：GitHub Actions 生成 HTML 报告并上传 artifact（保留 14 天）
- **测试 fixture 修复**：`graph_app` 从 session 作用域改为 function，消除跨测试状态泄漏

### 文档 (2 项)
- **SECURITY.md**：完整的安全模型文档（认证架构 / 输入净化 / 限流策略 / LLM 安全 / 基础设施加固）
- **README 更新**：与代码实现对齐，修正过时描述

---

## v4.3 (2026-06-07) — 生产验收 + 密钥自动生成 + 低分重试

- **生产验收**：四维审查（安全 7.5 / 代码 8.0 / 测试 / 生产就绪性 7.0），综合评分 8.1/10
- **Feedback 类型修复**：`Feedback.created_at` 从 `time.time()` (float) 修正为 `datetime.now(timezone.utc)` (DateTime)
- **async 调用修复**：`container.py` 中 `create_session()` 添加 `await`
- **Prometheus 端点认证**：`/metrics/prometheus` 加入 admin 认证保护
- **密钥自动生成脚本**：`scripts/generate_prod_env.py` 生成 JWT Secret / Session Secret / API Key / Redis/PG 密码
- **低分重试机制**：ResponseAgent 评分低于 `EVAL_RETRY_THRESHOLD` 自动升级协作模式重试
- **漂移检测拆分**：`drift_detector.py` 独立模块，支持 4 种漂移类型
- **LLM Router 超时优化**：从 8s 降至 4s，配合熔断器快速 fallback
- **ReAct 迭代优化**：从 5 次降至 3 次，控制延迟在 20s 内

---

## v4.2 (2026-06-06) — SSE 真流式 + ServiceContainer 完全迁移

- **SSE 真流式输出**：`OpenAICompatibleClient.async_invoke_stream()` 使用 `httpx.stream()` 逐 token 推送
- **Agent 自动流式**：`BaseAgent._process_with_llm()` 检测 `stream_callback` 自动切换，所有 Agent 零改动
- **ServiceContainer 纯容器模式**：`api/app_factory.py` 完全使用 DI 容器，消除模块级全局变量
- **真实 LLM E2E 测试**：5/5 全部通过（硅基流动 Qwen2.5-7B-Instruct）
- **注入泄露修复**：输出层正则检测系统提示泄露 + 安全回复替换
- **RAG 检索质量评估**：30 条测试查询，Hit Rate@3 = 63.3%

---

## v4.1 (2026-06-05) — DeepSeek LLM + 依赖注入 + 全栈增强

- **DeepSeek LLM 接入**：默认模型切换为 deepseek-chat，支持 `LLM_PROVIDER` 环境变量切换
- **依赖注入容器**：`core/container.py` ServiceContainer 管理所有服务生命周期（两阶段：sync 基础设施 + async 组件）
- **SSE 流式输出**：`POST /api/chat/stream` 端点
- **Redis JWT 黑名单**：Token 吊销持久化到 Redis，重启不丢失
- **PostgreSQL 支持**：`DATABASE_URL` 环境变量切换 SQLite / PostgreSQL
- **Alembic 数据库迁移**：版本化 schema 管理
- **满意度反馈系统**：Feedback 模型 + 👍/👎 按钮 + 统计面板
- **自我评估闭环**：ResponseEvaluator 五维评分（完整性/准确性/简洁性/礼貌性/相关性）
- **A/B 测试框架**：SHA-256 确定性分流 + 统计分析
- **多模态图片识别**：`POST /api/chat/image` 端点
- **Loki 日志聚合**：Docker Compose + Promtail
- **灰度发布**：canary 服务 + Nginx 90/10 流量分割

---

## v4.0 (2026-06-05) — 用户认证 + 知识库管理 + 告警通知

- **用户认证体系**：SQLAlchemy ORM + JWT + PBKDF2-SHA256（600K 迭代）
- **双认证模式**：API Key（系统间）+ JWT Bearer（终端用户）
- **管理后台**：用户管理 / 知识库管理 / 告警配置 / 审计日志
- **知识库管理 API**：查看统计 / 重新种子 / ERP 同步 / 添加文档
- **告警通知**：Webhook（钉钉/企微/飞书）+ SMTP 邮件（含 SSRF 防护）
- **审计日志**：记录注册/登录等关键操作

---

## v3.9 (2026-06-03) — 生产就绪

- Nginx 反向代理 + TLS + WebSocket 支持
- Gunicorn 多 Worker（UvicornWorker）
- Prometheus + Grafana + Alertmanager 全栈监控
- 文件日志轮转（10MB / 5 份 / gzip 压缩）
- CI/CD 流水线（GitHub Actions：Python 3.10/3.11/3.12 + 安全扫描 + Docker 构建）

---

## v3.5 ~ v3.8 (2026-06-02 ~ 2026-06-03)

- **v3.8**：安全审计修复 + 测试合并
- **v3.7**：安全加固（监控端点 Token + WebSocket 限制 + 会话令牌签名 + TLS）
- **v3.6**：前端 WebSocket 修复 + 暗色主题 + 并发安全
- **v3.5**：RAG 知识库（ChromaDB）+ Function Calling + ReAct 推理模式

---

## v3.3 ~ v3.4 (2026-05-30 ~ 2026-06-01)

- **v3.4**：并发安全 + 安全加固 + 中文缓存（jieba + Jaccard）
- **v3.3**：缓存前置 + L1/L2 二级缓存
