# Context Engineering Interview Notes

## Why optimize Tool Result tokens?

Every ReAct observation can be sent again in later rounds. The cost is not only
the result itself: it increases every subsequent input, context noise, and
latency. This repository now applies a deterministic budget before appending a
ToolMessage.

## Why not `result[:2000]`?

Character slicing ignores structure, can cut an ID or JSON syntax in half, and
does not distinguish a continuation field from debug payload. The optimizer
filters structured records, deduplicates, applies top-k, preserves semantic
fields, then enforces a token estimate.

## Field filtering, Top-K, and truncation

Field filtering decides *which attributes* survive. Top-K/max-items decides
*how many records* survive. Truncation is the final size guard. They solve
different failure modes and are deliberately composed.

## Why not delete old ToolMessages?

LangChain/OpenAI tool calling requires the `AIMessage.tool_calls` and matching
`ToolMessage.tool_call_id` relationship. Deleting a message can make the next
request protocol-invalid. This implementation replaces old content while
keeping message count, order, and IDs.

## How is information loss controlled?

Policies are per tool. ERP policies retain IDs and fields needed by downstream
calls; the golden tests check products, inventory, orders, customers, and
multi-round protocol pairing. A too-small budget prioritizes valid semantic
fields over an invalid JSON slice. The benchmark checks task outcome as well as
estimated token reduction.

## Why no default LLM summary in V1?

An LLM summary adds another call, tokens, latency, and hallucination risk.
V1 uses deterministic filtering, deduplication, top-k, and compaction. A future
semantic summarizer can implement the same interface behind a separate flag.

## What did the local benchmark actually measure?

Command: `scripts/benchmark_tool_result_context.py`. It used the repository
token counter with tiktoken available and covered seven local scenarios. In the
recorded run, estimated input tokens changed as follows:

| Scenario | Baseline | Optimized | Estimated reduction | Outcome |
|---|---:|---:|---:|---|
| single tool call | 48 | 36 | 25.00% | PASS/PASS |
| three rounds | 766 | 284 | 62.92% | PASS/PASS |
| five rounds | 2796 | 386 | 86.19% | PASS/PASS |
| large list | 4703 | 38 | 99.19% | PASS/PASS |
| large JSON | 2253 | 168 | 92.54% | PASS/PASS |
| Chinese text | 1683 | 38 | 97.74% | PASS/PASS |
| multi-agent/ReAct-shaped | 1785 | 376 | 78.94% | PASS/PASS |

These are estimated local benchmark values, not production claims. API latency
was `NOT_MEASURED`; only local optimization processing time was measured.

## Compression vs RAG context compression

Tool Result Compression shapes operational observations produced during an
action loop and must preserve tool-call recoverability. RAG compression shapes
retrieved evidence before generation and is primarily about relevance/ranking.
They can share token estimation but have different correctness contracts.

## Relationship to Memory and SharedBlackboard

Session memory manages conversation history; SharedBlackboard shares bounded
agent findings. The optimizer is a boundary layer for tool observations before
they enter the active LLM context. It does not replace either store.

## Context Engineering vs Prompt Engineering

Prompt Engineering changes instructions and formatting. Context Engineering
controls what information is admitted, retained, compacted, or recovered over
time. This feature is mainly the latter.

## 60–90 second project answer

“这个项目的问题是 ReAct 多轮调用里，Tool Result 会作为 ToolMessage 原样回灌，后续每轮都重复携带，造成上下文膨胀。我先审计了现有链路，确认它确实是 ToolRegistry.execute 到 ToolMessage 再到下一轮 LLM，并复用了仓库已有 token counter，而不是再造 tokenizer。实现上增加了可关闭的 ToolResultOptimizer：对 dict、list 和 JSON 做字段白名单/黑名单、去重、Top-K 和 token budget；ERP 工具保留 order、product、customer 等下游需要的 ID。旧 ToolMessage 不删除，只压缩 content，保留 tool_call_id 和消息顺序，避免破坏 OpenAI/LangChain 协议。V1 没有默认使用 LLM 摘要，因为那会增加一次模型调用、延迟和幻觉风险，也没有虚构 reference store。我们用本地可重复 benchmark 对 baseline 和 optimized 做了七类场景对比，估算 input token reduction 从单次调用的 25.00% 到大列表的 99.19%，所有 golden outcome 都是 PASS；但外部 API latency 没有测量，所以不会把这些结果写成生产性能承诺。”
