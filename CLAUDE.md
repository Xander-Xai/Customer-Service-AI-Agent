# 药妆智多星多智能体客服系统

## 当前事实入口

- Runtime version: `6.3` (`core/config.py`); no `v6.4` release is declared.
- Current entry point: [docs/reference/current-state.md](docs/reference/current-state.md)（当前事实 + 验证命令；不硬编码 HEAD，`git rev-parse HEAD` 获取）。
- 带日期的对齐/审计报告（`docs/reports/plans/**`）是 HISTORICAL AUDIT SNAPSHOT，只在审计执行时点有效，不是永久 Current Truth。
- `UNKNOWN` / `NOT_MEASURED` / `NOT_VERIFIED` are evidence states, not successful outcomes.

## 项目概述
基于 LangGraph 的多智能体客服系统，面向化妆品生产/销售企业。
- Python 3.10+ / FastAPI / LangGraph / Qdrant
- 四层状态机：缓存 → 路由 → 协作模式 → 响应后处理
- 9 个 Agent 角色，5 种协作模式；Response Cache（三层）与 Tool Result Cache/Store/Compression/Session Memory 分开
- RAG：Qdrant 向量 + BM25 混合检索、retrieval contract、reranker、确定性 point ID 与迁移工具
- Tool Result Context Engineering：`core/tool_result_*.py`，含预算、压缩、offload/recovery、scope-safe exact reuse
- 测试数量以 `pytest --collect-only -q` 当前输出为准；不要复制历史 1400+ 数字
- v6.0 新增：Qdrant 向量数据库迁移 + ChromaDB→Qdrant 数据迁移脚本 + 并行运行模式
- v6.1 新增：统一多模态入口 + Widget 图片/语音 + 场景过滤 RAG + 5000+ 知识库文档
- 当前 HEAD：Tool Result Context Engineering、BM25 lifecycle、retrieval contract、Qdrant point-id migration、production evidence 口径收紧

## 常用命令

```bash
# 开发环境启动
make dev

# HTTPS 开发环境（支持麦克风等安全上下文功能）
make dev-https

# 生产环境启动
make prod

# 运行测试
make test

# 快速测试（跳过慢测试）
make test-fast

# 带覆盖率报告
make test-cov

# 代码检查（Ruff）
make lint

# 代码格式化（Ruff）
make format

# RAG 检索质量评估
make eval-rag

# 类型检查
mypy . --ignore-missing-imports

# pre-commit hooks 检查（防 .env 提交 + Ruff）
pre-commit run --all-files

# 环境切换
make env-dev     # 开发环境
make env-prod    # 生产环境
make env-test    # 测试环境
make env-check   # 查看当前环境

# 数据库迁移
make db-migrate   # 创建迁移
make db-upgrade   # 执行迁移
make db-downgrade # 回滚迁移
```

## 代码规范

### Python
- 异步优先：所有图节点和 API 端点使用 async/await
- 结构化日志：使用 `from core.logger import get_logger` 获取 logger
- 配置统一：所有配置从 `core/config.py` 读取，支持环境变量覆盖
- 类型注解：函数签名使用 typing 模块的类型注解
- Ruff 规则：E/W/F/I/UP/B/SIM，行宽 100，目标 Python 3.10
- 详见 `docs/standards/conventions.md` 和 `pyproject.toml`

### 文件组织
- `agents/` — AI Agent（继承 BaseAgent）+ ResponseEvaluator
- `api/` — FastAPI 服务层（app_factory/middleware/routes/dependencies）
- `auth/` — JWT 认证（Argon2id + Redis 黑名单 + Refresh Token）
- `core/` — 基础设施（DI容器/图构建/MessageBus/SharedBlackboard/Monitoring/PromptManager/ABTest/TokenTracker/TokenQuota）
- `core/session/` — 会话管理（SessionManager/DriftDetector/TokenCounter + **会话数据加密 AES-256-Fernet**）
- `db/` — SQLAlchemy 模型 + Alembic 迁移
- `rag/` — Qdrant 知识库 + 查询改写 + BM25 混合检索 + ApiReranker 重排 + RRF 融合（v6.0: 从 ChromaDB 迁移至 Qdrant，历史记录）
- `router/` — 双层查询路由（LLM + 规则并行 + 熔断器降级）
- `collaboration/` — 5 种协作模式 + Orchestrator
- `tools/` — Function Calling 工具注册（OpenAI 格式）
- `erp/` — 金蝶 ERP 适配器（Mock/Real + HMAC 认证 + 重试 + 分页）
- `llm/` — LLM 客户端（重试 + 熔断 + FC + SSE 流式 + 连接池）+ 规则兜底 LLM
- `media/` — 多模态处理（图片/音频/视频/文档/TTS 5 个处理器）
- `alerts/` — 告警通知（Webhook + SMTP）
- `knowledge/` — 知识库管理路由
- `cache/` — 三层缓存（L1 Redis MD5 精确匹配 + L2 Qdrant 向量语义 + L3 Jaccard 回退）
- `web/` — 前端（原生 JS + Vite 8 构建）
- `tests/` — 测试套件（unit/integration/e2e/stress/performance）

