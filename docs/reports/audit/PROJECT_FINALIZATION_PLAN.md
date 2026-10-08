> **HISTORICAL AUDIT SNAPSHOT** — 2026-10-08 收尾审计的一次性计划快照。仅在该审计执行时点有效；
> 现状与证据边界以 [reference/current-state.md](../../reference/current-state.md) 为准。
> 计划项的执行状态不写在本文件里；每项落到 GitHub Issue 或代码后，由代码与
> `scripts/audit_doc_consistency.py` 守卫维持真相。

# Project Finalization Plan

> 目标：把本仓库整理成**可信的企业 Agent 工程项目展示仓库**。
> 判定标准只有一条——**README 上的每一句能力声明，都能在代码里指出 `file:line`**。

审计对象：Customer Service AI Agent（LangGraph 多智能体客服系统）
审计基线：`d7dfab1`（`git rev-parse HEAD`）
审计方式：全量源码审计 + 运行时校验，不采信历史报告结论。

---

## 0. 审计结论摘要

一句话：**这不是一个"缺功能"的项目，而是一个"部分能力宣称强于可验证事实"的项目。**

架构主线（LangGraph 图、工具循环、RAG 检索、分布式 Runtime、HITL 审批、OTel 追踪）
在代码层面**都是真的**，且证据链比多数开源 Agent 项目完整得多。真正的风险集中在
三处，且都不是"缺功能"，而是"**声明与事实不对齐**"：

| # | 性质 | 结论 |
|---|---|---|
| 1 | **功能声明超出可观测事实** | 70+ Prometheus 指标注册了却从未被 scrape，DLQ 告警规则**永远不可能触发** |
| 2 | **文档暗示存在、代码不存在** | "Human Escalation（转人工）"只是**关键词状态标签**，没有工单/队列/坐席台 |
| 3 | **指标口径诚实，但结论是"不可测"** | 649-query RAG 正式指标是 `NOT_VERIFIED`，且根因是**数据集缺陷**（gold 非相关度标注） |

第 1 条和第 2 条是本次收尾的 P0：它们会在对外评审/评审中被一句"这个告警怎么验证？"或
"转人工之后谁接？"直接击穿。第 3 条**不是缺陷，是优点**——本仓库选择把"不可测"
写清楚而不是编一个数字，应当保持。

---

## 1. 逐项能力判定（Implemented / Partial / Designed）

判定口径：
- **Implemented** = 代码存在 + 已接线 + 有测试，且能指出验证方式
- **Partial** = 代码存在但不完整 / 默认关闭 / 有已知断链
- **Designed** = 只有设计文档或注释，无可运行代码

### 1.1 Agent Workflow

| 能力 | 判定 | 证据 |
|---|---|---|
| 图编排（9 节点 / 2 条件边 / 1 checkpointer） | **Implemented** | `core/graph_builder.py:399-457` |
| 意图识别（LLM + 规则并行，高置信规则短路 LLM） | **Implemented** | `core/graph_builder.py:87`；`router/` |
| 双层路由（LLM 分类器 ‖ 规则分类器 + 熔断降级） | **Implemented** | `router/`；`core/monitoring.py:1009` 熔断器 |
| 5 种协作模式（Sequential/Parallel/Consultation/Hierarchical/ReAct） | **Implemented** | `collaboration/orchestrator.py:149-198` |
| Tool Calling 循环 | **Implemented** | `agents/base_agent.py:629-982` |
| RAG 混合检索（向量 + BM25 + RRF + rerank） | **Implemented** | `rag/qdrant_knowledge_base.py` |
| Tool Result Context Engineering | **Implemented** | `core/tool_result_{optimizer,compressors,cache,store}.py` |
| 响应质量评估 + 模式升级重试 | **Implemented** | `agents/evaluator.py`（启发式，非 LLM 判官） |
| **Human Escalation（转人工）** | **Partial** | 仅 `resolution_status="escalated"` 标签 + 提示词；**无工单/队列/坐席台** |
| LangGraph 原生流式（`astream` / `astream_events`） | **Designed / 未使用** | 全仓 0 命中；SSE 走自建 callback 总线 |

