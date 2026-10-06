# 药妆智多星 · LangGraph 多智能体客服系统

面向化妆品生产/销售企业的 **LangGraph 多 Agent 客服运行时**：四层状态机动态路由（缓存检查 → 意图路由 → 专家 Agent 协作 → 响应后处理），叠加分布式执行底座（PostgreSQL checkpoint + Redis 锁 + Celery worker + 工具副作用幂等 ledger）与高风险操作的人工审批治理。

不是 demo：`make test` 默认**离线**可跑（无 API Key、不出公网），CI 在 3 个 Python 版本 + 真实 PostgreSQL/Redis 上跑同一套用例。

| 事实项 | 当前值 | 真相源 |
|---|---|---|
| Runtime version | `6.3` | `core/config.py::VERSION` |
| LLM（默认） | `siliconflow` / `Qwen/Qwen3-8B` | `core/config.py` |
| Embedding / Rerank | `BAAI/bge-large-zh-v1.5` / `BAAI/bge-reranker-v2-m3` | `core/config.py` |
| Agent 角色 / 协作模式 | 9 / 5 | `agents/`、`collaboration/modes.py` |
| OpenAPI 路径数 | 62 | `docs/openapi.json`（`make openapi-check` 校验） |
| RAG 正式 649-query 指标 | **NOT_VERIFIED** | [docs/reference/rag-evaluation.md](docs/reference/rag-evaluation.md) |
| 生产上线验收 | **未完成** | [生产准备度检查清单](docs/checklists/production-readiness-checklist.md) |

**入口文档**：[docs/reference/current-state.md](docs/reference/current-state.md)。历史版本变更在 [docs/reports/releases/changelog.md](docs/reports/releases/changelog.md)，本 README 不展开逐版本历史。

