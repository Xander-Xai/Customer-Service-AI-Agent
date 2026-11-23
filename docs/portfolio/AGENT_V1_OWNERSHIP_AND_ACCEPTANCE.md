# Agent 工程标准 V1.0 — 企业客服项目责任边界与验收台账

> 状态：执行台账（不是测试报告）。基线：`main` @ `c623c964b2337018bbe3385a9a3664c75c2c7ff7`（2026-11-24 核查）。后续只以指定提交的源码、CI 和机器可读 artifact 升级状态。

## 求职项目定位与时间边界

- **唯一主责**：企业客服的多 Agent 编排、可靠性 Runtime、工具治理、审批/恢复、跨进程状态和可观测性。
- 与 `Beauty-Industry-RAG-QA-System` 属于**同一历史任职阶段的两个不同技术侧重点**；不要把行业 RAG 的完整检索算法重新当作本仓新增 Agent 功能。
- `Gaobao-Advisor` 属于**另一段更近期任职经历**，负责全栈业务与 Harness/Eval 实验；不能把三个仓库的后续改造成单一企业的历史上线记录。
- 本仓是持续独立维护的**公开工程参考实现**，不是任职企业官方系统，不代表历史工作期间全部代码和指标。任何简历中的时间、职责、成绩应与可核验的本人实际工作记录匹配。

## V1.0 模块责任

| 模块 | 本仓责任 | 已有入口 | 当前证据边界 |
| --- | --- | --- | --- |
| M01/M02 | FastAPI / SSE / Python async / Function Calling | `api/` `llm/client.py` | 已实现；个人脱稿讲解另验收 |
| M03/M04 | 模型提议、后端工具校验、工具循环与停止条件 | `agents/base_agent.py` `tools/tool_registry.py` | 有实现；独立无框架 8 场景的统一验收未完成 |
| M05/M06/M12 | LangGraph 四层状态机、协作模式、Interrupt | `core/graph_builder.py` `collaboration/modes.py` | 多模式存在；Supervisor 分解、子 Agent 隔离/共享冲突尚未有完整专项证据 |
| M09/M10 | AgentRun/Queue/Lease/Checkpoint、HITL、幂等、重放 | `runtime/` `core/hitl/` `tests/integration/runtime/` | 真实 PostgreSQL + Redis CI/故障注入；不等于真实 ERP exactly-once |
| M11（部分） | MCP 只读 Client、内部 ToolRegistry 授权边界 | `tools/mcp_adapter.py` `tests/integration/test_mcp_contract_e2e.py` | 本地 fake server；业务 MCP Server/A2A/不可信代码强隔离不归入现有已验证能力 |
| M14/M15 | OTel、Metrics、RBAC、审批与脱敏日志 | `core/telemetry.py` `core/monitoring.py` `auth/` | 本地链路验证不等于真实生产观测/安全验收 |
| M13（集成） | 面向运行时和客服任务的四层 Agent Eval | `evaluation/agent_eval/` `docs/reference/agent-evaluation.md` | 当前真实图由 scripted LLM 驱动；115 条路由候选缺人工确认，模型成功率未验证 |

**非主责**：行业多模态复杂版面解析、完整检索消融（归行业 RAG）；高考业务 SQL Oracle、独立 Harness/Skills/Prompt 优化实验（归 Gaobao）。不要为覆盖矩阵复制三套实现。

## 第一批补全任务（按先决条件顺序）

| 编号 | 执行项 | 通过标准 | 当前状态 / 关联变更 |
| --- | --- | --- | --- |
| CSA-P0-01 | 原生无框架 Agent Loop × 8 场景：无工具、单工具、依赖工具、Schema 纠错、读写超时、循环预算、取消、HITL | 一组可复跑的独立测试，模型选工具与服务端授权分开断言，写操作状态未知时不得报成功 | **REFERENCE_IMPLEMENTED / CI_PENDING**：`core/standalone_agent_loop.py` + `tests/unit/test_standalone_agent_loop.py` 覆盖 8 条基础场景 + 2 条写操作状态核验回归；`tests/unit/test_standalone_loop_staging_integration.py` 另对接本仓真实 HITL staging 工具验证响应丢失和一次副作用。仅用可信外部查询确认已执行；未知写操作禁止自动重投；**非 LangGraph 主链路/真实 ERP E2E** |
| CSA-P0-02 | 服务端直接 ERP 只读工具超时/失败治理，协作模式传播失败状态 | 真实被测调用点、五种协作路径回归、异常不泄密 | **PR #40 Draft**；未合并，禁止写成完成 |
| CSA-P0-03 | 真实图路由金标审查及真实 Provider 任务评测 | 人工确认标注；混合/故障场景可复测；分母/跳过/成本来源完整；模型调用经授权 | **PR #41、#42 Open**，尚无正式模型任务完成率 |
| CSA-P0-04 | 模拟退款状态闭环：审批、ledger、外部状态查询/对账、取消后的在途核实 | kill -9/重复投递/取消/未知写结果/并发时最多一次可确认有效副作用；提供 DB & trace 证据 | **PARTIAL**；现仅 staging 工具，本仓 ledger 不保证下游 exactly-once |
| CSA-P1-05 | 坐席转人工接管、工单前端与 SLA 事件 | 真实 E2E 客户→坐席→消息→关闭/重开，服务端 RBAC 与操作日志 | **PR #43 Draft**；后端已存在，完整运营闭环未验收 |
| CSA-P1-06 | 追踪/压测/灰度回滚的同一提交交付包 | 同一测试环境 P95/P99、Cost per Task、失败轨迹、通知送达、灰度回滚脚本证据 | **PARTIAL**；本地 Metrics/OTel 已验，不宣称生产 |

## 当前可执行验证（仅证明各自覆盖的事实）

```bash
pytest -q tests/unit/test_standalone_agent_loop.py tests/unit/test_standalone_loop_staging_integration.py --no-cov -o addopts=''
make test
make runtime-e2e
make runtime-chaos
make runtime-delivery
make runtime-verify
make test-mcp
make agent-eval
make metrics-exposure-verify
```

核验时务必保留精确 Commit SHA、数据集摘要、环境、分母、失败数、artifact；上述命令不是一个「全部通过即生产就绪」的超级门禁。真实 Provider 与真实 ERP 验证分别需要明确的授权、合规测试资产与成本预算。

## 发布与简历约束

1. **代码状态**：`DESIGNED / IMPLEMENTED / VERIFIED_LOCAL / VERIFIED_CI / VERIFIED_REAL_BUSINESS` 分层；不把 Draft PR 或历史绿色 CI 计入主分支。
2. **不宣称**：生产 QPS/P95、真实退款正确率、正式路由准确率、正式 649-query RAG 准确率、端到端 exactly-once。
3. **学习 KPI**：独立脱稿解释 Agent Loop、幂等身份、CAS、lease、checkpoint、取消与故障窗口；现场复现指定 Bad Case。
4. **生产 KPI**：必须通过真实外部业务 UAT、明确 SLO、安全审批、成本和运维告警值守，公开仓库尚无该证据。

参考：`docs/reference/current-state.md`、`docs/limitations.md`、`docs/production-readiness.md`。
