# Evaluation Credibility Remediation — Task A (RAG gold) + Task B (Agent eval)

> **HISTORICAL AUDIT SNAPSHOT** — 本文件是一次评估可信度整改的**执行时点**记录，
> 只在该时点有效，**不是**永久 Current Truth。当前事实入口是
> [../../reference/current-state.md](../../reference/current-state.md)。
>
> - Branch: `fix/eval-reviewed-gold-credibility`
> - Base (审计时点 HEAD): `c67e8ed9daf49bdb9c8fbe2b547cbc6a805ad2c0`
> - 语料 hash: `a81ea7f3347b45b3adefe474790fabd0b2277eada05327422134d3ca0737502d`
>   （`data/knowledge_base/knowledge_base_5000.jsonl`，5000 docs）

---

## 1. 一句话结论

- **Task A**：新增 `scripts/evaluate_rag_reviewed_gold.py`，成为 `rag-gold-label/v1`
  契约的**唯一评测消费方**。当前仓库 **0 条人工 JUDGED 标签**，因此正式检索指标与
  Recall/NDCG 保持 **NOT_MEASURABLE**（fail closed），4 组消融**未执行**。旧 649
  benchmark、审计证据、历史 artifact **未改动**；未生成任何伪人工标注。
- **Task B**：补 `expected_parameters` + `expected_parameter_match_rate` 诊断指标、
  `agent_llm_timeout` 超时故障注入、参数正/负控用例；`provenance.real_provider` 显式
  **NOT_MEASURED**。评测集 111 → **114** case，全部离线可复现。
- **证据等级**：Agent Eval = `LEVEL_2_APPLICATION_MEASURED`（真实图 + 脚本化 LLM，
  零出网）。reviewed-gold RAG 指标 = `NOT_MEASURABLE`（缺人工标注）。真实模型评测
  = `NOT_MEASURED`（无凭据）。

---

## 2. Task A — Gold Label 整改

### 2.1 已确认的旧缺陷（不改写、不覆盖）

`tests/eval/rag_benchmark.json`（649 query）的 `expected_doc_ids` **不是相关度判定**；
静态审计（`scripts/rag_gold_label_provenance.py`，见
[docs/reference/rag-gold-label-provenance.md](../../reference/rag-gold-label-provenance.md)）
判定 `FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD`：
1947 个 gold 标签中 **0 个**有 relevance provenance，307 个可证无效，其余来源无记录。
本次整改**保留**原 649 数据、审计脚本与历史 artifact，**不覆盖、不删除、不伪造**。

### 2.2 新增：reviewed-gold 评测入口（本契约缺少的消费方）

之前只有契约（`scripts/gold_label_contract.py`）与校验器
（`scripts/validate_gold_labels.py`），**没有任何评测器消费 JUDGED 标签**——
契约无人消费等于摆设。新增 `scripts/evaluate_rag_reviewed_gold.py`
（artifact schema `rag-reviewed-gold-eval/v1`）：

| 规则 | 实现 |
|---|---|
| 只计人工核验的 `JUDGED` | `DRAFT_UNVERIFIED` / `llm_suggested` / `category_random_match` / `EXCLUDED` / `UNDETERMINABLE` 只计数不计分 |
| 未标注 ≠ 不相关 | 检索到的无判定文档计入 `unjudged_at_K` 并从 `precision_judged@K` 分母剔除；同时给出标准 `precision@K` 并标 `precision_is_lower_bound` |
| Recall/NDCG 需完整判断 | 仅当一条 query 的全部 JUDGED 记录带 `judgment_completeness: "closed"` 时计算；否则记 `null` + `recall_ndcg_reason=JUDGMENT_SET_NOT_CLOSED` |
| 无确认相关文档 | 全部 `grade=0` 的 query 记为 `NO_CONFIRMED_RELEVANT_DOC` 并排除，**不当作 0 分** |
| 无 JUDGED 标签 | 整体 `NOT_MEASURABLE`（正式入口退出码 2），**不执行 4 组消融** |

