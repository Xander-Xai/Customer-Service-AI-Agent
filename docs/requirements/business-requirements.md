# 业务需求与验收规格（Business Requirements & Specification）

> 🟢 CURRENT — 本文是本仓库的**业务侧需求真相源**。
>
> 它回答一个此前无人回答的问题：**这套系统为谁解决什么业务问题，怎么算做成了。**
>
> 边界（务必先读）
> ----------------
> 1. 本文**不含任何业务 KPI 数字**（FCR / 人效倍数 / AHT）。本仓库没有真实企业
>    流量、没有真实客服工单、没有人工基线，**任何数字都是编的**。仓库对此有
>    机器守卫（`scripts/check_public_repository_boundary.py::D1-outbound-business-kpi`）。
>    需要业务数字时，请看 §6 的**测量计划**，而不是引用本文。
> 2. 本文描述的**每个业务能力都必须能在代码里指出 `file:line`**。指不出的，
>    写在 §7「已知不宣称」里，而不是写成承诺。
> 3. 技术设计见 [../architecture.md](../architecture.md)；
>    证据与评测口径见 [../reference/current-state.md](../reference/current-state.md)。

---

## 1. 业务问题与目标

### 1.1 系统定位

一套**化妆品电商客服业务**的 Agent 编排与治理系统。它不是"通用聊天机器人"，
也不是"通用执行沙箱" —— 这条边界在架构层被冻结：

