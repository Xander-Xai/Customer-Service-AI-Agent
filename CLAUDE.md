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
- **分布式 Agent Runtime**：`runtime/` + `core/concurrency/distributed_lock.py`（Hybrid 实时/异步架构 + PostgreSQL checkpoint + Redis Session/Thread Lock + AgentRun/Celery worker + at-least-once + 幂等副作用 + 崩溃恢复）
- 测试数量以 `pytest --collect-only -q` 当前输出为准；不要复制历史 1400+ 数字
- v6.0 新增：Qdrant 向量数据库迁移 + ChromaDB→Qdrant 数据迁移脚本 + 并行运行模式
- v6.1 新增：统一多模态入口 + Widget 图片/语音 + 场景过滤 RAG + 5000+ 知识库文档
- 当前 HEAD：Distributed Agent Runtime（durable checkpoint resume / worker recovery / tool side-effect 幂等 wiring / run events + DLQ replay / real-infrastructure 验收与 chaos 测试）。**这些是当前已实现能力，不是计划**；证据等级见下方 Level 1/2/3。
- **RAG 当前 649-query 正式指标为 NOT_VERIFIED**：有 provenance-bearing 的正式 artifact 之前，AI Agent 不得生成、引用或传播任何"当前 Hit@K / MRR / NDCG / Recall 百分比"。历史 30-query 快照（2026-06）只能作历史对比叙事。评测（ablation / 指标分母 / evidence 状态）canonical reference：[docs/reference/rag-evaluation.md](docs/reference/rag-evaluation.md)。

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

# RAG 检索质量评估（兼容 alias；正式评测入口是 rag-eval-649）
make eval-rag

# RAG 649 evidence pipeline（canonical formal evaluation）
make rag-eval-import          # 导入评测语料（幂等 + gold 覆盖率审计 + manifest）
make rag-eval-649-preflight   # preflight gate（provider auth / Qdrant / BM25）
make rag-eval-649-smoke       # 冒烟（前 16 条，不是正式证据）
make rag-eval-649             # 正式 649 全量 4-config ablation → evidence artifact

# 分布式 Agent Runtime 验证（需要真实 PostgreSQL + Redis；Makefile 始终注入
# TEST_DISTRIBUTED_DB_URL / TEST_REDIS_URL，基础设施缺失时是硬 FAIL，不静默 skip）
make runtime-e2e        # tests/integration/runtime：真实 PG + Redis + 多进程 Celery 验收
make runtime-chaos      # scripts/test_worker_crash_recovery.py → artifacts/runtime/chaos-<ts>.json
make runtime-verify     # scripts/verify_distributed_runtime.py → artifacts/distributed-runtime/<ts>/report.json
make runtime-replay-help # scripts/replay_dead_run.py --help（DLQ 人工重放）

# 指标暴露链（Prometheus 告警闭环；见 docs/limitations.md §1.2）
make metrics-exposure-check  # 契约测试：告警/Grafana 引用的每个指标名都真的可达
make monitoring-token        # 生成 Prometheus 抓取凭据（fail-closed：缺失/占位符/过短直接失败）
make alert-rules-test        # 官方 promtool check config + test rules（真会 firing / 不 latching）
make metrics-exposure-verify # 真实 Prometheus 端到端：DLQ 告警实际进入 FIRING
make monitoring-up           # 启动完整监控栈（Prometheus + Grafana + Alertmanager + Loki + Promtail）
make openapi-check      # docs/openapi.json 与 app.openapi() 表面一致
make facts              # python3 scripts/project_facts.py
make audit-docs         # 文档一致性/语义漂移守卫

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
- `core/concurrency/` — 跨进程互斥（`distributed_lock.py`：Redis per-thread 分布式锁，owner token + TTL + Lua 原子 compare-and-delete；API 执行边界与 worker 共用同一 key namespace `agent:thread-lock:`）
- `runtime/` — 分布式 Agent Runtime（`run_service.py` AgentRun 真相源 / `statuses.py` 状态机 / `executor.py` worker 执行与失败分类 / `dispatch.py` Celery 投递 / `celery_app.py`+`tasks.py` worker / `thread_lock.py` per-thread 锁 / `side_effects.py` 工具幂等 ledger / `events.py` Redis Stream 事件 / `bootstrap.py` checkpoint resume / `retry.py` 退避 / `metrics.py` 指标转发 / `errors.py` 错误分类）
- `db/` — SQLAlchemy 模型 + Alembic 迁移（含 `agent_runs` / `agent_dead_letters` / `tool_side_effects`）
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
- **分布式 runtime 真实基础设施验收在 `tests/integration/runtime/`**（真实 PostgreSQL + Redis + 多进程 Celery；`TEST_DISTRIBUTED_DB_URL` / `TEST_REDIS_URL` 未设置时 skip，经 `make runtime-e2e` 时是硬 FAIL）
- runtime 契约单测：`tests/unit/test_distributed_runtime.py` / `test_runtime_architecture_contract.py` / `test_execution_mode_contract.py` / `test_runtime_metrics_contract.py`（无外部依赖，纯配置/契约断言）
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

