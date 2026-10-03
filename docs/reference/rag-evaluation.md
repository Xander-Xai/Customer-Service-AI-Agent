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

### 3.1 快速执行（evidence pipeline）

```bash
# 1) 导入评测语料到本地 Qdrant（幂等，含 BM25 rebuild + gold 覆盖率审计）
make rag-eval-import

# 2) preflight gate（Qdrant 集合计数 / embedding 探针 / reranker 探针 / BM25 就绪）
make rag-eval-649-preflight

# 3) 冒烟（前 16 条，subset_run=true，不可作为正式证据）
make rag-eval-649-smoke

# 4) 正式 649 全量评测（4 个检索配置 ablation，生成 evidence artifact）
make rag-eval-649
```

正式评测脚本 `scripts/evaluate_rag.py` 评测 4 个检索配置：

| 实验 | Vector | BM25 | RRF | Reranker | 说明 |
|------|--------|------|-----|----------|------|
| vector_only | ✅ | ❌ | ❌ | ❌ | 单路向量基线 |
| bm25_only | ❌ | ✅ | ❌ | ❌ | 单路词法基线 |
| hybrid_no_rerank | ✅ | ✅ | ✅ | ❌ | 混合检索（无重排） |
| hybrid_rerank | ✅ | ✅ | ✅ | ✅ | 生产架构（production-like） |

> ablation 切换只作用于评测进程内的 KB 实例/请求参数
> （`_hybrid_enabled` / `_embed_fn` / `request.rerank`），生产默认配置
> （`HYBRID_SEARCH_ENABLED` 等）不被修改；测试防护见
> `tests/unit/test_rag_eval_harness.py`。

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

### 3.3 结果解读（evidence artifact）

正式评测输出到 `artifacts/evaluation/rag-649/<run-id>/`：

1. **report.json**：机器可读证据（提交）——schema_version / run_id / timestamp /
   git SHA / benchmark sha256 与条数 / runtime config / 4 实验的
   Hit@K、Recall@K、Precision@K、NDCG@K、MRR / 实测 latency（mean、P50/P90/P95/P99，
   含 VECTOR/BM25/FUSION_RRF/RERANK 分阶段实测值）/ category 明细 /
   ablation 对比（hybrid vs 单路、reranker uplift）与逐 query 改善/退化计数 /
   failure taxonomy 计数与样本 / preflight gate 结果
2. **failures.json**：全量失败明细（提交）——每条失败含 query_id、category、
   expected/gold、rank、failure_type（TIMEOUT / PROVIDER_ERROR /
   GOLD_NOT_INDEXED / MISS_ALL / LOW_RANK）与通道级诊断
3. **raw_results.json**：逐 query 原始检索结果（本地保留，不提交，
   sha256 记录于 report.json）

指标分母定义（写进 artifact notes）：指标在「成功执行检索」的查询上取均值；
exception / timeout 计入 failures，不进指标分母；降级查询（如向量通道超时
退化为词法）计入指标并按 `degraded_reason` 单独计数。

#### 3.3.1 Blocker 语义（schema v2，`rag-eval-evidence/v2`）

preflight 复合判定遵循 **primary cause != downstream symptom** 原则，
机器可读结构：

```json
{
  "status": "BLOCKED",
  "primary_blocker": "EMBEDDING_PROVIDER_AUTH",
  "blockers": [
    {"code": "EMBEDDING_PROVIDER_AUTH", "stage": "embedding", "blocking": true,
     "http_status": 401},
    {"code": "VECTOR_INDEX_EMPTY", "stage": "qdrant", "blocking": true,
     "caused_by": "EMBEDDING_PROVIDER_AUTH"}
  ]
}
```

- `status`：BLOCKED（存在阻塞管线的 blocker）/ PARTIAL（仅局部阻塞）/
  OK。
- `primary_blocker`：根因（当前已知根因代码：`EMBEDDING_PROVIDER_AUTH` /
  `EMBEDDING_PROVIDER_UNAVAILABLE` / `VECTOR_INDEX_EMPTY`）。
- `caused_by`：downstream 症状与根因的因果链接。索引为空且 embedding
  认证失败时，`VECTOR_INDEX_EMPTY.caused_by = EMBEDDING_PROVIDER_AUTH`；
  embedding 健康而索引为空时，`VECTOR_INDEX_EMPTY` 本身即根因（待导入），
  不带 `caused_by`。
