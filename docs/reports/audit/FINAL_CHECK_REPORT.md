> **HISTORICAL AUDIT SNAPSHOT** — 2026-10-08 收尾审计的最终核对报告。
> 仅在该审计执行时点有效；当前事实以
> [reference/current-state.md](../../reference/current-state.md) 为准。

# Final Check Report

审计基线：`d7dfab1`（`git rev-parse HEAD`）
Runtime version：`6.3`（`core/config.py::VERSION`）
审计方法：全量源码审计 + 运行时校验，**不采信历史报告结论**

---

## 0. 结论

> **这不是一个"缺功能"的项目，而是一个"部分能力宣称强于可验证事实"的项目。**

架构主线（图编排、工具循环、RAG、分布式 Runtime、HITL、OTel）在代码层面
**都是真的**，证据链完整度高于多数开源 Agent 项目。
风险集中在三处，且都不是缺功能，而是**声明与事实不对齐**：

| # | 性质 | 结论 | 状态 |
|---|---|---|---|
| 1 | 功能声明超出可观测事实 | ~70 个 Prometheus 指标注册了却从未被 scrape → **DLQ 告警规则永远不可能触发** | 已开 Issue #141 |
| 2 | 文档暗示存在、代码不存在 | "Human Escalation" 只是**关键词状态标签**，无工单/队列/坐席台 | 文档已修 + Issue #144 |
| 3 | 工程原则不一致 | `HITL_ENABLED=true` + 空配置 = **静默 fail-open** | 已开 Issue #142 |
| — | 诚实但结论是"不可测" | 649-query RAG 正式指标 `NOT_VERIFIED`（数据契约缺陷） | **保持现状，这是优点** |

本次收尾**没有改任何生产代码**——只做了文档与索引工作，
把上述事实写进读者一定会看到的位置。P0 代码修复已开 issue，未实施。

---

## 1. 代码一致性

### 1.1 请求流程核对（逐跳）

用户给的参考流程 → 代码事实：

| 参考流程步骤 | 代码实现 | 一致？ |
|---|---|---|
| 用户请求 | `POST /api/chat`、`/api/chat/stream`、`WS /ws/chat`、`/api/runs` | ✅ |
| Intent 识别 | `core/graph_builder.py:87` `_classify_query_node`（LLM ∥ 规则 + RAG 预取） | ✅ |
| Agent Router | `router/` + `collaboration/orchestrator.py:149-198`（5 模式选择） | ✅ |
| Tool 调用 | `agents/base_agent.py:629-982` 工具循环 | ✅ 但**仅 ReAct 角色使用**（其余走 `_process_with_llm`） |
| Knowledge Retrieval | `agents/base_agent.py:430-506` → `rag/qdrant_knowledge_base.py` | ✅ |
| Response Generation | `agents/response_agent.py` | ✅ |
| **Human Escalation** | `agents/response_agent.py:280`（关键词）+ `agents/complaint_agent.py:88`（flag） | ❌ **只是状态标签，无转交机制** |

**唯一不一致就是最后一步。** 这也是本次审计最有价值的发现——
它只差一句文档说明就完全成立，现在已在
[docs/limitations.md](../../limitations.md) §1.3 与
[docs/agent-design.md](../../agent-design.md) §4 写清楚。

### 1.2 LangGraph 核对

| 审计项 | 结论 | 证据 |
|---|---|---|
| State 设计 | `AgentState`，`TypedDict(total=False)`，`core/state.py:6`，**单一定义点**（全仓无第二份 schema） | ✅ 与文档一致 |
| Node 设计 | 9 个节点，`core/graph_builder.py:402-414` | ✅ 与文档一致 |
| Edge 设计 | 2 条条件边 + 6 条普通边，entry=`check_cache`，finish=`final_response` | ✅ |
| Memory | `core/checkpointer.py` 双后端；`compile()` **只传 checkpointer**（无 `store`） | ✅ 与文档一致 |
| Interrupt | **全仓唯一 `interrupt()`** 在 `core/hitl/gate.py:205`；`Command(resume=...)` 唯一构造点 `runtime/executor.py:103` | ✅ 代码实现，非文档描述 |
| Streaming | **LangGraph 原生 `astream`/`astream_events` 0 使用**；SSE 走自建 callback 总线 | ⚠️ 文档已标注是自建 |