### 分布式 Agent Runtime（已实现，非计划）

- **Hybrid 实时/异步架构**：实时快路径 `POST /api/chat` / `/api/chat/stream`
  （FastAPI → LangGraph → SSE，始终 inline，不经 worker）保持不变；长任务走
  异步路径 `POST /api/runs`（落库 + 入队立即返回）→ Celery worker →
  `GET /api/runs/{run_id}` polling。
- **PostgreSQL LangGraph checkpoint**：`core/checkpointer.py`，
  `LANGGRAPH_CHECKPOINT_BACKEND=postgres` → 官方 `AsyncPostgresSaver`，
  跨 worker/副本共享、重启可恢复；生产初始化失败 **fail closed**，不静默回退
  `MemorySaver`。API 快路径与 worker 异步路径共享同一后端。
- **Redis Session**：`SESSION_STORAGE_BACKEND=redis`（生产强制），
  避免多 worker / 多副本分片与重启丢失。
- **Redis Thread Lock**：`agent:thread-lock:{thread_id}`，owner token + TTL +
  Lua 原子 compare-and-delete；同一 thread 串行、不同 thread 并发。
  API 执行边界（`api/app.py::_run_graph`）与 worker 共用同一 key namespace。
- **AgentRun canonical state**：数据库 `agent_runs` 表是业务状态**唯一真相源**，
  状态机 `PENDING → QUEUED → RUNNING → WAITING_APPROVAL → RUNNING →
  SUCCEEDED | FAILED | RETRYING → RUNNING | DEAD_LETTER`，任意未终态可
  `→ CANCELLED`（`runtime/statuses.py`）。
  Celery result backend **不是**真相源（`task_ignore_result=True`）。
- **四个 ID/概念严格区分**：`thread_id`（对话级 == `session_id` == LangGraph
  thread）/ `run_id`（`agent_runs.id`，单轮执行）/ `task_id`（Celery task id）/
  `AgentRun`（业务运行记录）。
- **at-least-once + 幂等**：`task_acks_late` + `task_reject_on_worker_lost` +
  Redis `visibility_timeout` → **不是** exactly-once。幂等由三层承担：
  run 级（终态重复投递 no-op + `idempotency_key` 唯一约束）、thread 级（Redis
  锁 + lease）、工具级（`tool_side_effects` ledger，
  `operation_key = run_id:tool_call_id`）。
- **崩溃/恢复语义**：worker 崩溃 → 未 ACK 任务经 `visibility_timeout` 重投 →
  lease 过期后 `mark_running` 接管（attempt+1）→ **从 checkpoint `next` 续跑**
  （`runtime/bootstrap.py::invoke_graph_with_resume`）→ retry 退避 → 用尽
  写 `agent_dead_letters` + `DEAD_LETTER` → `scripts/replay_dead_run.py`
  人工重放（**复用原 run_id**，避免绕过工具幂等键）。
- **Run 事件流**：worker 写 Redis Stream `agent:run:{run_id}:events`，
  API 经 `GET /api/runs/{run_id}/events` 转 SSE（支持 `Last-Event-ID` 续读）。
  定位是**观测通道而非真相源**：best-effort resumable，**不是** exactly-once。

#### 证据边界（Level 1/2/3）

| 层级 | 含义 | 当前状态 |
|---|---|---|
| **Level 1 — IMPLEMENTED** | 代码存在且可读 | Postgres checkpoint、Redis session、Redis per-thread lock、AgentRun 真相源、Celery + Redis broker、worker execution、`run_id` dispatch、acks_late / reject_on_worker_lost / visibility_timeout、retry、tool ledger、Prometheus metrics |
| **Level 2 — CI VERIFIED** | 真实基础设施 + 命令 + artifact | `make runtime-e2e`（真实 PG + Redis + 多进程 Celery）、`make runtime-chaos`（SIGKILL worker 恢复）、`make runtime-verify`（`artifacts/distributed-runtime/<ts>/report.json`，schema `distributed-runtime-evidence/v2`，带 `tested_code_sha` + `generated_at`） |
| **Level 3 — 未生产验证** | `NOT_VERIFIED` | 真实生产集群 / 多副本长期稳定 / 真实用户流量 / 真实 ERP 写操作 / 大规模 queue backlog / K8s autoscaling / multi-region |

