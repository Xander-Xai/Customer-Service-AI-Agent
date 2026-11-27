# Production Readiness

> 🟢 CURRENT — 本文回答一个问题：**这套系统今天能宣称到什么程度，离真实上线还差什么？**
>
> 这是一份**能力状态声明**，不是营销页。每个能力必须落到下面的某一级；禁止
> 把「代码存在」写成「生产验证」，也禁止把「本地/CI 验证」写成「生产就绪」。
> 逐项执行 gate 见 [checklists/production-readiness-checklist.md](checklists/production-readiness-checklist.md)；
> 不宣称清单见 [limitations.md](limitations.md)；当前事实入口见
> [reference/current-state.md](reference/current-state.md)。

## 怎么读这份文档

| 标记 | 含义 | 证据形态 |
|---|---|---|
| **Implemented** | 代码存在、已接线、可指出 `file:line` | 源码 |
| **Verified (Local)** | 本地命令跑通并产出可复查 artifact | 命令 + artifact |
| **Verified (CI)** | CI lane 在真实基础设施上跑通，不静默 skip | workflow + artifact |
| **Not Verified** | 设计/能力存在，但**没有**对应环境的验证证据 | `NOT_VERIFIED` / `NOT_MEASURED` |

> **Verified ≠ Production。** 本文件没有任何一条把 Local/CI 验证当作生产验证。

---

## Current Capability

代码存在且已接线（**Implemented**，不构成生产验证）：

| 能力 | 源码锚点 | 说明 |
|---|---|---|
| 四层状态机（缓存 → 路由 → 协作 → 后处理） | `core/graph_builder.py` · `core/state.py` | 单一定义点，11 个显式注册图节点（不含 START/END） |
| 7 个领域角色 + ReAct + 确定性后处理 / 5 个兼容执行标识 | `agents/` · `collaboration/modes.py` | Sequential / ReAct 共用单专家边界；Parallel 为并行；Consultation / Hierarchical 为固定主从模式族 |
| Agent 工具循环（含 ReAct） | `agents/base_agent.py` · `agents/react_agent.py` | 工具、缓存、风险分流、幂等、上下文工程统一边界 |
| 混合检索 RAG | `rag/` | Qdrant + BM25 → RRF → cross-encoder rerank，确定性 point ID |
| 三层响应缓存 | `cache/` | L1 Redis MD5 · L2 Qdrant 语义 · L3 Jaccard 回退 |
| Tool Result Context Engineering | `core/tool_result_*.py` | 预算/压缩/offload/recovery/scope-safe reuse |
| Function Calling + MCP 适配器 | `tools/` | 标准 Compose 默认启用合成 SKU 文档，read-only-first；工业部署需批准目录，生产数据待验证 |
| A2A 售后程序专家 | `integrations/a2a_policy/` | 标准 Compose 启动独立合成 peer 并生成私有 bearer；工业部署要求批准目录和私有 HTTPS/CA；企业 TLS/目录待验证 |
| 分布式 Agent Runtime | `runtime/` | AgentRun 唯一真相源 · Postgres checkpoint · Redis 锁 · Celery worker · 三层幂等 · DLQ 重放 · run 事件流 |
| LLM 请求准入（专项切片，Implemented） | `llm/admission.py` · `llm/client.py` | Industrial ordinary/raw/stream HTTP attempts use shared Redis Lua token buckets; configured Provider/Model + user RPM/TPM; Redis/identity/config failure closes admission |
| Human-in-the-Loop 审批治理 | `core/hitl/` · `runtime/statuses.py` | 执行前拦截 · durable 审批 · 职责分离 · TTL 拒绝优先 · 审批幂等双防线 |
| FastAPI 服务与安全 | `api/` · `auth/` | JWT + Argon2id + 4 级 RBAC · CSRF 双提交 · CSP nonce · 限流 · SSRF 防护 |
| 可观测性 | `core/tracing.py` · `core/telemetry.py` · `core/monitoring.py` | OTel 语义 span · Prometheus 指标 · 结构化日志脱敏 |
| 多模态与告警（API 层集成） | `media/` · `alerts/` | **不是** Agent 可调用工具 |

设计理由：[architecture.md](architecture.md) · [agent-design.md](agent-design.md)。

---

## Verified Capability

**有命令、有 artifact、可独立复查**的能力（Local/CI，**不等于**生产验证）：