NDCG 采用分级增益（`gain = 2^grade - 1`），Hit/Precision/Recall/MRR 为二元相关。
artifact **固定记录**：git SHA + code provenance、语料 hash/doc 数、query 数、
有效标签数、各状态计数、模型与环境、失败查询、各指标 `numerator_sum/denominator/not_measured`。

### 2.3 复现（离线，无需 provider）

```bash
# 契约 + 语料一致性（旧系统，未改）
make rag-gold-validate

# 评测消费方：对 649 工作清单做状态/质检（离线，不触 Qdrant）
make rag-gold-reviewed-status GOLD_LABELS=tests/eval/gold_labels/review_worklist.jsonl
#   -> status=NOT_MEASURABLE (0 judged queries, 0 recall-eligible, 649 excluded)

# 人工标注完成后的正式入口（当前会 fail closed；需 Qdrant + provider + 完整判断）
make rag-gold-reviewed-eval GOLD_LABELS=<人工标注产出>.jsonl
```

### 2.4 人工标注工作量

- 待标注工作清单：`tests/eval/gold_labels/review_worklist.jsonl`
  —— 1787 行 `DRAFT_UNVERIFIED` + 40 行 `UNDETERMINABLE`，覆盖 649 query。
- **人工判定为 0**（本整改**不代替**人工判定）。`review_worklist` 中 `KNOWN_ITEM`
  的构造 gold 单独走 `make rag-gold-known-item` / `make rag-ablation`，与 JUDGED
  **永不合并**。
- 建议：先标注 `full_gold_covered` 子集（gold 均在语料内的 query），每条 query 填
  `evidence_span` + `relevance_grade(0-3)` + `judgment_completeness`。

---

## 3. Task B — Agent 业务评测

### 3.1 已有基础（本次未推翻）

`evaluation/agent_eval/` + `scripts/evaluate_agent.py` 已在**真实编译图**
（`container.graph_app.astream`）上用**脚本化 LLM**（零出网）驱动。四个必需指标
均已存在且**任务成功 ≠ HTTP/无异常**：
`route_accuracy` / `tool_selection_accuracy` / `tool_argument_schema_pass_rate`
（= 参数校验通过率）/ `task_completion_rate`（终态 + 非空回复 + 无未预期降级 +
业务完成口径适用；`WAITING_APPROVAL` 排除）。

### 3.2 本次新增

| 项 | 内容 |
|---|---|
| 数据集字段 | `expected_parameters`（tool → 期望参数子集）；loader 严格校验（须有 `expected_tools` + `scripted_tool_calls`，工具须在 `expected_tools` 内） |
| 新诊断指标 | `expected_parameter_match_rate`（子集匹配；故障注入 case 排除并记账；分母 0 → NOT_MEASURED） |
| 新故障注入 | `scripted_failure="agent_llm_timeout"` → `asyncio.TimeoutError`，新增 case `fault_agent_llm_timeout_001` |
| 参数用例 | `tool_param_match_001`（命中）+ `tool_param_mismatch_001`（**负控**，证明指标有区分力） |
| 真实模型 lane | artifact `provenance.real_provider`：`status=NOT_MEASURED`，与 `scripted_llm_regression` 显式分栏，禁止混合统计 |

### 3.3 真实运行结果（离线，可复现）

`make agent-eval`（114 case，真实图 + 脚本化 LLM，零出网）：

| 指标 | 值 | 分母 |
|---|---|---|
| `route_accuracy` | **NOT_MEASURED** | 0（0 条 human_confirmed，契约设计如此） |
| `tool_selection_accuracy` | 1.000 | 16（+2 excluded） |
| `tool_argument_schema_pass_rate` | 1.000 | 20 |
| `expected_parameter_match_rate` | 0.950 | 20（负控使其 < 1.0） |
| `task_completion_rate` | 0.962 | 104（+10 excluded） |
| `forbidden_tool_rate` | 0.000 | 5 |
| `hitl_trigger_accuracy` | 1.000 | 16 |
| `fallback_detection_accuracy` | 1.000 | 4（含 timeout 用例） |
| `overall_status` | **INCONCLUSIVE** | 缺 human_confirmed 标注 |