**代码 vs 文档无冲突。** 但发现两处**代码内部不一致**：
- `AgentState` 有 7 个运行时写入但未声明的键（`_rag_prefetch` 等）
- 文档/注释称「实测于 langgraph 1.2.12」（本地 venv 确实是），
  但 `requirements-lock.txt:144` 锁的是 **1.2.2**——锁定的版本没被实测过

### 1.3 工具调用核对

| 审计项 | 结论 | 证据 |
|---|---|---|
| **MCP** | **Implemented**（不是"文档描述"） | `tools/mcp_adapter.py` 1293 行；11 条 fail-closed 策略；8 类错误；CI 有真实 stdio 子进程契约测试（`tests/integration/test_mcp_contract_e2e.py` + `fake_mcp_server.py`）。默认**关闭** |
| Function Calling | **Implemented** | `tools/tool_registry.py:111-123` OpenAI schema 构造 |
| External Tools | 金蝶 ERP（Mock/Real）、Qdrant、BM25、Redis、Celery | ⚠️ `media/` 与 `alerts/` 是 **API 层集成，不是 Agent 工具** |
| 接口定义 | 全部工具穿过 `ToolRegistry.execute_raw` 统一边界 | ✅ |
| 调用流程 | 有工具调用硬上限（`max_tool_rounds=3`）防死循环 | ✅ |
| **异常处理** | ⚠️ **本报告初版此处写错，已更正（见下方勘误）** | 优先级最高的原始表述"读工具错误被吞成字符串返回；**写工具异常故意向上抛**"落错了层 |
| **超时** | `agents/` 内**无 `wait_for`**；只有 MCP 有 per-call timeout | ⚠️ Partial |
| **重试 / 并行 / 熔断** | 全部只作用于 LLM，**不作用于工具**；工具调用是顺序 `for` 而非 `gather` | ⚠️ Partial |

#### 勘误：工具异常传播（初版表述不准确）

初版把 **ledger 层**的行为误记成了**Agent 循环层**的行为。代码事实是：

| 层 | 实际行为 | 证据 |
|---|---|---|
| `ToolRegistry` 前置条件缺失 | **fail-closed**：返回显式拒绝文案，**不**静默降级为非幂等直调 | `tools/tool_registry.py:271-329`（Issue #130） |
| `execute_idempotent_operation` 失败 | `mark_failed` 后 **re-raise**；**只有成功才 `mark_succeeded`** | `runtime/side_effects.py:413-422` |
| **Agent 循环边界** | **所有**工具异常被 `except Exception` 捕获，转成 `工具暂时不可用，请稍后重试` 交给模型观察 | `agents/base_agent.py:824-826` |
| run 级重试 / DLQ 分类 | 由 **run 级**错误分类驱动（`runtime/errors.py`），与单个工具异常是否冒泡**无关** | `runtime/executor.py:587` `_handle_failure` |

**"恰好一次"的安全性质由"失败绝不 `mark_succeeded`"保证，
而不是靠异常一路冒泡到 run 层。** 修正后结论不变：
`mark_failed` 使重试安全，且不产生"失败被记成成功"。

---

## 2. Agent 能力

