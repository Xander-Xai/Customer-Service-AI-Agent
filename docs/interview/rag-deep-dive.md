# RAG Deep Dive — 检索链路、评测口径与失败分析

> ⚠️ **先读这一段**：当前正式 649-query RAG 指标是 **`NOT_VERIFIED`**。
> **preflight 已通过**（最新已提交 evidence
> `artifacts/evaluation/rag-649/preflight-20261003T212439Z/report.json`，
> `rag-eval-evidence/v2`，`status: OK`，`primary_blocker: null`；Qdrant 5000
> points / BM25 `READY` / reranker 200），此前 provider 认证 401 的阻塞
> （`{"code":30014,"message":"Token is invalid."}`，根因是活动 `.env` 只有
> `sk-placeholder-*`）**已解除**。
> **但正式评测仍未执行**，原因不是基础设施而是**数据集缺陷**：
> benchmark 的 `expected_doc_ids` 由 `scripts/regenerate_benchmark_ids.py`
> 以 `random.sample(同类文档池, 3)` 生成（池容量 865–1500 篇），做的是 doc-ID
> 对齐而非相关性标注。实测 `cos(query, gold)=0.3732` 与
> `cos(query, 随机同类)=0.3696` 无统计差异（Welch **p=0.63**），而检索本身
> `cos(query, top-1)=0.6265` 远高于随机文档 `0.3093`——**检索链路正常，
> 标签是缺陷**。因此 Recall@K / MRR / NDCG / reranker uplift 全部
> **`NOT_MEASURED`**。
> 本文件**不包含任何指标数字**。任何声称本项目"RAG 指标是 XX%"的说法都没有 artifact
> 支撑。见 Issue #7、blocked evidence
> `artifacts/evaluation/rag-649/blocked-invalid-gold-labels-20261003T214052Z.json`
> 与 blocker 字段口径的 canonical 定义
> [rag-evaluation.md](../reference/rag-evaluation.md) §3.3.1。
>
> 交叉引用：[source-map.md](source-map.md)、
> [rag-evaluation.md](../reference/rag-evaluation.md)（评测口径的 canonical 定义）。

---

## 1. Chunking（切分）

**是什么**：把长文档切成检索单元（chunk）。

**为什么需要**：向量检索是"先切分、再嵌入、再检索"的。切分粒度决定了两件相反的事：

- **切得太小**：单个 chunk 语义不完整（"该成分适用于"后面没有主语）；
- **切得太大**：一个向量被平均掉多个主题，检索命中了但内容不相关。

**关键判断**：切分策略是**精度天花板**。检索算法（BM25 / RRF / rerank）只能在
切分结果里排序，救不回被切碎的语义。**融合与重排是召回之后的优化，不是替代。**

本项目提供可配置 chunk 策略与确定性 point ID
（`rag/qdrant_knowledge_base.py`，PR #11 / #12）：

**为什么要确定性 point ID**：Qdrant 的 point ID 决定**同一段文本是否被去重**。
如果每次导入生成随机 ID，同一份语料导入两次会得到两套点，检索结果里出现重复文档，
而 BM25 与向量通道的去重键不一致 → RRF 融合出重复条目。
确定性 ID 让"重新导入"是幂等的，也让"语料版本"可以被引用。

**trade-off**：确定性 ID 意味着语料内容改变会生成新点，**旧点不会自动清理**。
因此需要显式的迁移/清理路径，而不是靠"重建 collection"糊过去。

---

## 2. Embedding（嵌入）

**是什么**：把 query 和 chunk 映射到同一向量空间。

**为什么需要**：语义相似检索。BM25 只能匹配字面（"透明质酸" vs "玻尿酸"就匹配不上）。

**关键设计 —— fail-closed**（`rag/embedding_status.py`，PR #12）：

embedding 不可用时不使用随机向量，也不跳过向量通道假装成功。返回
`REASON_EMBEDDING_UNAVAILABLE` 并让整条检索明确降级。