> `INCONCLUSIVE ≠ PASS`：`route_accuracy` 因缺人工确认保持 NOT_AVAILABLE，门禁
> **不**宣称达标。artifact：`artifacts/agent-eval/<ts>/report.json`（gitignored）。

`make rag-ablation`（CONSTRUCTED known-item，真实本地 Qdrant，`--limit 200`）：
`bm25_only hit@1=1.0`，负控 `hit@1=0.0`（`discriminates=True`）；
`vector_only / hybrid_*` 结构化 `BLOCKED`（无 embedding provider 凭据）。

### 3.4 Task B 未覆盖项（明确登记，不宣称已测）

| 场景 | 状态 |
|---|---|
| 跨 Agent 协作交接（`agents_used`） | **未覆盖** —— 无断言；工具类 case 被钉在 `react`（5 种模式只实际覆盖 1 种） |
| RBAC 角色拒绝 | **未覆盖** —— harness 无角色上下文 |
| 模型主动选错工具 | **不测** —— 工具计划来自数据集，V1 度量的是治理保真度 |
| timeout | **已覆盖**（LLM 超时降级）；工具执行超时未覆盖 |

---

## 4. 测试覆盖（新增）

```bash
pytest tests/unit/test_reviewed_gold_eval.py -q          # 17 passed
pytest tests/unit/test_agent_eval_parameters.py -q       # 8 passed
make agent-eval-contract                                  # 30 passed（含契约守卫）
pytest tests/unit/test_gold_label_contract.py -q          # 模板 JUDGED 行通过契约
```

新增测试钉住的关键语义：未标注不计入 precision 分母、Recall/NDCG 在判断集未闭合时
为 `null`、全 grade 0 query 被排除、无 JUDGED → NOT_MEASURABLE、参数负控使指标
下降、故障注入 case 被排除而非记 0。

---

## 5. 验收与证据等级

| 项 | 状态 | 证据 |
|---|---|---|
| reviewed-gold 消费方存在且 fail closed | **Implemented / 测试通过** | `scripts/evaluate_rag_reviewed_gold.py` + 17 单测 |
| 旧 649 benchmark / 审计证据未被改写 | **已确认** | `git diff` 不含 `tests/eval/rag_benchmark.json`；benchmark sha 不变 |
| Agent 路由/工具评测可复现 | **LEVEL_2_APPLICATION_MEASURED** | `make agent-eval`（真实图 + 脚本化 LLM） |
| RAG reviewed-gold 正式指标 | **NOT_MEASURABLE** | 0 条 JUDGED |
| 真实模型评测 | **NOT_MEASURED** | 无 provider 凭据；`provenance.real_provider` |
| 真实 ERP 写操作 | **未触碰 / NOT_VERIFIED** | 只用 `tools/hitl_staging_tools.py` 治理验证 |

---

## 6. 阻塞项

1. **人工 JUDGED 标注**（外部/人工工作量）：完成后才能解除 reviewed-gold
   `NOT_MEASURABLE` 并运行正式 4 组消融。
2. **embedding provider 凭据**：缺凭据时 `vector_only / hybrid_*` 结构化 BLOCKED。
3. **真实 provider 凭据**：Agent 真实模型 lane 保持 `NOT_MEASURED`。
4. Task B 未覆盖场景（跨 Agent / RBAC / 模型选错工具）需要 harness 扩展，属后续工作。

---

## 7. 复现命令汇总

```bash
python3 scripts/audit_doc_consistency.py                 # 文档一致性守卫
python3 scripts/rag_gold_label_provenance.py             # 旧 gold provenance（静态）
make rag-gold-validate                                   # 标注契约校验
make rag-gold-reviewed-status GOLD_LABELS=tests/eval/gold_labels/review_worklist.jsonl
make agent-eval                                          # Agent 行为评测（离线）
make agent-eval-contract                                 # Agent 契约守卫
make rag-ablation                                        # CONSTRUCTED known-item 消融 + 负控
```