### 1.2 RAG

| 能力 | 判定 | 证据 |
|---|---|---|
| Qdrant 向量 + BM25 混合 + RRF 融合 + ApiReranker | **Implemented** | `rag/`；ADR-008 |
| 确定性 point ID + ChromaDB 迁移工具 | **Implemented** | `scripts/migrate_point_ids.py`；`tests/unit/test_qdrant_point_id.py` |
| 5000+ 语料导入（幂等 + manifest + 覆盖率审计） | **Implemented** | `scripts/import_eval_corpus.py`；`make rag-eval-import` |
| 649-query 评测流水线（4-config ablation，Hit/Recall/Precision/NDCG/MRR@{1,3,5,8}） | **Implemented** | `scripts/evaluate_rag.py`；`scripts/eval_contract.py` |
| 版本化 gold 标注契约 + 离线校验器 | **Implemented** | `scripts/gold_label_contract.py`；`rag-gold-label/v1` |
| **649-query 正式指标数值** | **NOT_VERIFIED（且当前不可测）** | `FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD` |
| 持久化 trace 后端（Jaeger/Tempo） | **Designed** | `deploy/otel/collector-config.yaml` 仅 `debug` exporter |

### 1.3 Tool Calling

| 能力 | 判定 | 证据 |
|---|---|---|
| OpenAI function-calling schema 构造 | **Implemented** | `tools/tool_registry.py:111-123` |
| MCP 客户端 + 工具发现 + 治理（11 条 fail-closed 策略） | **Implemented** | `tools/mcp_adapter.py`（1293 行）；CI 有真实 stdio 子进程契约测试 |
| 工具结果缓存 / 存储 / 压缩 / 卸载 / 恢复 | **Implemented** | `core/tool_result_*.py` |
| 工具副作用幂等（`operation_key = run_id:tool_call_id`，原子 claim） | **Implemented** | `runtime/side_effects.py:140-277` |
| 原生工具超时 | **Partial** | `agents/` 无 `wait_for`；仅 MCP 有 per-call timeout |
| 工具级重试 | **Partial** | `RETRY_MAX_ATTEMPTS` 只作用于 LLM 客户端 |
| 并行工具调用 | **Partial** | `base_agent.py:733` 顺序 for；`gather` 只用于上下文准备 |
| 工具级熔断器 | **Partial** | 熔断器只保护 LLM，不保护工具/ERP |
| 真实 ERP 写操作（退款/改单）正确性 | **NOT_VERIFIED** | 仅 `tools/hitl_staging_tools.py` 确定性 staging 工具 |

### 1.4 Human-in-the-Loop

| 能力 | 判定 | 证据 |
|---|---|---|
| 风险分级（LOW/MEDIUM/HIGH，优先级链） | **Implemented** | `core/hitl/risk.py:43-88` |
| 拦在执行之前（摘出到 `pending_actions`） | **Implemented** | `agents/base_agent.py:774-804` |
| LangGraph `interrupt()` + `Command(resume=...)` | **Implemented** | `core/hitl/gate.py:205`；`runtime/executor.py:103`；`runtime/bootstrap.py:79` |
| durable 审批表 + 唯一约束 + 幂等决策 | **Implemented** | `alembic/versions/006_add_human_approvals.py` |
| TTL → EXPIRED 按拒绝处理（读/决策/恢复三处收敛） | **Implemented** | `core/hitl/approval_service.py:230,388,511` |
| 职责分离（service 层强制，非仅 API 层） | **Implemented** | `core/hitl/approval_service.py:338-349` |
| RBAC（admin/supervisor） | **Implemented** | `api/routes/approvals.py:67-89` |
| 快路径无 run 上下文时拒绝无治理写 | **Implemented** | `tools/tool_registry.py:192-215` |
| **`HITL_ENABLED=true` + 空白名单 = 静默放行** | **Partial（fail-open）** | `core/hitl/risk.py:66-72` → LOW → 不拦；无启动校验 |
| 待审批主动通知（push / 邮件 / IM） | **Designed** | 仅轮询队列 |

