# Tool Result Context Engineering V2

## Problem and baseline

In a ReAct loop, each observation becomes a `ToolMessage` and can be carried
into later rounds. A large result therefore multiplies context size. The V1
chain is:

`ToolRegistry.execute_raw() → ToolResultOptimizer → ToolMessage → historical compaction → next LLM call`.

V1 is deterministic and rollback-safe: it filters structured fields, removes
empty values, deduplicates, applies Top-K and an estimated token budget, while
preserving `tool_call_id` and message order. The estimator is not provider
exact tokenization.

## V2 architecture

```text
tool execution → policy → normalization → JSON/search/HTML compressor
               → token budget → compact result OR scoped external reference
               → ToolMessage → historical compaction → next LLM round
```

`ToolResultStoreProtocol` is independent from the optimizer. `InMemoryToolResultStore`
is used for tests and deterministic local runs. `RedisToolResultStore` accepts
the existing Redis client/`REDIS_URL`; it does not create a second application
configuration. Records contain an opaque `tr_...` reference, tool name, raw
payload, timestamps, TTL, scope, metadata, and schema version.

Offload is disabled by default. When enabled, results over
`TOOL_RESULT_OFFLOAD_MIN_TOKENS` are stored with a TTL and the model receives a
bounded preview (`status`, reference, summary, item count, recoverability).
The application calls `agent.recover_tool_result()`; the model never accesses
Redis. Store errors fall back to the deterministic compact result and never
crash the tool loop. Unknown, expired, corrupted, or wrong-scope references
are unavailable.

## Specialized compressors

- JSON keeps the V1 deterministic structured policy.
- Search results keep rank/title/source URL/snippet/score, remove duplicates and
  common tracking parameters, and bound snippets. Raw HTML and arbitrary
  metadata are not retained.
- HTML uses the standard-library parser as a best-effort visible-text extractor;
  it removes script/style/comment content, normalizes whitespace, keeps title,
  handles malformed markup, and enforces the token budget.

All tool results are untrusted data. Compressor output is data in a ToolMessage,
never system or developer instructions.

## Pagination versus Top-K

Pagination reduces the size returned by a data source (`limit`, offset cursor,
`next_cursor`, `has_more`). Top-K reduces what enters the LLM context after the
tool has returned. They are different layers. The repository provides a
deterministic offset pagination contract and mock ERP test path; the real ERP
backend has not been proven to support this contract, so production pagination
is not claimed.

## Optional semantic summary

`ToolResultSummarizerProtocol` is an optional fallback after deterministic
compression/offload and is disabled by default. It has a timeout, metrics, and
fallback to the deterministic preview. The default path does not add another
LLM call, latency, cost, or hallucination surface.

## Configuration and rollback

`TOOL_RESULT_OPTIMIZATION_ENABLED=false` remains the V1 rollback switch.
V2 adds `TOOL_RESULT_OFFLOAD_ENABLED=false`,
`TOOL_RESULT_OFFLOAD_MIN_TOKENS=1200`, `TOOL_RESULT_STORE_TTL_SECONDS=900`, and
`TOOL_RESULT_SEMANTIC_SUMMARY_ENABLED=false`. Turning off optimization disables
the new context path; turning off offload retains local deterministic
compression. Redis failure also rolls back per result to local compression.

## Security and observability

Recovery requires exact scope equality (`user_id` and/or `session_id`). IDs are
opaque and never derived from customer/order/query values. TTL is mandatory.
Raw payloads, references, and business IDs are not logged or Prometheus labels;
only bounded tool/strategy labels are used. Metrics cover offload, recovery,
store latency/errors, summaries, and compressor strategy.

## Benchmark and limitations

Run `python3 scripts/benchmark_tool_result_context.py`. It records raw and
optimized characters, estimated tokens, compression ratio, offload/recovery,
local processing latency, and golden outcome. `NOT_MEASURED` is used for
external provider/API latency unless an independently authorized real-provider
run is supplied. Local estimated reduction is not a production cost or latency
claim.

Known limitations: Redis integration requires a reachable deployment for a real
integration test; real ERP pagination is documented but not asserted; no
default semantic summary; and exact provider token counts remain provider
specific.