**为什么这么设计**：拿占位向量兜底的检索会返回"看起来相关但完全无关"的文档，
并且**不会报错**。它比明确失败危险得多 —— LLM 会拿这些无关文档生成一个自信的错误答案，
而系统层面一切正常。这是最难排查的一类故障。

**已验证**：embedding 不可用时的 fail-closed 行为有单测
（`tests/unit/test_cache_embedding_fail_closed.py`），CI VERIFIED。

**trade-off**：fail-closed 意味着 embedding 服务挂掉时检索整体不可用，
可用性下降。换来的是"失败可见"。对客服系统这个取舍是对的：
返回错误比返回错答案好。

---

## 3. BM25（稀疏检索）

**是什么**：基于词频/逆文档频率的稀疏检索，核心思想是**字面精确匹配 + 稀有词加权**。

**为什么需要**：向量检索有两个已知弱点：

1. **专有名词 / 型号 / 订单号**：嵌入会把 `ORD-2024-8891` 这种字符串压成一个没有
   区分度的向量；BM25 精确匹配它。
2. **罕见术语**："烟酰胺"这类词在通用语料里罕见，BM25 的 IDF 权重会自动提高。

**关键判断**：BM25 和向量检索的失败模式**互补**。向量擅长同义表达
（"退款" ↔ "退钱"），BM25 擅长字面精确（订单号、型号、成分英文名）。
这就是为什么要混合，而不是二选一。

**依赖**：BM25 索引的生命周期与 Qdrant 绑定 —— 由 Qdrant 重建
（因此向量索引为空时 `bm25_only` 也会被阻塞，见 preflight 的
`VECTOR_INDEX_EMPTY`）。这是有意的耦合：**避免两份索引状态不一致**
（"BM25 里有、向量里没有"这种不一致会产生难以解释的融合结果）。

---

## 4. Vector retrieval（稠密检索）

**是什么**：用 query 向量在 Qdrant 里做 ANN 检索。

**为什么需要**：见 §3 的语义匹配优势。

**实现细节**（`rag/qdrant_knowledge_base.py`）：用 `query_points()`
（qdrant-client 1.18 API）而不是已损坏的 `self._client.search`。
这段修复的记录本身是有价值的面试材料：**API 演进会让调用点静默损坏**。

**场景过滤**：多模态入口带来不同场景（售前/售后/投诉），不同场景应召回不同文档子集。
场景过滤在检索早期做（`STAGE_FILTER`），避免召回无关场景的文档再靠重排剔除 ——
过滤是免费的，重排是花钱的。

---

## 5. Fusion（融合）：RRF

**是什么**：Reciprocal Rank Fusion。把多个排序列表合成一个。

```
RRF(d) = Σ_i 1 / (k + rank_i(d))      k 默认 60（标准值）
```

**为什么需要**：向量分数和 BM25 分数**不可直接比较** —— 一个是余弦相似度
（范围约 -1..1，可能未归一化），一个是信息量分数（范围随语料变化）。
直接加权求和需要归一化，而归一化对分布漂移敏感（换了语料就要重新调）。

RRF **只用排名，不用分数**，因此天然免疫标度问题。这是它被广泛使用的根本原因。

**为什么 k=60**：平滑常数。k 大 → 各列表贡献趋于均等（更"民主"，但区分度低）；
k 小 → 头部分数被放大（更"强势"，但第 1 名和第 2 名差距被过度放大）。
60 是原论文的经验值，不是本项目调出来的。

**去重**：RRF 按内容去重，同一文档在两个通道出现时**分数相加**。

**trade-off**：RRF 丢掉了分数信息。如果向量相似度是 0.99 而 BM25 是 0.3（几乎不匹配），
RRF 不会知道这个差距有多大 —— 它只看排名。所以 RRF 之后仍然需要 rerank。
融合是"召回合并"，不是"精确排序"。

---

## 6. Reranking（重排）

