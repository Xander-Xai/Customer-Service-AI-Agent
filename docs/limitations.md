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

### 1.2 DLQ 告警：指标暴露链已修复，通知投递仍未端到端验证

**状态：Partial（暴露链 ✅ VERIFIED_LOCAL / 通知链 ❌ NOT_VERIFIED）**

这一节曾经是「告警规则**永远不可能触发**」。断链有两半，**两半都已修复**，
但只有一半拿到了真实 Prometheus 的取证。

| 环节 | 修复前 | 现在 | 证据 |
|---|---|---|---|
| 指标注册 | `core/monitoring.py` 注册约 70 个 | 同左 | `core/monitoring.py` |
| **注册表被序列化** | **全仓 `generate_latest` 0 命中 —— 从未输出** | `GET /metrics` = `prometheus_client.generate_latest(REGISTRY)` | `api/routes/monitoring.py::_registry_exposition` |
| **跨进程可见** | worker 容器递增，Prometheus 只抓 app → 恒为 0 | `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector` 聚合共享卷 | `core/metrics_exposition.py` |
| 抓取认证 | 端点在 supervisor/admin 面内，抓取配置**不带凭据** → 401 | `bearer_token_file` + `Authorization: Bearer` 接受 | `api/utils.py::check_admin_token` |
| 告警规则 | `increase(agent_run_dead_letter_total[5m]) > 0` 引用不存在的时间序列 | 同一表达式，**真实 Prometheus 上进入 FIRING** | `make metrics-exposure-verify` |
| Grafana 面板 | 6 个面板引用从未输出的指标 | 补齐输出（`get_stats`/`get_kpi_stats` 里本来就有） | `tests/unit/test_metrics_exposure_contract.py` |

**已验证（VERIFIED_LOCAL）**：`make metrics-exposure-verify` —— 真实
`prom/prometheus:v2.51.0`、真实抓取、真实 worker 进程注入 3 次 dead-letter、
`AgentRunDeadLetterDetected` 实际进入 `firing`。证据：
`artifacts/observability/metrics-exposure-<ts>/report.json`。
另有 `make alert-rules-test`（官方 `promtool test rules`，引用**真实规则文件**）
钉住「真会触发」与「历史值不 latching」。

**仍未验证（NOT_VERIFIED）**：

- **Alertmanager 通知投递**（webhook / SMTP 真的把消息送出去）—— 未做端到端；
- **整套 docker compose 栈**（真实 FastAPI app + 真实 Celery worker 二进制 +
  Grafana 渲染）—— 上面用的是复用了真实暴露函数的 harness，不是整套栈；
- 生产集群 / 多副本长期运行。

因此准确表述是：**「指标可达 + 告警表达式在真实 Prometheus 上成立」已验证，
「有人真的收到通知」尚未验证。** 不要把它写成「告警闭环已完成」。

防复发：`tests/unit/test_metrics_exposure_contract.py` 断言告警规则与 Grafana
面板引用的**每个** metric name 都出现在暴露面里 —— 这类「注册了却没暴露」的断链
不再依赖人记得检查。

### 1.3 "转人工"不是已实现的功能

**状态：Partial（仅状态标记）**

这一条**本轮未改代码**，结论不变：人工**审批**与人工**坐席接管**是两件事，
仓库只有前者。

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
它是一个**会话级状态标记 + 响应策略提示**，不是工单系统。

→ [agent-design.md](agent-design.md) §4

---

## 2. Human-in-the-Loop 的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 审批机制本身 | **Implemented** | 风险分级、拦在执行前、`interrupt()`、durable 表、TTL、职责分离、RBAC |
| 默认开关 | **默认关闭** | `HITL_ENABLED=false`（`core/config.py`） |
| ~~开了但没配 = 静默放行~~ | **已修复：fail-closed** | `HITL_ENABLED=true` 且两条 HIGH 规则全空 → **拒绝启动**（`core.config.validate_hitl_settings`）。与 MCP / 分布式运行时同一原则 |
| ~~没声明风险等级的有副作用工具 = 静默放行~~ | **已修复：fail-closed** | 治理开启时，`side_effect=True` 却没被任何规则认领的工具一律判 **HIGH**。只读工具不受影响 |
| `/api/chat` 快路径 | **明确不在治理边界内** | 快路径无 run 上下文（`core/hitl/gate.py::is_ungoverned_side_effect`）。补偿措施是**拒绝**无治理的副作用调用，而不是放行 |
| 并发审批 | **Implemented** | 真实 PostgreSQL 下 N 个并发决策**恰好一个赢家**（`UPDATE ... WHERE status=PENDING` 的数据库级 CAS），见 `tests/integration/runtime/test_hitl_approval_concurrency.py` |
| 待审批通知 | **Designed** | 只有轮询队列，**没有 push / 邮件 / IM 主动通知** |
| 真实 ERP 退款/改单 | **NOT_VERIFIED** | `tools/hitl_staging_tools.py` 是确定性 staging 工具，验证的是**治理机制**，不是金蝶 ERP 的正确性。无企业 staging 环境 |
| MCP 写操作工具 | **结构上不可能存在** | 只注册 `risk_level=low` 的 MCP 服务器（`tools/mcp_adapter.py`），且 `side_effect=False` 硬编码 |

