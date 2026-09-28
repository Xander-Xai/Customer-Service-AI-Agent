# Tool Result Cache Reuse Audit

## CURRENT_HEAD

`245d2829446df0a9eeca0863734a2b012cad9642` (`origin/main` at audit time).

## CACHE_INFRASTRUCTURE

- `cache.ResponseCache` is an application response cache with Redis L1,
  Qdrant L2 semantic lookup, and Jaccard L3 fallback. It caches user-facing
  responses by query/intent metadata; it is not a raw Tool Call execution cache.
- `cache.CachePolicy` provides scope, TTL, sensitivity, and version semantics for
  response caching and is useful security precedent, but its query-text API does
  not directly represent `(tool_name, arguments, scope)`.
- `core.session.session_manager` has optional Redis session persistence; it is
  not a tool-result cache.
- `core.tool_result_store` is V2 external raw-result storage for reference
  recovery. It intentionally has different semantics from cache reuse.
- Redis configuration is centralized in `core.config.REDIS_URL`; the existing
  `ServiceContainer` passes one Redis client to `ResponseCache` and the V2 store.
  This change will use an independent cache abstraction with an injectable client
  and the same existing Redis infrastructure/prefix family, not a new service.
- Qdrant/RAG cache is semantic retrieval infrastructure and is explicitly out of
  scope for exact Tool Call cache reuse.

## TOOL_CLASSIFICATION

Current `ToolRegistry` registrations are the four ERP query tools below. No
create/update/delete/send/refund/cancel/write tool is registered in the current
main tool registry.

| Tool | Class | Cache decision | Reason / TTL direction |
|---|---|---|---|
| `query_product` | READ_ONLY_IDEMPOTENT | cacheable | Product catalogue read; moderate TTL, default 300s |
| `query_inventory` | VOLATILE_READ | cacheable with short TTL | Stock changes quickly; default 30s |
| `query_order` | VOLATILE_READ + private | cacheable only with authenticated scope, short TTL | Order state/物流 changes; default 30s |
| `query_customer` | READ_ONLY_IDEMPOTENT + private | cacheable only with authenticated scope, short TTL | Personal data and profile freshness; default 30s |
| unknown/unregistered | UNKNOWN | bypass | No safe policy can be inferred |
| mutating/side-effect names | MUTATING / SIDE_EFFECTING | bypass | Never reuse execution result by default |

`query_order` and `query_customer` continue to pass through the existing ERP
authorization boundary before their results are exposed. A cache hit will be
scoped to the authenticated principal and session policy; it is not an
authorization bypass.

## SAFE_CACHE_CANDIDATES

The four existing query tools are candidates only after policy lookup and scope
construction. Cache policy is an explicit allowlist, disabled by default, and
does not apply to arbitrary future tools. Valid empty results may be cached only
as a short-lived negative result; exceptions, timeouts, authorization failures,
provider failures, rate limits, and temporary ERP errors are never cached.

## UNSAFE_TOOLS

Any tool with mutation or side-effect semantics (`create`, `update`, `delete`,
`send`, `refund`, `cancel`, `submit`, `write`) is bypassed regardless of a
similar-looking name. Unknown tools and results without a trustworthy scope are
also bypassed. The repository currently has no registered mutating tool, so this
policy is defensive for future registrations.

## KEY_DESIGN

The proposed exact key is a SHA-256 digest over a versioned canonical envelope:

```json
{
  "schema_version": "tool-cache-v1",
  "tool_name": "query_order",
  "arguments": {"...": "canonical JSON with sorted keys"},
  "scope": {"...": "hashed identity boundary"},
  "policy_version": "..."
}
```

Serialization uses sorted JSON keys, compact separators, UTF-8, and stable
Unicode handling. Raw user/customer/order/query values are not used as Redis key
text; sensitive scope and argument material is represented only inside the
digest. Transient fields are not silently removed in V1 because changing tool
semantics requires an explicit per-tool normalizer.

## TTL_DESIGN

TTL is per-tool and conservative rather than a universal best practice:

- product: 300 seconds
- inventory: 30 seconds
- order: 30 seconds
- customer: 30 seconds

These are opt-in defaults for the new layer and are not claims about production
freshness. The feature flag defaults off, and callers may provide a policy.

## INVALIDATION_RISKS

External ERP updates can make a cached read stale before TTL expiry. This slice
does not invent ERP mutation events or claim complete invalidation. TTL is the
baseline safety bound; the abstraction includes `invalidate` for explicit future
integration. Cache correctness is prioritized over hit rate.

## SECURITY_RISKS

- Cache keys never expose raw arguments, identities, tokens, or references.
- Scope includes authenticated `user_id` and, when required by the request
  contract, `session_id`; different scopes miss rather than share.
- Private ERP tools remain behind AuthZ; cache lookup occurs only after policy
  classification and with the same trusted scope material.
- A cache miss or cache backend failure executes the real tool. A cache write
  failure does not change the returned result.
- No cache metric label includes user/session/query/order/customer/cache key.

## PROPOSED_SCOPE

Add a small `ToolResultCacheProtocol`, deterministic in-memory implementation,
Redis implementation using the existing client seam, policy metadata on tool
definitions, and a cache lookup immediately before `execute_raw()`. Cache hits
return raw structured data to the existing V2 optimizer, so compression/offload
still runs on every result. Add feature flags, bounded metrics, exact-key tests,
scope/TTL/failure tests, Agent integration, deterministic execution-avoidance
benchmark, and documentation/interview updates.

## NON_GOALS

- No semantic/embedding/LLM cache.
- No change to `ToolResultStore` recovery semantics.
- No new Redis service, database, agent, or cache layer.
- No single-flight/distributed lock in this slice; stampede protection is
  `CACHE_STAMPEDE_PROTECTION=FUTURE_WORK` and must not be implied by metrics.
- No real ERP simulator, production latency claim, or production cost claim.
- No unrelated RAG, Agent, or authentication refactor.