### 测试
- 单元测试在 `tests/unit/`，集成测试在 `tests/integration/`，端到端测试在 `tests/e2e/`（`real_llm` 标记需真实 API Key）
- 压力测试在 `tests/stress/`（`@pytest.mark.stress`）；RAG 基准资产在 `tests/eval/`
- 前端测试在 `web/src/__tests__/`（Vitest）
- **不要在文档里硬编码测试文件数或用例数**：用 `pytest --collect-only -q` / `npm test` 当前输出为准
- 大部分测试使用 MockLLM，不需要真实 API Key
- 真实 LLM E2E 测试需配置 `OPENAI_API_KEY`

### 环境配置
- `.env` — 当前激活环境配置（gitignore）
- `.env.example` — 配置模板（占位符，安全提交）
- `.env.dev` — 开发环境（宽松认证，详细日志，SQLite，gitignore）
- `.env.prod` — 生产环境（严格认证，JSON 日志，PostgreSQL + Redis，gitignore）
- `.env.test` — 测试环境（最小化依赖，Mock 一切，**提交到仓库**）
- 通过 `make env-dev/prod/test` 切换
- 生产密钥生成：`python3 scripts/generate_prod_env.py`

## 架构要点

### 四层状态机
1. **Layer 0 - 缓存检查**：L1 Redis MD5 精确匹配 + L2 Qdrant 向量语义 + L3 Jaccard 回退
2. **Layer 1 - 路由分类**：LLM 分类器 + 规则分类器并行（asyncio.gather），高置信规则匹配可跳过 LLM（路由捷径）
3. **Layer 2 - 协作模式**：Sequential / Parallel / Consultation / Hierarchical / ReAct
4. **Layer 3 - 响应后处理**：质量评估 + 模式升级重试 + 缓存写入 + SLA 监控 + 事件广播

### 安全要点
- API Key (系统间) + JWT (终端用户) 双认证模式
- Argon2id 密码哈希（v5.4 升级，OWASP 2023 推荐，64MB 内存硬度）+ PBKDF2-SHA256 向后兼容
- 4 级 RBAC：customer / agent / supervisor / admin
- 输入净化（控制字符 + HTML 标签 + XSS 防护）
- 限流：通用 60/min/IP，登录 5/5min，注册 3/hour
- CSRF 双重 Cookie 提交 + CSP（script-src 用 nonce，style-src 用 'unsafe-inline'）
- WebSocket 首条消息认证 + 连接限制 + 消息限流 + 空闲超时
- SSRF 防护：Webhook URL 验证阻止私有 IP / 回环 / 元数据端点
- **Token Quota：用户级 Token 消耗限额（每日/每月），Redis 持久化 + 内存回退**
- **黑板 Session 隔离：ContextVar 按 session 隔离 Agent 间共享数据**
- **会话数据加密：AES-256-Fernet 可选加密（`SESSION_ENCRYPTION_KEY`）**
- 生产启动校验：强制 `JWT_SECRET`(≥32字符) / `SESSION_TOKEN_SECRET` / `API_KEY`

### LLM 配置
- 默认使用硅基流动（`Qwen/Qwen3-8B` 模型）
- 通过 `LLM_PROVIDER` 环境变量切换（siliconflow / deepseek / openai / custom）
- 熔断器保护：连续 5 次失败后自动降级到规则引擎
- 路由捷径：高置信规则匹配（confidence ≥ 0.75）跳过 LLM 路由调用
- RAG 预取：与路由分类并行执行，Agent 可直接使用预取结果
- 缓存预热：启动时通过 HTTP API 预热通用高频问题（`scripts/warm_cache.py`；条数以脚本内 WARM_QUERIES 当前值为准）

### 部署
- Docker Compose 6 个变体：base / prod / override / canary / scale / monitoring
- Nginx 反向代理 + TLS + WebSocket 支持
- Prometheus + Grafana + Alertmanager + Loki 监控栈
- 灰度部署：`make canary`（90/10 流量分割）
- 水平扩展：`make scale N=3`
