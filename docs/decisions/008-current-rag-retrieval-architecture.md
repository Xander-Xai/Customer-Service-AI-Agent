# ADR-008: 当前 RAG 检索与 Embedding 架构（Qdrant + API Embedding + BM25 混合检索）

**日期**：2026-09-30
**状态**：已采纳（Partially supersedes [ADR-004](004-rag-embedding-selection.md)；ADR-004 的向量库决策由 v6.0 Qdrant 迁移取代，本 ADR 固化当前检索链路）
**决策者**：项目负责人

## 背景

ADR-004（2026-06-06）记录的是：ChromaDB（嵌入式）+ 本地 sentence-transformers
三级降级链（bge-small-zh-v1.5 → text2vec-base-chinese → all-MiniLM-L6-v2）+
CrossEncoder → BM25 的重排降级。此后实现发生实质变化：

1. v6.0 向量库迁移到 Qdrant（`VECTOR_DB_MODE=qdrant_only`，ChromaDB 已移除）。
2. Embedding 从本地 sentence-transformers 改为 **HTTP API 计算**
   （`rag/api_embedding.py`），应用侧计算、Qdrant 只做向量存储检索。
3. 主 Embedding 模型从 bge-small-zh-v1.5（768 维）变为
   **`BAAI/bge-large-zh-v1.5`（1024 维）**（`core/config.py::EMBEDDING_MODEL`）。
4. 检索升级为混合检索：向量通道 + BM25 词法通道（`rag/bm25_retriever.py`），
   经 RRF（k=60）融合（`rag/qdrant_knowledge_base.py::rrf_fusion`），并有
   retrieval contract（`rag/retrieval_contract.py`）与 BM25 lifecycle
   （`rag/bm25_lifecycle.py`）。
5. Reranker 从本地 CrossEncoder 降级链改为 **API Reranker**
   （`rag/reranker.py::ApiReranker`，默认模型 `BAAI/bge-reranker-v2-m3`）；
   本地 BM25 词法重排器已移除，API 不可用时回退原始顺序，检索不再依赖
   本地 sentence-transformers。

## 决策

当前 RAG 链路（当前实现）：

```text
query → rewrite/filter（rag/query_rewriter.py + 场景过滤）
      → vector（Qdrant, bge-large-zh-v1.5 @ rag/api_embedding.py）
        + BM25（rag/bm25_retriever.py + rag/bm25_lifecycle.py）
      → retrieval contract（rag/retrieval_contract.py）
      → RRF 融合（k=60）
      → ApiReranker 重排（BAAI/bge-reranker-v2-m3 @ rag/reranker.py）
      → context（rag/qdrant_knowledge_base.py）
```

关键配置（`core/config.py`）：

```text
EMBEDDING_MODEL          BAAI/bge-large-zh-v1.5
EMBEDDING_DIM            1024
EMBEDDING_BASE_URL       https://api.siliconflow.cn/v1
RERANKER_MODEL           BAAI/bge-reranker-v2-m3
HYBRID_SEARCH_ENABLED    true
HYBRID_RRF_K             60
HYBRID_VECTOR_TOP_K      8
HYBRID_BM25_TOP_K        8
VECTOR_DB_MODE           qdrant_only
QDRANT_COLLECTION_CONFIG vectors.size=1024, distance=Cosine
BM25_REBUILD_TIMEOUT     60
```

## 历史记录（ADR-004 保留，不再是当前事实）

- ChromaDB 嵌入式方案、本地三级 embedding 降级链、本地 CrossEncoder → BM25
  重排降级：历史实现，正文见 [ADR-004](004-rag-embedding-selection.md)。
- 早期 30 条查询评估集（2026-06）与其 63.3% → 80.0% 的基线对比：历史实验数据，
  见 [rag-evaluation.md](../reference/rag-evaluation.md) 的历史章节。

## 证据与未验证项

- **有证据**（当前 checkout 代码）：
  - 上述配置默认值与链路文件均存在（`core/config.py:257-284`、`rag/*.py`）。
  - BM25 lifecycle（重建/降级/重启恢复）、retrieval contract、确定性 point ID
    （`rag/point_id.py`）与迁移工具（`rag/point_id_migration.py`、
    `scripts/migrate_point_ids.py`）有对应单元/集成测试。
  - 当前评估集为 `tests/eval/rag_benchmark.json`：**649 条查询**（metadata 与
    queries 长度一致），由 `scripts/evaluate_rag.py` 评估；查询数以 metadata 为准。
- **未验证 / NOT_MEASURED**：
  - bge-large-zh-v1.5 相对 bge-small-zh-v1.5 的检索质量差异（无当前可复现
    对比 artifact）。
  - embedding/rerank API 的生产延迟与成本。
  - 649 条基准上的 Hit Rate/MRR 当前值（重跑 `scripts/evaluate_rag.py` 生成
    新 artifact 后才能引用；旧 80.0%/0.778 属于 30 条查询的历史报告）。

## 回滚方法

全部可通过环境变量回退：

```bash
HYBRID_SEARCH_ENABLED=false   # 关闭 BM25 通道，仅向量检索
RAG_QUERY_REWRITING=false     # 关闭查询改写
# 重排不可用时 ApiReranker 自动回退原始顺序；关闭 API 重排可留空 RERANKER_API_KEY
```

向量库回滚到 ChromaDB 已不受支持（`VECTOR_DB_MODE` 仅 `qdrant_only`）。

## 守卫

- `scripts/audit_doc_consistency.py`：benchmark metadata 完整性 +
  active 文档不得把 bge-small-zh-v1.5 / ChromaDB 描述为当前实现。
- `python3 scripts/project_facts.py`：输出当前 embedding/reranker/混合检索事实。
