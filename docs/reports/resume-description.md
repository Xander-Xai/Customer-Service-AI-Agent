# 简历项目描述修正稿

> **本文件是 evidence-frozen 快照，冻结时点为 2026-09-29。**
> 下方的量化条目按当时证据冻结，**不随代码演进重写**。
>
> 🚨 **要写"当前"的面试口径，请不要改这个文件**，改用：
> - 项目介绍（60s / 2min / 3min）→ [../design/interview-intro.md](../design/interview-intro.md)
> - 深度问答（含分布式 Agent Runtime R1–R11）→ [../design/interview-deep-dive.md](../design/interview-deep-dive.md)
> - 题集与必问优先级 → [../interview-questions-final.md](../interview-questions-final.md)
> - 当前事实入口 → [../reference/current-state.md](../reference/current-state.md)
>
> 注意：冻结时点之后，本项目已落地**分布式 Agent Runtime**
> （PostgreSQL checkpoint / Redis session + per-thread lock / Celery worker /
> AgentRun 真相源 / 幂等副作用 / DLQ 重放）。**本文件的简历描述不包含这些内容**，
> 也因此不能被当作当前简历口径使用。

> 修正依据：基于项目代码库实际实现数据（config/benchmark/RAG eval）

> **Evidence freeze (2026-09-29)**：本稿不得把 local/fixture benchmark 或假设写成
> production outcome。FCR、人效、真实 QPS、provider latency、P99、Token 成本下降和
> LLM 调用下降均为 `NOT_MEASURED` / `NOT_VERIFIED`，除非链接当前带 provenance 的 artifact。

## 原版 → 修正对比

| 条目 | 原版 | 修正 | 依据 |
|------|------|------|------|
| Top-3 Recall | ~88% | **~80%**（2026-06 的 30 条查询集历史报告值；当前 649 条基准值需重跑 `scripts/evaluate_rag.py` 后引用） | docs/reference/rag-evaluation.md 历史报告章节 + tests/eval/rag_benchmark.json |
| LLM 调用降低 | ~45% | **~65%（2026-06 本地 benchmark 快照，非生产结果）** | changelog v6.1: scripts/benchmark_ab_test.py → 本地 A/B benchmark 历史 |
| Token 成本下降 | ~30% | **~35%（2026-06 本地估算快照，非 provider billing）** | changelog v6.1: scripts/benchmark_cost.py → 本地成本估算历史 |
| 流式首字 P99 | < 1.8s | **< 2s（2026-06 本地环境快照，非生产）** | changelog v6.1 → 本地 benchmark 历史 |
| RAG 评测集 | 650+ query | **649 query** | tests/eval/rag_benchmark.json → total_queries: 649（以 metadata 为准） |
| 测试用例 | 1,400+ | 以 `pytest --collect-only -q` 当前输出为准（不写固定数字） | pytest --collect-only |
| FCR/人效 | 80%/3倍 | **NOT_MEASURED / NOT_VERIFIED**（无任何代码/benchmark 佐证；面试只能作为"预期指标/待验证指标"表述，不得作为已达成事实） | 无 artifact |
| 多集合召回 | 未提及场景过滤 | **添加场景过滤** | v6.1 新增 scene 参数 + SCENE_MAPPING |

## 修正版（定稿）

药妆智多星 — 基于LangGraph的多智能体化妆品客服系统（核心开发者）

● 技术栈：Python 3.10+ / LangGraph / FastAPI / Qdrant / Redis / PostgreSQL / Docker / BGE Embedding

● 项目概述：从0到1研发面向化妆品复杂客服场景的多智能体协同系统，覆盖售前、售后、技术支持、投诉四大场景，支持文字、图片等输入与 SSE 流式输出。真实生产 FCR 与人工客服人效：**NOT_MEASURED / NOT_VERIFIED**。

● 多智能体编排与自适应路由：基于 LangGraph 构建"缓存-路由-协作-后处理"四层状态机。规则分类器优先匹配（置信度≥0.75触发捷径跳过LLM推理），不足时LLM兜底分类。按意图动态调度 Sequential、Parallel、Hierarchical、Consultation、ReAct 5种协作模式，设定 15-30s 超时降级阈值；结合运行时质量评分（<30分自动升级重试），保障复杂链路交付质量。

● 多阶段 RAG 与幻觉抑制：基于 Qdrant 搭建"场景过滤-查询改写-多集合向量召回-RRF融合-重排(bge-reranker-v2-m3)"链路。检索预取与意图分类异步并行，实现检索与分类零等待。核心评测集（649 query；历史报告口径 Hit Rate@3 ≈ 80% 需以最近一次评估 artifact 为准），显著降低成分功效、肤质搭配等交叉查询幻觉风险。

● 高可用底座与上下文工程：引入 ServiceContainer 与统一 LLM 客户端池（熔断/指数退避）；Response Cache 使用 L1 Redis + L2 Qdrant + L3 Jaccard，另行实现 scope-safe Tool Result exact reuse、压缩、预算与可选 offload/recovery。真实 provider latency/P99：**NOT_MEASURED**。

● 质量保障与证据闭环：Trace ID 贯穿全链路，测试数量以当前 `pytest --collect-only -q` 为准；集成 Prometheus/Grafana 监控与 Docker 多环境部署。LLM 调用量与 Token 成本的真实生产变化：**NOT_MEASURED / NOT_VERIFIED**，本地 benchmark 与生产结果分开记录。