**是什么**：对融合后的候选做一次更精确的相关性打分。

**为什么需要**：这是最关键的一问。

**召回和精排分别优化什么？**

| | 召回（recall） | 精排（rerank） |
|---|---|---|
| 优化目标 | 别漏（高召回率） | 别乱（高精确率） |
| 可用模型 | 便宜的：BM25、双塔向量 | 贵的：cross-encoder |
| 看什么 | query 和 doc **各自独立**编码 | query 和 doc **交互**（cross-attention） |
| 代价 | O(N)，要扫全库 | O(k)，只对候选做 |

双塔（bi-encoder）模型的两个向量独立编码，好处是可以离线把文档向量全部算好，
查询时只做一次向量检索 —— 这让"扫全库"成为可能。但**独立性也意味着它看不到交互**：
它不知道 query 里某个词在 doc 里的具体语境里意味着什么。

Cross-encoder 把 query 和 doc 拼在一起过一遍模型，能捕捉细粒度交互，
所以精度显著更高 —— 但必须对每个候选都算一遍，**无法预计算**，
所以只能作用在小候选集上（top-K）。

**这就是为什么要两阶段**：召回要"快而全"，精排要"准而慢"。
只在召回阶段优化，模型能力受限；只在精排阶段优化，候选已经漏了就没救。

**实现**：`rag/reranker.py::ApiReranker.rerank`，模型 `BAAI/bge-reranker-v2-m3`。

**关键设计 —— graceful fallback，但不再 silent**：provider 不可用/失败时**仍然**
返回融合后的原始排序（`RERANK` 阶段 `applied=false`），**不会**让整个 RAG 请求失败。
可用性优先于严格性，这是刻意的 trade-off，不是 bug。

**但 fallback 已不再 silent**。`rag/reranker.py` 现在提供 per-call 的不可变
`RerankOutcome`（`applied` / `degraded` / `reason` / `http_status` /
`provider_called`），`reason` 是有界枚举（`unavailable` / `timeout` /
`http_error` / `provider_error` / `invalid_response`）。关键在于 outcome 来自
**本次调用的返回值**，而不是实例上的 `last_error_status` —— 后者是共享可变状态，
并发请求下会把 B 的失败算到 A 头上。它现在**仓库内已无消费者**：runtime 与
preflight 探针都只用 `RerankOutcome`（该字段仅为向后兼容保留）。

因此"真的重排过"和"只是回退了"现在可以被区分：

| 情况 | `RERANK` trace | `meta` |
|---|---|---|
| 真实重排成功 | `EXECUTED` | `rerank_applied=true`、`rerank_degraded=false` |
| 请求了但 provider 失败/不可用 | `DEGRADED` + 有界 reason | `rerank_applied=false`、`rerank_degraded=true`、`rerank_reason=<enum>` |
| `request.rerank=false` | `SKIPPED` / `rerank_disabled` | `rerank_requested=false` |
| 候选 ≤ 1 | `SKIPPED` / `insufficient_candidates` | `rerank_requested=false` |

其它三条配套事实：

- **HTTP 200 不等于重排成功**：响应 schema 不合法、`results` 缺失、index 越界、
  没有可用 relevance score，都会判为 `invalid_response` + `degraded`，而不是让
  一条 malformed 200 冒充 rerank 成功。
- **降级会合并而不是覆盖**：embedding/BM25 已降级时，`degraded_reason` 保留上游
  根因，reranker 的事实放在自己的 `rerank_degraded` / `rerank_reason` 字段里，
  不会用 reranker timeout 抹掉 embedding 故障。
- **可观测**：`rag.stage.RERANK` event 的 `csai.stage.status` 反映真实状态，
  新增低基数 `csai.stage.reason`；`csai.reranker_count` 只在真实重排时才为正，
  未重排一律为 0。provider 请求实际发出后失败会打 `logger.error`（只含 reason
  枚举、HTTP 状态码、模型名 —— **不记录** query / documents / 响应体 / 凭据）。