- reranker 失败（401/403 → `RERANKER_PROVIDER_AUTH`；其他失败 →
  `RERANKER_PROVIDER_DEGRADED`）**只阻塞 hybrid_rerank**
  （`blocks_experiments: ["hybrid_rerank"]`，`blocking: false`），
  不得据此阻塞 vector_only / bm25_only / hybrid_no_rerank。
- artifact 只记录 provider HTTP 状态码与探针结果，绝不记录凭据材料
  （key / Authorization / 凭据 hash / 前后缀均不落盘）。

历史 v1 artifact（`schema_version: rag-eval-evidence/v1`，如
`preflight-20260929T191128Z`）使用单层 status 字符串
（如 `BLOCKED_VECTOR_INDEX`），会被原样保留、不做回填；其根因分析结论为
PROVIDER_AUTH 是 primary blocker、empty index 是 downstream blocker，
两者不矛盾（v1 顶层 status 采用了 downstream 症状命名）。v2 起新
artifact 使用上述结构化语义。

#### 3.3.2 评测分母三视图（evaluation_populations）

正式 artifact 必须输出三套 population 口径，**全部运行时动态计算，
禁止硬编码任何分母数字**：

| 视图 | 定义 | 用途 |
|------|------|------|
| `all_queries`（View A，**主口径**） | 全部查询进入分母；gold 未进入索引的查询（GOLD_NOT_INDEXED）仍算失败、按 0 分计入 | 系统级结果：corpus coverage + indexing + retrieval algorithm |
| `retrieval_eligible`（View B） | 至少 1 个 gold document 已进入 corpus/index 的查询（数量动态计算） | 分析 retriever 在「至少存在可命中文档」情况下的表现 |
| `full_gold_covered`（View C） | 全部 gold documents 都存在于当前 corpus/index 的查询（数量动态计算） | Recall@K / NDCG 分析，避免 gold 缺失直接压低算法指标 |

**主口径固定为 `all_queries`（端到端）**；`retrieval_eligible` /
`full_gold_covered` 只作为诊断视图并列输出，不得挑选其中最好看的一组
单独对外宣称。引用指标时必须注明 population（例如
"649-query end-to-end benchmark" 或 "full-gold-covered subset"）。

### 3.4 当前评测状态