### 1.5 分布式 Runtime

| 能力 | 判定 | 证据 |
|---|---|---|
| AgentRun 状态机（9 状态，DB 原子 CAS 转移） | **Implemented** | `runtime/statuses.py:45-103`；`runtime/repository.py:182,211` |
| Celery（acks_late / reject_on_worker_lost / visibility_timeout） | **Implemented** | `runtime/celery_app.py:34-67` |
| Postgres checkpoint（生产 fail-closed，不回退 MemorySaver） | **Implemented** | `core/container.py:341-351`；`core/checkpointer.py` |
| Redis per-thread 锁（Lua compare-and-delete），API 与 worker 共用 key 空间 | **Implemented** | `runtime/thread_lock.py:32-39`；`core/concurrency/distributed_lock.py` |
| 崩溃恢复（SIGKILL → lease 过期 → checkpoint `next` 续跑） | **Implemented（Level 2 证据）** | `artifacts/runtime/chaos-20261006T081808Z.json` |
| DLQ + 人工重放（复用原 run_id） | **Implemented** | `runtime/run_service.py:512-553`；`scripts/replay_dead_run.py` |
| Run 事件流（Redis Stream → SSE，支持续读） | **Implemented** | `runtime/events.py`；`api/routes/runs.py:181-279` |
| 卡死 run 兜底扫描器 | **Partial** | `scripts/reconcile_stuck_runs.py` 是**手动/cron 脚本**，无 Beat / 无 CI 接线 |
| 多副本 / K8s 弹性 / 真实流量 | **NOT_VERIFIED** | Level 3 |

### 1.6 Observability

| 能力 | 判定 | 证据 |
|---|---|---|
| OTel 分布式追踪（provider + 5 个语义 span + 自动 instrument） | **Implemented（本地已验证）** | `core/tracing.py`；`core/telemetry.py`；`artifacts/observability/otel-collector-20261003T040014Z/report.json` |
| 结构化日志 + 两层密钥脱敏 + ASGI 泄漏封堵 | **Implemented** | `core/logger.py:86-191` |
| `X-Trace-ID` HTTP 传播 | **Implemented** | `api/middleware/trace_middleware.py:24-35` |
| Prometheus 指标注册（~70 个，含 runtime / HITL / tool-result / MCP） | **Implemented** | `core/monitoring.py:284-408` |
| **Prometheus 指标 HTTP 暴露** | **Partial（断链）** | `api/routes/monitoring.py:332-405` 手写 10 个 `csai_*`；全仓无 `generate_latest` |
| **DLQ 告警规则** | **Partial（永不触发）** | `monitoring/alert_rules.yml` 依赖从未被 scrape 的 `agent_run_dead_letter_total` |
| Grafana 看板 | **Partial** | `csai-overview.json` 6 个面板引用从未输出的指标 |
| 持久化/可查询 trace 后端 | **Designed** | collector 仅 `debug` exporter |
| LangSmith / Langfuse / OpenInference | **未集成** | 应用代码 0 引用（`langsmith` 仅作为 lockfile 传递依赖） |

---

## 2. P0 —— 必须修复（影响展示可信度）

> P0 的定义：**一个懂行的评审者追问两句，就会发现"README 说的和代码做的对不上"。**
> 这类问题比缺功能更伤，因为它损伤的是整个仓库的可信度。

### P0-1 `agent_run_dead_letter_total` 从未被 scrape，DLQ 告警规则是死代码 🔴

**性质**：功能声明与事实不符（最严重的一类）

**证据链**：
1. `core/monitoring.py` 通过 `prometheus_client` 注册了约 70 个指标，包含
   `agent_run_dead_letter_total`、`agent_run_retry_total`、全部 HITL 指标、全部
   tool-result 指标。