| 能力 | 判定 | 说明 |
|---|---|---|
| 四层状态机 | **Implemented** | 9 节点，`core/graph_builder.py:399-457` |
| 双层路由（LLM ∥ 规则 + 熔断） | **Implemented** | 高置信规则可短路 LLM |
| 5 种协作模式 | **Implemented** | `collaboration/orchestrator.py:149-198` |
| 9 个 Agent 角色 | **Implemented** | 7 领域 + ReAct + Response |
| Tool Calling 循环 | **Implemented** | 硬上限 3 轮 |
| RAG 混合检索 | **Implemented** | Qdrant + BM25 + RRF + rerank |
| Tool Result Context Engineering | **Implemented** | 5 个模块，**全确定性无 LLM** |
| 工具副作用幂等 | **Implemented** | 原子 claim + lease + owner fencing |
| 响应质量评估 | **Implemented（启发式）** | **不是 LLM 判官** |
| HITL 审批 | **Implemented** | 默认关闭 |
| **Human Escalation** | **Partial** | 仅状态标签 |
| LangGraph 原生流式 | **Designed / 未使用** | 自建 callback 总线 |
| LangGraph `store` 长期记忆 | **Designed** | 未使用 |
| **Agent 行为回归评测** | **未实现** | `evaluation/agent_eval/` 只剩 gitignore 的 `.pyc`，源码从未提交 |

---

## 3. 企业能力

| 能力 | 判定 | 证据等级 |
|---|---|---|
| Async workflow（Celery + 状态机 + 崩溃恢复） | **Implemented** | **Level 2**：真实 PG + Redis + 多进程，`artifacts/runtime/chaos-20261006T081808Z.json` `result: PASS` |
| HITL | **Implemented**（1 处 fail-open） | Level 2：确定性 staging 工具；真实 ERP 写操作 `NOT_VERIFIED` |
| Evaluation（RAG） | 流水线 **Implemented**；**正式指标 NOT_VERIFIED 且当前不可测** | 数据契约缺陷 |
| Evaluation（Agent 行为） | **未实现** | — |
| Evaluation（响应质量） | **Implemented（启发式）** | — |
| Tracing（OTel） | **Implemented，本地已验证** | `artifacts/observability/otel-collector-20261003T040014Z/report.json`，5 个 span 到达真实 Collector |
| **Trace 后端持久化** | **Designed** | collector 仅 `debug` exporter |
| Metrics 注册（~70 个） | **Implemented** | `core/monitoring.py:284-408` |
| **Metrics HTTP 暴露** | **Partial（断链）** | 无 `generate_latest` |
| **DLQ 告警** | **Partial（永不触发）** | Issue #141 |
| Grafana | **Partial** | 6 个面板永远为空 |
| Logging | **Implemented** | 两层密钥脱敏 + ASGI 泄漏封堵 |
| MCP | **Implemented**，默认关闭 | 真实第三方 server `NOT_VERIFIED` |
| 卡死 run 兜底 | **Partial** | 手动/cron，无 Beat |

---

## 4. 文档状态

### 4.1 本次新增

| 文件 | 生命周期 | 内容 |
|---|---|---|
| [docs/architecture.md](../../architecture.md) | 🟢 CURRENT | 架构入口：为什么两层路径、四层状态机、四个 ID、状态归属、可观测性、刻意不做的事 |
| [docs/agent-design.md](../../agent-design.md) | 🟢 CURRENT | 设计理由：为什么 LangGraph 不用裸 Chain、为什么 Agent 不能只是 Chain、**为什么需要 Human fallback** |
| [docs/evaluation.md](../../evaluation.md) | 🟢 CURRENT | 评测入口：四类评测、证据分级、什么测过什么没测过、怎么自己复核 |
| [docs/deployment.md](../../deployment.md) | 🟢 CURRENT | 部署入口：拓扑、fail-fast 校验、三条关键 TTL、上线步骤、已知观测缺口 |
| [docs/limitations.md](../../limitations.md) | 🟢 CURRENT | **不宣称清单**：最重要的三条边界 + 逐项状态 + 代码债务 + 未做方向 |
| [PROJECT_FINALIZATION_PLAN.md](PROJECT_FINALIZATION_PLAN.md) | 🟡 HISTORICAL | P0/P1/P2 整改计划（本次产出） |

### 4.2 本次重写

- **README.md** — 新增 `Problem` / `Solution` 两节（原先缺失），
  重构为 `Problem → Solution → Architecture → Engineering Highlights → Limitations → Quick Start →
  Test & Evidence → Tech Stack → Project Structure → Business Scenarios → Docs`，
  同步更新 mermaid 架构图（补 `/api/runs` 与 MCP 节点），
  **并把三条功能断链写进 Limitations 置顶**。
  **未包含工程问答口径。**

