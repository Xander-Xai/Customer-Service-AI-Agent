# 2026 Agent 求职项目定位与验收

## 经历定位
本仓与 Beauty-Industry-RAG-QA-System 对应**此前同一段任职经历**的不同技术主题。本项目专注 LangGraph 多 Agent、AgentRun/Celery/Postgres/Redis Runtime、工具副作用幂等与人工审批；Beauty RAG 负责知识检索和多模态。Gaobao-Advisor 属于**最近另一段任职经历**，重点是业务 Agent 与全栈交付。

这是个人作品集的分工，公开仓库仍是独立维护的参考实现，不能据此证明公司官方产品、真实业务指标或公司代码归属。简历中的个人贡献应单独核实。

## 已有能力与证据
- `core/graph_builder.py`：真实状态图及协作路由；`core/hitl/`：高风险工具执行前审批。
- `runtime/`：持久化 AgentRun、Checkpoint、所有权租约、Tool Ledger、崩溃恢复。
- `evaluation/agent_eval/`：脚本化真实图回放；它测编排治理，**不测**真实模型任务质量。

## 本轮代码修复
`evaluation/agent_eval/real_provider.py` 新增预算未知时的 fail-stop：开启总 Token 或费用上限而单次执行不返回相应 usage 时，记录 `provider_token_usage_unavailable` / `provider_cost_usage_unavailable`，不再发出下一次模型请求。**这不是硬预算**：已发出的单次调用仍可能超过限制，图内 provider usage 捕获尚未闭环。

```bash
pytest -q tests/unit/test_real_provider_missing_usage_guard.py tests/unit/test_agent_eval_real_provider_lane.py
make agent-eval
```

## 待验收，不得提前写成成果
- PR #38 / Issue #31：人工坐席工单交接；PR #40：ERP 只读超时与异常归因；PR #41：人工复核路由金标。它们是独立进行中的工作，不与本次代码重复。
- 649-query RAG 正式指标缺人工 relevance gold；真实模型 Agent 业务成功率、真实 ERP 退款及生产 QPS/P95 均尚未验证。
- 取得真实 provider 凭据与授权后，需补 usage 捕获、冻结评测集、多次任务执行、人工复核与真实业务成功标准；没有结果时明确写 `NOT_MEASURED`。