> **当前 649-query 正式指标：NOT_VERIFIED。**
>
> 状态推导（治理方向）：本状态由 `scripts/rag_evidence_status.py` 从
> `artifacts/evaluation/rag-649/**/report.json` 中满足 formal full-run
> contract 的 artifact 动态推导，方向是 artifact/evidence → facts → docs →
> guard——本文档（与其它 active 文档）只渲染、不允许自行 claim 该状态；
> preflight-only 与 smoke/subset artifact 结构上即被拒绝，
> 不满足 formal contract 的 artifact 一律维持 NOT_VERIFIED。
> `scripts/project_facts.py --check` 与 `scripts/audit_doc_consistency.py`
> 会将文档行与推导状态比对，双向漂移都算 FAIL。
>
> **最新已提交的 preflight evidence**：
> `artifacts/evaluation/rag-649/preflight-20261003T212439Z/report.json`
> （`schema_version: rag-eval-evidence/v2`，`timestamp 2026-10-03T21:24:39Z`，
> `status: OK`，`primary_blocker: null`，`blockers: []`）。
> 四个 gate 全绿：Qdrant 5000 points（`product_knowledge` 1466 / `faq` 902 /
> `tech_support` 1827 / `complaint_knowledge` 805）、embedding 探针 OK
> （dim 1024）、BM25 `READY`（5000 docs）、reranker 探针 HTTP 200。
> **provider 认证阻塞已解除**（见下方历史条目：根因是活动 `.env` 只含
> `sk-placeholder-*`，provider 返回 401 / code 30014）。
>
> **但正式 649 全量评测仍未执行，`NOT_VERIFIED` 的原因已改为数据集缺陷。**
> `tests/eval/rag_benchmark.json` 的 `expected_doc_ids` 由
> `scripts/regenerate_benchmark_ids.py` 以
> `random.seed(42)` + `random.sample(pool, 3)` 生成，`pool` 为该 query 类别
> 映射下的全部文档（865–1500 篇）：它做的是 doc-ID 对齐，不是相关性标注。
> `tests/eval/golden/expected_doc_ids.json` 与之内容一致，仓库内不存在
> 相关性标注版 gold。检验（评测自身 embedding 模型）：
> `cos(query, gold)=0.3732` vs `cos(query, 随机同类文档)=0.3696`
> （差 +0.0036，Welch **p=0.63**）；label-free 检索健康检查
> `cos(query, top-1)=0.6265` vs `cos(query, 随机文档)=0.3093`。
> 结论：**检索链路正常，标签是缺陷**；正确检索器在此 gold 上必然≈0 分，
> 而抬高 Hit@K 的唯一办法是退化成同类随机推荐器。证据 artifact：
> `artifacts/evaluation/rag-649/blocked-invalid-gold-labels-20261003T214052Z.json`
> （`metrics_produced: null`，`NOT_MEASURED` / `NOT_VERIFIED`）。
> benchmark sha256 `0185fcfabbb7437c87dc0ff417af883c468761cc2931f75e29b68aab833e693c`
> 未变，649 条 query 与 1250 个 gold id 全部保留，未剔除坏 case。
> 解除条件：为 649 条 query 建相关性标注 gold（对齐 corpus sha256
> `a81ea7f3347b45b3adefe474790fabd0b2277eada05327422134d3ca0737502d`），
> 然后直接重跑 `make rag-eval-649`；基础设施无需再动。
>
> 历史 artifact（保留不改、不回填）：`preflight-20261002T194209Z`
> （v2，`status: BLOCKED`，`primary_blocker: EMBEDDING_PROVIDER_AUTH`）。
> 该 artifact 的三个 blocker 按 §3.3.1 **必须分开表述**：
>
> | blocker code | stage | blocking | 影响范围 |
> |---|---|---|---|
> | `EMBEDDING_PROVIDER_AUTH` | embedding | `true` | 根因；`blocks_corpus_import: true`，阻塞 `vector_only` / `hybrid_no_rerank` / `hybrid_rerank`（HTTP 401） |
> | `VECTOR_INDEX_EMPTY` | qdrant | `true` | downstream 症状，`caused_by: EMBEDDING_PROVIDER_AUTH`；集合 0 points，且 BM25 索引由 Qdrant 重建，故 `bm25_only` 也被阻塞 |
> | `RERANKER_PROVIDER_AUTH` | reranker | **`false`** | 只阻塞 `hybrid_rerank`，**不得**据此声称其余实验也被 reranker 阻塞（HTTP 401，`silent_fallback`） |
>
> 该 artifact 的 notes 明写 `formal evaluation not run; no metrics generated`。
> `declared_queries: 649` / `executed_queries: 649` 是 preflight 的探针计数，
> **不等于** 649-query 正式评测已完成。最新 preflight 的 notes 首条同样是
> `formal evaluation not run; no metrics generated`——**preflight 通过不等于
> 正式评测完成**，正式指标仍为 `NOT_VERIFIED`。
>
> 历史尝试（保留不改）：2026-09-30 的评测尝试在 preflight gate 被阻塞
> （evidence artifact：`artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json`，
> v1 schema，顶层 `status: BLOCKED_VECTOR_INDEX`，原样保留不回填）。按 v2 口径
> 复盘该 v1 artifact：primary blocker = `EMBEDDING_PROVIDER_AUTH`（根因）；
> `RERANKER_PROVIDER_AUTH` 同级独立阻塞 hybrid_rerank；`VECTOR_INDEX_EMPTY`
> 为 downstream 症状——v1 顶层 status 采用 downstream 症状命名，与根因结论不矛盾。
>
> 语料与基准的 gold 覆盖率审计（导入 manifest：
> `artifacts/evaluation/rag-649/import_manifest_import-20260929T190455Z.json`）：
> 基准 1250 个 unique gold doc 中 1220 个存在于当前 5000 条语料
> （coverage 97.6%），30 个 `scene_0008xx` gold 文档不在语料中，
> 影响 80 条查询（其中 40 条查询的全部 gold 缺失）。正式评测时这些查询
> 按 GOLD_NOT_INDEXED 记账；三套 population 分母
> （all_queries / retrieval_eligible / full_gold_covered）由
> `scripts/evaluate_rag.py` 在运行时对索引 corpus 动态计算（§3.3.2），
> 上述 manifest 数字仅为语料文件口径的参考，不作为评测分母硬编码。
>
> 待 provider 凭据恢复后，按 §3.1 复现：`make rag-eval-import` →
> `make rag-eval-649-preflight` → `make rag-eval-649`。

### 3.5 历史评估结果

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

> "初期选择 ChromaDB 三个原因：第一，嵌入式不需要单独部署，开发测试方便。第二，原生支持 metadata 过滤和 collection 隔离。第三，自带默认 embedding 不需要额外配置。v6.0 迁移到 Qdrant 是因为生产环境需要更高并发性能和独立部署的可靠性——Qdrant 用 Rust 编写、通过 Docker 部署、支持 gRPC 通信，更适合生产化部署的多智能体客服系统。"