| 能力 | 验证方式 | 级别 |
|---|---|---|
| 离线测试套件（无 API Key、不出公网） | `make test` | Verified (Local) |
| 代码规范（Ruff，阻塞） | `.github/workflows/ci.yml` `lint` lane | Verified (CI) |
| 单测/集成 + 覆盖率门槛 + MCP 契约禁 skip | CI `test` lane（3 个 Python 版本） | Verified (CI) |
| 标准合成产品文档 MCP/A2A business tool round-trip（真实 SDK / 本地进程 / allowlist / fail-closed） | `tests/integration/test_mcp_product_documents_stdio.py` · `tests/integration/test_a2a_business_policy.py` · CI runs `45738703085` / `45738703866` | Verified (CI; synthetic fixtures only) |
| 分布式 Runtime 验收（真实 PG + Redis + 多进程 Celery） | `make runtime-e2e` · CI `runtime-e2e` lane | Verified (CI) |
| worker SIGKILL 崩溃恢复 + 副作用只发生一次 | `make runtime-chaos` → `artifacts/runtime/chaos-*.json` | Verified (CI) |
| Runtime 证据报告 | `make runtime-verify` → `artifacts/distributed-runtime/<ts>/report.json`（schema `distributed-runtime-evidence/v2`，带 `tested_code_sha`） | Verified (Local+CI) |
| DLQ 人工重放闭环（不授权未知外部写重试） | `scripts/replay_dead_run.py`（复用原 `run_id`） | Verified (Local) |
| Prometheus 指标暴露 + DLQ 告警表达式 | `make metrics-exposure-verify` → `artifacts/observability/metrics-exposure-*/report.json`（`VERIFIED_LOCAL`，真实 `prom/prometheus:v2.51.0` 上告警实际 FIRING）+ `make alert-rules-test`（官方 `promtool test rules`） | Verified (Local) |
| 指标暴露 / 告警引用契约（防复发） | `make metrics-exposure-check`（告警与 Grafana 引用的每个指标名都必须可达） | Verified (CI) |
| OTel Collector 传输链路（traces only） | `make otel-collector-smoke` → `artifacts/observability/otel-collector-*/report.json`（`VERIFIED_LOCAL`） | Verified (Local) |
| 安全扫描 | CI `security` lane（严格 mypy + bandit + secret guard） | Verified (CI) |
| 离线可复现 demo（证据卡） | `make demo-offline` | Verified (Local) |
| 文档事实一致性守卫 | CI `test (3.11)` 执行 `project_facts.py --check` 与 `scripts/audit_doc_consistency.py`；本地 `make audit-docs` 同入口 | Verified (Local+CI) |

复核命令与证据口径：[evaluation.md](evaluation.md) · [evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md)。

---

## Not Verified

能力存在但**没有**对应环境证据的项目（`NOT_VERIFIED` / `NOT_MEASURED`）：

