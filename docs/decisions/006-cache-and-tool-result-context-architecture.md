# ADR-006: Cache and Tool Result Context Architecture

- Status: Accepted for current HEAD
- Date: 2026-09-29
- Supersedes: [ADR-005](005-dual-layer-cache.md)

## Decision

Keep these mechanisms separate:

1. **Response Cache**: L1 Redis exact lookup, L2 Qdrant semantic lookup, and L3 Jaccard fallback. It caches final response payloads under response-cache policy, TTL, content version, and identity/resource isolation rules.
2. **Tool Result exact reuse cache**: optionally avoids re-executing a safe, scope-bound read tool. Its key includes tool name, canonical arguments, schema version, and trusted scope. It must not cache errors or cross-user data by default.
3. **Tool Result Store**: optionally offloads an already-produced large result and recovers it by a scoped reference. This is storage/recovery, not cache reuse.
4. **Tool Result compression**: deterministic field/item reduction, specialized compressors, observation compaction, and token budgeting before `ToolMessage`. Compression is not pagination and does not prove provider billing savings.
5. **Session Memory**: conversation state/window/summary management. It is not response caching or tool-result storage.

## Current flow

```text
tool call → cache policy/key → scoped exact hit OR real execution
→ raw structured result → specialized compression → token budget
→ compact result or external reference → ToolMessage
→ history compaction → next LLM round
```

All feature flags default to off in `.env.example` and have rollback switches. Offload/store requires its configured backend; failures must degrade without exposing raw sensitive results. Local estimated token reduction is not provider billing or production latency evidence.