> **主架构：LangGraph 路由式领域工作流 + 受限 ReAct + 双执行路径。**
> 本仓承担客服业务编排、工具治理、人工审批和耐久运行时；不承担通用执行沙箱、
> 跨任务用户画像或独立 RAG 算法实验。
> —— [../architecture.md](../architecture.md#architecture-decision-freeze--2026-11-25)

### 1.2 业务痛点（工程形态）

痛点不是"模型不够聪明"，而是**业务动作不可控**。本系统针对四条具体痛点：

| # | 业务痛点 | 业务后果 | 本系统的应对 | 代码落点 |
|---|---|---|---|---|
| P1 | 高风险写操作（退款/改单）由模型直接执行 | 误退款、越权改单，无法追责 | 执行前审批闸门 + 幂等 ledger + 职责分离 | `core/hitl/`、`runtime/side_effects.py` |
| P2 | 长任务（审批可能挂数小时）与秒级问答是两种负载 | 一种实现必然牺牲另一种 | 双执行路径：同步快路径 / 异步耐久 Run | `core/graph_builder.py`、`runtime/` |
| P3 | 崩溃 + 队列重投 = 重复副作用 | 重复扣款、重复退款 | 三层幂等（run / thread / 工具）+ at-least-once 诚实声明 | `runtime/statuses.py`、`tools/tool_registry.py` |
| P4 | "转人工"只是一句提示词，没有东西接住它 | 客户被反复转接，工单丢失 | 耐久工单 + 坐席工作台（默认关闭） | `core/handoff_service.py`、`api/routes/handoffs.py` |

### 1.3 目标用户与角色

| 角色 | 是谁 | 诉求 | 系统如何服务 | 边界 |
|---|---|---|---|---|
| **终端客户** | 电商平台上的化妆品消费者 | 快速拿到准确答复；需要时快速找到真人 | 意图路由 + 领域 Agent + RAG 证据；必要时转人工工单 | 无人工基线，FCR 不宣称（§6） |
| **客服坐席** | 人工客服 | 拿到足够的上下文来接手；不被重复打断 | 坐席工作台 + 任务诊断（task episode） | 推送通知、SLA 升级未实现（§7） |
| **审批人** | 主管 / 管理员 | 看清"要退多少、哪个订单"再决定 | 审批提案 + 脱敏留痕 + 职责分离 | 不含工单系统对接（§7） |
| **运维/工程** | 值班工程师 | 知道系统现在是什么状态、坏在哪 | 可观测性 + 状态机 + DLQ + 运维手册 | 生产集群未验证（§7） |

---

## 2. 业务范围

### 2.1 业务场景与承担者（全部对应真实代码）

| 业务场景 | 意图（`router/query_router.py::INTENT_CLASSES`） | 承接 Agent | 涉及能力 | 代码落点 |
|---|---|---|---|---|
| 售前咨询 | `product_info` / `recommendation` | `product_agent` / `sales_agent` | RAG 知识检索、成分与功效问答 | `agents/product_agent.py`、`agents/sales_agent.py` |
| 售后支持 | `order_status` / `return_policy` | `billing_agent` / `aftersales_agent` | ERP 只读查询（订单/客户/库存/产品） | `agents/billing_agent.py`、`tools/erp_tools.py` |
| 技术答疑 | `technical_support` / `usage_guide` | `tech_agent` | 成分冲突、用法顺序、敏感肌禁忌 | `agents/tech_agent.py` |
| 投诉处理 | `complaint` / `negative_feedback` | `complaint_agent` | 情绪安抚 + 升级标记 | `agents/complaint_agent.py` |
| 通用咨询 | `greeting` / `general` | `general_agent` | 兜底应答 | `agents/general_agent.py` |
| 复杂混合查询 | 路由判定后进入受限 ReAct | `react_agent` | 多工具编排（受步数与预算约束） | `collaboration/modes.py::ReActMode` |

场景 → 意图的权威映射是 `router/query_router.py::SCENE_MAPPING`；意图 → Agent
是同文件的 `INTENT_AGENT_MAP`。本文不重复定义，只指向它们。

### 2.2 明确不做的事（范围外）

这些是**业务范围**上的"不做"，不是技术限制：

| 不做 | 理由 | 关联 |
|---|---|---|
| 不做坐席工单系统的**通知推送 / SLA 升级 / 排班** | 需要产品定义（通知渠道、SLA 分级、坐席权限模型），非工程可单方面决定 | [../reports/audit/PROJECT_FINALIZATION_PLAN.md](../reports/audit/PROJECT_FINALIZATION_PLAN.md) |
| 不做真实 ERP 写操作的**业务正确性**担保 | 需要企业 staging + 对账流程 | [../limitations.md](../limitations.md) §2 |
| 不做跨会话用户画像 | 会与 `core/session/` 职责重叠，且需隐私评估 | [../limitations.md](../limitations.md) §5 |
| 不做多租户 / 多语言 | 当前为单租户单语言设计 | [../limitations.md](../limitations.md) §10 |

---

## 3. 业务流程与任务建模

### 3.1 业务任务状态机（业务侧，不是运行时侧）

运行时状态机在 `runtime/statuses.py::ALLOWED_TRANSITIONS`（**唯一真相源**）。
本文描述的是**业务侧**任务生命周期 —— 它跨多次运行、跨人工参与，是运行时
状态机之上的业务语义。

```text
客户发起咨询
      │
      ▼
┌─────────────┐   意图路由 + 领域 Agent
│ 受理中       │──── 纯咨询/只读查询 ──────────────────┐
│ (INTAKE)    │                                        │
└──────┬──────┘                                        │
       │ 涉及写操作 / 需人工判断                        │
       ▼                                               │
┌─────────────┐   人工审批（approve / edit / reject）  │
│ 待人工决策   │◀───────────────────────────────────────┤
│ (PENDING)   │                                        │
└──────┬──────┘                                        │
       │ 批准                                          │
       ▼                                               │
┌─────────────┐   工具执行 + 幂等校验                  │
│ 执行中       │◀───────────────────────────────────────┘
│ (EXECUTION) │
└──────┬──────┘
       │
       ├── 成功 ──▶ ┌─────────────┐
       │            │ 已解决       │── 有独立执行证据 → 业务可核验
       │            │ (RESOLVED)  │
       │            └─────────────┘
       │
       ├── 失败 ──▶ ┌─────────────┐
       │            │ 未解决       │── 降级 / 兜底文案 / 空回复
       │            │ (UNRESOLVED) │
       │            └─────────────┘
       │
       └── 需人工接管 ──▶ ┌─────────────┐
                        │ 已转人工     │── 耐久工单 + 坐席接手
                        │ (HANDOFF)   │
                        └─────────────┘
```

**这个状态机与运行时状态机的关系**（必须分清，否则会互相误读）：

| | 业务任务状态机（本文） | 运行时 Run 状态机 |
|---|---|---|
| 生命周期 | 跨多次运行、跨人工参与 | 单次 Graph 执行 |
| 真相源 | 业务语义（本图为**投影**，权威在工单与审批表） | `runtime/statuses.py::ALLOWED_TRANSITIONS` |
| 典型时长 | 小时到天（等审批、等坐席） | 秒到分钟 |
| 状态举例 | 待人工决策 / 已转人工 | `WAITING_APPROVAL` / `QUEUED` / `DEAD_LETERER` |

一个业务任务可以产生**多个** Run（例如审批后恢复执行会产生新的 run）。
Run 的幂等键 `run_id:approval:{approval_id}` 保证恢复不会重复执行副作用
（`core/hitl/`）。

### 3.2 业务规则表

确定性规则由**代码**控制，不交给 LLM。下表每一行都能在代码里定位：

| # | 业务规则 | 强制点 | 代码落点 |
|---|---|---|---|
| B1 | 高风险写操作**必须在执行前**被审批拦下 | 工具循环摘出 → 图挂 interrupt → 审批表落库 | `agents/base_agent.py`（`_should_gate_tool`）、`core/hitl/gate.py` |
| B2 | 审批人不得是发起人 | 数据库级 CAS + 角色校验 | `api/routes/approvals.py` |
| B3 | 无 durable run 上下文的写操作**一律拒绝** | 执行边界 fail-closed | `core/hitl/gate.py::is_ungoverned_side_effect`、`tools/tool_registry.py` |
| B4 | 副作用工具**不能自称只读**来逃过治理 | 依据注册表声明的 `side_effect`，不信模型描述 | `tools/tool_registry.py` |
| B5 | 同一笔业务操作重放**只执行一次** | 内容寻址幂等键 + 账本 | `runtime/side_effects.py::build_replay_stable_operation_key` |
| B6 | 工单存在期间机器人**不得自动应答** | 图入口早退 | `core/graph_builder.py` |
| B7 | 会话/响应缓存**不得跨用户**泄漏 | 作用域按用户；无身份 + 个性化意图 = 不缓存 | `cache/cache_policy.py::resolve_cache_policy` |
| B8 | 审批提案与 run 事件流**不得**记录凭据/原文 | 提案黑名单脱敏；事件流白名单 | `core/hitl/sanitize.py`、`runtime/events.py::sanitize_payload` |
| B9 | 用户输入与历史对话是**不可信数据**，不是指令 | 标签包裹 + 显式不可信提示 | `agents/base_agent.py::_prepare_llm_messages` |
| B10 | 确定性业务规则由代码执行，不问 LLM | 风险分级、金额阈值、职责分离全在代码 | `core/hitl/risk.py` |

### 3.3 异常分支与人工介入点

| 异常 | 系统行为 | 人工介入 | 代码落点 |
|---|---|---|---|
| LLM 不可用 | 熔断 → 规则引擎降级 → 兜底文案 | 否（但答复质量下降） | `llm/client.py`、`llm/rule_based_llm.py` |
| 工具执行失败（只读） | 降级为可解释错误字符串，Agent 换策略 | 否 | `agents/base_agent.py` |
| 工具执行失败（**副作用**） | **冒泡**为 `SideEffectExecutionError`，结果未知 | 是（人工对账） | `runtime/errors.py` |
| 工具超时 | 只读降级；副作用冒泡（不记成功） | 副作用需是 | `tools/tool_registry.py` |
| 模型要求跳过审批 | **不受影响** —— 治理层不依赖模型判断 | 是 | `core/hitl/risk.py` |
| 客户要求转人工 | 创建耐久工单，机器人挂起 | 是（坐席接手） | `core/graph_builder.py`、`core/handoff_service.py` |
| worker 崩溃 | lease 过期 → reconciler 回收 → 从 checkpoint 续跑 | 通常否 | `runtime/executor.py`、`runtime/repository.py` |
| 重试耗尽 | 落 DLQ（可人工重放，**复用原 run_id**） | 是（人工重放） | `runtime/run_service.py`、`scripts/replay_dead_run.py` |
| 知识库检索失败 | 显式降级标记，不伪装成正常回答 | 否 | `rag/`、`core/outcome.py` |

---

## 4. 需求规格（SDD）

### 4.1 功能规格

| 编号 | 功能 | 验收方式 | 状态 |
|---|---|---|---|
| F1 | 意图路由到 7 个领域 Agent + 受限 ReAct | Agent Eval `route_accuracy` / `tool_selection_accuracy` | 实现；路由准确率 **NOT_MEASURED**（缺人工标注） |
| F2 | 高风险写操作执行前审批 | `hitl_trigger_accuracy` + `hitl_gate_propagation` | 实现并验证 |
| F3 | 审批后可恢复执行，且不重复副作用 | `make runtime-e2e`（真实 PG + Redis） | 实现并验证（Level 2） |
| F4 | 业务完成必须区分"交付"与"解决" | `core.outcome` 五级 `business_outcome` | 实现并验证 |
| F5 | 耐久工单 + 坐席工作台 | `tests/unit/test_handoff_*.py` | 实现，默认关闭；生产验收未做 |
| F6 | 异步耐久 Run（跨进程恢复） | `make runtime-e2e` / `make runtime-chaos` | 实现并验证（Level 2） |
| F7 | MCP 只读工具接入（默认关闭） | `make test-mcp` / `make mcp-verify` | 实现并验证（本地 fake server） |
| F8 | 转人工的完整闭环（工单 + 消息 + 坐席认领） | `tests/unit/test_handoff_*.py` | 实现；通知/SLA 未做 |

### 4.2 接口规格

| 类别 | 权威来源 |
|---|---|
| HTTP API（63+ 路径） | `docs/reference/api-reference.md` + `docs/openapi.json`（`make openapi-check` 保证同步） |
| Tool Schema | `tools/tool_registry.py` 注册时的 `parameters` |
| Agent Run 状态机 | `runtime/statuses.py::ALLOWED_TRANSITIONS` |
| 审批提案结构 | `core/hitl/sanitize.py` |
| 评测 artifact | `evaluation/agent_eval/contract.py` |

### 4.3 非功能规格

| 类别 | 规格 | 当前证据状态 |
|---|---|---|
| 可靠性 | at-least-once 投递 + 三层幂等；**不宣称 exactly-once** | Level 2 验证；生产集群 NOT_VERIFIED |
| 性能 | 端到端 P50/P95/P99、TTFT、QPS | **NOT_VERIFIED（BLOCKED）** —— 无 provider 凭据，不产生估算 |
| 成本 | 单任务 Token 预算与配额 | `core/token_quota.py` 实现；真实成本 NOT_VERIFIED |
| 安全 | 权限隔离、审批、审计、脱敏 | 对抗性套件 `tests/security/` + 真实 PG 并发验证 |
| 可观测 | 结构化日志 + OTel span + Prometheus | 指标暴露链 VERIFIED_LOCAL；通知投递 NOT_VERIFIED |

---

## 5. 业务验收标准

**这一节的每一条都绑定到可执行的命令或可核验的 artifact。**
"看起来能跑"不算验收；README 里写了也不算。

### 5.1 当前可执行的门禁

| 编号 | 验收标准 | 判定方式 | 通过条件 |
|---|---|---|---|
| AC1 | 工具选择与参数校验正确 | `make agent-eval` | `tool_selection_accuracy` = 1.0 且 `tool_argument_schema_pass_rate` = 1.0 |
| AC2 | 工具执行成功可核验 | `make agent-eval` | `tool_execution_success_rate` = 1.0 |
| AC3 | 安全边界未被越过 | `make agent-eval` | `safety_violation_rate` = 0.0 且 `forbidden_tool_rate` = 0.0 |
| AC4 | 故障被吸收而非崩溃 | `make agent-eval` | `recovery_rate` = 1.0 |
| AC5 | 高风险动作被正确拦下 | `make agent-eval` | `hitl_trigger_accuracy` = 1.0 且 `hitl_gate_propagation` = 1.0 |
| AC6 | 治理结果与业务结果**分开**统计 | `make agent-eval` | `governance_outcome_match_rate` = 1.0 且 `task_completion_rate` ≥ 0.95 |
| AC7 | 能力未相对基线退化 | `make agent-eval-regression` | 无 `REGRESSION` 判定；`INCOMPARABLE` 需人工确认口径 |
| AC8 | 安全回归会让 CI 变红 | `make test-security` | 全部通过；该 lane 在 CI 中**阻塞** |
| AC9 | 对抗性攻击被结构性控制挡住 | `make test-security` | 注入载荷不改变风险分级；越权写操作副作用发生次数 = 0 |
| AC10 | 耐久运行时在真实基础设施上成立 | `make runtime-e2e` | 全部通过（缺基础设施时**硬失败**，不静默 skip） |
| AC11 | 崩溃后可恢复且副作用不重复 | `make runtime-chaos` | 恢复后 `attempt` 递增，副作用仍只发生一次 |
| AC12 | 文档与运行时事实一致 | `make audit-docs` | 0 error |

### 5.2 三态判定的诚实语义

`make agent-eval` 的结论是**三态**，不是二态：

| 结论 | 含义 | 能否宣称"验收通过" |
|---|---|---|
| `PASS` | 全部可测门禁通过且无一项未测出 | 可以 |
| `INCONCLUSIVE` | 可测门禁全过，但仍有门禁 `NOT_AVAILABLE` | **不可以** —— "没测出来"不等于"达标" |
| `FAIL` | 至少一条可测门禁真的没过 | 不可以，且必须修 |

当前 `route_accuracy` 因缺人工确认标注恒为 `NOT_AVAILABLE`，因此整体结论是
`INCONCLUSIVE`。这是**刻意的正确状态**，不是缺陷。

### 5.3 尚不能验收的业务标准（及解除条件）

| 业务标准 | 为什么现在不能验 | 解除条件 |
|---|---|---|
| 路由准确率达标 | 数据集 115 条标注全部是 `llm_candidate`，人工确认数 0 | `scripts/approve_agent_eval_annotations.py` 完成人工确认（并给出 Kappa 一致性度量） |
| 首次接触解决率（FCR） | 无真实流量、无工单量、无"是否再次联系"的事件 | 接入真实工单系统并按 issue 级定义采集（窗口需为显式业务参数） |
| 平均处理时长（AHT）/ 人效 | 无人工基线、无处理量、无坐席工时 | 先采集 before/after AHT 与吞吐量样本，再谈结论 |
| 真实 ERP 写操作正确性 | 无企业 staging、无对账流程 | 企业 staging + 下游接受幂等键 + 人工对账 |
| 端到端延迟与成本 | provider 凭据为占位符 | 提供真实凭据后跑 `make perf-evidence` |
| 告警真的送达 | 只验证了表达式在真实 Prometheus 上 FIRING | 可送达的 webhook/SMTP 接收端 + 端到端投递验证 |
| 坐席工作台的运营闭环 | 无推送、无 SLA、无排班 | 产品定义 + 真实坐席值守验收 |

---

## 6. 业务价值测量计划（而不是测量结果）

本节是本仓库**唯一**应该出现的业务数字入口 —— 但它现在全是**待采集**，
因为没有真实业务数据。

### 6.1 需要的基线（全部待采集）

人工基线当前状态：**`NOT_MEASURED`**。仓库没有可用于对照的人工客服工单、坐席工时或
真实流量；Agent Eval 中的 `human_intervention_rate` 仅描述固定测试集，不可替代该基线。

| 指标 | 定义 | 采集方式 | 当前状态 |
|---|---|---|---|
| AHT（人工） | 人工坐席单次会话平均处理时长 | 工单系统的 `closed_at - opened_at` | 待采集 |
| AHT（Agent） | Agent 会话从受理到终态的时长 | `agent_runs` 的 `started_at` → 终态时间戳 | 待采集 |
| 处理量 | 单位时间完成的会话数 | 工单 + `agent_runs` 计数 | 待采集 |
| 转人工率 | 需要人工的会话占比 | Agent Eval `human_intervention_rate` 口径（**注意**：那是数据集分布，不是生产流量） | 生产值待采集 |
| FCR | 窗口内未再次联系的问题数 / 总问题数 | 需要"再次联系"事件，当前**没有** | 未定义 |
| 客户满意度 | CSAT / NPS | `POST /api/feedback` 已存在但无真实流量 | 待采集 |

### 6.2 测量纪律

1. **样本窗口必须显式**（例如"FCR 的 N 小时窗口"是业务参数，不是实现细节）。
2. **口径必须写进 artifact**，附分子/分母/排除数（仓库在
   `evaluation/agent_eval/` 已建立这套纪律，直接复用）。
3. **没有 before/after 对照就不给倍数结论**。
4. **不得把数据集里的分布当生产分布**：`human_intervention_rate` = 0.087 反映的是
   人工编排的数据集构造，不是真实流量的人工介入率。

---

## 7. 已知不宣称清单（业务侧）

本文档**不**承诺以下能力。完整技术边界见 [../limitations.md](../limitations.md)。

| 不宣称 | 原因 |
|---|---|
| "FCR 达 X%" / "人效提升 N 倍" | 无真实流量、无人工基线；仓库有机器守卫禁止发布此类数字 |
| "生产可用" | 生产集群、多副本长期运行、真实流量均 NOT_VERIFIED |
| "接了任意第三方 MCP server 都成立" | 只验证过本仓适配器对**本地 fake server** 的契约 |
| "转人工是完整的企业级闭环" | 工单后端已实现，但推送、SLA、真实坐席值守未做 |
| "支持 exactly-once" | 投递语义是 at-least-once，正确性靠三层幂等 |
| "有持久化可查询的 trace 后端" | Collector 只有 `debug` exporter，无存储、无查询 UI |

---

## 8. 变更规则

1. 改动**业务规则**（§3.2）必须同时更新本文件与 `core/hitl/risk.py` 等实现，
   并补 `tests/security/` 下的回归用例。
2. 新增**业务指标**必须先落地为可执行门禁（§5.1 格式），再写进 §6.1 ——
   顺序反过来就会产生"文档里有、测不出来"的指标。
3. 本文件引用的每个 `file:line` 都必须存在。文档一致性审计
   （`make audit-docs`）会检查链接与事实漂移。
4. §5.3 的每一行被解除时，必须**同时**给出证据 artifact 与状态变更，
   不得只改文字。