| 项 | 状态 | 原因 |
|---|---|---|
| 生产集群 / 多副本长期稳定性 / 真实用户流量 | `NOT_VERIFIED` | 无生产环境 |
| 正式 649-query RAG 指标（Hit@K / MRR / NDCG …） | `NOT_VERIFIED`，且**当前不可测** | shipped gold 无 relevance 标注（`DATASET_DEFECT`） |
| 标准 Compose 合成启动（app/worker/beat/MCP/A2A peer） | `VERIFIED_LOCAL`（启动与协议调用）；模型业务 readiness `BLOCKED` | 2026-11-26 本地 WSL2、显式仓库 Compose 文件、隔离 Compose project、合成 fixtures：镜像构建成功；PostgreSQL/Redis/Qdrant healthy；`protocol-init`/`metrics-init` 成功退出；A2A peer、Celery worker/beat 与 app 容器 healthcheck 均 healthy；MCP/A2A 实际经 SDK 和 ToolRegistry 调用。应用 `/api/health` 返回 HTTP 200，但 payload 为 `unhealthy`（`llm.key_usable=false`、`degraded=true`），向量通道因占位 key 禁用。Docker healthcheck、协议 peer readiness、应用/model readiness 和业务成功是不同信号。未验收工业 profile、真实 provider、批准企业目录、TLS/CA、ERP、生产 nginx/Prometheus/Alertmanager 全栈或部署；本地合成启动不构成生产 readiness。|
| 真实金蝶 ERP 写操作（退款/改单） | `NOT_VERIFIED` | 仅用 `tools/hitl_staging_tools.py` 验证了治理机制，无企业 staging |
| provider auth / staging 与计费 | `NOT_VERIFIED` | 历史 preflight 曾因 HTTP 401 阻塞，非当前根因 |
| 生产 QPS / P95 / P99 / Token 成本 / FCR / 满意度 | `NOT_MEASURED` | 有 benchmark 脚本，但无 SLO 定义、无生产基线 |
| 持久化、可查询的 trace 后端（Jaeger / Tempo / Langfuse） | `NOT_VERIFIED` | 被验证的 Collector 只有 `debug` exporter，无存储/UI |
| Nginx 多副本横向扩容（upstream 分发） | `NOT_VERIFIED` | 多副本可渲染，未在真实部署验证 |
| 真实第三方 MCP server（网络/鉴权/写工具） | `NOT_VERIFIED` | 有确定性协议及第一方 SKU 文档契约；真实第三方服务与真实授权目录尚未验收 |
| 真实 A2A 售后业务 | `NOT_VERIFIED` | 有独立 SDK 和规则引擎契约；真实授权目录、生产网络与模型路由尚未验收 |
| 生产 worker autoscaling / durable-run backpressure | `NOT_VERIFIED` | 无自动扩缩；durable Run admission 仅限制新建 Run 数，不是 Worker 自动扩缩或完整 backpressure |
| LLM 分布式准入、Token Quota 预占结算、Run 资源预算 | `PARTIAL / NOT_VERIFIED` | 代码新增 LLM API HTTP 尝试令牌桶，但真实 Redis 多进程集成尚未运行；Token Quota 仍无原子预占/结算，Run 级模型/Token/时间/工具总预算未闭环，Provider 限额参数须由环境所有者确认 |
| 审批主动通知（push / 邮件 / IM）、审批 SLA | `NOT_VERIFIED` / `NOT_MEASURED` | 只有轮询队列 |
| Agent 行为回归评测（Agent Eval V1） | `LEVEL_2_APPLICATION_MEASURED` | 已实现评测框架与**真实图脚本化回放**（零出网、JSONL 用例集、显式分子/分母/证据边界）。测编排/治理/路由行为，**不测**模型能力；见 [reference/agent-evaluation.md](reference/agent-evaluation.md) |
| LLM-as-judge 语义质量 | 未实现 | 需固定 judge 模型 + 判官一致性验证；`agents/evaluator.py` 仍为启发式 |
| 「转人工」坐席台 / 工单闭环 | 部分实现，生产 NOT_VERIFIED | 已提交持久化工单、坐席领取、消息和关闭/重开 API（默认关闭）；**已有基础坐席 Web 工单页面，但无推送 / SLA 运营验收**，详见 [human-handoff-operations.md](human-handoff-operations.md) |
| **Alertmanager 通知实际送达** | `NOT_VERIFIED` | 指标可达与告警表达式已在真实 Prometheus 上验证（告警实际 FIRING），但 webhook / SMTP 投递、整套 compose 栈端到端、Grafana 面板真实渲染均未验证 |
| 工具级重试 / 熔断 / 并行调用 | 未实现 | 仅 LLM 客户端有重试与熔断；工具层此前无超时，现已补 `TOOL_EXECUTION_TIMEOUT_SECONDS`（重试/熔断/并行仍缺） |

> **已闭环的声明-事实断链**：原 P0-1（Prometheus 注册表从未被序列化，导致 DLQ 告警
> 依赖的时间序列不存在）与 P0-3（`HITL_ENABLED=true` 但规则为空时静默放行）已修复。
> 指标侧除了暴露端点，还补了**跨进程聚合** —— worker 容器递增的计数器在只抓 app 的
> Prometheus 眼里本就恒为 0，只补端点会得到一个「看起来正常却不触发」的假修复。
> 详见 [limitations.md](limitations.md) §1.2 / §2 与
> [current-state.md](reference/current-state.md)。
>
> **仍然不要写成「告警闭环已完成」**：通知投递段仍未验证。

---

## Production Upgrade Roadmap

按「影响上线可信度 → 工程纵深 → 未来扩展」排序。完整规格与验收方式见
[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md)。

### P0 — 上线前必须闭环（否则存在静默故障/安全边界缺口）

上线按业务能力签署验收，而不是仅依据服务能启动：真实 ERP 路径须有授权、写入结果对账与未知结果拒绝重试；模拟 ERP 不得作为真实客户订单事实。生产会话须强制加密并验证旧明文迁移；响应缓存须证明授权、无状态和新鲜度边界，未完成配对评估时不宣称语义缓存安全。HITL 与人工坐席需要独立业务值班流程。任何未通过的真实业务能力不得开放给真实客户。

1. ~~**监控序列化闭环**~~ **已完成**：`GET /metrics` 序列化真实 REGISTRY +
   `PROMETHEUS_MULTIPROC_DIR` 跨进程聚合 + `bearer_token_file` 抓取凭据；
   真实 Prometheus 上 DLQ 告警实际 FIRING。**剩余**：Alertmanager 实际投递、
   整套 compose 栈端到端验证。