**默认配置下实际生效的高危工具集 = 只有两个 staging 工具**
（`staging_refund` / `staging_order_change`）。4 个 ERP 工具全部
`side_effect=False`、`risk_level` 未声明 —— 它们是**只读查询**，因此不受
新的 side-effect 兜底影响（这正是兜底没有把「所有 ERP 查询」都拖进审批的原因）。

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
| **原生工具超时** | **Implemented**（本轮新增） | `TOOL_EXECUTION_TIMEOUT_SECONDS`（默认 30s）覆盖**两条**执行分支。只读工具超时降级为可解释错误；**副作用工具超时冒泡**（结果未知），绝不被 ledger 记成成功。契约：`tests/unit/test_tool_execution_reliability.py` |
| 工具级重试 | **不存在** | `RETRY_MAX_ATTEMPTS` 只作用于 **LLM 客户端**。工具失败不以退避重试的形式回到 executor；run 级 retry 只会因**可重试错误分类**而重投整条 run |
| 并行工具调用 | **不存在** | `agents/base_agent.py` 的工具循环是顺序 `for`；`asyncio.gather` 只用于上下文准备。LLM 一次返回多个 tool_call 时没有并行收益 |
| 工具级熔断器 | **不存在** | `CircuitBreaker` 只保护 LLM，不保护 ERP / MCP。ERP 挂掉时每次调用都会等到超时，而不是熔断后快速失败 |
| MCP 工具治理边界 | **Implemented（只读）** | 只注册 `risk_level=low` 的 server；`side_effect=False` 硬编码，因此 MCP 写操作在结构上不可能存在 |
| 真实 ERP 写操作 | **NOT_VERIFIED** | 同 §2 |
| `media/`（图片/音频/视频/文档/TTS）与 `alerts/` | **不在工具循环内** | 它们是 API 层集成，**不是 Agent 可调用的工具**。不要把它们算进"工具生态" |

> 「不存在」三行是被**测试断言**锁住的（`TestAbsentToolLayerFeatures`）：
> 一旦有人给工具层补上同名同参的重试 / 熔断 / 并行，测试会失败并要求同时补
> 配置项、文档与验证 —— 避免出现「文档说有、代码没有」或反之。

---

## 5. RAG 的边界