2. 唯一的 Prometheus HTTP 端点是 `api/routes/monitoring.py:332` `GET /metrics/prometheus`，
   它**手工拼接** 10 个 `csai_*` 文本行（`csai_info` / `csai_requests_total` /
   `csai_errors_total` / `csai_error_rate_percent` / `csai_avg_response_time_seconds` /
   `csai_agent_calls_total` / `csai_sla_violation_rate` /
   `csai_sla_window_violation_rate_percent` / `csai_circuit_breaker_state` /
   `csai_circuit_breaker_consecutive_failures`）。
3. 全仓 grep `generate_latest|make_asgi_app` → **0 命中**。即 `prometheus_client`
   的 `REGISTRY` 从未被序列化输出。
4. `monitoring/prometheus.yml:17` 正是抓 `/metrics/prometheus`。
   → **第 1 步注册的约 70 个指标，Prometheus 一个都收不到。**
5. `monitoring/alert_rules.yml` 的 `AgentRunDeadLetterDetected`
   `expr: increase(agent_run_dead_letter_total[5m]) > 0` → 依赖一个**永远不存在的时间序列**，
   **该告警永远不可能触发**。
6. `tests/unit/test_alert_rules_contract.py:15` 只做 YAML 结构/语义断言，
   明确**不跑 `promtool`**，因此这个断链测试抓不到。

**为什么这条是 P0 而不是 P1**：本仓库最值钱的资产是"分布式 Runtime 的 DLQ 与崩溃恢复"，
而 DLQ 告警是它的**运维闭环最后一环**。现在有恢复、有重放、有 runbook，
但**没有人在被通知**。这在对外评审中正好是必问的最后一刀。

**修复方案（建议方案 A，最小且标准）**：

- **方案 A（推荐）**：在 `api/routes/monitoring.py` 增加一个标准端点
  `GET /metrics`，用 `prometheus_client.generate_latest(REGISTRY)` 输出完整注册表；
  保留现有 `/metrics/prometheus` 不动（避免破坏既有 `csai_*` 告警规则与看板），
  并把 `monitoring/prometheus.yml` 改成**两个 job**：一个抓 `/metrics`（标准指标），
  一个抓 `/metrics/prometheus`（`csai_*` 业务聚合指标）。
  改动面：`api/routes/monitoring.py` + `monitoring/prometheus.yml` + 一个契约单测。
- **方案 B**：把 `agent_run_*` 指标的 `expr` 改写成基于 `csai_*` 可得字段的表达式。
  **不推荐**——这会让指标语义退化成从另一套聚合值二次推导，掩盖真实计数。
- **方案 C**：删掉 `AgentRunDeadLetterDetected` 规则，承认无告警。
  **最诚实但不解决问题**——可以作为修复前的临时止血。

**验收方式**：
1. `make monitoring-up` 后 `curl -s localhost:8000/metrics | grep agent_run_dead_letter_total` 有输出。
2. Prometheus target 页面该 job 为 `UP`。
3. 故意制造一个 DEAD_LETTER run，5 分钟内 Alertmanager 收到通知
   （runbook：`docs/operations/distributed-runtime-runbook.md`）。
4. 补一个**契约单测**：断言 `monitoring/alert_rules.yml` 里每一个 `expr` 引用的
   metric name，都能（由代码或 `/metrics` 输出）提供——**防止同类断链再次发生**。

**关联**：`docs/limitations.md`（当前必须承认告警缺口）、`monitoring/alert_rules.yml`

---

### P0-2 "Human Escalation（转人工）" 没有实现，但文档措辞容易被读成已实现 🔴

**性质**：文档暗示存在、代码不存在

**证据链**：
1. `agents/response_agent.py:280` 的 `escalated` 判定**仅**是
   `any(phrase in response for phrase in ESCALATION_PHRASES)`——
   检查回复文本里是否出现"转人工"等词（`agents/response_agent.py:39-50`）。
2. `agents/complaint_agent.py:88` 的 `escalation_flag` 只是给下游**注入一句提示词**
   （`agents/base_agent.py:378-381`）。
3. 全仓 grep `handoff|ticket|human_handoff` → 生产代码 0 命中。
   **没有工单表、没有转人工队列、没有坐席分配、没有 WebSocket 推送给人。**