**仍然没有验证的部分**：真实 provider 当前仍返回 401，因此
reranker uplift 仍是 `NOT_MEASURED`，formal 649-query 指标仍是 `NOT_VERIFIED`。
本轮是 **runtime engineering contract**，不是 provider 可用性证据 ——
`hybrid_rerank` 依然不能作为有效的 rerank evaluation 结果。

---

## 7. Evidence assembly（证据组装）

**是什么**：把检索结果组装成带来源、供生成使用的证据块。

**为什么需要**：检索结果不能直接当答案。它需要：去重、按相关性排序、附带可引用的来源、
控制长度（塞太多会挤掉指令，塞太少支撑不了回答）。

**关键判断**：**证据不足时应该明说"不知道"，而不是用通用知识补齐**。
本项目的 RAG 与生成之间有一个"groundedness"目标，但**当前没有可用的测量**
（评测被阻塞），所以这一项目前是设计意图而非已验证能力。

---

## 8. Generation（生成）

**是什么**：LLM 基于证据 + 问题生成回答。

**关键判断**：RAG 只能保证"证据是检索来的"，**不能保证"回答只用了证据"**。
这是两件事，后者需要生成侧的约束（citation 强制、groundedness 校验），
也需要评测来证明。当前 grounding 相关指标 `NOT_MEASURED`。

**LLM 侧的重要配置**（`llm/client.py`）：
- 熔断器：连续失败后降级到规则引擎（`LLM_CIRCUIT_BREAKER`）；
- 重试 + 退避，失败路径显式标记 `error_type` 以便 trace 归因
  （`csai.error_type` / `csai.error_code`，见 `core/telemetry.py`）；
- token 用量与配额（`core/token_quota.py`）。

---

## 9. Evaluation（评测）

**这是本文件最需要小心的部分。**

### 9.1 语料与 harness

| 项 | 值 |
|---|---|
| 语料 | `tests/eval/rag_benchmark.json`，**649 条 query** |
| 单条字段 | `query_id` / `query` / `expected_doc_ids[]` / `category` / `scene` / `difficulty` / `query_type` |
| 拆分 | `tests/eval/queries/{single_condition_200,multi_condition_150,fuzzy_150}.json` |
| gold | `tests/eval/golden/expected_doc_ids.json` |
| harness | `scripts/evaluate_rag.py`（canonical 入口，`make rag-eval-649`） |
| 契约守卫 | `scripts/eval_contract.py` |
| 评测集合定义 | `rag/retrieval_contract.py`（7 个阶段） |

`query_type` 覆盖 `single` / 多条件 / fuzzy，说明语料**不只有简单单跳查询**。

### 9.2 指标分层（重要）

必须区分两类指标：

| 层 | 指标 | 回答什么 |
|---|---|---|
| **检索层** | Recall@K / MRR / NDCG@K | 正确答案有没有被召回、排得对不对 |
| **回答层** | groundedness / citation correctness | 最终回答是否基于证据、引用是否正确 |

**为什么 Recall@K 和最终回答质量不是同一个指标**（这是高频追问）：

1. **Recall@K 只看有没有，不看用没用。** 召回了正确文档，不等于模型引用了它。
2. **回答质量受生成侧影响。** 同一个检索结果，模型可能答错、可能答对。
3. **K 的选择会移动指标。** Recall@20 很高但 Recall@3 很低，说明正确答案排得很后 ——
   而用户等不了 20 条。**报告单一 K 会掩盖排序质量。**
4. **推理型查询无法用 Recall 充分刻画。** 多跳查询下，"包含答案的文档"可能不止一条，
   或者需要跨文档综合，Recall 的二值命中不足以表达。
5. **citation correctness 是独立失败模式。** 答案对了但引用了错的段落
   （或引用了根本没召回的内容），是真实事故，Recall 完全测不出来。

