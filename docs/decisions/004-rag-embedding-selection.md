# ADR-004: RAG 向量库与中文 Embedding 选型

**日期**：2026-06-06
**状态**：Partially Superseded by [ADR-008](008-current-rag-retrieval-architecture.md)。
向量库决策（ChromaDB → Qdrant，v6.0）与本地 sentence-transformers 三级降级链均已
被当前实现取代；本文保留 2026-06-06 的历史决策与当时的评测数字，正文不再维护。
**决策者**：项目负责人

## 背景

RAG 知识检索需要向量库和 Embedding 模型。初期使用 ChromaDB 默认的 all-MiniLM-L6-v2（英文模型），中文 Hit Rate@3 仅 63.3%。需要选择中文优化方案。

## 决策

- 向量库：ChromaDB（嵌入式，无需独立部署）
- Embedding：三级降级链 `bge-small-zh-v1.5 → text2vec-base-chinese → all-MiniLM-L6-v2`
- Reranker：二级降级 `CrossEncoder(bge-reranker-base) → BM25`

## 理由

**向量库选型（历史记录 — v6.0 已迁移至 Qdrant）**：
- ChromaDB 嵌入式部署，无外部依赖，开发/测试友好（v6.0 已替换为 Qdrant：支持水平扩展、Docker 容器化部署、gRPC 协议、更适合生产环境）
- FAISS 无元数据过滤能力，Pinecone 需要云服务
- 项目规模（5 个 collection，~170 篇文档）不需要分布式向量库

**Embedding 选型**：
- bge-small-zh-v1.5：专为中文优化，326M 参数，Hit Rate@3 从 63% → 80%
- text2vec-base-chinese：通用中文向量，作为第二备选
- all-MiniLM-L6-v2：ChromaDB 默认，英文模型，最后兜底

**Reranker 选型**：
- CrossEncoder 精度高但需要 sentence-transformers 依赖
- BM25 纯算法实现，零外部依赖，作为降级方案

## 影响

**正面**：
- Hit Rate@3 从 63.3% 提升到 80.0%（+16.7pp）
- MRR 从 0.500 提升到 0.778（+55.6%）
- 三级降级保证任何环境都能运行（无 GPU → BM25 兜底）

**负面**：
- bge-small-zh-v1.5 首次加载需要下载模型（~100MB）
- CrossEncoder 推理速度较慢，高并发场景可能成为瓶颈
- 评估数据集仅 30 条查询，覆盖率有限

## 来源文档

[rag-evaluation.md](../reference/rag-evaluation.md) · [model-comparison.md](../reference/model-comparison.md) 第 3 节