4. 也就是说：系统能*说出*"建议转人工"，但没有任何东西接住这句话。

**为什么这条是 P0**：本仓库的 HITL 审批（`WAITING_APPROVAL`）做得很扎实，
容易让人把"人工介入"整体当成已实现。实际上是**两件不同的事**：

| | 人工**审批高危工具副作用** | 转**人工坐席**处理会话 |
|---|---|---|
| 状态 | `WAITING_APPROVAL` | 不存在 |
| 持久化 | `human_approvals` 表 | 无 |
| API | `/api/approvals*` 4 个端点 | 无 |
| 送达 | 轮询待审批队列 | 无 |

**修复方案（文档层，低成本高收益）**：

- **方案 A（推荐）**：在 [docs/limitations.md](../../../docs/limitations.md) 与
  README 中，用一张明确的对照表区分这两件事，并把 escalation 描述为
  **"会话级状态标记 + 响应策略提示"**，明确写出"**不含坐席工作台/工单系统**"。
  这是**诚实的边界声明**，符合本仓库既有风格。
- **方案 B（实现，成本较高）**：新增 `human_handoff` 表 + `HANDOFF_PENDING` 状态
  + `POST /api/handoffs` + 坐席认领 API + WebSocket 推送。
  属于新功能，不应混入"收尾"范围，建议开 Issue 排入路线图（见 P2-3）。
- **方案 C**：删除 escalation 相关字段。不推荐——它们对路由与监控有真实价值。

**验收方式**：`grep -rni "转人工\|escalat" README.md docs/architecture.md docs/agent-design.md`
的每一处表述都明确标注为状态标记，且 `docs/limitations.md` 有对应"不宣称项"条目。

**关联**：[docs/limitations.md](../../../docs/limitations.md)、[docs/agent-design.md](../../../docs/agent-design.md)

---

### P0-3 `HITL_ENABLED=true` + 空配置 = 静默放行（fail-open）🟠

**性质**：与仓库其余部分 fail-closed 的工程原则不一致

**证据链**：
1. `core/config.py:795` `HITL_ENABLED = os.getenv("HITL_ENABLED","false")=="true"` → 默认关。
2. 若只开 `HITL_ENABLED=true` 而不配 `HITL_HIGH_RISK_TOOLS`（默认 `""`）与
   `HITL_HIGH_AMOUNT_THRESHOLD`（默认 `0.0`）：`core/hitl/risk.py:66-72` 的优先级链
   全部落空 → 判定为 `LOW` → `requires_approval` 为 `False` → **什么都不拦**。
3. **没有启动校验**。对比同仓库 MCP：`core/config.py:944-1011` 在配置非法时
   fail-closed 抛错。HITL 缺的正是这一层。
4. `.env.prod` 完全不含任何 `HITL_*` 键 → 生产继承 `false` 默认。

**修复方案**：

- **方案 A（推荐）**：`HITL_ENABLED=true` 且风险覆盖为空（无显式 HIGH 工具、
  `HITL_HIGH_RISK_TOOLS` 空、`HITL_HIGH_AMOUNT_THRESHOLD<=0`）时，在
  `core/config.py` 启动校验里**抛 `ConfigurationError`**——
  "开了审批开关但没有任何规则会被命中"。这与 MCP 的 fail-closed 原则一致。
- **方案 B**：至少 `logger.warning` + 在 `/api/health` 暴露 `hitl_effective: false`。
  比 A 弱（静默风险仍在），但比现状好。

**验收方式**：`HITL_ENABLED=true HITL_HIGH_RISK_TOOLS= HITL_HIGH_AMOUNT_THRESHOLD=0`
启动 → 应 fail-fast，报错信息指明"开了开关但无规则命中"。

---

### P0-4 langgraph 版本"实测"声明与 lockfile 不一致 🟡

**性质**：可验证性声明失真

