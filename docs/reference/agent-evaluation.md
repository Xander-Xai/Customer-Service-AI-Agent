# Agent 行为评测（Agent Eval V1）

> 🟢 CURRENT — 本文件是 `evaluation/agent_eval/` 的权威说明。
> 指标名、证据边界、降级标记清单的**唯一真相源**是
> `evaluation/agent_eval/contract.py`，本文件是它的可读投影。

---

## 0. 一句话定位

**Agent Eval V1 衡量的是编排与治理层的行为，不是模型的决策质量。**

它跑的是**真实编译图**（`container.graph_app.astream(stream_mode="updates")`），
但用**脚本化 LLM** 驱动，因此结构上不出网、可重复，且不度量模型能力。

这条边界不写清楚，读者会把 `tool_selection_accuracy = 100%` 误读成
"模型选工具选得准"。它实际的含义是："给定一份脚本化的工具计划，编排层把它
执行对了。" 这句话被逐字写进每个 evidence artifact 的
`evidence_boundaries`。

---

## 1. 为什么不能拿 `pytest` 通过率当 Agent 任务成功率

| | 单元测试通过率 | Agent Eval |
|---|---|---|
| 分母 | 测试函数数 | 业务 case 数 |
| 失败定义 | 断言不成立 | 任务未完成 / 降级 / 治理失守 |
| 能证明 | 代码按预期被写 | 端到端行为符合预期 |
| 不能证明 | 端到端能否完成任务 | 回答内容是否正确（需人工/LLM 判官） |

两者分母、失败定义、证据形态都不同，混用等于宣称"测试通过 = Agent 能干活"。

---

## 2. 数据集（`tests/eval/agent_cases.jsonl`）

JSONL，一条 case 一行。字段与约束见 `evaluation/agent_eval/cases.py`。
**严格校验、失败即崩**：一条语义不合法的 case 被静默跳过会让分母变小、分数变高 ——
那是评测系统最危险的缺陷。

覆盖的类别（111 条候选）：

| 类别 | 数量 | 说明 |
|---|---|---|
| 产品咨询 | 10 | 意图分类 |
| 成分与肤质 | 12 | 意图分类 |
| 技术支持 | 12 | 意图分类 |
| 售前销售 | 10 | 意图分类 |
| 费用与订单查询 | 10 | 意图分类 |
| 售后处理 | 10 | 意图分类 |
| 投诉反馈 | 10 | 意图分类 |
| 通用 / 问候 | 6 | 意图分类 |
| 多意图组合 | 8 | 意图分类 |
| 工具执行（只读 / HIGH / 混合） | 15 | 治理保真度 |
| 故障注入 | 4 | 降级路径 |
| 禁止工具 | 5 | 治理边界 |

### 2.1 标注必须人工确认（最重要的纪律）

每条 case 的 `annotation.provenance` 默认是 **`llm_candidate`**：

> 标签由 LLM 依据 `router/query_router.py` 的意图分类契约起草，**尚未**经人确认。

**只有 `human_confirmed` 的 `expected_route` 才进入正式 `route_accuracy` 分母。**
否则就是"用自己生成的标签验证自己"，得到的准确率没有意义。

人工确认入口：

```bash
make agent-eval-annotation-status                      # 看当前确认进度
python3 scripts/approve_agent_eval_annotations.py --list --tag 技术支持
python3 scripts/approve_agent_eval_annotations.py \
    --confirm route_技术支持_023 --reviewer alice
python3 scripts/generate_agent_eval_cases.py \
    --apply-approvals tests/eval/agent_annotation_approvals.json
```

确认记录单独落 `tests/eval/agent_annotation_approvals.json`（可审计、可回滚），
**不**直接改写数据集。`tests/unit/test_agent_eval_contract.py` 有一条守卫：
仓库里不得出现自称 `human_confirmed` 却没有 `confirmed_by` 的 case。

---

## 3. 指标

### 3.1 正式指标（12）