> **Level 2 ≠ 生产验证。** 本仓库文档与任何对外表述都不得把 Level 2 表述为
> "生产集群已验证"。

### Human-in-the-Loop 高风险审批治理（已实现，非计划）

目标不是展示 LangGraph `interrupt` API，而是给**高风险 Tool Side Effect**
建立服务端治理边界（完整设计：
[docs/design/human-in-the-loop.md](docs/design/human-in-the-loop.md)）。

- **风险分级** `core/hitl/risk.py`：LOW / MEDIUM / HIGH；优先级为
  「工具显式声明 > HIGH 白名单 > 金额阈值 > MEDIUM 白名单 >
  **未认领风险等级的有副作用工具 = HIGH** > 默认 LOW」；**只有 HIGH 需要
  人工审批**。判定失败时 fail-closed（挂起而非放行）。
- **治理必须真的生效（fail-closed，两层互补）**：
  - 启动校验 `core.config.validate_hitl_settings`：`HITL_ENABLED=true` 但两条
    HIGH 规则全空 → **拒绝启动**（此前是 LOW 全放行且无任何告警）。
    校验**先于** `DEV_MODE` 早退，开发机同样受约束。
  - 执行期兜底：治理开启时，`side_effect=True` 却未被任何规则认领的工具一律
    判 HIGH；**只读工具完全不受影响**（普通问答/订单查询路径不变）。
- **并发与恢复（真实 PostgreSQL 验证）**：`decide()` 是数据库级 CAS
  （`UPDATE ... WHERE status=PENDING`），N 个并发决策**恰好一个赢家**；
  `consume_resume()` 是 `WHERE resumed_at IS NULL`，N 个并发恢复**恰好一份载荷**。
  **无决策时 `consume_resume` 返回 None** —— 重试与 worker 崩溃恢复**不能**绕过审批。
- **拦在执行之前**：Agent 工具循环把 HIGH 风险调用**摘出**到
  `state["pending_actions"]`（不执行），由图节点 `human_approval_gate`
  （协作模式与 `final_response` 之间）逐个 `interrupt()`。
- **durable 审批**：`human_approvals` 表（`alembic 006`，唯一约束
  `(run_id, action, proposal_fingerprint)`）；`PENDING → APPROVED | REJECTED |
  EXPIRED`，均为终态。
- **职责分离**：`reviewer_id != user_id`，在 **service 层**强制
  （`ApprovalService._guard_reviewer`），API 层不是唯一防线。
- **RBAC**：仅 `admin` / `supervisor`（4 级 RBAC 的高级角色）可读可决策。
- **TTL**：`HITL_APPROVAL_TTL_SECONDS`（默认 3600s），到期落 `EXPIRED`
  **按拒绝处理，绝不默认放行**；可在决策 / 读取 / 恢复三处收敛，图不会死等。
- **脱敏留痕**：`core/hitl/sanitize.py` 黑名单 + 定长截断（**不用**事件流白名单
  语义——那会把提案整个抹掉，审批退化成盲批）。
- **幂等双防线（缺一不可）**：审批防「不该做的被做了」；side-effect ledger 防
  「做了一次被重做」。已批准执行走
  `operation_key = run_id:approval:{approval_id}`（由 `approval_id` 派生，
  在任意次重试中恒定）→ 恰好一次。**复用** `runtime/side_effects.py` 的原子
  `claim`（PR #28），不重复实现。
- **新非终态 `WAITING_APPROVAL`**（`runtime/statuses.py`）：**不进**
  `EXECUTABLE_STATUSES`（通用轮询不捞起，否则忙循环）；`→ RUNNING`
  **不递增 attempt**（等人不是失败，不该消耗 `AGENT_RUN_MAX_ATTEMPTS`）。
- **API**：`GET /api/approvals`、`GET /api/approvals/{id}`、
  `POST /api/approvals/{id}/decision`（approve / edit / reject）、
  `GET /api/approvals/by-run/{run_id}`。
