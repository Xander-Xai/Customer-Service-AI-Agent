# 简历项目描述修正稿

> 修正依据：基于项目代码库实际实现数据（config/benchmark/RAG eval）

> **Evidence freeze (2026-09-29)**：本稿不得把 local/fixture benchmark 或假设写成
> production outcome。FCR、人效、真实 QPS、provider latency、P99、Token 成本下降和
> LLM 调用下降均为 `NOT_MEASURED` / `NOT_VERIFIED`，除非链接当前带 provenance 的 artifact。

## 原版 → 修正对比

| 条目 | 原版 | 修正 | 依据 |
|------|------|------|------|
| Top-3 Recall | ~88% | **~80%** | docs/reference/rag-evaluation.md: Hit Rate@3 = 80.0%（bge-large-zh-v1.5 + query rewrite + reranker） |
| LLM 调用降低 | ~45% | **~65%** | changelog v6.1: scripts/benchmark_ab_test.py → LLM 调用降低 ~65% |
| Token 成本下降 | ~30% | **~35%** | changelog v6.1: scripts/benchmark_cost.py → Token 成本下降 ~35% |
| 流式首字 P99 | < 1.8s | **< 2s** | changelog v6.1: 流式首字 P99 < 2s |
| RAG 评测集 | 650+ query | **649 query** | tests/eval/rag_benchmark.json → total_queries: 649（保留"650+"可接受） |
| 测试用例 | 1,400+ | **1,375** (1400+可接受) | grep "def test_" → 1375 个测试函数 |
| FCR/人效 | 80%/3倍 | 保留（无代码库数据，合理假设） | 无代码佐证，面试可作预期值表述 |
| 多集合召回 | 未提及场景过滤 | **添加场景过滤** | v6.1 新增 scene 参数 + SCENE_MAPPING |

## 修正版（定稿）

药妆智多星 — 基于LangGraph的多智能体化妆品客服系统（核心开发者）

● 技术栈：Python 3.10+ / LangGraph / FastAPI / Qdrant / Redis / PostgreSQL / Docker / BGE Embedding

● 项目概述：从0到1研发面向化妆品复杂客服场景的多智能体协同系统，覆盖售前、售后、技术支持、投诉四大场景，支持文字、图片等输入与 SSE 流式输出。真实生产 FCR 与人工客服人效：**NOT_MEASURED / NOT_VERIFIED**。

● 多智能体编排与自适应路由：基于 LangGraph 构建"缓存-路由-协作-后处理"四层状态机。规则分类器优先匹配（置信度≥0.75触发捷径跳过LLM推理），不足时LLM兜底分类。按意图动态调度 Sequential、Parallel、Hierarchical、Consultation、ReAct 5种协作模式，设定 15-30s 超时降级阈值；结合运行时质量评分（<30分自动升级重试），保障复杂链路交付质量。

● 多阶段 RAG 与幻觉抑制：基于 Qdrant 搭建"场景过滤-查询改写-多集合向量召回-RRF融合-重排(bge-reranker-v2-m3)"链路。检索预取与意图分类异步并行，实现检索与分类零等待。核心评测集（649 query）Top-3 命中率约 80%，显著降低成分功效、肤质搭配等交叉查询幻觉风险。

● 高可用底座与上下文工程：引入 ServiceContainer 与统一 LLM 客户端池（熔断/指数退避）；Response Cache 使用 L1 Redis + L2 Qdrant + L3 Jaccard，另行实现 scope-safe Tool Result exact reuse、压缩、预算与可选 offload/recovery。真实 provider latency/P99：**NOT_MEASURED**。

● 质量保障与证据闭环：Trace ID 贯穿全链路，测试数量以当前 `pytest --collect-only -q` 为准；集成 Prometheus/Grafana 监控与 Docker 多环境部署。LLM 调用量与 Token 成本的真实生产变化：**NOT_MEASURED / NOT_VERIFIED**，本地 benchmark 与生产结果分开记录。
