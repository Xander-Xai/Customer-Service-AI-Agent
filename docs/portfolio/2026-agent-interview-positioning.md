# 2026 Agent 求职定位：业务编排与可靠运行时（早期任职阶段）

> **文档性质**：求职展示与工程验收映射，不是雇主官方项目说明、生产部署证明或个人贡献的自动归属声明。公开仓库的后续维护能力不得直接倒填为历史任职成果；面试时按实际参与时间、职责、可验证证据叙述。以最新默认分支及其 CI 结果为准，不以本文日期替代当前状态。

## 项目定位

- **与 Beauty-Industry-RAG-QA-System 属于同一个较早任职阶段**（任职归属来自仓库所有者的履历陈述；不代表两个仓库存在代码继承关系）。
- **唯一主线**：企业智能客服 Agent 的编排、受治理工具执行、Checkpoint/Run/Worker 可靠性、人工审批与人工接管。
- **不承担**：多模态 RAG 的独立检索算法对比（交由 Beauty RAG）；新一代只读自主 Harness 的业务对照实验（交由 Gaobao）。
- 不把独立开源参考实现表述为雇主的可公开生产镜像，不展示未授权客户/ERP/日志数据。

## 当前可复核依据

| 能力 | 仓库入口 | 证据边界 |
|---|---|---|
| LangGraph 编排与质量路由 | `core/graph_builder.py`, `collaboration/` | 可核查实现与测试，不等于生产任务完成率 |
| AgentRun、租约、Celery、DLQ | `runtime/run_service.py`, `runtime/executor.py`, `tests/integration/runtime/` | 集成环境结果与生产长期运行分开 |
| 工具治理 / 幂等 / HITL | `tools/tool_registry.py`, `runtime/side_effects.py`, `core/hitl/` | 不能承诺外部 ERP exactly-once |
| MCP 只读适配 | `tools/mcp_adapter.py`, `tests/integration/test_mcp_contract_e2e.py` | 真实第三方服务器连通性待核验 |
| 人工接管 | `core/handoff_service.py`, `docs/limitations.md` | 工单后端与坐席端到端值守是两级不同的证据 |
| 正式 Eval | `evaluation/agent_eval/`, `docs/evaluation.md` | 未经人工确认的 gold 不得算实际业务准确率 |

## 缺口责任与执行顺序（只在本仓库处理）

1. **P0 — 标注及业务级 Eval（Issue #33、PR #41）**：完成盲审清单、独立人工纠正/批准的 route/tool/outcome gold，提交冻结数据集版本、分母、Trace、失败归因和重复实验。未完成时保持 `NOT_MEASURED`。CI 全绿只是脚本/数据契约完成，不是人工审核完成。
2. **P0 — 客服接管闭环（Issue #31、PR #43）**：客户申请 → 持久化 ticket → 坐席认证/认领 → 回复可见 → 关闭/机器人恢复，验证权限、幂等、竞争认领、重放及超时；明确区分 HITL *审批* 和 human handoff *接管*。
3. **P0 — 真实 ERP staging 证据（Issue #33）**：只有合法授权及受限环境才执行退款/改单；具名记录外部请求幂等键、未知结果/超时、回查和失败修复。无环境时不得把 mock/staging 工具称为真实 ERP。
4. **P1 — 工具可靠性（PR #40 为相关现有切片）**：只读工具可有界重试；写操作禁止自动盲重试。工具超时、失败回执、分层熔断/并发执行需单独协议与故障注入验收。
5. **P1 — 可靠性证据**：真实 PG/Redis/Celery 重启、SIGKILL、重复投递、审批恢复、DLQ；提交测试 SHA、环境和报告。外部副作用端到端 exactly-once 仍不宣称。
6. **P1 — 可观测**：持久化 Trace 查询和 Alertmanager 实际投递；仅有 /metrics 和规则 firing 不能冒充人工收到告警。

## 面试最小验收包

- 一张 LangGraph + Run/Worker/HITL 时序图，解释同步 SSE 快路径为何拒绝高风险副作用。
- 两条可回放 Trace：成功业务查询、审批后写操作（使用合成/授权 staging 数据，明确来源）。
- 三个故障注入：worker 被杀后恢复；重复 delivery 不重复写；失去 lease 的旧 worker 不覆盖新 owner 的状态。
- 一份 *人工确认* 的 route/tool/outcome 评测报告，给出分母、错误类型、质量/成本/延迟；没有数据时明确 `NOT_MEASURED`。
- 个人口述必须能逐个解释：CAS 与租约、side-effect ledger、HITL、重试策略、为什么不承诺 exactly-once。

## 跨项目分工

- [Beauty RAG](https://github.com/Xander-Xai/Beauty-Industry-RAG-QA-System)：负责检索/Rerank/引用质量、数据权限与知识版本。
- [Gaobao](https://github.com/Xander-Xai/Gaobao-Advisor)：负责后续阶段的自主 Planner/Harness、业务 SQL 真值、模型 A/B Eval 和全栈交付。
- 任何“任职期间已实现 / 自己负责 / 生产指标”都必须有额外履历事实或允许披露的证据。当前仓库代码和测试不能单独证明这些陈述。