| 指标 | 分母 | 备注 |
|---|---|---|
| `route_accuracy` | **human_confirmed** 且走 `rule_shortcut` 的 case | 端到端、真实生产代码。未标注时 `NOT_MEASURED` |
| `tool_selection_accuracy` | `expected_tools` 非空且落在 `react` 模式的 case | 治理层保真度，**不是**模型选工具能力 |
| `tool_argument_schema_pass_rate` | 实际发起的工具调用 | 校验参数是否通过工具声明的 JSON Schema 子集 |
| `forbidden_tool_rate` | 声明了 `forbidden_tools` 的 case | 目标 0，越低越好 |
| `hitl_trigger_accuracy` | `expected_risk` 且产生可观测工具调用的 case | HIGH 必须被摘出且未执行；低风险不得被误拦 |
| `workflow_execution_rate` | 全部观测到的 case | 图是否正常结束（无未捕获异常） |
| `response_delivery_rate` | 全部观测到的 case | 是否交付了非空回复。**交付 ≠ 解决** |
| `governance_outcome_match_rate` | 全部观测到的 case | 图是否抵达 `expected_terminal_state`，**包含** `WAITING_APPROVAL`（治理正确） |
| `task_completion_rate` | **业务完成口径适用**的 case | 抵达终态 + 非空回复 + **无未预期降级**；`WAITING_APPROVAL` 被排除并计入 `excluded`；故障注入 case（`expect_task_completed=false`）不计入分子 |
| `task_completion_evidence_coverage` | 被计为完成的 case | 完成结论有**真实执行过的工具结果**支撑的比例。interrupt/pending_actions **不算**业务证据 |
| `fallback_rate` | 全部观测到的 case | 命中至少一个真实降级标记的占比 |
| `fallback_detection_accuracy` | 声明 `expect_fallback` 或命中标记的 case | 标注与实测是否一致 |

### 3.1.1 三态 overall_status（NOT_MEASURED ≠ 达标）

| overall_status | 含义 | 退出码 |
|---|---|---|
| `FAIL` | 至少一条**可测**门禁真的没过（系统缺陷） | 1 |
| `INCONCLUSIVE` | 可测门禁全过，但仍有门禁 `NOT_AVAILABLE`（缺外部前提，如人工标注） | 0 |
| `PASS` | 全部门禁 `PASS` 且无一条 `NOT_AVAILABLE` | 0 |

**`PASS` 与"存在 `NOT_AVAILABLE`"严格互斥**：没测出来的东西永远不能算达标。
artifact 会逐条列出 `measurement_gaps`（含每个缺口的"解除条件"），CI 的
`agent-eval` step 因此是**真正阻塞**的，不再使用 `continue-on-error` 把缺口吞掉。

> **`task_completion_rate` 的口径修订（v1 → v1.1 → v1.2）**：
> - **v1**：只要求"抵达终态 + 非空回复" → LLM 故障后的兜底文案被算成完成（111/111）。
> - **v1.1**：要求"无未预期降级"；拆出 `workflow_execution_rate` /
>   `response_delivery_rate` / `task_completion_evidence_coverage`。
> - **v1.2**：**`WAITING_APPROVAL` 不再计入业务完成**（新增
>   `governance_outcome_match_rate` 承接"治理正确"的语义），并把
>   interrupt/pending_actions 从"业务完成证据"里剔除 —— 它只证明"拦住了"，
>   不证明"退款真的退了"。
>
> 三代数字**互不可直接比较**。运行时监控侧用同一套定义
> （`core.outcome.Outcome` 的 `business_outcome` 五级：
> `not_resolved` / `requires_human` / `unverified_degraded` / `assessed` /
> `evidenced`），降级交付不计入 `total_single_turn_resolved`。

### 3.2 诊断指标（3）

`rule_route_agreement`（纯规则分类器 vs 标注）、
`hitl_gate_propagation`（摘出 → interrupt 的端到端连通性）、
`step_count`（节点执行次数分布 + 超预算 case）。

### 3.3 三条不可让步的计数纪律

1. **每个指标都带 `numerator` / `denominator` / `excluded`**；
2. **分母为 0 返回 `NOT_MEASURED`，不返回 0%** —— 把"没跑"渲染成"全军覆没"
   与把"跑了没过"渲染成"没跑"一样是撒谎，两个方向都有测试钉住；