**证据**：`core/hitl/gate.py:23,48,184`、`runtime/bootstrap.py:59`、
`runtime/executor.py:62`、`CLAUDE.md:228` 均声明语义"实测于 **langgraph 1.2.12**"。
本地 venv 实测 `importlib.metadata.version("langgraph")` = **1.2.12**（一致），
但 `requirements-lock.txt:144` 锁的是 **1.2.2**。
即：**"实测"结论对应本地 venv，lockfile 对应的版本并未实测过。**

**修复方案**：
- 短期（文档级，零风险）：把"实测于"改为"**实测于本地 venv 1.2.12；lockfile 锁定 1.2.2，该版本未复测**"。
- 长期：把 lockfile 提升到实测版本，或在 CI 加一条 langgraph 版本契约测试。

**验收方式**：`grep -rn "1\.2\.12" core/ runtime/ CLAUDE.md docs/` 每一处都带 lockfile 差异说明。

---

### P0-5 死代码与协议漂移（低风险，但会被 review 抓到）🟢

| 项 | 位置 | 说明 |
|---|---|---|
| `register_hitl_staging_tools` 无生产调用点 | `tools/hitl_staging_tools.py:122` | 只在 6 个测试里注册。**这是有意的**（staging 工具不该进生产注册表），但需要一个 docstring 显式声明"仅供测试/验证" |
| 坏文档引用 | `tools/mcp_adapter.py:42` → `docs/design/mcp-tool-adapter.md` | **该文件不存在**（`find docs -iname "*mcp*"` 为空） |
| 协议/实现命名漂移 | `core/protocols.py:159` `get_tools_for_llm` vs `tools/tool_registry.py:111` `get_openai_tools` | `@runtime_checkable` Protocol 声明的方法名与实现不一致 |
| `langgraph.json` 孤儿文件 | 仓库根 | 只被自身与 `core/graph_builder.py:477` docstring 引用；Makefile/requirements/pyproject/docs 全部 0 命中。其中的 `store.ttl` 配置**从未生效** |
| `has_pending_interrupt` 无生产调用方 | `core/hitl/gate.py:157` | 与 `runtime/executor.py:58` `_awaiting_approval` 重复实现 |
| `AgentState` 7 个键未声明 | `core/state.py` | `_rag_prefetch` / `_needs_upgrade` / `_retried` / `_retried_failed` / `ab_variant` / `ab_experiment` / `extracted_entities` 在运行时被写入但不在 TypedDict 里 |
| `stream_callback` 作为 state channel | `core/state.py:19` | 已知会破坏 checkpoint msgpack 序列化（`core/streaming_context.py:1-25` 有详述），生产已改走 ContextVar，但**声明还在**，任何调用方误填即炸生产 |
| `default_operation_key` 不可达 | `runtime/side_effects.py:92` | 当前工具路径不经过它 |
| `mcp` 版本要求冲突 | `requirements-optional.txt:13` `mcp>=2.0.0` vs `requirements-lock.txt:149` `mcp==1.27.2` | `tools/mcp_adapter.py:44-45` 已诚实标注 2.x 未验证 |
| `pyproject.toml` 无 `[project].dependencies` | `pyproject.toml` | 只含 `[tool.ruff]` / `[tool.pytest]`；依赖真相源是 `requirements*.txt` |

---

## 3. P1 —— 提升企业感（工程纵深）

> P1 的定义：**不影响可信度，但决定"这是一个做过生产的项目"还是"这是一个很好的练习项目"。**