> **关于证据**：本仓库区分「代码存在」与「证据支持」。正式 RAG 指标、生产 QPS/P95、真实 ERP 写操作、真实用户业务指标一律标记 `NOT_VERIFIED` / `NOT_MEASURED`，并登记在 [Issue #7](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/7)。任何未经 provenance 支撑的数字都不应从这里、README 或面试材料中被引用为当前结果。

---

## 目录

- [架构](#架构)
- [四层状态机](#四层状态机)
- [五种协作模式](#五种协作模式)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [业务场景](#业务场景)
- [快速开始](#快速开始)
- [测试与证据](#测试与证据)
- [企业工程能力](#企业工程能力)
- [已知限制](#已知限制)
- [文档导航](#文档导航)

---

## 架构

```
graph TB
    subgraph Input["接入层"]
        WS[WebSocket /ws/chat]
        REST[REST API /api/chat]
        SSE[SSE /api/chat/stream]
        MM["统一多模态入口 /api/chat/multimodal"]
    end

    subgraph Middleware["中间件层"]
        TRACE["1. trace_id 追踪"]
        CSRF["2. CSRF 双重 Cookie 提交"]
        AUTH["3. API Key / JWT · 4 级 RBAC"]
        CSP["4. 安全头 CSP/HSTS"]
        RATE["5. 限流 60req/min/IP"]
    end

    subgraph Graph["LangGraph StateGraph"]
        C0["Layer 0 · check_cache"]
        C1["Layer 1 · classify_query"]
        C2["Layer 2 · 协作模式（5 选 1）"]
        C3["Layer 3 · final_response"]
    end

    subgraph Cache["三层响应缓存"]
        L1["L1 Redis 精确缓存 · MD5 SETEX"]
        L2["L2 Qdrant 语义缓存 · BGE 向量"]
        L3["L3 Jaccard 回退 · jieba 分词"]
    end

    subgraph Router["双层路由（asyncio.gather 并行）"]
        LLM["LLM Router · JSON 分类"]
        RULE["Rule Classifier · 正则 + 复杂度评分"]
    end

    subgraph Experts["9 个 Agent 角色"]
        PA[ProductAgent] TA[TechAgent] BA[BillingAgent]
        CA[ComplaintAgent] GA[GeneralAgent] SA[SalesAgent]
        AA[AftersalesAgent] RA[ReActAgent] RPA[ResponseAgent]
    end

    subgraph Retrieval["检索与工具"]
        RAG["Qdrant · 4+1 collection"]
        BM25["BM25 词法检索"]
        RRF["RRF 融合"]
        RRK["Reranker · bge-reranker-v2-m3"]
        ERP["金蝶 ERP · Mock/Real · HMAC"]
        FC["Function Calling 工具集"]
    end

    subgraph Runtime["分布式执行底座"]
        RUN["AgentRun 状态机 · agent_runs 表为唯一真相源"]
        CP["PostgreSQL LangGraph Checkpoint"]
        LOCK["Redis per-thread 锁 · Lua CAS"]
        CELERY["Celery worker · acks_late + 幂等"]
        LEDGER["tool_side_effects 幂等 ledger"]
        DLQ["agent_dead_letters + 重放"]
    end

    subgraph HITL["人工审批治理"]
        GATE["human_approval_gate · 风险分级"]
        APPR["human_approvals · TTL / 职责分离"]
    end

    WS --> TRACE --> CSRF --> AUTH --> CSP --> RATE --> C0
    C0 --> L1 -->|"hit"| C3
    L1 -->|"miss"| L2 -->|"hit"| C3
    L2 -->|"miss"| L3 -->|"hit"| C3
    L3 -->|"miss"| C1
    C1 --> LLM & RULE --> C2
    C2 --> Experts --> C3
    Experts --> RAG & ERP & FC
    RAG --> BM25 --> RRF --> RRK --> Experts
    C2 --> GATE --> APPR
    RUN --> CP & LOCK & CELERY & LEDGER & DLQ

    style C0 fill:#e1f5fe
    style C1 fill:#fff3e0
    style C2 fill:#e8f5e9
    style C3 fill:#fce4ec
    style HITL fill:#fff8e1
```

## 四层状态机

| 层 | 节点 | 做什么 | 关键设计 |
|---|---|---|---|
| **Layer 0** | `check_cache` | L1 精确 → L2 语义 → L3 Jaccard 回退 | 命中直接短路到 Layer 3；embedding 不可用时 **fail closed**，不静默降级为词法命中 |
| **Layer 1** | `classify_query` | LLM 分类器 ∥ 规则分类器 | `asyncio.gather` 并行；高置信规则匹配（≥ 0.75）**跳过 LLM 调用** |
| **Layer 2** | 协作模式节点 | Sequential / Parallel / Consultation / Hierarchical / ReAct | 模式由路由结果 + 查询复杂度决定，不是硬编码分支 |
| **Layer 3** | `final_response` | 质量评估 → 模式升级重试 → 缓存写入 → SLA 监控 → 事件广播 | 评估不达标则升级协作模式重跑，而不是原样返回 |

Response Cache（L1/L2/L3）与 **Tool Result Cache、Tool Result Store、Session Memory、LangGraph Checkpoint 是相互独立的机制**（[ADR-006](docs/decisions/)），不共用 TTL 或失效条件。

## 五种协作模式

| 模式 | 何时选 | 结构 |
|---|---|---|
| `Sequential` | 单意图、单一领域 | 一个 Agent 串行执行 |
| `Parallel` | 多领域可切分（成分 + 库存 + 价格） | 并发执行 → 聚合 |
| `Consultation` | 需要专家意见但由主 Agent 交付 | 主 Agent + 顾问 Agent |
| `Hierarchical` | 复杂任务需分解 | 协调者拆解 → 子任务 → 汇总 |
| `ReAct` | 需检索 + 工具调用的多步推理 | RAG → Function Calling → 观察 → 迭代 |

实现：`collaboration/modes.py`。

## 项目结构

```
customer-service-ai-agent/
├── agents/         # 9 个 Agent 角色 + 质量评估器
├── core/           # 配置 / DI 容器 / 图构建 / 消息总线 / 监控 / 会话 / Prompt 管理 / Token 配额
├── api/            # FastAPI 服务层（路由 / SSE / WebSocket / 中间件）
├── auth/           # JWT + Argon2id + Redis 黑名单 + 4 级 RBAC
├── router/         # 双层查询路由（LLM ∥ 规则 + 熔断器降级）
├── collaboration/  # 5 种协作模式 + 升级重试
├── rag/            # Qdrant + BM25 + RRF + 重排 + 查询改写
├── cache/          # 三层响应缓存（与 Tool Result Cache / Store 分离）
├── runtime/        # 分布式 Agent Runtime（状态机 / thread 锁 / worker / retry / DLQ）
├── db/             # SQLAlchemy 模型 + Alembic 迁移（7 个版本）
├── erp/            # 金蝶 ERP 适配器（Mock / Real + HMAC + 重试 + 分页）
├── tools/          # Function Calling 工具注册
├── llm/            # LLM 客户端（重试 / 熔断 / FC / SSE / 连接池）+ 规则兜底
├── media/          # 多模态（图片 / 音频 / 视频 / 文档 / TTS）
├── deploy/         # 部署资产根（compose/ · nginx/ · loki/ · otel/）
├── monitoring/     # Prometheus + Grafana + Alertmanager
├── web/            # 前端（原生 JS + Vite 8 + Vitest）
├── tests/          # unit / integration / runtime / e2e / stress
├── docs/           # 文档与 ADR
├── alembic/        # 数据库迁移脚本（7 个版本）
└── scripts/        # 运维脚本（部署 / 备份 / RAG 评估 / 密钥生成）
```

完整目录说明见 [docs/reference/project-structure.md](docs/reference/project-structure.md)。

> `nginx/` 与 `loki/` **不在仓库根**，它们在 `deploy/nginx/` 与 `deploy/loki/` 下——这正是 issue #48 里 `build: ../../nginx` 被误信的由来。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph（StateGraph + PostgreSQL checkpointer + `interrupt()`） |
| 服务 | Python 3.10+ · FastAPI · SSE · WebSocket · Uvicorn/Gunicorn |
| 检索 | Qdrant（4+1 collection） · BM25 · RRF 融合 · `bge-reranker-v2-m3` |
| 异步 | Celery（Redis broker） · `acks_late` · `visibility_timeout` · DLQ |
| 状态 | PostgreSQL 15（`agent_runs` / `agent_dead_letters` / `tool_side_effects` / `human_approvals`）+ Redis 7（Session / Cache / JWT 黑名单 / per-thread 锁） |
| 观测 | OpenTelemetry 语义 span + Prometheus + Grafana + Alertmanager + Loki |
| 安全 | Argon2id · JWT（access + refresh + Redis 黑名单）· 4 级 RBAC · CSRF 双提交 · CSP nonce · SSRF 防护 · 限流 |
| 部署 | Docker Compose 6 变体（base / prod / override / canary / scale / monitoring）· Nginx + TLS |
| 工具 | OpenAI Function Calling 格式 · 金蝶 ERP 适配器（Mock/Real + HMAC + 重试 + 分页） |
| 前端 | 原生 JS + Vite 8 · Vitest（可嵌入 widget） |

## 业务场景

| 场景 | Agent | 涉及能力 |
|---|---|---|
| 产品成分/功效咨询 | `ProductAgent` | RAG + 查询改写 + BM25 混合检索 |
| 技术使用与故障排查 | `TechAgent` | RAG + 多跳重排 |
| 账单与对账 | `BillingAgent` | ERP Function Calling |
| 投诉与差评处理 | `ComplaintAgent` | RAG + 会话记忆 + 情绪路由 |
| 售前推荐 | `SalesAgent` | RAG + 场景过滤 |
| 售后退换 | `AftersalesAgent` | ERP 写操作 → **高风险 → HITL 审批** |
| 复杂订单问题 | `GeneralAgent` + 协作模式 | Hierarchical 任务分解 |

---

## 快速开始

### 最小可跑路径（默认离线，不需要任何 API Key）

```bash
# 1) 环境
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2) 跑测试（默认离线：不发起公网请求，Mock 一切外部依赖）
make test

# 3) 读当前事实（不硬编码任何数字，全部从代码/配置推导）
make facts
make audit-docs        # 文档一致性守卫
```

### 起服务

```bash
# 开发（热重载）
make dev               # → http://localhost:8000

# 生产栈（Docker Compose + Nginx + TLS）
make prod              # 缺 TLS 物料会 fail fast 并告诉你准备什么
make tls-local-cert    # 仅本地开发：生成本地自签证书
```

### 部署前必须设置的变量

| 变量 | 为什么 | 缺了会怎样 |
|---|---|---|
| `ADMIN_PASSWORD` | app 首次启动用它引导 admin 账号 | `docker compose config` 直接失败并提示（worker 不需要，也不会收到这个变量） |
| `POSTGRES_PASSWORD` / `JWT_SECRET` / `SESSION_TOKEN_SECRET` / `API_KEY` | 生产凭据 | 启动校验 fail-fast，不降级 |
| `OPENAI_API_KEY` 或 `LLM_PROVIDER` 对应凭据 | LLM 调用 | 熔断器连续失败后降级到规则引擎 |

配置全表见 [docs/reference/configuration.md](docs/reference/configuration.md)，模板见 [`.env.example`](.env.example)（提交的是占位符，不是密钥）。

### 端口语义

| 场景 | 地址 | 说明 |
|---|---|---|
| 开发（`make dev` / dev override） | `localhost:8000` | dev override **确实**发布 app:8000 |
| 生产 | nginx `NGINX_HTTP_PORT` / `NGINX_HTTPS_PORT`（默认 80 / 443） | 生产栈里 app 只 `expose: 8000`、**不对宿主机发布** |

生产栈唯一发布应用端口的是 nginx——TLS、安全响应头、灰度流量分割都在它那里。绕开 nginx 直连 8000 会同时丢掉这三层能力，所以 **8000 不是生产可达地址**。Prometheus 在所有变体里都是 `expose`-only（它的 HTTP API 无鉴权），只能通过 `docker compose exec` 访问。


### 数据与检索

```bash
make use-qdrant                 # 启动本地 Qdrant
make generate-knowledge-base     # 灌入种子知识库
```

---

## 测试与证据

```bash
make test          # 默认离线：无 API Key、无公网访问
make test-fast     # 跳过 stress 标记的慢测试
make test-cov      # 覆盖率报告（门槛 80%）
npm test           # 前端 Vitest（数量以命令输出为准）
npm run build      # 前端产物构建
```

测试数量以 `pytest --collect-only -q` / `npm test` 的当前输出为准，不在文档里硬编码。

| 门禁 | 命令 | 需要的环境 |
|---|---|---|
| 文档事实一致性 | `make audit-docs` | 无 |
| OpenAPI 快照一致 | `make openapi-check` | 无 |
| 分布式运行时验收 | `make runtime-e2e` | 真实 PostgreSQL + Redis + 多进程 Celery |
| worker 崩溃恢复 | `make runtime-chaos` | 同上 |
| 运行时证据报告 | `make runtime-verify` | 同上，产出 `artifacts/distributed-runtime/<ts>/report.json` |
| DLQ 人工重放 | `make runtime-replay-help` | 同上 |
| RAG 正式 649-query 评测 | `make rag-eval-import` → `make rag-eval-649-preflight` → `make rag-eval-649` | 真实 provider + 已索引语料（**当前 NOT_VERIFIED**，见 Issue #7） |

CI 在 Python 3.10 / 3.11 / 3.12 上跑同一套单测与集成测试，另有 `dev-compat`（pytest 8 兼容）、`runtime-e2e`（真实 PG + Redis）、`security`（mypy 严格档 + bandit + secret guard）三条独立 lane。

评测与证据的完整口径见 [docs/evaluation/](docs/evaluation/) 与 [docs/reference/rag-evaluation.md](docs/reference/rag-evaluation.md)。

---

## 企业工程能力

这一节是本项目与「会调 LangChain」的差别所在。每一项都有源码与测试锚点。

### 分布式 Agent Runtime

- **AgentRun 是唯一真相源**：业务状态只认 `agent_runs` 表；Celery result backend **不是**真相源（`task_ignore_result=True`）。状态机见 `runtime/statuses.py`。
- **四个 ID 严格区分**：`thread_id`（对话级 == `session_id`）· `run_id`（单轮执行）· `task_id`（Celery）· `AgentRun`（业务记录）。
- **at-least-once，不是 exactly-once**：`task_acks_late` + `task_reject_on_worker_lost` + Redis `visibility_timeout`。幂等由三层承担——run 级（终态重复投递 no-op + `idempotency_key` 唯一约束）· thread 级（Redis 锁 + lease）· 工具级（`tool_side_effects` ledger，`operation_key = run_id:tool_call_id`）。
- **崩溃恢复**：worker 崩溃 → 未 ACK 任务重投 → lease 过期后 `mark_running` 接管 → **从 checkpoint `next` 续跑**（`runtime/bootstrap.py`）→ 退避重试 → 用尽写 DLQ → `scripts/replay_dead_run.py` 复用原 `run_id` 人工重放（不绕过工具幂等键）。
- **归属权 fencing（owner CAS）**：worker 提交终态走 `transition_owned()`，ownership 谓词长在 **同一条 UPDATE 的 WHERE 内部**（`SELECT → 判断 → UPDATE` 是 TOCTOU）。失去所有权抛 `RunOwnershipLost`，executor 读到即退出：不记失败、不重试、不进 DLQ、不消耗 attempt。

### Human-in-the-Loop 高风险审批

高风险工具调用在**执行前**被摘出到 `state["pending_actions"]`（不执行），由图节点 `human_approval_gate` 逐个 `interrupt()`。

- 风险分级 LOW/MEDIUM/HIGH，优先级为「工具显式声明 > 工具名白名单 > 金额阈值 > 默认 LOW」，**只有 HIGH 需要审批**；判定失败 fail-closed。
- 审批落表（唯一约束 `(run_id, action, proposal_fingerprint)`），`PENDING → APPROVED | REJECTED | EXPIRED` 均为终态。
- **职责分离** `reviewer_id != user_id` 在 **service 层**强制，API 层不是唯一防线。
- TTL 到期落 `EXPIRED`，**按拒绝处理，绝不默认放行**。
- 幂等双防线：审批防「不该做的被做了」，side-effect ledger 防「做了一次被重做」。已批准执行走 `operation_key = run_id:approval:{approval_id}`，在任意次重试中恒定。

设计：[docs/design/human-in-the-loop.md](docs/design/human-in-the-loop.md) · 深潜：[docs/interview/hitl-deep-dive.md](docs/interview/hitl-deep-dive.md)

### 评测的真实性

正式评测流水线会**自我认证失败**：`scripts/rag_evidence_validity.py` 从执行事实（子集运行？查询数？四条 ablation leg？请求错误？降级比例？required channel 是否真的跑过并产出候选？gold 覆盖？）判定证据有效性，**不看指标大小**——一个真正差的检索器产出低 Hit@K 是有效测量。

生产者写 `evidence_validity` 块且在 `INVALID` 时不允许输出 `VERIFIED_FULL`；消费者 `scripts/rag_evidence_status.py` **重算**该判定，并与 artifact 自身的 `run_summary` / `evaluation_populations` / `preflight` 交叉核对。手工编辑或旧版 artifact 无法自我认证。

### 其他

| 能力 | 锚点 |
|---|---|
| Tool Result Context Engineering | `core/tool_result_*.py`（预算、压缩、offload/recovery、scope-safe exact reuse） |
| 三层响应缓存 | `cache/`（L1 Redis MD5 · L2 Qdrant 语义 · L3 Jaccard 回退） |
| Token 配额与追踪 | `core/token_tracker.py`（用户级日/月限额，Redis 持久化 + 内存回退） |
| Prompt 版本管理 | `core/prompt_manager.py` |
| 可观测性 | `core/tracing.py` 语义 span → OTLP → Prometheus/Grafana/Loki |
| 输入净化与注入防御 | `api/utils.py` + 历史对话包在「不可信数据」边界标记内 |
| 数据权限与会话隔离 | ContextVar per-session SharedBlackboard + 会话数据可选 AES-256-Fernet 加密 |

---

## 已知限制

诚实清单。每一项都是当前**没有**做到的事，不是待办承诺。

> 本节整体状态：**`PRODUCTION NOT_VERIFIED`**。Compose 扩容能力可配置 ≠ 多副本在生产跑过。

| 限制 | 状态 |
|---|---|
| RAG 正式 649-query 指标（Hit@K / Recall / NDCG / MRR） | `NOT_VERIFIED` — 当前 root blocker 未定；Issue #99 先审计 benchmark gold-label provenance，真实 provider / full-run 证据后补 |
| 生产集群 / 多副本长期稳定性 / K8s autoscaling / 跨区域 | `NOT_VERIFIED` |
| 真实 ERP 写操作（退款、改单） | `NOT_VERIFIED` — 只在 `tools/hitl_staging_tools.py` 上验证治理机制 |
| 生产 QPS / P95 / P99 / Token 成本 / 真实用户 FCR 与满意度 | `NOT_MEASURED` |
| `HITL_ENABLED` 默认 `false` | 快路径 `POST /api/chat` 无 run 上下文，**明确不在**该审批治理边界内 |
| Nginx 横向扩容 | 多副本可渲染，但 upstream 分发到多副本**未验证** |
| 真实 trace 后端（Jaeger / Langfuse / Tempo） | `NOT_VERIFIED` — 已验证的 Collector 只有 `debug` exporter |
| 无审批主动通知链路 | 需轮询待审批队列 |
| MCP 适配器 | **已进入 `main`，默认关闭 / read-only-first**；PR #61 + #64 提供 deterministic fake-server 契约，真实第三方 server / 生产网络与鉴权 / 多副本 / 写工具仍 `NOT_VERIFIED` |

完整证据边界见 [docs/evaluation/production-evidence.md](docs/evaluation/production-evidence.md) 与 [Issue #7](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/7)。

---

## 文档导航

| 我想知道… | 去哪 |
|---|---|
| 当前系统的真实状态（唯一入口） | [docs/reference/current-state.md](docs/reference/current-state.md) |
| 架构设计与 ADR | [docs/design/](docs/design/) · [docs/decisions/](docs/decisions/) |
| API 参考 | [docs/reference/api-reference.md](docs/reference/api-reference.md) |
| 配置项全表 | [docs/reference/configuration.md](docs/reference/configuration.md) |
| 项目结构（完整） | [docs/reference/project-structure.md](docs/reference/project-structure.md) |
| 测试怎么跑、怎么加 | [docs/reference/testing-guide.md](docs/reference/testing-guide.md) |
| 分布式运行时代运维 | [docs/operations/distributed-runtime-runbook.md](docs/operations/distributed-runtime-runbook.md) |
| 生产部署与上线前检查 | [docs/operations/production-operations-guide.md](docs/operations/production-operations-guide.md) · [检查清单](docs/checklists/production-readiness-checklist.md) |
| RAG 方法论与评测口径 | [docs/reference/rag-evaluation.md](docs/reference/rag-evaluation.md) |
| 面试深潜（架构/运行时/RAG/HITL/取舍） | [docs/interview/](docs/interview/) |
| 历史版本与审计快照 | [docs/reports/](docs/reports/)（**历史快照，非当前事实**） |
| 文档总索引 | [docs/README.md](docs/README.md) |

---

## 许可证

[Apache 2.0](LICENSE)。

企业级实践参考实现；生产环境所需凭据与基础设施不在本仓库内。