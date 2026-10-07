# RAG Benchmark Gold-Label Provenance

> **Evidence semantics**：本文是 `tests/eval/rag_benchmark.json` 的 **静态数据 provenance
> 审计**，不产生任何 Hit@K / Precision@K / Recall@K / MRR / NDCG。
> 它是 **DATASET_DEFECT** 层面的结论，不代表任何生产验证。
>
> **当前结论**：shipped benchmark 的 `expected_doc_ids` **不具备 relevance 标注
> provenance**，因此正式检索指标用当前这份 gold 集 **当前不可测**
> （`FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD`）。
> `rag_formal_metrics_status` 保持 **NOT_VERIFIED**。
>
> 可复现脚本：`scripts/rag_gold_label_provenance.py`
> （`python3 scripts/rag_gold_label_provenance.py`）。
> 机器可读 artifact：`artifacts/evaluation/rag-gold-provenance/<ts>/report.json`，
> schema `rag-gold-label-provenance/v1`。

---

## 1. 为什么需要这份审计

正式 649-query 评测输出 Hit@K / Precision@K / Recall@K / MRR / NDCG@K，
全部相对 `expected_doc_ids` 计算。**指标的有效性完全依赖 gold 标签是不是
relevance judgement。** 如果 gold 只是"同类别随机文档"，那么：

- 数字在算术上成立；
- 语义上不衡量检索质量；
- 一旦生成 provenance-bearing 的正式 artifact，会得到一个**形状像正式证据、
  数值却毫无意义**的结果——这比诚实的 `NOT_VERIFIED` 更危险。

Issue #99 的诉求就是：在跑正式评测之前，先把 gold 的来源说清楚。本审计用**纯静态
仓库事实**回答该问题，不调用 provider / LLM / embedding / reranker。

---

## 2. 仓库里唯一记录的 gold 生成方式

`scripts/regenerate_benchmark_ids.py` 是仓库中唯一给 `expected_doc_ids` 赋值的脚本。它的逻辑是：

```python
random.seed(42)
q["expected_doc_ids"] = random.sample(pool, min(3, len(pool)))
```

其中 `pool` 是该 query 的 **benchmark category 映射到 KB category 后的全部文档**
（`CATEGORY_MAP`：`成分知识→成分知识`、`产品推荐→产品介绍`、`使用指导→使用方法`、
`售后问题→售后政策`；`投诉处理` 未映射，回退到**全部 5000 文档**）。
脚本 docstring 明确其目的：**doc-ID alignment，不是 relevance labelling**。

`tests/eval/golden/expected_doc_ids.json` 是 benchmark `expected_doc_ids` 的机械副本，
**不是**独立的人工标注文件。

---

## 3. 审计方法

`scripts/rag_gold_label_provenance.py` 执行：

1. 用 `scripts/generate_knowledge_base.generate()`（纯 stdlib、确定性）重建文档集合；
2. 精确复现 `regenerate_benchmark_ids.py` 的 `seed(42)` + `random.sample` 抽样；
3. 逐条对比 committed benchmark 的 `expected_doc_ids` 是否与复现结果一致；
4. 对每个 gold label 按下列 provenance class 分类：
   - `relevance_annotation`：有记录的人工/LLM relevance 标注。**当前为 0**。
   - `documented_generator_random_same_category`：可被 `seed(42)` 生成器精确复现
     （即"同类别随机抽样"，无 relevance 信号）。
   - `absent_from_corpus`：gold id 不在评测语料 `knowledge_base_5000.jsonl` 中，
     在当前语料上不可被检索到。
   - `unknown_provenance`：id 在语料中，但仓库中没有任何记录解释它为何是 gold。

---

## 4. 审计结果（当前 benchmark）

| 项 | 值 |
|---|---|
| total queries | 649 |
| total gold labels | 1947（distinct 1250） |
| **valid labels（relevance 标注）** | **0** |
| invalid labels（可证非 relevance：生成器随机 147 + 语料缺失 160） | 307 |
| unknown provenance labels | 1640 |
| 可被 `seed(42)` 生成器精确复现的 query | 49 / 649（全部为 `成分知识`，484 个 label 中的 147 个） |
| gold id 不在语料中的 label | 160（distinct 30，全部 `scene_*`） |
| 受影响 query（≥1 个 gold 缺失） | 80 |
| 全部 gold 缺失的 query | 40 |

> 关键点：**649 条 query 中只有 49 条能从仓库记录的生成方式复现，其余 600 条
> 的 gold 来源在仓库中无任何记录**；而这 49 条复现出来的也恰恰只是"同类别随机抽样"。
> 没有任何一条 gold 是 relevance judgement。

一个反例说明"类别相同 ≠ relevant"：`bench_0056`（category `产品推荐`）的 3 个
gold 全部属于 `售后政策` 类别。类别一致性都不满足，更谈不上 relevance。

---

## 5. 判定

```
FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD
```

用当前 shipped benchmark：

- 不能支撑 `Hit@K / Precision@K / Recall@K / MRR / NDCG@K` 作为**检索质量**的度量；
- 因此正式 649-query 指标不只是 `NOT_VERIFIED`（未测），而是**当前不可测**
  （unmeasurable），直到替换为 relevance-judged gold 集。

这**不**否定 `scripts/evaluate_rag.py` 的执行链路，也**不**否定 #93 的
evidence-validity gate（它守的是执行/corpus/通道事实；本审计守的是 gold 语义）。
两者互补：即使 #93 判 VALID，若 gold 无 relevance provenance，指标仍不可发布。

---

## 6. 复现

```bash
python3 scripts/rag_gold_label_provenance.py
python3 -m pytest tests/unit/test_rag_gold_label_provenance.py -q
```

不需要任何 provider 凭据、LLM、embedding 或 reranker。

---

## 7. 边界（不得越界宣称）

- 本审计**不**声称 provider auth 已解决；当前环境无真实凭据，provider 401 仍是
  历史 preflight 证据（见 [current-state.md](current-state.md)）。
- 本审计**不**修改 benchmark、语料或检索算法，也**不**生成任何指标。
- 本审计**不**声称"这 600 条 gold 一定是随机生成"——只声称**仓库中没有记录**
  其 relevance provenance，且唯一记录的生成方式就是随机同类别抽样。
- 修复路径（构建 relevance-judged gold，通常需人工标注或 LLM-judge + 人工抽检）
  超出本 PR 范围，属环境/人工依赖工作量。

---

## 相关

- [rag-evaluation.md](rag-evaluation.md) — 评测方法论与 canonical 执行链
- [rag-gold-label-contract.md](rag-gold-label-contract.md) — 修复路径的标注契约 + 离线校验器（#119）
- [current-state.md](current-state.md) — 当前事实入口
- `scripts/rag_evidence_validity.py` — 执行/corpus evidence-validity 判定（#93）
- Issue #99 — 本审计的 tracking issue
- Issue #7 — 生产证据 backlog（formal 649-query 指标在其验收项内）