| 项 | 状态 | 说明 |
|---|---|---|
| 混合检索 + RRF + rerank + 确定性 point ID + 迁移工具 | **Implemented** | ADR-008 |
| 5000+ 语料导入（幂等 + manifest + 覆盖率审计） | **Implemented** | — |
| 正式检索指标（人工相关度判定） | **NOT_VERIFIED，且当前不可测** | 见 §1.1。**全量 649 指标不得对外发布** |
| **gold 数据集缺陷（已量化并修复到"可人工核验"状态）** | **Partial** | 649 query 中 **40 条全部 gold 缺失**、**160 个 gold 引用不在语料中**（见 `make rag-gold-review` 输出）。已产出 1787 条 `DRAFT_UNVERIFIED` + 40 条 `UNDETERMINABLE` 工作清单，**全部通过 `rag-gold-label/v1` 校验**，可交人工判定 |
| **reviewed-gold 评测入口（JUDGED 消费方）** | **Implemented / 待人工标注** | `scripts/evaluate_rag_reviewed_gold.py` 是 `rag-gold-label/v1` 的**唯一评测消费方**：只把人工核验的 `JUDGED` 记录计入正式指标；`DRAFT`/`llm_suggested`/`category_random_match`/`EXCLUDED`/`UNDETERMINABLE` 只计数不计分。**无 JUDGED 标签 → `NOT_MEASURABLE`，不跑 4 组消融**（fail closed）。未标注项**不算作不相关**（`precision_judged` 分母剔除 unjudged）。见 `make rag-gold-reviewed-eval` / `make rag-gold-reviewed-status` |
| **JUDGED 人工相关度标签** | **NOT_MEASURED** | 仓库当前 **0 条**人工 JUDGED 标签（`review_worklist` 全为 `DRAFT_UNVERIFIED`）。正式 reviewed-gold 指标与 Recall/NDCG 因此保持 `NOT_MEASURABLE`，直到人工标注完成 |
| **known-item（构造）gold 与 BM25 消融** | **Implemented / 可测** | query := 文档标题（逐字），相关度**由构造保证**，非人工判定。度量**索引词法可检索性**，不是搜索质量。负控通过（打乱 gold 后 `hit@1` 由 1.0 → 0.0）。见 `make rag-gold-known-item` / `make rag-ablation` |
| **vector_only / hybrid / hybrid+rerank 消融** | **BLOCKED** | embedding provider 在本环境不可用（凭据为占位符）。**不使用占位向量替代**：占位向量会让 Qdrant 返回任意结果，把 `vector_only` 的 Hit@K 变成随机召回率 |
| 语料质量 | **已知缺陷** | 5000 条中仅 **617 条标题唯一**（4383 条文档共享标题），且 `source: "synthetic"`。任何指标都必须附带这一事实 |
| 语义缓存（L2）的向量质量 | 依赖 embedding provider | embedding 不可用时 L2 失效并**显式标记 degraded**，回退 L3 |
| Reranker 不可用 | 降级到 `hybrid_no_rerank` | 该配置的指标是独立口径，不能与 rerank 结果混报 |
| LangGraph `store` 长期记忆 | **Designed（未使用）** | `compile()` 只传 `checkpointer`。跨会话记忆由 `core/session/` 承担，两者**明确分离** |

→ 详见 [reference/agent-evaluation.md](reference/agent-evaluation.md)、[reference/rag-evaluation.md](reference/rag-evaluation.md)

---

## 6. 可观测性的边界

| 项 | 状态 | 说明 |
|---|---|---|
| OTel tracing（provider + 5 个语义 span + 自动 instrument） | **Implemented，本地已验证** | 对真实 Collector 验证过 span 到达与隐私 canary 不泄漏 |
| 结构化日志 + 两层密钥脱敏 | **Implemented** | 含 `scrub_exception_message()` 就地脱敏，防止 ASGI 自己的 traceback 泄漏 |
| 指标注册（~70 个） | **Implemented** | `core/monitoring.py`；实际暴露的 family 数由 `prometheus_exposition_metric_families` 自报 |
| **指标 HTTP 暴露** | **Implemented（VERIFIED_LOCAL）** | `GET /metrics` 序列化真实 REGISTRY；`/metrics/prometheus` 保留 `csai_*` 业务聚合。真实 Prometheus 抓取验证过，见 §1.2 |
| **跨进程指标聚合** | **Implemented（VERIFIED_LOCAL）** | app / worker 是两个容器，`PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`。暴露面自报 `prometheus_multiprocess_enabled`，可对它配告警 |
| **DLQ 告警规则** | **表达式 VERIFIED_LOCAL / 通知 NOT_VERIFIED** | `promtool test rules` + 真实 Prometheus FIRING 均通过；Alertmanager 投递未验证，见 §1.2 |
| Grafana 看板 | **Implemented（未做真实渲染验证）** | 6 个空白面板已补齐输出；**NOT_VERIFIED**：未在真实 Grafana 里渲染确认 |
| 持久化 / 可查询 trace 后端 | **Designed** | `deploy/otel/collector-config.yaml` 只有 `debug` exporter，无存储、无保留期、无查询 UI |
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
| **Agent 行为评测（Agent Eval V1）** | **Implemented（LEVEL_2_APPLICATION_MEASURED）** | 真实编译图 + 脚本化 LLM（零出网）。测**编排/治理/路由**行为，**不测**模型能力。见 [reference/agent-evaluation.md](reference/agent-evaluation.md) |
| **Agent 路由准确率（正式）** | **NOT_MEASURED** | 数据集 111 条全部是 `llm_candidate`，**人工确认数为 0**。门禁因此刻意报 `NOT_AVAILABLE` —— 没测出来不等于达标。用 LLM 起草的标签验证 LLM 驱动的系统 = 自我验证 |
| **工具选择准确率的语义** | **治理层保真度，非模型能力** | 工具计划来自数据集；指标回答的是"编排层有没有把计划执行对" |
| 真实用户满意度 / NPS | **NOT_MEASURED** | 无真实流量 |
| **端到端 P50/P95/P99 / TTFT / QPS / Token 成本** | **NOT_VERIFIED（BLOCKED）** | LLM provider 不可用（凭据为占位符）。**不产生任何估算**。见 `make perf-evidence` |
| 离线工程逻辑（token 计数确定性 / 缓存作用域隔离） | **Implemented** | 不依赖 provider，随 `make perf-evidence` 一起验证 |
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