**结论**：报 Recall@K 时**必须同时**报 K 的取值、查询类型分布、融合配置、
以及回答层指标。只报一个"Recall 0.xx"是无意义的数字。

### 9.3 正式评测的 ablation 设计

`make rag-eval-649` 跑 4 个 config（`vector_only` / `bm25_only` /
`hybrid_no_rerank` / `hybrid_rerank`）。**这是正确的设计**：只跑 hybrid 拿到一个数字，
无法归因 —— 数字好坏可能来自 BM25、也可能来自 rerank。ablation 才能回答
"哪个组件贡献了什么"。

**当前状态：BLOCKED（`INVALID_GOLD_LABELS`），但主因既不是 reranker 也不是认证。**
preflight 本身已通过（`preflight-20261003T212439Z`，`rag-eval-evidence/v2`，
`status: OK`，`primary_blocker: null`）；认证 401 的根因是活动 `.env` 只含
`sk-placeholder-*`，换真实凭据后 embeddings / chat / rerank 三通道均 200，
5000 篇语料已导入、BM25 `READY`。

真正的阻塞是**评测集标签无效**：`tests/eval/rag_benchmark.json` 的
`expected_doc_ids` 由 `scripts/regenerate_benchmark_ids.py` 用
`random.seed(42)` + `random.sample(pool, 3)` 从**同类全部文档**（865–1500 篇）
里随机取，对齐的是 doc ID 而不是相关性。后果是可量化的：
`cos(query, gold)=0.3732` 与 `cos(query, 随机同类)=0.3696` 无统计差异
（Welch p=0.63），而检索 top-1 达 `0.6265`（随机文档 `0.3093`）。
也就是说**一个语义正确的检索器在这套 gold 上必然拿 0 分**，而唯一能抬高
Hit@K 的做法是放弃语义检索、退化成"返回任意同类文档"——这是把系统改坏去迎合
测试集，不是改进。证据：
`artifacts/evaluation/rag-649/blocked-invalid-gold-labels-20261003T214052Z.json`
（`metrics_produced: null`）。解除条件是重建相关性标注 gold，不是调检索参数。

> 历史记录（认证阻塞期，原样保留）：`preflight-20261002T194209Z` 的
> `EMBEDDING_PROVIDER_AUTH`（根因，HTTP 401）/ `VECTOR_INDEX_EMPTY`
> （downstream 症状，带 `caused_by`）/ `RERANKER_PROVIDER_AUTH`
> （**`blocking: false`**，只影响 `hybrid_rerank`）。这套"根因 vs downstream
> 症状 vs 非阻塞"的分层口径今天仍然适用——只是当前 blocker 已经换成数据集缺陷。

### 9.4 失败分析框架（应当输出什么）

即使指标跑通，aggregate score 也不足以指导改进。必须抽出失败案例并归类：

| 失败模式 | 含义 | 可能的对策 |
|---|---|---|
| **retrieval miss** | 正确文档根本没进候选 | 调 K、调融合权重、加 BM25 通道、改切分 |
| **reranker failure** | 候选里有正确文档但被排掉 | 换 reranker 模型、调融合权重 |
| **chunking failure** | 答案被切碎或跨 chunk | 调 chunk size/overlap、按结构切分 |
| **query rewrite failure** | 改写把问题改坏了 | 约束改写、保留原 query 参与混合 |
| **conflicting evidence** | 语料内部矛盾 | 消解语料、标注权威版本 |
| **insufficient evidence** | 语料里根本没有答案 | 补语料，或承认"不知道" |
| **generation unsupported** | 证据没支撑回答却答了 | 收紧生成约束、加 groundedness 校验 |
| **wrong citation** | 答案对但引用错 | 强制引用校验 |

**归因顺序很重要**：从链路上游往下游查 —— 先确定正确答案是否在语料里
（`insufficient evidence`），再确定是否被召回（`retrieval miss`），
再确定是否被排上来（`reranker failure`），最后才看生成。
跳过前面几步直接调生成 prompt，是在错误的层优化。