2. ~~**HITL 配置 fail-closed**~~ **已完成**：启动校验（无规则即拒绝启动，
   且先于 `DEV_MODE` 早退）+ 执行期兜底（未认领风险等级的有副作用工具 = HIGH）。
3. **生产凭据/证书/ERP/CORS**：替换全部占位符，接入真实 TLS、金蝶 ERP 与
   `CORS_ORIGINS`。另外 `make monitoring-token` 必须先跑，否则 Prometheus
   拒绝启动（fail-closed，这是刻意设计）。
4. ~~**告警可执行性契约测试**~~ **已完成**：告警与 Grafana 引用的每个指标名
   都必须可达（`make metrics-exposure-check`）+ 官方 `promtool test rules`
   （`make alert-rules-test`）。

### P1 — 工程纵深（让「企业级」从设计变成可验证）

1. ~~原生工具超时~~ **已完成**；仍待做：并行调用、工具级熔断与重试
   （需先定义「读可重试、写不可重试」的边界）。
2. ~~`reconcile_stuck_runs` 接入调度~~ **已完成**：compose 里**独立 `celery beat`
   service**（刻意不内嵌 `worker -B`，worker 可被 `--scale` 成多副本）+ 周期
   `AGENT_RUN_RECONCILE_INTERVAL_SECONDS`（默认 30s）。CI 契约测试
   （`tests/unit/test_runtime_review_findings.py::TestDeliverySemanticsContract`）
   直接解析 compose YAML，断言 `beat` service 存在且 worker 命令不含 `-B`。
   **边界**：beat 停摆 = 滞留 run 无自动恢复（`beat` 刻意单实例，HA 未做）；
   **beat 存活 ≠ 扫描成功**，扫描失败由 `AgentReconcileStalled` 覆盖。
3. Grafana 面板**真实渲染**验证（指标输出已补齐，渲染未验证）。
4. 持久化 trace 后端（Jaeger/Tempo）接入，tracing 从本地验证升级为可查询。

### P2 — 路线图（不在本次收尾范围）

LLM-as-judge · 坐席工作台/转人工闭环 · LangGraph `store`
长期记忆 · 真实 ERP 写操作验证 · RAG gold relevance 人工标注 · Kubernetes
部署与多副本一致性 · 压测与容量基线 · MCP 写操作工具 · 多 LLM 供应商容灾。

---

## 一句话结论

**代码能力范围 = Implemented；关键基础设施 = Verified (Local/CI)；生产上线能力 =
`PRODUCTION NOT_VERIFIED`。** 对外评审或对外表述应停在这条线上，不得再往前一步。


## 新增交付验收边界（2026-11-25）

#79 已合入隐私保护的离线格式盘点，不代替授权迁移及 provenance；#81 已合入轨迹和计划分母治理，不代替真实业务 gold。实际 TLS、ERP 写结果查询、真实模型、数据授权、SLA 和运维值班仍须在授权环境验证。


生产在线图生成式响应缓存采取 fail-closed 策略：`DEV_MODE=false` 时，无论意图标签或命中分数均不读写响应缓存；开发环境仍保留评测路径。此决策不改变知识检索缓存和独立工具回执/工具结果复用的合同。重新启用前须通过跨客户、同客户跨任务、上下文、权限与新鲜度的配对负控制，不以旧 benchmark 作为上线许可。

### Main CI / Gate11 evidence snapshot (2026-11-26)

Main `31a11f91d9a09ba6d5e8763db84151be930f9a27` CI [45785580625](https://github.com/Xander-Xai/Customer-Service-AI-Agent/actions/runs/45785580625) completed successfully, including Python 3.10/3.11/3.12, lint, dev-compat, Runtime E2E/worker-kill, metrics-alerting, Agent Eval, frontend and security. `build-and-push` succeeded after all blocking gates; `deploy` was skipped. This CI run is not production deployment evidence.

The workflow published GHCR tags `31a11f9` and `latest` with manifest digest `sha256:fd50bd3ceee95a8d020b1f54fb4c21e8fc95b90fd91a2037ff07c123ef0586c7` for this source SHA. This is a build artifact, not a GitHub Release or a verified deployment; the deploy job was skipped.

Earlier main SHA `1f29fa2fa2699ac3be46822e96de402b0ce5adad` had a Gate11 worker-kill recovery failure (`CheckpointEpisodeMismatch`, attempt 1) and passed on same-SHA attempt 2. A focused local run on the current SHA passed using temporary PostgreSQL 15 + Redis 7 and a fake side-effect provider, but the original failure's specific checkpoint identity mismatch was not captured and its root cause remains unlocated. Do not claim the concurrency issue fixed; preserve the strict mismatch rejection and track further evidence through Issue #46.