| # | 项目 | 现状 | 目标 | 价值 |
|---|---|---|---|---|
| P1-1 | **原生工具加超时** | `agents/` 无 `wait_for`；只有 MCP 有 per-call timeout | 工具执行包 `asyncio.wait_for(handler(), timeout=cfg)`，超时分类进 `runtime/errors.py` | 防止单个慢工具拖垮整条 run；MCP 已有成熟写法可复用 |
| P1-2 | **并行工具调用** | `base_agent.py:733` 顺序 `for` | 对同一轮的多个 tool_call 用 `asyncio.gather`（**注意保持 side-effect 工具串行**） | 直接影响 token 成本与延迟；LLM 常一次返回多个 tool_call |
| P1-3 | **工具级熔断器** | 熔断只保护 LLM | ERP/MCP 复用 `core/monitoring.py:1009` 的 `CircuitBreaker` | ERP 挂掉时降级到规则引擎，而不是每次都超时 |
| P1-4 | **工具级重试** | 只有 LLM 有 | 读操作（`side_effect=False`）指数退避重试；写操作**不重试**（交给幂等 ledger） | 明确"读可重试、写不可重试"的边界 |
| P1-5 | **`reconcile_stuck_runs` 接入调度** | 手动/cron 脚本 | compose 加 `celery beat` 定时任务，或加 CronJob manifest + CI 契约测试 | 卡死 run 兜底从"记在文档里"变成"真在跑" |
| P1-6 | **Grafana 面板对齐** | 6 个面板引用从未输出的指标 | 补齐或删除；配 `make monitoring-up` 后的截图/校验 | 看板空面板是评审时一眼可见的破绽 |
| P1-7 | **告警规则可执行性契约测试** | 只做 YAML 结构断言 | 增加"每个 `expr` 的 metric name 必须可得"的断言（依赖 P0-1） | 防同类断链复发 |
| P1-8 | **持久化 trace 后端** | collector 仅 `debug` exporter | compose 增加 Jaeger 或 Tempo，配 retention | 让"有 tracing"从本地验证升级为可查询 |
| P1-9 | **`AgentState` 收敛** | 7 个未声明键 | 私有键统一加 `_` 前缀并补进 TypedDict；`stream_callback` 从 channel 移除 | 消除 schema 漂移与 checkpoint 序列化隐患 |
| P1-10 | **tool_result_cache_key 可读性** | 已做 scope 隔离，但 key 不可读 | 增加"运维可读别名 + 哈希值"双写，便于排障 | 排障时能直接按用户/会话查缓存命中 |
| P1-11 | **LangGraph 原生流式** | 0 使用，全自建 | 评估 `astream_events` 替代自建 callback 总线 | 对外评审中"为什么不用 LangGraph 原生能力"是常见追问 |
| P1-12 | **`docs/design/mcp-tool-adapter.md` 补齐** | 引用存在、文件不存在 | 补写 MCP 适配器设计（11 条策略 + 8 类错误 + 回滚事务） | MCP 是本项目差异化能力之一，目前**完全没有文档** |

---

## 4. P2 —— 未来扩展（路线图，不在本次收尾内）

| # | 项目 | 理由 | 前置条件 |
|---|---|---|---|
| P2-1 | **Agent 行为评测 harness** | 仓库里 `evaluation/` 只覆盖检索与 provider 证据，**没有 agent 行为回归评测**（`evaluation/agent_eval/` 只剩 gitignore 的 `.pyc`，源码从未提交）。企业 Agent 项目的评测重心恰恰是"Agent 行为是否回归" | 需要确定性 mock graph + JSONL 行为用例集 |
| P2-2 | **LLM-as-judge 质量评测** | 现在 `agents/evaluator.py` 是**启发式**（关键词打分），不是 LLM 判官。启发式可作为 CI 门禁，判官才能测语义质量 | 需固定 judge 模型 + 判官一致性验证 |
| P2-3 | **坐席工作台 / 转人工闭环** | 见 P0-2 方案 B | 需产品定义（工单模型、SLA、坐席权限） |
| P2-4 | **LangGraph `store` 长期记忆** | 目前 `compile()` 只传 `checkpointer`，**无 `store`**。跨会话用户偏好/画像是客服场景刚需 | 需先定义 memory schema 与失效策略 |
| P2-5 | **真实 ERP 写操作验证** | 退款/改单只验证了**治理机制**（staging 工具），未验证 ERP 集成正确性 | 需要企业 staging 环境 |
| P2-6 | **RAG gold 人工标注** | 649-query 正式指标**当前不可测**（gold 不是相关度标注）。契约与校验器已就绪，缺的是标注 | 需人工标注预算 + 标注一致性（Kappa）度量 |
| P2-7 | **Kubernetes 部署** | 目前只有 Docker Compose（6 变体）。K8s manifest / HPA / 多副本 | 需先有真实集群与多副本一致性验证 |
| P2-8 | **压测与容量基线** | 有 `scripts/benchmark_*.py`（9 个），但**无生产 SLA、无容量模型、无 P99 承诺** | 需明确 SLO 定义再压测 |
| P2-9 | **MCP 写操作工具** | 当前 MCP 只注册 `risk_level=low` 的服务器（`mcp_adapter.py:1078-1086`），**写操作 MCP 工具结构上不可能存在** | 需要 write ledger 与审批链路先支持 MCP 来源 |
| P2-10 | **多 LLM 供应商容灾** | 默认硅基流动单一 provider，熔断只降级到规则引擎 | 需多 provider 抽象与一致性验证 |

