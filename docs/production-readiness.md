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
| 四层状态机（缓存 → 路由 → 协作 → 后处理） | `core/graph_builder.py` · `core/state.py` | 单一定义点，9 个图节点 |
| 9 个 Agent 角色 / 5 种协作模式 | `agents/` · `collaboration/modes.py` | Sequential / Parallel / Consultation / Hierarchical / ReAct |
| Agent 工具循环（含 ReAct） | `agents/base_agent.py` · `agents/react_agent.py` | 工具、缓存、风险分流、幂等、上下文工程统一边界 |
| 混合检索 RAG | `rag/` | Qdrant + BM25 → RRF → cross-encoder rerank，确定性 point ID |
| 三层响应缓存 | `cache/` | L1 Redis MD5 · L2 Qdrant 语义 · L3 Jaccard 回退 |
| Tool Result Context Engineering | `core/tool_result_*.py` | 预算/压缩/offload/recovery/scope-safe reuse |
| Function Calling + MCP 适配器 | `tools/` | MCP 默认关闭，read-only-first，11 条 fail-closed 策略 |
| 分布式 Agent Runtime | `runtime/` | AgentRun 唯一真相源 · Postgres checkpoint · Redis 锁 · Celery worker · 三层幂等 · DLQ 重放 · run 事件流 |
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
| 分布式 Runtime 验收（真实 PG + Redis + 多进程 Celery） | `make runtime-e2e` · CI `runtime-e2e` lane | Verified (CI) |
| worker SIGKILL 崩溃恢复 + 副作用只发生一次 | `make runtime-chaos` → `artifacts/runtime/chaos-*.json` | Verified (CI) |
| Runtime 证据报告 | `make runtime-verify` → `artifacts/distributed-runtime/<ts>/report.json`（schema `distributed-runtime-evidence/v2`，带 `tested_code_sha`） | Verified (Local+CI) |
| DLQ 人工重放闭环 | `scripts/replay_dead_run.py`（复用原 `run_id`） | Verified (Local) |
| OTel Collector 传输链路（traces only） | `make otel-collector-smoke` → `artifacts/observability/otel-collector-*/report.json`（`VERIFIED_LOCAL`） | Verified (Local) |
| 安全扫描 | CI `security` lane（严格 mypy + bandit + secret guard） | Verified (CI) |
| 离线可复现 demo（证据卡） | `make demo-offline` | Verified (Local) |
| 文档事实一致性守卫 | `make audit-docs`（**未接入 CI**，本地/提交前） | Verified (Local) |

复核命令与证据口径：[evaluation.md](evaluation.md) · [evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md)。

---

## Not Verified

能力存在但**没有**对应环境证据的项目（`NOT_VERIFIED` / `NOT_MEASURED`）：

| 项 | 状态 | 原因 |
|---|---|---|
| 生产集群 / 多副本长期稳定性 / 真实用户流量 | `NOT_VERIFIED` | 无生产环境 |
| 正式 649-query RAG 指标（Hit@K / MRR / NDCG …） | `NOT_VERIFIED`，且**当前不可测** | shipped gold 无 relevance 标注（`DATASET_DEFECT`） |
| 真实金蝶 ERP 写操作（退款/改单） | `NOT_VERIFIED` | 仅用 `tools/hitl_staging_tools.py` 验证了治理机制，无企业 staging |
| provider auth / staging 与计费 | `NOT_VERIFIED` | 历史 preflight 曾因 HTTP 401 阻塞，非当前根因 |
| 生产 QPS / P95 / P99 / Token 成本 / FCR / 满意度 | `NOT_MEASURED` | 有 benchmark 脚本，但无 SLO 定义、无生产基线 |
| 持久化、可查询的 trace 后端（Jaeger / Tempo / Langfuse） | `NOT_VERIFIED` | 被验证的 Collector 只有 `debug` exporter，无存储/UI |
| Nginx 多副本横向扩容（upstream 分发） | `NOT_VERIFIED` | 多副本可渲染，未在真实部署验证 |
| 真实第三方 MCP server（网络/鉴权/写工具） | `NOT_VERIFIED` | 只有 deterministic fake-server 契约测试 |
| 生产 worker autoscaling / backpressure / admission control | `NOT_VERIFIED` | 无自动扩缩与准入控制 |
| 审批主动通知（push / 邮件 / IM）、审批 SLA | `NOT_VERIFIED` / `NOT_MEASURED` | 只有轮询队列 |
| Agent 行为回归评测 / LLM-as-judge | 未实现 | 无 golden 用例集；`agents/evaluator.py` 为启发式 |
| 「转人工」坐席台 / 工单闭环 | 未实现 | 只有关键词 escalation，无工单/队列/坐席台 |

> **已知的声明-事实断链**（须优先闭环）：`agent_run_dead_letter_total` 等
> Prometheus 指标已注册但 `GET /metrics/prometheus` 手工拼接、注册表从未被
> `generate_latest` 序列化，导致 DLQ 告警规则依赖的时间序列不存在。详见
> [limitations.md](limitations.md) 与 [PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P0-1。

---

## Production Upgrade Roadmap

按「影响上线可信度 → 工程纵深 → 未来扩展」排序。完整规格与验收方式见
[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md)。

### P0 — 上线前必须闭环（否则存在静默故障/安全边界缺口）

1. **监控序列化闭环**：`GET /metrics/prometheus` 改用 `prometheus_client.generate_latest`，
   让 DLQ 告警规则真正可触发。
2. **HITL 配置 fail-closed**：`HITL_ENABLED=true` 但风险规则为空时拒绝启动，
   消除「开着却静默放行」。
3. **生产凭据/证书/ERP/CORS**：替换全部占位符，接入真实 TLS、金蝶 ERP 与
   `CORS_ORIGINS`。
4. **告警可执行性契约测试**：每个 `expr` 的指标名必须真实可得。

### P1 — 工程纵深（让「企业级」从设计变成可验证）

1. 原生工具超时 / 并行调用 / 工具级熔断与重试（读可重试、写不重试）。
2. `reconcile_stuck_runs` 接入调度（beat/CronJob），卡死 run 兜底从文档变为运行。
3. Grafana 面板对齐实际指标；补齐 language/observability 契约测试。
4. 持久化 trace 后端（Jaeger/Tempo）接入，tracing 从本地验证升级为可查询。

### P2 — 路线图（不在本次收尾范围）

Agent 行为评测 harness · LLM-as-judge · 坐席工作台/转人工闭环 · LangGraph `store`
长期记忆 · 真实 ERP 写操作验证 · RAG gold relevance 人工标注 · Kubernetes
部署与多副本一致性 · 压测与容量基线 · MCP 写操作工具 · 多 LLM 供应商容灾。

---

## 一句话结论

**代码能力范围 = Implemented；关键基础设施 = Verified (Local/CI)；生产上线能力 =
`PRODUCTION NOT_VERIFIED`。** 面试或对外表述应停在这条线上，不得再往前一步。
