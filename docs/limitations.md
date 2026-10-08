# Limitations

> 🟢 CURRENT — 本文是本仓库的**不宣称清单**。
> 一条能力如果不能在代码里指出 `file:line` 并给出验证方式，就**必须**出现在这里。
>
> 这不是免责声明，是**工程结论**：写清楚边界比夸大能力更能说明工程判断。

---

## 0. 怎么读这份文档

| 状态 | 含义 |
|---|---|
| **Implemented（已实现）** | 代码存在、已接线、有测试，能给出验证命令 |
| **Partial（部分）** | 代码存在但不完整 / 默认关闭 / 有已知断链 |
| **Designed（仅设计）** | 有设计文档或注释，**无可运行代码** |
| **NOT_VERIFIED（未验证）** | 跑过或审查过，结论是无法确立 |
| **NOT_MEASURED（未测）** | 没跑过，将来能跑 |

> 这些词在仓库里是一等公民，由 `scripts/rag_evidence_status.py` 派生、
> 由 `scripts/audit_doc_consistency.py` 的文档一致性守卫维护。

---

## 1. 最重要的三条边界

如果只看三条，看这三条。

### 1.1 检索质量指标当前**不可测**（不是"未测"）

**状态：`NOT_VERIFIED`，且当前用这份数据集测不出来。**

根因审计结论：`FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD`

| 事实 | 含义 |
|---|---|
| 649 条 benchmark query 的 gold 中，**没有任何一条是人工相关度判定** | gold 是"同类别随机抽样" |
| 仅 49 条能复现仓库唯一记录过的生成方式 | 其余 600 条来源无仓库记录 |
| 160 个 gold id 不在 5000 条语料中 | 40 条 query 全部 gold 缺失 |

**所以本仓库不发布任何"当前 Hit@K / MRR / NDCG / Recall 百分比"。**
用随机同类别文档当 gold 算出来的数字衡量的是"能不能召回随机同类的文档"，
不是"能不能找到正确答案"——**发这种数字比不发更糟**。

流水线本身是完整的（4-config 消融、multi-K、三种统计口径、失败分类、
preflight gate），**缺的是人工相关度标注**。

→ 详见 [evaluation.md](evaluation.md) §3、[reference/rag-evaluation.md](reference/rag-evaluation.md)

### 1.2 DLQ 告警规则当前**不可能触发**

**状态：Partial（断链）**

| 环节 | 事实 |
|---|---|
| 指标注册 | `core/monitoring.py` 用 `prometheus_client` 注册了约 70 个，含 `agent_run_dead_letter_total` |
| HTTP 暴露 | 唯一端点 `GET /metrics/prometheus`（`api/routes/monitoring.py:332`）**手工拼接** 10 个 `csai_*` 聚合行 |
| 序列化 | 全仓 `generate_latest` / `make_asgi_app` → **0 命中**，即 `REGISTRY` 从未被输出 |
| 抓取配置 | `monitoring/prometheus.yml:17` 正是抓该端点 |
| 告警规则 | `AgentRunDeadLetterDetected` 用 `increase(agent_run_dead_letter_total[5m])` |
| 测试 | `tests/unit/test_alert_rules_contract.py` 只做 YAML 结构断言，明确不跑 `promtool` |

**结论：DLQ 目前只能靠人看或主动查询，没有自动通知。**
Grafana `csai-overview.json` 另有 6 个面板引用从未输出的指标，永远为空。

**为什么这条是"声称与事实不符"而非"缺功能"**：仓库提供了完整的
崩溃恢复 + DLQ + 重放 + runbook，唯独缺了"通知"这一环，
这会让读者以为闭环是完整的。