| 项 | 前置条件 | 状态 |
|---|---|---|
| ~~修复 DLQ 告警断链~~ | — | **已完成**（§1.2）：指标可达 VERIFIED_LOCAL；通知投递仍 NOT_VERIFIED |
| ~~HITL fail-closed 启动校验~~ | — | **已完成**（§2）：启动校验 + side_effect 兜底 + 并发审批真实 PG 验证 |
| ~~原生工具超时~~ | — | **已完成**（§4）；副作用超时的「冒泡而非降级」语义已锁定 |
| 工具并行 / 熔断 / 重试 | 需先定义"读可重试、写不可重试"的边界；并行还需先确认副作用工具的调用序语义 | 未开始 |
| Alertmanager 通知投递端到端 | 需可送达的 webhook/SMTP 接收端 | 未开始（§1.2） |
| ~~Agent 行为评测 harness~~ | — | **已完成**（Agent Eval V1）：真实编译图 + 脚本化 LLM（零出网）+ JSONL 行为用例集；见 [reference/agent-evaluation.md](reference/agent-evaluation.md) |
| LLM-as-judge | 需固定 judge 模型 + 判官一致性验证 | 未开始（`agents/evaluator.py` 仍为启发式） |
| 坐席工作台 | 需产品定义（工单模型、SLA、坐席权限） | 未开始 |
| RAG gold 人工标注 | 需标注预算 + Kappa 一致性度量 | 未开始 |
| 持久化 trace 后端 | 需选定 Jaeger / Tempo 并配 retention | 未开始 |
| 压测与容量基线 | 需先定义 SLO | 未开始 |
| Kubernetes | 需先有真实集群与多副本一致性验证 | 未开始 |

---

## 12. 复现本文的每一条结论

```bash
make facts          # 当前事实（版本/模型/评测状态），不硬编码数字
make audit-docs     # 文档一致性守卫
pytest --collect-only -q   # 测试规模以当前输出为准

# 指标暴露链（本轮修复的 P0）
make metrics-exposure-check     # 契约测试：告警/Grafana 引用的每个指标名都可达
make monitoring-token           # 生成 Prometheus 抓取凭据（fail-closed）
make alert-rules-test           # 官方 promtool：规则语法 + 真会 firing + 不 latching
make metrics-exposure-verify    # 真实 Prometheus 端到端（DLQ 告警实际 FIRING）
```

关键证据 artifact（可逐个打开复查）：

| artifact | 结论 |
|---|---|
| `artifacts/distributed-runtime/<ts>/report.json` | 分布式 Runtime 跨进程/锁/幂等 **PASS**（带 `tested_code_sha`） |
| `artifacts/runtime/chaos-<ts>.json` | SIGKILL 崩溃恢复 **PASS**（恢复后 `attempt` 递增、副作用仍只发生一次） |
| `artifacts/observability/otel-collector-<ts>/report.json` | 5 个语义 span 到达真实 Collector，隐私 canary 未出现 |
| `artifacts/observability/metrics-exposure-<ts>/report.json` | 指标可达 + DLQ 告警在真实 Prometheus 上 **FIRING**；边界写明未验证什么 |
| `artifacts/evaluation/rag-gold-provenance/<ts>/report.json` | 正式检索指标 **当前不可测** 的根因 |
| `artifacts/evaluation/rag-649/preflight-<ts>/report.json` | 那次 preflight 被环境问题阻塞（历史证据，不是当前根因） |
| `artifacts/agent-eval/<ts>/report.json` | Agent 行为评测（真实图回放）；含每个指标的分子/分母/排除数与**证据边界** |
| `artifacts/evaluation/rag-ablation/<ts>/report.json` | BM25 消融（known-item CONSTRUCTED gold）+ 负控；vector/hybrid 配置结构化 BLOCKED |
| `artifacts/evaluation/performance/<ts>/report.json` | 性能/成本门禁：provider 不可用 -> 全部 NOT_VERIFIED，**不产生估算** |
| `artifacts/distributed-runtime-summary/<ts>.json` | 运行时验收汇总；副作用按**真实执行次数**计（非调用次数、非 ledger 命中） |