### 4.3 索引更新

`docs/README.md` 快速定位表、真相层级表、`design/` 前置阅读说明。

### 4.4 放置位置的工程决策（重要）

两份产出放在 `docs/reports/audit/` 而**不是仓库根**。
理由：仓库已有守卫
`scripts/audit_doc_consistency.py::check_no_root_level_audit_snapshots`（Rule Z），
它只扫描 `README.md` / `CLAUDE.md` 两个根级 markdown；
根目录的审计快照会**绕过全部 35 项守卫**，
却仍与 `docs/reference/current-state.md` 竞争 Current Truth。
放到 `docs/reports/audit/` 并加 `HISTORICAL AUDIT SNAPSHOT` banner 后，
被 `is_historical_dir` + banner 双重识别为历史快照。
**保留了用户要求的文件名，只调整了目录。**

### 4.5 守卫结果

| 门禁 | 结果 |
|---|---|
| `make audit-docs`（35 项） | ✅ **OK — checked 48 active documents**（审计前为 43，新增 5 个 CURRENT 文档） |
| `make openapi-check` | ✅ **OK — 62 paths 与 `app.openapi()` 一致** |
| `make lint`（Ruff） | 见 §6 |

审计过程中本守卫**拦下了我自己新写的两处违规**（已修正），
这本身说明该守卫是有效的：
- `docs/evaluation.md` 的 `ChromaDB` 未标注为历史 → 已加"历史"框架
- `docs/evaluation.md` 的"零延迟"是无实测的延迟绝对值 → 已改为"无模型调用"

---

## 5. 未完成事项

### 5.1 已开 issue（未实施）

