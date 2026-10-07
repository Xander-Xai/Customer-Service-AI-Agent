# RAG Gold-Label Contract (`rag-gold-label/v1`)

> **Evidence semantics**：本文是 RAG 评测 **gold 标注规范与离线校验器**的说明，**不产生**
> 任何 Hit@K / Precision@K / Recall@K / MRR / NDCG。正式 649-query 指标状态保持
> **`NOT_VERIFIED`**；真实人工标注与正式全量评测属 [Issue #7](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/7)。
>
> **不修改** shipped 649 benchmark（`tests/eval/rag_benchmark.json`）。本契约是**新**的、
> provenance-enforced 的标注格式，用于替换而非回填旧 gold。
>
> 背景（为什么旧 gold 不可用）：[rag-gold-label-provenance.md](rag-gold-label-provenance.md)。
> 离线校验器：`scripts/validate_gold_labels.py`；规范/规则实现：`scripts/gold_label_contract.py`。

---

## 1. 为什么需要这份契约

`scripts/rag_gold_label_provenance.py` 的静态审计结论是：现有 `expected_doc_ids` 的
唯一记录生成方式是「同类别随机抽样」（`random.seed(42)` + `random.sample`），
**没有一条是 relevance judgement**（`DATASET_DEFECT`）。用它算出的 Hit/MRR/NDCG
在算术上成立、在语义上不衡量检索质量，属于**形状像正式证据、数值无意义**——比诚实的
`NOT_VERIFIED` 更危险。

因此在跑正式评测之前，必须先有一套**可校验的标注 provenance**：每条 gold 都要能回答
「谁、用什么方法、基于哪段证据、在哪个语料版本上、于何时判定它相关」。

---

## 2. Schema

一行一条 JSON（JSONL）。每条记录必须有下列字段（值可为 `null`，但**键必须存在**）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 固定 `"rag-gold-label/v1"` |
| `query_id` | str | benchmark query id（如 `bench_0000`）或 `EXAMPLE_*` 模板值 |
| `query` | str | 用户查询原文 |
| `corpus_version` | str | 评测语料版本标签（如 `knowledge_base_5000@2026-06`） |
| `corpus_hash` | str | 评测语料的 sha256（64 位小写 hex） |
| `doc_id` | str \| null | 语料文档 id；`EXCLUDED` 允许为 `null` |
| `evidence_span` | str | **逐字**支撑「相关」的语料片段 |
| `relevance_grade` | int \| null | `0..3`；**仅** `JUDGED` 可非空（0=不相关但仍判定过，1–3=相关性等级） |
| `annotator` | str | 产出该判定的人（或 LLM 候选的来源） |
| `annotation_method` | str | 见 §3 |
| `reviewed_at` | str \| null | **人工**复核时间的 ISO-8601 UTC；`JUDGED` 必填 |
| `label_status` | str | 见 §3 |
| `exclusion_reason` | str \| null | `EXCLUDED` / `UNDETERMINABLE` 必填 |

### `label_status`

- `DRAFT_UNVERIFIED`：候选，**不可**用于正式评测。
- `JUDGED`：已人工复核的相关性判定，可用于正式评测。
- `EXCLUDED`：明确排除（如 gold 不在语料中），须给 `exclusion_reason`。
- `UNDETERMINABLE`：无法判断相关性，须给 `exclusion_reason`，同样排除出正式分母。

### `annotation_method`

- `human`：人工标注。
- `llm_judge_human_verified`：LLM-judge 产生、**人工逐条复核**。
- `llm_suggested`：LLM 候选，**只能**是 `DRAFT_UNVERIFIED`。
- `category_random_match`：同类别随机匹配（旧的伪 gold 生成方式），**永远不能**是 `JUDGED`。

---

## 3. 校验器强制的不变量

`scripts/validate_gold_labels.py`（内核 `scripts/gold_label_contract.py::validate_records`）
在离线、确定性、无 LLM/embedding/reranker 的前提下拒绝下列情况：

1. **缺失字段**（schema 要求的键不全）；
2. **重复 ID**（同一 `(query_id, doc_id)` 重复出现）；
3. **无效文档 ID**（提供语料时，`doc_id` 不在语料中）；
4. **无证据来源的相关性标签**（`JUDGED` 但 `evidence_span` 为空）；
5. **未经审核却标记 `JUDGED`**（缺 `reviewed_at` / `annotator`，或 method 非人工复核类）；
6. **同类随机匹配构造的伪 gold**（`category_random_match` + `JUDGED`，或带 `relevance_grade`）；
7. **无法判断相关性却未显式排除**（`EXCLUDED` / `UNDETERMINABLE` 缺 `exclusion_reason`）。

此外：非 `JUDGED` 记录**不得**携带 `relevance_grade`；`corpus_hash` 若与评测语料不符会被拒绝。

> **LLM 生成候选的定位**：LLM 只能产出 `llm_suggested` + `DRAFT_UNVERIFIED` 候选；
> 必须经人工复核（改为 `human` / `llm_judge_human_verified` 并填 `reviewed_at`）后
> 才可能成为 `JUDGED`。**候选永远不能直接当作可信 Gold。**

---

## 4. 使用

模板（可直接复制填充）：`tests/eval/gold_labels/template.jsonl`（两条示例：一条 LLM 候选、
一条显式排除；均可通过校验）。

```bash
# 纯 schema/规则校验（语料未生成时，doc-id/语料哈希检查记为 NOT_RUN）
python3 scripts/validate_gold_labels.py tests/eval/gold_labels/template.jsonl --no-corpus

# 带语料：校验 doc-id 存在性与 corpus_hash 一致性
make generate-knowledge-base   # 生成 data/knowledge_base/knowledge_base_5000.jsonl
python3 scripts/validate_gold_labels.py your_labels.jsonl \
    --corpus data/knowledge_base/knowledge_base_5000.jsonl
```

退出码：`0` 合法，`1` 存在被拒绝的记录，`2` 输入错误。「没跑」（语料缺失导致 doc-id
检查跳过）在报告里显式记为 `NOT_RUN`，不被当作通过。

---

## 5. 人工标注与复核流程（建议）

1. **候选生成**：可用 LLM 生成候选，写为 `llm_suggested` + `DRAFT_UNVERIFIED`，`relevance_grade=null`。
2. **人工复核**：逐条看候选，填 `evidence_span`（逐字片段）、`relevance_grade`（0–3）、
   `annotator`、`annotation_method`（`human` 或 `llm_judge_human_verified`）、`reviewed_at`，
   状态改为 `JUDGED`。
3. **无法判断**的条目显式记为 `UNDETERMINABLE` 并写 `exclusion_reason`，不要硬编一个分数。
4. **语料版本**：填写同一 `corpus_version` / `corpus_hash`，确保标注与评测语料一致。
5. 提交前跑校验器；`INVALID` 的批次不允许进入正式评测。

---

## 6. 边界（不得越界宣称）

- 本契约**不修改** `tests/eval/rag_benchmark.json`，**不生成**任何指标。
- 校验通过**只**证明标注记录满足 provenance 契约，**不**证明检索质量、**不**构成正式评测证据。
- 真实人工标注（E1）与正式全量评测（E2）仍为 [Issue #7](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/7)
  的验收项；在其完成前，`rag_formal_metrics_status` 保持 `NOT_VERIFIED`。
- 不得把 `DRAFT_UNVERIFIED` 候选、`EXCLUDED` / `UNDETERMINABLE` 条目计入正式指标分母。

---

## 相关

- [rag-gold-label-provenance.md](rag-gold-label-provenance.md) — 旧 gold 的静态 provenance 审计（#99）
- [rag-evaluation.md](rag-evaluation.md) — 评测方法论与 canonical 执行链
- [current-state.md](current-state.md) — 当前事实入口
- Issue #119 — 本契约的 tracking issue
- Issue #7 — 生产证据 backlog（E1 人工标注 / E2 正式评测）