3. **不可达的 case 显式排除并记账**（例如故障注入 case 的工具调用按设计不该发生）。

---

## 4. 命令

```bash
make agent-eval                      # 全量回放 + evidence artifact
make agent-eval-contract             # 契约守卫（指标名/边界/降级标记与生产同步）
make agent-eval-annotation-status    # 标注状态 + 各指标分母
make agent-eval-cases                # 重新生成候选数据集
```

artifact：`artifacts/agent-eval/<ts>/report.json`（schema `agent-eval-evidence/v1`），
带 `git_sha` + 数据集 `sha256`。

---

## 5. 这个 harness 已经抓到的真实缺陷

### 5.1 HITL 闸门在真实图上从未触发（P0，已修复）

构建 harness 时发现：`collaboration/modes.py::ReActMode.execute` 的返回值只挑了
`response / mode / agents_used / elapsed`，**丢掉了 Agent 写进自己那份
`dict(state)` 副本的 `pending_actions`**。

后果（在真实图上实测）：

```
[HITL] 高风险工具已摘出待人工审批 tool=staging_refund
HITL interrupt?  : False
pending_actions? : None
SIDE EFFECTS RUN : 0
```

被摘出的退款**既没执行、也没审批**，run 照常报告成功 —— 比"不拦"更危险：
不拦会执行，静默丢弃会让用户的钱无声消失，而 SLA/监控把这次会话记成 resolved。

修复：五个协作模式的返回值全部透传 `pending_actions`（不只 react ——
让"模式返回值漏字段"这一整类 bug 不再有存活空间）。
回归：`tests/unit/test_hitl_real_graph_gate.py` 驱动**真实编译图**断言
摘出 → 透传 → `__interrupt__`，并断言副作用一次都没发生。

> 为什么之前没抓到：仓库里唯一验证 HITL 的集成测试是**自己搭的最小图**
> （只含 gate 节点），直接驱动 `run_approval_gate`。它验证的是 LangGraph 的
> interrupt/resume 语义，完全覆盖不到"协作模式 → 图节点"这条接缝。

### 5.2 LLM 不可用时静默返回无关文案

`fault_agent_llm_*` 用例显示：Agent LLM 抛错后系统回落到 `RuleBasedLLM`，
返回一段与用户问题无关的模板文案，run 记 SUCCEEDED，**没有任何降级标记**。
用户无法分辨这是"真的回答"还是"降级文案"。已登记为降级标记并被 `fallback_rate`
覆盖，属于已知可观测性缺口（见 `limitations.md`）。

### 5.3 会话语义：非法 session_id 导致图崩溃（已加固）

`get_conversation_context` 对不存在的会话抛
`TypeError: 'NoneType' object is not subscriptable`。API 层在进图前会规范化
`session_id`，因此正常 HTTP 路径不可达；但 worker / 评测 / 直接调用
`graph_app` 的路径没有这道规范化。已改为返回空上下文
（调用方本来就写了空值分支）。

---

## 6. 边界（不得越界宣称）

| 项 | 状态 |
|---|---|
| 编排 / 治理 / 路由行为 | **LEVEL_2_APPLICATION_MEASURED**（真实图 + 脚本化 LLM，零出网） |
| 模型能力（选工具、答对、推理质量） | **不测** —— 需要 LLM-as-a-Judge 与真实 provider |
| 真实 ERP 写入 | **NOT_VERIFIED** —— staging 工具验证的是治理机制 |
| 检索质量 | **不测** —— 那是 `make rag-eval-649` 的事 |
| `elapsed_ms` | **不是 SLO 证据** —— 进程内无网络回放，只反映 harness 开销 |
| 生产 P50/P95 | **NOT_VERIFIED** —— 见 `make perf-evidence` |

---

## 7. 复现本文的每一条结论

```bash
make agent-eval                   # 指标 + 分母 + artifact
make agent-eval-contract          # 契约守卫
make agent-eval-annotation-status # 人工确认进度（当前应为 0%，这是正确状态）
python3 scripts/evaluate_agent.py --print-contract   # 完整证据边界
```