→ 修复方案：[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P0-1

### 1.3 "转人工"不是已实现的功能

**状态：Partial（仅状态标记）**

| 机制 | 人工**审批**高危副作用 | 转**人工坐席**处理会话 |
|---|---|---|
| 介入时机 | 工具**执行之前** | 响应生成**之后** |
| 实现状态 | **Implemented**（`HITL_ENABLED` 默认 `false`） | **无工单、无队列、无坐席台、无推送** |
| 代码证据 | `core/hitl/*`、`human_approvals` 表、`/api/approvals` × 4 | 仅 `resolution_status="escalated"` 标签 + 提示词 |

escalation 的真实实现只有两处，都不是"转交"：

- `agents/response_agent.py:280`：`any(phrase in response for phrase in ESCALATION_PHRASES)`
  —— 检查回复文本里是否出现"转人工"等词。
- `agents/complaint_agent.py:88`：`escalation_flag` —— 给下游**注入一句提示词**。

全仓 `handoff|ticket|human_handoff` 在生产代码中 **0 命中**。

**准确表述**：本系统能*说出* "建议转人工"，但**没有任何东西接住这句话**。

→ [agent-design.md](agent-design.md) §4、[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P0-2

---

## 2. Human-in-the-Loop 的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 审批机制本身 | **Implemented** | 风险分级、拦在执行前、`interrupt()`、durable 表、TTL、职责分离、RBAC |
| 默认开关 | **默认关闭** | `HITL_ENABLED=false`（`core/config.py:795`） |
| **开了但没配 = 静默放行** | **Partial（fail-open）** | `HITL_ENABLED=true` + `HITL_HIGH_RISK_TOOLS` 空 + `HITL_HIGH_AMOUNT_THRESHOLD=0` → 全部判为 `LOW` → 什么都不拦，**且不报警**。与仓库其余部分（checkpoint / MCP / 分布式运行时都 fail-closed）不一致 |
| `/api/chat` 快路径 | **明确不在治理边界内** | 快路径无 run 上下文（`core/hitl/gate.py:82-87`）。补偿措施是**拒绝**无治理的副作用调用，而不是放行 |
| 待审批通知 | **Designed** | 只有轮询队列，**没有 push / 邮件 / IM 主动通知** |
| 真实 ERP 退款/改单 | **NOT_VERIFIED** | `tools/hitl_staging_tools.py` 是确定性 staging 工具，验证的是**治理机制**，不是金蝶 ERP 的正确性。无企业 staging 环境 |
| MCP 写操作工具 | **结构上不可能存在** | 只注册 `risk_level=low` 的 MCP 服务器（`tools/mcp_adapter.py:1078-1086`），且 `side_effect=False` 硬编码 |

**默认配置下实际生效的高危工具集 = 只有两个 staging 工具**
（`staging_refund` / `staging_order_change`）。4 个 ERP 工具全部
`side_effect=False`、`risk_level` 未声明。

---

## 3. 分布式 Runtime 的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 状态机 / Celery / checkpoint / 锁 / 幂等 / DLQ | **Implemented** | 见 [architecture.md](architecture.md) |
| 真实 PG + Redis + 多进程 worker 验收 | **Level 2 已验证** | `make runtime-e2e`（基础设施缺失时硬 FAIL，不静默 skip） |
| SIGKILL 崩溃恢复 | **Level 2 已验证** | `make runtime-chaos`，artifact 记录 8 步与恢复后的 `attempt` 递增 |
| **多副本长期运行** | **NOT_VERIFIED** | 无生产集群证据 |
| **真实用户流量下的行为** | **NOT_VERIFIED** | — |
| **大规模 queue backlog** | **NOT_VERIFIED** | 无压测与容量模型 |
| **K8s autoscaling / multi-region** | **NOT_VERIFIED** | 无 K8s manifest |
| 卡死 run 兜底扫描 | **Partial** | `scripts/reconcile_stuck_runs.py` 是**手动 / cron 脚本**，没接 Celery Beat，也无 CI 契约保证会跑 |
| **正确性语义是 at-least-once，不是 exactly-once** | 设计如此 | `acks_late` + `reject_on_worker_lost` + `visibility_timeout`。正确性靠三层幂等（run / thread / 工具） |
| Run 事件流 | **不是真相源** | Redis Stream → SSE，best-effort 可续读，**非 exactly-once**。断线会丢事件（可续读，但非完整） |
| DLQ 重放 | **需人工** | 复用原 `run_id`（避免绕过工具幂等键），无自动重放策略 |

---

## 4. 工具层的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 注册表 / OpenAI schema / ERP 授权 / HITL 拦截 / 幂等 | **Implemented** | — |
| MCP 适配器 | **Implemented**（11 条 fail-closed 策略 + 真实 stdio 子进程契约测试） | 默认**不启用**（`MCP_SERVERS` 为空）；`mcp` 是 optional 依赖 |
| 原生工具超时 | **Partial** | `agents/` 内无 `asyncio.wait_for`。**只有 MCP 有 per-call timeout**。一个慢的 ERP 工具可以拖住整个 run |
| 工具级重试 | **Partial** | `RETRY_MAX_ATTEMPTS` 只作用于 LLM 客户端，不作用于工具 |
| 并行工具调用 | **Partial** | `agents/base_agent.py:733` 是顺序 `for`；`asyncio.gather` 只用于上下文准备。LLM 一次返回多个 tool_call 时没有并行收益 |
| 工具级熔断器 | **Partial** | `CircuitBreaker` 只保护 LLM，不保护 ERP / MCP |
| 真实 ERP 写操作 | **NOT_VERIFIED** | 同 §2 |
| `media/`（图片/音频/视频/文档/TTS）与 `alerts/` | **不在工具循环内** | 它们是 API 层集成，**不是 Agent 可调用的工具**。不要把它们算进"工具生态" |

---

## 5. RAG 的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 混合检索 + RRF + rerank + 确定性 point ID + 迁移工具 | **Implemented** | ADR-008 |
| 5000+ 语料导入（幂等 + manifest + 覆盖率审计） | **Implemented** | — |
| 正式检索指标 | **NOT_VERIFIED，且当前不可测** | 见 §1.1 |
| 语义缓存（L2）的向量质量 | 依赖 embedding provider | embedding 不可用时 L2 失效并**显式标记 degraded**，回退 L3 |
| Reranker 不可用 | 降级到 `hybrid_no_rerank` | 该配置的指标是独立口径，不能与 rerank 结果混报 |
| LangGraph `store` 长期记忆 | **Designed（未使用）** | `compile()` 只传 `checkpointer`。跨会话记忆由 `core/session/` 承担，两者**明确分离** |

---

## 6. 可观测性的边界

| 项 | 状态 | 说明 |
|---|---|---|
| OTel tracing（provider + 5 个语义 span + 自动 instrument） | **Implemented，本地已验证** | 对真实 Collector 验证过 span 到达与隐私 canary 不泄漏 |
| 结构化日志 + 两层密钥脱敏 | **Implemented** | 含 `scrub_exception_message()` 就地脱敏，防止 ASGI 自己的 traceback 泄漏 |
| 指标注册（~70 个） | **Implemented** | — |
| **指标 HTTP 暴露** | **Partial（断链）** | 见 §1.2 |
| **DLQ 告警** | **Partial（永不触发）** | 见 §1.2 |
| Grafana 看板 | **Partial** | 6 个面板永远为空 |
| **持久化 / 可查询 trace 后端** | **Designed** | `deploy/otel/collector-config.yaml` 只有 `debug` exporter，无存储、无保留期、无查询 UI |
| LangSmith / Langfuse / OpenInference | **未集成** | 应用代码 0 引用。`langsmith` 只作为 lockfile 传递依赖存在 |
| LangChain 原生 callback handler | **未使用** | tracing 是手写 span，不是 LangChain-native |
| 日志聚合 | **Implemented** | Loki + Promtail |
| 真实流量下的采样与保留策略 | **NOT_VERIFIED** | — |

---

## 7. 评测的边界

| 项 | 状态 | 说明 |
|---|---|---|
| RAG 评测流水线 | **Implemented** | 4-config 消融、multi-K、三口径、失败分类、preflight gate |
| 分布式 Runtime 验收 + chaos | **Level 2 已验证** | 有 artifact（带 `tested_code_sha`） |
| OTel span 完整性 | **Level 2 本地已验证** | 有 artifact |
| 响应质量评分 | **Implemented，但是启发式** | `agents/evaluator.py` 是关键词打分，**不是 LLM-as-judge**。它的真实用途是"要不要升级重试"的触发器 |
| **LLM-as-judge 语义质量** | **未实现** | 启发式测不了"答非所问但用词礼貌" |
| **Agent 行为回归评测** | **未实现** | 无 golden 用例集。`evaluation/agent_eval/` 只在 gitignore 的 `.pyc` 里留有痕迹，源码从未提交、当前不可运行 |
| 真实用户满意度 / NPS | **NOT_MEASURED** | 无真实流量 |
| **P99 延迟 / 容量基线** | **NOT_MEASURED** | 有 9 个 `scripts/benchmark_*.py`，但**无 SLO 定义、无生产基线** |
| **生产 SLA** | **不声明** | 无生产环境、无证据。任何百分比都是编的 |

---

## 8. 没有做的技术选择（及理由）

写清楚不做什么，和写清楚做了什么一样重要：

| 不做 | 理由 |
|---|---|
| `interrupt_before` / `interrupt_after` | 无法表达"只有 HIGH 风险工具才审批"这种数据相关条件 |
| LangGraph `store` 长期记忆 | 会与 `core/session/` 职责重叠；无 memory schema 设计 |
| LangGraph 原生 `astream` / `astream_events` | 走自建 callback 总线。**有代价**：`stream_callback` 若作为 state channel 会破坏 checkpoint 的 msgpack 序列化，所以生产改走 ContextVar（`core/streaming_context.py:1-25`）。但 `core/state.py:19` 里的声明**仍在**，任何调用方误填即破坏 checkpoint 持久化 |
| 工具结果直接进 prompt | 5000+ 文档的检索结果会撑爆上下文；走 offload + `reference_id` |
| Celery result backend 作为状态真相源 | `task_ignore_result=True`；真相源是 `agent_runs` 表 |
| Agent 之间自由对话 | 本项目是**受控编排**（LangGraph 决定调谁），要可预测的成本与延迟 |
| 把同步快路径也异步化 | 给秒级交互凭空加一跳延迟 |
| 把异步长任务也走同步 | 审批可能挂几十分钟到几小时，HTTP 等不了；且崩溃 = 状态丢失 |
| MCP 写操作工具 | 只注册 `risk_level=low` 的服务器；写 ledger 未支持 MCP 来源 |

---

## 9. 已知代码债务

不影响功能，但会被 code review 抓到：

| 项 | 位置 |
|---|---|
| `AgentState` 有 7 个运行时写入但**未声明**的键（`_rag_prefetch` / `_needs_upgrade` / `_retried` / `_retried_failed` / `ab_variant` / `ab_experiment` / `extracted_entities`） | `core/state.py` |
| 坏文档引用 `docs/design/mcp-tool-adapter.md`（**该文件不存在**）——MCP 是本项目差异化能力之一，却**完全没有设计文档** | `tools/mcp_adapter.py:42` |
| 协议/实现命名漂移：`ToolRegistryProtocol.get_tools_for_llm` vs 实际 `get_openai_tools` | `core/protocols.py:159` vs `tools/tool_registry.py:111` |
| 根目录 `langgraph.json` 是**孤儿文件**：只被自身与 `core/graph_builder.py:477` docstring 引用，Makefile/requirements/pyproject/docs 全部 0 命中；其中的 `store.ttl` 配置**从未生效** | `langgraph.json` |
| `has_pending_interrupt` 无生产调用方，与 `runtime/executor.py:58` 重复实现 | `core/hitl/gate.py:157` |
| `default_operation_key` 在当前工具路径不可达 | `runtime/side_effects.py:92` |
| `register_hitl_staging_tools` 只在测试里注册（**有意的**，但 docstring 未声明） | `tools/hitl_staging_tools.py:122` |
| langgraph 版本"实测"声明与 lockfile 不符：文档/代码注释称 **1.2.12**（本地 venv 确实是），但 `requirements-lock.txt:144` 锁的是 **1.2.2** | `core/hitl/gate.py:23,48,184` 等 |
| `mcp` 版本要求冲突：`requirements-optional.txt:13` 要 `mcp>=2.0.0`，`requirements-lock.txt:149` 锁 `mcp==1.27.2` | 已在 `tools/mcp_adapter.py:44-45` 标注 2.x 未验证 |
| `pyproject.toml` **没有 `[project].dependencies`**，依赖真相源是 `requirements*.txt` | `pyproject.toml` |

---

## 10. 完全未做的方向

| 方向 | 说明 |
|---|---|
| **Kubernetes 部署** | 只有 Docker Compose（6 变体）。**没有真实集群与多副本一致性验证的 K8s manifest 是装饰性声明，比没有更糟** |
| **自动蓝绿 / 金丝雀切换** | 有 `make canary`（90/10 流量分割），但自动判据与自动回滚**未经真实流量验证** |
| **Serverless 部署** | checkpoint + 长时 worker + Redis 锁的生命周期与 FaaS 冲突 |
| **多 LLM 供应商容灾** | 默认单一 provider；熔断只降级到规则引擎，不切换供应商 |
| **坐席工作台 / 工单系统** | 见 §1.3 |
| **真实 ERP 写操作验证** | 见 §2 |
| **跨会话长期记忆** | 见 §5 |
| **多语言 / 多租户** | 单语言、单租户设计 |

---

## 11. 未来工作（路线图，附前置条件）

完整优先级与执行顺序见
[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) §3–§5。

| 项 | 前置条件 |
|---|---|
| 修复 DLQ 告警断链 | 无（一个端点 + 抓取配置 + 一个契约测试） |
| HITL fail-closed 启动校验 | 无 |
| 工具超时 / 并行 / 熔断 / 重试 | 需先定义"读可重试、写不可重试"的边界 |
| Agent 行为评测 harness | 需确定性 mock graph + JSONL 行为用例集 |
| LLM-as-judge | 需固定 judge 模型 + 判官一致性验证 |
| 坐席工作台 | 需产品定义（工单模型、SLA、坐席权限） |
| RAG gold 人工标注 | 需标注预算 + Kappa 一致性度量 |
| 持久化 trace 后端 | 需选定 Jaeger / Tempo 并配 retention |
| 压测与容量基线 | 需先定义 SLO |
| Kubernetes | 需先有真实集群与多副本一致性验证 |

---

## 12. 复现本文的每一条结论

```bash
make facts          # 当前事实（版本/模型/评测状态），不硬编码数字
make audit-docs     # 文档一致性守卫
pytest --collect-only -q   # 测试规模以当前输出为准
```

关键证据 artifact（可逐个打开复查）：

| artifact | 结论 |
|---|---|
| `artifacts/distributed-runtime/<ts>/report.json` | 分布式 Runtime 跨进程/锁/幂等 **PASS**（带 `tested_code_sha`） |
| `artifacts/runtime/chaos-<ts>.json` | SIGKILL 崩溃恢复 **PASS**（恢复后 `attempt` 递增、副作用仍只发生一次） |
| `artifacts/observability/otel-collector-<ts>/report.json` | 5 个语义 span 到达真实 Collector，隐私 canary 未出现 |
| `artifacts/evaluation/rag-gold-provenance/<ts>/report.json` | 正式检索指标 **当前不可测** 的根因 |
| `artifacts/evaluation/rag-649/preflight-<ts>/report.json` | 那次 preflight 被环境问题阻塞（历史证据，不是当前根因） |
