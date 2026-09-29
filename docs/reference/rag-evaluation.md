# RAG 检索质量评估方案

> **当前口径（2026-09-30 收敛）**：向量数据库为 Qdrant（`VECTOR_DB_MODE=qdrant_only`，
> ChromaDB 已移除）；Embedding 为 `BAAI/bge-large-zh-v1.5`（1024 维，HTTP API 计算，
> `rag/api_embedding.py`）；检索链路为 rewrite/filter → vector + BM25 →
> retrieval contract → RRF(k=60) → `bge-reranker-v2-m3` 重排（ADR-008）。
> 当前基准查询数由 `tests/eval/rag_benchmark.json` 的 metadata 决定
> （**649 条**，2026-06-24 创建；以 `python3 scripts/project_facts.py` 的输出为准）。
> 文中所有具体指标数字均标注了生成日期与数据集，属于对应日期的报告快照；
> 当前值必须用 `python3 scripts/evaluate_rag.py` 重新生成 artifact 后引用。
>
> 本文档解决"RAG 无评估指标"缺口，提供评估方法论、执行脚本和面试话术。

---

## 1. 为什么面试会被追问 RAG？

RAG（Retrieval-Augmented Generation）是 AI 应用开发岗位的**核心考点**。面试官常问：

- "你怎么评估 RAG 的检索质量？"
- "怎么证明 RAG 比不用 RAG 效果好？"
- "RAG 检索不到相关内容时怎么办？"

如果答不上来，说明对 RAG 的理解停留在"调 API"层面，缺乏工程化思维。

---

## 2. 评估指标体系

### 2.1 核心指标

| 指标 | 含义 | 计算方式 | 面试怎么说 |
|------|------|---------|-----------|
| **Hit Rate@K** | Top-K 结果中是否包含正确答案 | 命中数 / 总查询数 | "X% 的问题能在前 K 条结果中找到答案" |
| **Precision@K** | Top-K 结果中有多少是相关的 | 相关文档数 / K | "平均每次检索有 Y 条是真正相关的" |
| **Recall@K** | 正确答案是否被检索到 | 命中数 / 总相关文档数 | "Z% 的正确答案不会被遗漏" |
| **MRR** | 第一个正确结果排在第几位 | 平均(1/排名) | "用户通常在第 N 条结果就能看到答案" |
| **平均距离** | 检索结果与查询的向量距离 | 距离越小越相关 | "语义相似度在合理范围内" |

### 2.2 历史基线数据（ChromaDB + all-MiniLM-L6-v2，仅用于对比）

本项目有 3 个知识集合，建议按类别分别评估：

| Collection | 历史文档数（2026-06 报告） | 评估重点 |
|-----------|--------|---------|
| product_knowledge | 35 | 成分查询、产品匹配的精确度 |
| faq | 30 | 常见问题的快速命中率 |
| tech_support | 20 | 技术问题的专业性匹配 |

> 当前 collection 文档数以 `python3 scripts/evaluate_rag.py` / `/api/knowledge/stats`
> 的当前输出为准；上表为 2026-06 历史报告快照。

---

## 3. 执行评估

### 3.1 快速执行

```bash
# 在项目根目录执行
python3 scripts/evaluate_rag.py
```

### 3.2 评估数据集

当前评估数据集为 `tests/eval/rag_benchmark.json`：**649 条查询**
（数字由 metadata `total_queries` 决定，2026-06-24 生成；不要复制为永久数字），
覆盖 5 类（成分知识/产品推荐/使用指导/售后问题/投诉处理）与 3 级难度
（easy/medium/hard）。历史脚本曾内置 30 条查询套件
（`docs/reference/rag-evaluation-report.json`），属于旧口径，仅作历史对比。

| 维度（当前 649 条基准） | 数量 |
|------|--------|
| 成分知识 | 218 |
| 产品推荐 | 150 |
| 使用指导 | 100 |
| 售后问题 | 101 |
| 投诉处理 | 80 |
| difficulty: easy / medium / hard | 219 / 225 / 205 |

### 3.3 结果解读

评估脚本会输出：

1. **逐条查询结果**：每条查询的命中状态（✅/❌）、命中排名、P@K 值
2. **汇总统计表**：Hit Rate、Precision、Recall、MRR、平均距离
3. **分类统计**：按查询类别的命中率对比
4. **JSON 报告**：保存到 `docs/rag-evaluation-report.json`

### 3.4 评估结果

> 当前值必须由 `python3 scripts/evaluate_rag.py`（649 条基准）重新生成。
> 以下两个小节是**历史报告快照**，数据集与配置与当前不同，只能作为对比叙事。

#### 历史基线数据（2026-06-06，改进前）

使用默认 embedding（all-MiniLM-L6-v2，英文模型，ChromaDB 历史数据，30 条查询集）：

| 指标 | 值 | 说明 |
|------|---------|------|
| Hit Rate@3 | **63.3%** | 受英文 embedding 模型限制，低于 70% 目标 |
| MRR | **0.500** | 首条结果命中率中等 |
| 平均距离 | **0.7226** | ChromaDB L2 距离（历史数据） |

> 完整报告：`docs/archive/rag-evaluation-report.json`

#### 历史改进数据（2026-06-10，中文 embedding + query rewriting + reranker，30 条查询集）

| 指标 | 基线（英文 embedding） | 改进后 | 提升 |
|------|----------------------|--------|------|
| **Hit Rate@3** | 63.3% | **80.0%** | +16.7pp |
| **MRR** | 0.500 | **0.778** | +55.6% |

