# ADR-005: 双层缓存策略（MD5 + Jaccard）— 历史决策

> Status: **Superseded**. This ADR records the 2026-06-02 design and is not the current architecture.
> Superseded by: [ADR-006](006-cache-and-tool-result-context-architecture.md)

**日期**：2026-06-02
**状态**：已采纳
**决策者**：项目负责人

## 背景

化妆品客服场景中 60-70% 的查询是高频重复问题。需要缓存机制减少 LLM 调用，降低成本和延迟。候选方案有 Redis 单层缓存、自研双层缓存、无缓存。

## 决策

自研双层缓存：L1 MD5 精确匹配 + L2 Jaccard 语义匹配，Redis 作为持久化后端（可选）。

## 理由

- **L1（MD5 精确匹配）**：O(1) 查找，完全相同的问题直接命中，
- **L2（Jaccard 语义匹配）**：jieba 分词后计算集合相似度，"精华液多少钱" ≈ "这款精华价格是多少"
- 两级组合的设计目标是把可缓存的问题挡在 LLM 之前；命中率与调用量降幅以
  `scripts/benchmark_cache.py` / `scripts/benchmark_ab_test.py` 的当前输出为准，本 ADR 不写死数字
- **Redis 单层无法实现语义匹配**：Redis 只支持精确 key 匹配或有限的全文搜索，无法做 Jaccard 相似度计算
- **倒排索引优化**：L2 通过 token → 候选集的倒排索引，将 O(n) 降到 O(k)

## 影响

**正面**：
- 缓存命中时把一次完整 Agent 往返换成一次缓存读取（具体分位值 `NOT_MEASURED`，
  需真实复跑 `scripts/benchmark_latency.py`）
- 命中即省一次 LLM 调用，直接降低 Token 成本（降幅 `NOT_MEASURED`）
- 防缓存雪崩：淘汰时只清除 5% 低热度条目

**负面**：
- 自研代码需要维护（~233 行），不如直接用 Redis 成熟方案
- Jaccard 相似度对语义相近但措辞差异大的查询命中率低
- 缓存一致性需要处理（Prompt 更新后旧缓存可能返回过时回答）

## 来源文档

[architecture-design.md](../design/architecture-design.md) 第 3.1 节