- **验证过的 LangGraph 语义**（langgraph 1.2.12 / Python 3.10，真实 PG
  checkpoint 上实测，测试
  `tests/integration/runtime/test_hitl_langgraph_interrupt.py`）：
  `interrupt()` 不抛异常而注入 `__interrupt__`；`ainvoke(None)` **解除不了**
  interrupt（故崩溃恢复与审批恢复必须分两条路径）；`Command(resume=...)` 需要
  checkpointer；已完成的 run 返回值**不得**再带 `__interrupt__`（依赖图状态声明
  为 TypedDict）。
- **边界（不得越界宣称）**：`HITL_ENABLED` 默认 `false`；`/api/chat` 快路径无
  run 上下文，**明确不在该治理边界内**；**真实 ERP 退款/改单未验证**（无企业
  staging，`NOT_VERIFIED`）——副作用验证走 `tools/hitl_staging_tools.py`
  确定性 staging 工具，它验证的是**治理机制**而非 ERP 集成正确性；无审批主动
  通知链路（需轮询待审批队列）。

### 安全要点
- API Key (系统间) + JWT (终端用户) 双认证模式
- Argon2id 密码哈希（v5.4 升级，OWASP 2023 推荐，64MB 内存硬度）+ PBKDF2-SHA256 向后兼容
- 4 级 RBAC：customer / agent / supervisor / admin
- 输入净化（控制字符 + HTML 标签 + XSS 防护）
- 限流：通用 60/min/IP，登录 5/5min，注册 3/hour
- CSRF 双重 Cookie 提交 + CSP（script-src 用 nonce，style-src 用 'unsafe-inline'）
- WebSocket 首条消息认证 + 连接限制 + 消息限流 + 空闲超时
- SSRF 防护：Webhook URL 验证阻止私有 IP / 回环 / 元数据端点
- 结构化日志 + 两层密钥脱敏：**Implemented**（含 `scrub_exception_message()` 就地脱敏，防止 ASGI 自己的 traceback 泄漏）
- **Token Quota：用户级 Token 消耗限额（每日/每月），Redis 持久化 + 内存回退**
- **黑板 Session 隔离：ContextVar 按 session 隔离 Agent 间共享数据**
- **会话数据加密：AES-256-Fernet 可选加密（`SESSION_ENCRYPTION_KEY`）**
- 生产启动校验（fail-fast，非 warning）：
  - 凭据类：`JWT_SECRET`(≥32字符) / `SESSION_TOKEN_SECRET` / `API_KEY`
  - 分布式运行时（`core/config.py::validate_distributed_runtime_settings`）：
    `SESSION_STORAGE_BACKEND=redis` + `AGENT_RUN_DISPATCH=celery`
    （等价 canonical 旋钮 `AGENT_EXECUTION_MODE=queued`）；
    `LANGGRAPH_CHECKPOINT_BACKEND` 生产不得为 `memory`；
    `AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT + 30`
  - 多 worker 一致性 gate（`GUNICORN_WORKERS>1` 时额外要求）：
    `LANGGRAPH_CHECKPOINT_BACKEND=postgres` + `SESSION_STORAGE_BACKEND=redis`
    + `AGENT_RUN_THREAD_LOCK_ENABLED=true`/`AGENT_RUN_THREAD_LOCK_BACKEND=redis`

### LLM 配置
- 默认使用硅基流动（`Qwen/Qwen3-8B` 模型）
- 通过 `LLM_PROVIDER` 环境变量切换（siliconflow / deepseek / openai / custom）
- 熔断器保护：连续 5 次失败后自动降级到规则引擎
- 路由捷径：高置信规则匹配（confidence ≥ 0.75）跳过 LLM 路由调用
- RAG 预取：与路由分类并行执行，Agent 可直接使用预取结果
- 缓存预热：启动时通过 HTTP API 预热通用高频问题（`scripts/warm_cache.py`；条数以脚本内 WARM_QUERIES 当前值为准）

### 部署
- Docker Compose 6 个变体：base / prod / override / canary / scale / monitoring
- 异步 Run 执行在独立 `worker` service（Celery）；生产必须
  `AGENT_RUN_DISPATCH=celery`，`inline` 仅开发/测试
- Nginx 反向代理 + TLS + WebSocket 支持
- Prometheus + Grafana + Alertmanager + Loki 监控栈
- 灰度部署：`make canary`（90/10 流量分割）
- 水平扩展：`make scale N=3`