---

## 5. 执行顺序建议

```
第一批（可信度修复，1-2 天）
  P0-1  Prometheus 标准端点 + 双 job 抓取     ← 最高优先：DLQ 闭环
  P0-1  告警规则可执行性契约测试
  P0-2  文档边界声明（README + limitations + agent-design）
  P0-4  langgraph 版本表述修正

第二批（一致性收尾，1 天）
  P0-3  HITL fail-closed 启动校验
  P0-5  死代码清理（孤儿 langgraph.json / 坏链接 / 重复实现 / unreachable）
  P1-9  AgentState 收敛
  P1-12 MCP 适配器设计文档

第三批（工程纵深，按需）
  P1-1 / P1-2 / P1-3 / P1-4  工具执行语义（超时/并行/熔断/重试）
  P1-5  卡死 run 调度接入
  P1-6 / P1-8  可观测性补齐
  P1-7  告警契约测试

第四批（路线图，独立排期）
  P2-*  行为评测 / 判官 / 坐席台 / 长期记忆 / gold 标注 / K8s / 压测
```

---

## 6. 本次收尾**不做**什么（防止范围蔓延）

| 不做 | 理由 |
|---|---|
| 补齐 649-query RAG 指标数值 | 那是 P2-6 人工标注的产物，**本轮不测更诚实**。已由 `rag-gold-label/v1` 契约与校验器把"不可测"变成可审计状态 |
| 实现转人工工作台 | P2-3，是新功能不是收尾 |
| 加 Kubernetes manifest | P2-7，脱离真实集群验证的 manifest 是**装饰性声明** |
| 写生产 SLA 数字 | 无压测与真实流量，编数字即造假 |
| 引入 LangSmith / Langfuse 账号依赖 | 会让"零外部依赖可离线跑"这一优势失效 |
| 任何"生产集群已验证"表述 | 触发 `scripts/audit_doc_consistency.py` 的 production-claim 守卫，且违反本仓库 Level 3 边界 |

---

## 7. 与已有治理机制的关系

本计划**不新建**治理体系，全部挂在仓库既有的机器守卫上：

| 守卫 | 位置 | 守护什么 |
|---|---|---|
| 文档一致性审计 | `scripts/audit_doc_consistency.py`（35 项检查） | 链接、配置、OpenAPI、生命周期词汇、指标声明、HITL 默认值、AgentRun 状态机完整性、production 声明、root 级快照卫生 |
| 根级文档卫生 | 同上 Rule Z | 禁止无守卫的根级 markdown 竞争 Current Truth |
| RAG 证据有效性 | `scripts/rag_evidence_status.py` | 从 artifact 派生 `VERIFIED` / `NOT_VERIFIED`，文档不能自证 |
| 运行时契约测试 | `tests/unit/test_*_contract.py` | 配置/架构契约漂移 |
| CI 阻塞门 | `.github/workflows/ci.yml` | Ruff（阻塞）、3 版本 Python 矩阵、MCP 契约（**禁止静默 skip**）、真实 PG+Redis runtime lane、安全扫描 |

> **本文件已按 Rule Z 放在 `docs/reports/audit/`**：仓库根级 markdown 只有
> `README.md` / `CLAUDE.md` 会被 `discover_docs` 扫描，放在根目录的快照会**绕过
> 所有守卫**却仍与 `current-state.md` 竞争 Current Truth。