**本轮状态：`NOT_MEASURED`。** 没有跑通的 artifact，因此上面是方法论，
**不是本项目的评测结论**。

---

## 10. 负样本怎么构造

**是什么**：与正样本（`expected_doc_ids`）相对、用于惩罚错误召回的文档。

**本项目的实际做法**（必须说清楚，不能吹成"精心构造的 hard negative 集"）：
评测语料只有**正样本标注**（`expected_doc_ids`）。负样本是**语料内的非 gold 文档**，
即"应该被排在后面"的全部其他文档。这是标准的信息检索评测做法（unranked negatives
近似），不是针对模型弱点构造的对抗负样本。

`query_type` 里的 `fuzzy`（150 条）与 `multi_condition`（150 条）是**难度分层**，
不是负样本工程。

**为什么没有构造对抗负样本**：本项目的目标是客服可用性，不是刷榜。
hard negative mining（用当前模型找出高分误召，再针对它们训练）会显著增加评测与训练复杂度，
而收益需要先证明当前瓶颈确实在精排。**当前无法证明**，因为 ablation 被阻塞。

**trade-off**：不做 hard negative 会让 reranker 的评测偏乐观（负样本太容易），
所以本项目的 reranker uplift 即使跑通也要谨慎解读。这正是 §9.2 要求同时报
`bm25_only` / `hybrid_no_rerank` / `hybrid_rerank` 的原因 ——
没有 baseline 对照，uplift 数字没有意义。

---

## 11. 已知工程债

| 债 | 影响 | 当前状态 |
|---|---|---|
| 正式指标从未跑通 | 无法证明检索质量 | **NOT_MEASURED**，provider 401 阻塞 |
| 消融实验从未执行 | 无法归因组件贡献 | **NOT_MEASURED** |
| 回答层指标无实现 | groundedness / citation 未测 | `NOT_MEASURED` |
| BM25 与 Qdrant 生命周期耦合 | Qdrant 空 → BM25 也不可用 | 有意设计（避免索引不一致），但增加了故障耦合 |
| 确定性 point ID 不自动清理 | 语料变更后留旧点 | 需要显式迁移路径 |
| 量化/延迟基线 | 无生产延迟 artifact | **NOT_MEASURED** |
| reranker 降级不可观测 | 降级伪装成成功，精度悄悄下降 | **已修**：per-call `RerankOutcome` + `RERANK=DEGRADED` + `meta.rerank_degraded` + `csai.stage.reason` + `csai.reranker_count=0` + `logger.error`。graceful fallback 保留（不 fail closed）；真实 provider 仍 401，uplift 仍 `NOT_MEASURED` |

---

## 12. 面试时的诚实表述

可以说：

- 检索链路是 **BM25 + 向量 → RRF 融合 → cross-encoder 重排**，
  每一层的职责和代价都能讲清；
- 切分粒度是精度天花板，融合和重排是召回之后的优化；
- embedding 用 fail-closed 而不是假向量，理由和测试都有；
- 评测是 4-config ablation + 失败案例分类，并知道 Recall@K 与回答质量不是一回事；
- **当前正式指标是 `NOT_VERIFIED`**，被 provider 认证阻塞，
  我不会给出任何没有 artifact 支撑的数字。

不能说：

- 任何具体的 Recall / 准确率 / 提升百分比；
- "RAG 效果很好""显著提升"；
- "已完成 649 条评测"。

被追问时的标准回答：

> 正式评测链路是通的（harness、语料、ablation 配置、契约守卫都在），
> 但当前 provider 认证返回 401，所以正式指标是 NOT_VERIFIED，我没有引用过任何数字。
> 我能讲的是方法和已知失败模式分类，以及为什么 Recall@K 不等于回答质量。
> 解除阻塞需要的是一个可用的凭据，然后跑 `make rag-eval-import && make rag-eval-649`。

这个回答本身比一个编造的数字更能说明工程判断力。