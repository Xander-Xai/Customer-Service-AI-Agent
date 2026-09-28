# Agent Context Engineering / Tool Result Context Budget Management

## Problem

The ReAct/Function Calling path used to execute a tool, convert its complete
result to `ToolMessage.content`, append it to `messages`, and send the growing
history to the next LLM round. Large observations therefore accumulated even
when only a few fields were needed for the next action.

## Baseline

The audited path is:

`ToolRegistry.execute() → raw/string result → ToolMessage(content=result, tool_call_id=...) → next LLM call`.

The repository already has a session-level token counter and conversation
window/summary logic. Those operate on conversation history and were not a
Tool Result policy layer, so this feature adds a separate, reusable optimizer.

## Architecture and lifecycle

When the feature flag is enabled:

`execute_raw() → ToolResultOptimizer → structured filtering/dedup/top-k/budget → ToolMessage → old-result compaction → next LLM call`.

`ToolRegistry.execute()` remains the compatibility string API. The new
`execute_raw()` path is used only by the enabled Agent context path, preserving
the old behavior when disabled. No external result store is used in V1.

## Policy

`ToolResultPolicy` supports `max_tokens`, `max_items`, field whitelist/blacklist,
`preserve_recent`, deduplication, empty-field removal, and a strategy label.
Default ERP policies preserve continuation and semantic fields:

- `query_product`: product identity and product facts, top 5
- `query_inventory`: product identity, stock and warehouse facts, top 5
- `query_order`: order identity, status, total, tracking and date, top 5
- `query_customer`: customer identity and profile facts, top 1

Unknown tools use conservative behavior. If a minimum token budget cannot fit
all preserved identifiers in valid JSON, identifiers/semantic fields win over
an invalid character slice; this is an explicit information-loss trade-off.

## Token budget

`core.session.token_counter._count_tokens` is reused. It uses `tiktoken` when
available and otherwise a character-based fallback. Every benchmark and result
field calls these values `estimated_tokens`; they are not a model-exact count.

Configuration:

`TOOL_RESULT_OPTIMIZATION_ENABLED`, `TOOL_RESULT_MAX_TOKENS`,
`TOOL_RESULT_MAX_ITEMS`, `TOOL_RESULT_PRESERVE_RECENT`, and
`TOOL_RESULT_OFFLOAD_ENABLED` (reserved for a future store integration).

The optimization flag defaults to `false`, making rollback an environment
configuration change. V1 does not implement offloading or fake reference IDs.

## Old-result compaction and recoverability

Tool messages are never deleted. Their `tool_call_id` and position remain
unchanged so each `AIMessage.tool_calls` entry still has its corresponding
`ToolMessage`. Once older than the preserve-recent window, content is replaced
with a deterministic marker and bounded semantic `key_fields`. Already
compacted messages are not compacted again.

## Metrics and privacy

Prometheus metrics record raw/optimized bytes, estimated tokens before/after,
processed results, and truncation counts. `tool_name` is the only label; query,
order ID, customer ID, session ID, and result content are not labels or log
payloads.

## Benchmark and trade-offs

Run:

```bash
.venv/bin/python scripts/benchmark_tool_result_context.py \
  --json-output /tmp/tool-result-context.json \
  --markdown-output /tmp/tool-result-context.md
```

The benchmark covers one, three, and five rounds; large lists/JSON; Chinese
text; and a multi-agent/ReAct-shaped sequence. It measures local optimization
latency only. External LLM/API latency is always `NOT_MEASURED` unless a
separate real service benchmark is supplied.

In the recorded local run used for this change, all seven scenarios kept the
deterministic golden outcome at `PASS`. Estimated input-token reduction ranged
from 25.00% (single call) to 99.19% (large list). These are local estimated
token results, not production performance claims.

## Known limitations and future work

- V1 does not persist raw results or implement reference-based recovery.
- V1 does not make ERP query APIs paginated; it shapes returned results and
  preserves the existing order query limit.
- Exact model tokenization and real API latency require a provider-specific
  integration benchmark.
- Semantic LLM summaries are intentionally not used in V1.