| Issue | 主题 | P0 计划项 |
|---|---|---|
| [#141](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/141) | `agent_run_dead_letter_total` 从未被 scrape，DLQ 告警不可能触发 | P0-1 |
| [#142](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/142) | `HITL_ENABLED=true` + 空配置 = 静默 fail-open | P0-3 |
| [#143](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/143) | 死代码、协议漂移、坏引用、依赖声明不一致 | P0-4 / P0-5 / P1-9 / P1-12 |
| [#144](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/144) | 「转人工」仅为状态标签；需决策是否实现坐席工作台 | P0-2（方案 B）/ P2-3 |

### 5.2 由 Issue #7 覆盖（未变）

`#7 Production evidence backlog` 已维护 E1–E8 全部证据缺口
（relevance-judged gold、正式全量跑、真实 provider 认证、真实 ERP staging、
生产 QPS/P95/P99、可查询 trace 后端、持续多副本验证、backpressure/autoscaling）。
**本次不重复登记。**

### 5.3 未完成（本次明确不做）

| 项 | 理由 |
|---|---|
| 补齐 649-query RAG 指标数值 | 需人工相关度标注（E1）。**本轮不测更诚实**；契约与校验器已把"不可测"变成可审计状态 |
| 实现转人工工作台 | 新功能，需产品定义（#144） |
| Kubernetes manifest | 无真实集群与多副本验证的 manifest 是**装饰性声明** |
| 写生产 SLA / P99 数字 | 无压测与真实流量，编数字即造假 |
| Agent 行为评测 harness | 需确定性 mock graph + 用例集（P2-1） |
| LLM-as-judge | 需固定 judge 模型 + 一致性验证（P2-2） |
| 工具超时/并行/熔断/重试 | P1-1~P1-4，需先定义"读可重试、写不可重试"边界 |

### 5.4 本次明确**没有**做

| 没做 | 理由 |
|---|---|
| 修改任何生产代码 | 本次范围是审计 + 文档；P0 代码修复以 issue 形式提出，**不静默改动行为** |
| 新建治理体系 | 全部挂在既有 35 项机器守卫上 |
| 重复登记证据缺口 | 已在 Issue #7 |
| 引入 LangSmith / Langfuse 账号依赖 | 会让"零外部依赖可离线跑"这一优势失效 |
| 任何"生产集群已验证"表述 | 违反 Level 3 边界，且会被 production-claim 守卫拦下 |

---

## 6. 验证记录

本次收尾实际执行的校验：

| 命令 | 结果 |
|---|---|
| `pytest --collect-only -q` | `3588 tests collected`（测试规模以命令输出为准，未写进文档） |
| `python3 scripts/audit_doc_consistency.py` | `OK: checked 48 active documents` — 35 项检查全过 |
| `python3 scripts/generate_openapi.py --check` | `OK: docs/openapi.json surface matches app.openapi() (62 paths)` |
| `python3 scripts/project_facts.py` | `rag_formal_metrics_status: NOT_VERIFIED`（与文档一致） |

已复查的证据 artifact：

| artifact | 结论 |
|---|---|
| `artifacts/distributed-runtime/20261006T081829Z/report.json` | `distributed-runtime-evidence/v2`，`overall_status: PASS`，带 `tested_code_sha`；`tool_idempotency.side_effect_calls: 1` |
| `artifacts/runtime/chaos-20261006T081808Z.json` | `result: PASS`，8 步含 `sigkill_worker_group` / `restart_worker` / 恢复后 `attempt: 2` / `ledger_hits: 1` |
| `artifacts/observability/otel-collector-20261003T040014Z/report.json` | `otel-collector-evidence/v1`，`status: VERIFIED_LOCAL`，`expected_spans == observed_spans`（5 个） |
| `artifacts/evaluation/rag-gold-provenance/20261006T223528Z/report.json` | `FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD` |

> ⚠️ 注意：分布式 Runtime 与 OTel 的 artifact 生成于 2026-10-03 / 2026-10-06，
> 对应的 `tested_code_sha` / `git_sha` 早于当前 HEAD。
> 它们证明**当时的代码**在真实基础设施上通过，**不**证明当前 HEAD 通过。
> 复核方式：`make runtime-verify` / `make otel-collector-smoke`（需真实 PG / Redis / Docker）。

---

## 7. 目标达成度

> 目标：成为**可信的企业 Agent 项目展示仓库**。

| 维度 | 审计前 | 审计后 | 判定 |
|---|---|---|---|
| 读者能在 5 分钟内知道项目解决什么问题 | ❌ README 直接进技术细节 | ✅ `Problem` / `Solution` 独立成节 | **达成** |
| 读者能区分"实现了"和"只是设计了" | ⚠️ 部分散落在各设计文档 | ✅ 统一在 `docs/limitations.md`，含三处功能断链的代码证据 | **达成** |
| "为什么这样设计"可读 | ⚠️ 分散 | ✅ `architecture.md` + `agent-design.md` | **达成** |
| 宣称与代码不矛盾 | ❌ DLQ 告警、转人工、HITL fail-open 三处 | ⚠️ 已在最显眼处标注，但**代码未修** | **部分达成**（需 #141/#142/#144） |
| 无虚假声明 | ✅ 原本就做得好 | ✅ 保持并强化 | **达成** |
| 文档一致性有机器守卫 | ✅ 35 项 | ✅ 48 个文档全过 | **达成** |

**剩余风险**：三条功能断链目前只在文档层面被诚实标注。
**代码本身仍有缺陷**——在 #141/#142 修复前，仓库不能声称
"DLQ 有告警闭环"或"审批治理处处 fail-closed"。

---

## 8. 建议的下一步

```
立即（可信度闭环）
  #141  Prometheus 标准端点 + 双 job 抓取 + 告警可执行性契约测试
  #142  HITL fail-closed 启动校验
  #143  死代码 / 漂移 / 坏引用 / 依赖声明
  #144  决策：escalation 是观测标签还是转交触发

下一批（工程纵深）
  P1-1~P1-4  工具超时 / 并行 / 熔断 / 重试（先定义读写重试边界）
  P1-5       reconcile_stuck_runs 接入调度
  P1-6/P1-7  Grafana 面板对齐 + 告警契约测试
  P1-8       持久化 trace 后端（Jaeger / Tempo）
  P1-12      补 docs/design/mcp-tool-adapter.md

独立排期（路线图）
  #7 E1     relevance-judged gold（人工标注 + Kappa）
  P2-1/P2-2  Agent 行为评测 harness + LLM-as-judge
  P2-7/P2-8  Kubernetes + 压测与容量基线（需先有 SLO）
```

---

## 9. 审阅者快速验证清单

想自己核对本报告的每一条结论：

```bash
# 1) 文档一致性（35 项守卫）
make audit-docs

# 2) 事实（不硬编码数字）
make facts

# 3) P0-1：确认 prometheus 注册表确实没被输出
grep -rn "generate_latest\|make_asgi_app" --include=*.py . | grep -v .venv
# 期望：0 命中
sed -n '332,405p' api/routes/monitoring.py   # 手工拼接的 10 行
grep -n "agent_run_dead_letter_total" monitoring/alert_rules.yml

# 4) P0-2：确认没有转人工工作台
grep -rni "handoff\|ticket" --include=*.py agents/ api/ core/ runtime/ erp/
# 期望：0 命中
sed -n '276,300p' agents/response_agent.py   # 仅关键词匹配

# 5) P0-3：确认 HITL 空配置 fail-open
grep -n "HITL_HIGH_RISK_TOOLS\|HITL_HIGH_AMOUNT_THRESHOLD" core/config.py
sed -n '43,72p' core/hitl/risk.py            # 优先级链落空 → LOW

# 6) 证据边界
cat artifacts/distributed-runtime/20261006T081829Z/report.json
cat artifacts/evaluation/rag-gold-provenance/20261006T223528Z/report.json
```

---

## 10. 附录：同日第二轮（图示补全与两处修正）

第一轮之后补了**对外评审必需、但仓库确实缺失的图示**，并修正两处问题。

### 10.1 新增图示（Phase 4 必做项）

| 图 | 位置 | 为什么必须有 |
|---|---|---|
| **一次请求的时序图** | `README.md` Architecture 节 | 评审者最常问"一次对话到底走了哪些组件"。原先只有静态拓扑，没有时序 |
| **Agent 工具循环流程图** | `docs/agent-design.md` §2.0 | **原先全仓没有任何 Agent 流程图**。含工具循环、缓存前置、HIGH 风险分流、幂等 claim、上下文工程五段 |
| **异步执行与崩溃恢复图** | `docs/architecture.md` §2.1 | 本项目最大差异化点（checkpoint 续跑 + lease 接管）原先只有散文 |

三张图都用**真实 mermaid 11 解析器**校验过（`mermaid.parse`），不是"看起来对"。

### 10.2 修正一：README 原有架构图在 mermaid 下解析失败

`README.md` 既有架构图用**单行多节点声明**（`PA[ProductAgent] TA[TechAgent] ...`），
这在 mermaid 里是非法语法：

```
Parse error on line 38: Expecting 'SEMI', 'NEWLINE', ... got 'NODE_STRING'
```

该图自加入起就无法保证在 GitHub 正常渲染。已改为每节点独立一行，4 张图现已全部
`mermaid.parse` 通过。**这条是校验器抓出来的，不是肉眼 review 出来的。**

### 10.3 修正二：工具异常传播的表述落错了层（见 §1.3 勘误）

初版称"写工具异常故意向上抛"——实际那是 **ledger 层**的行为；
**Agent 循环层会把所有工具异常转成一句提示交给模型**。
安全性由"失败绝不 `mark_succeeded`"保证，不是靠异常一路冒泡。

> **方法论记录**：本次两处修正都不是靠重读文档发现的，
> 一处靠 mermaid 解析器，一处靠逐层读 `agents/base_agent.py` 与
> `runtime/side_effects.py` 的异常路径。
> **文档守卫（35 项）无法发现这类问题**——它检查链接、配置与声明，
> 不检查"关于代码行为的叙述是否正确"。这类风险只能靠源码复核。