> 报告快照：[docs/reference/rag-evaluation-report.json](rag-evaluation-report.json)
> （自动生成，2026-06，30 条查询口径）

> **面试话术**：63.3% 是改进前的历史基线（英文 embedding，30 条查询集）。
> 当时的改进（中文 embedding + query rewriting + reranker）把 Hit Rate@3 提到
> 80.0%、MRR 提到 0.778（历史报告值，30 条查询口径）。当前实现已升级为
> Qdrant + bge-large-zh-v1.5 + BM25 混合检索 + 649 条基准评估集，
> 当前指标必须用评估脚本重新生成后引用。

---

## 4. 改进方向（面试加分项）

当面试官问"怎么改进 RAG"时，展示你对进阶技术的理解：

### 4.1 短期改进（成本低）

| 改进项 | 方案 | 预期提升 |
|--------|------|---------|
| **中文 Embedding 模型** | 已完成（历史改进记录：all-MiniLM-L6-v2 → bge-large-zh-v1.5，2026-06） | 已落地 |
| **Query 改写** | 用 LLM 将口语化查询改写为标准检索语句 | 长查询命中率提升 |
| **结果重排序（Rerank）** | 检索 Top-10 后用 Cross-Encoder 重排序取 Top-3 | Precision@3 提升 |

### 4.2 中期改进（效果显著）

| 改进项 | 方案 | 预期提升 |
|--------|------|---------|
| **Hybrid Search** | 向量检索 + BM25 关键词检索，加权融合 | 覆盖语义和精确匹配 |
| **文档分块（Chunking）** | 长文档按段落/语义切分，提高检索粒度 | 长文档命中率提升 |
| **Metadata 过滤** | 检索前先用 intent 过滤 collection，减少干扰 | Precision 提升 |

### 4.3 已有的降级策略

本项目已实现：
- **多集合并行检索**：[rag/qdrant_knowledge_base.py](../../rag/qdrant_knowledge_base.py) `query_multiple` 多 collection 并行 + 向量/BM25 双通道
- **检索契约与降级路径**：[rag/retrieval_contract.py](../../rag/retrieval_contract.py) 定义各阶段结果结构
- **距离排序去重**：[rag/qdrant_knowledge_base.py](../../rag/qdrant_knowledge_base.py) 距离升序 + 内容去重
- **RAG 失败降级**：[agents/base_agent.py](../../agents/base_agent.py) Agent 在 RAG 无结果时仍有 LLM 直接回答能力

---

## 5. 面试 Q&A 准备

### Q: "你怎么评估 RAG 检索质量？"

> "评估集是 `tests/eval/rag_benchmark.json`，当前为 649 条测试查询（数量以
> metadata 为准，2026-06-24 生成），覆盖成分知识、产品推荐、使用指导、售后、
> 投诉五大类及难中易三级难度。评估指标用 Hit Rate@K、Recall、Precision 和 MRR。
> 当前检索链路：查询改写 → 向量（Qdrant，bge-large-zh-v1.5）+ BM25 双通道 →
> RRF(k=60) 融合 → bge-reranker-v2-m3 重排。历史报告（2026-06，30 条查询集）
> 显示 Hit Rate@3 = 80.0%、MRR = 0.778；当前 649 条基准上的当前值需要用
> `scripts/evaluate_rag.py` 重跑生成 artifact 后引用。"

### Q: "Hit Rate 不够高怎么办？"

> "当前系统的检索链路是：同义词扩展查询改写 → 向量检索（Qdrant，bge-large-zh-v1.5，
> 1024 维）+ BM25 双通道并行 → RRF(k=60) 融合 → bge-reranker-v2-m3 重排序。
> 历史报告（30 条查询集）Hit Rate@3 约 80%，当前 649 条基准的当前值以最近一次
> 评估 artifact 为准。如果再优化，我会考虑：第一，针对专有名词（成分名、产品名）
> 优化 BM25 的词法索引权重。第二，引入多向量策略——对同一文档生成 Embedding 和
> 关键词两套表示，分别检索再融合。第三，经验反馈闭环——把人工客服标记的误检
> 案例加入 Hard Negative 训练集，微调重排序器。"

### Q: "RAG 检索不到怎么办？系统会怎么处理？"

> "有两层保障。第一层：RAG 组件通过 `query_multiple` 方法并行查询向量库和 BM25 索引，RRF 融合后经重排序器二轮筛选，最大化命中概率。第二层：如果 RAG 返回空结果或相关性不足，Agent 的 `_retrieve_knowledge` 方法会降级——不注入 RAG 上下文，直接用 LLM 自身知识回答。此外，Agent 在未被注入 RAG 上下文时不会编造数据来源，保障回答诚实性（测试防护）。ReAct 模式下还会尝试通过 Function Calling 调用 ERP 工具获取实时数据。"

### Q: "为什么先选 ChromaDB 后迁移到 Qdrant？"

> "初期选择 ChromaDB 三个原因：第一，嵌入式不需要单独部署，开发测试方便。第二，原生支持 metadata 过滤和 collection 隔离。第三，自带默认 embedding 不需要额外配置。v6.0 迁移到 Qdrant 是因为生产环境需要更高并发性能和独立部署的可靠性——Qdrant 用 Rust 编写、通过 Docker 部署、支持 gRPC 通信，更适合生产级多智能体客服系统。"
