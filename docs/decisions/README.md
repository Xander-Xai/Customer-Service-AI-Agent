# ADR 决策记录

此目录存放架构决策记录（Architecture Decision Records）。

## 索引

| ADR | 标题 | 日期 | 状态 |
|-----|------|------|------|
| [ADR-001](001-langgraph-multi-agent.md) | 选择 LangGraph 作为多 Agent 编排框架 | 2026-06-02 | 已采纳 |
| [ADR-002](002-vanilla-js-frontend.md) | 前端使用原生 JavaScript | 2026-06-08 | 已采纳 |
| [ADR-003](003-qwen-default-llm.md) | Qwen2.5-7B 作为默认 LLM | 2026-06-02 | 已采纳 |
| [ADR-004](004-rag-embedding-selection.md) | RAG 向量库与中文 Embedding 选型 | 2026-06-06 | 已采纳 |
| [ADR-005](005-dual-layer-cache.md) | 双层缓存策略（MD5 + Jaccard） | 2026-06-02 | 已采纳 |

## 模板

```markdown
# ADR-NNN: 决策标题

**日期**：YYYY-MM-DD
**状态**：提议 / 已采纳 / 已废弃
**决策者**：谁做的决定

## 背景
当时面临什么问题？（2-3 句话）

## 决策
做了什么选择？（一句话）

## 理由
为什么选这个？替代方案是什么？（要点列表）

## 影响
这个决策带来了什么后果？（正面 + 负面）

## 来源文档
原始分析文档的路径（供追溯，原文不删除但不再维护）。
```
