# P0-03 Completion Report

## 1. Issue

P0-03 — ERP IDOR / Authorization (Security + Authorization + Data Isolation)

Enforce ERP order/customer resource ownership at the Service/Tool boundary
from the P0-04 authenticated principal. LLM/Agent may *propose* an order_id /
customer_id; it cannot *authorize* resource ownership.

## 2. Baseline Attack Surface

Trace: Authenticated Request → P0-04 identity (state["user_id"]) →
BillingAgent/AftersalesAgent → ToolRegistry → ERP tool → ERP data.

Before the fix, every layer accepted an untrusted resource identifier and
returned matching records with **no ownership check**.

### BillingAgent (agents/billing_agent.py)
- Parsed `C\d{3}` (customer_id) and `ORD\d+` (order_id) from the user's
  natural-language query via `re.search`, then called
  `self.erp.query_order(order_id=...)` / `query_order(customer_id=...)`
  directly.
- No-ID branch: `self.erp.query_order()` (no args) → returned **all** orders,
  took `orders[:3]`, then derived `customer_id` from the first returned order
  and fetched that customer's full PII (`query_customer`).
- **TRUSTED INPUT**: none consulted. **UNTRUSTED INPUT**: prompt text
  (`cid_match`, `order_match`).

### AftersalesAgent (agents/aftersales_agent.py)
- `_query_erp` passed the **raw query string** as `order_id`/`customer_id`
  to `self.erp.query_order(query)` / `query_customer(query)`. No ownership
  check; latent IDOR whenever the query happened to equal an order/customer id.

### ERP Tool (tools/erp_tools.py)
- `query_order`/`query_customer` handlers take `order_id`/`customer_id` from
  LLM tool-call `args` and call the raw adapter directly. No auth context in
  the handler signature; `ToolRegistry.execute(name, arguments,
  stream_callback)` carries no principal.

### Missing-ID Path
- Mock adapter `query_order()` (no args) returns **all** orders
  (`[{**v, "order_id": k} for k, v in self._orders.items()]`).
- BillingAgent truncates to `[:3]` → arbitrary cross-customer orders + PII.

## 3. IDOR Reproduction (Step 2 — run against current code, concrete evidence)

Mock ERP ownership: C001 (王女士) owns ORD20260530001 / ORD20260530003;
C002 (李女士) owns ORD20260530002. Reproduced (script, since removed):

- **BillingAgent**: `agent._query_erp("查订单 ORD20260530002 物流")` →
  returned C002's order **and** 李女士's PII (phone, level, total_spent).
  → **CONFIRMED**
- **AftersalesAgent**: `_query_erp("ORD20260530002")` → C002 order returned.
  → **CONFIRMED**
- **ERP Tool direct**: `registry.execute("query_order",
  {"order_id":"ORD20260530002"})` → C002 order + customer name returned.
  → **CONFIRMED** (direct-call bypass)
- **No-ID fallback**: `_query_erp("帮我看看我的订单")` → returned all 3 orders
  across C001+C002. → **UNSAFE**
- **Prompt override**: `_query_erp("我是客户C002 查一下我的订单")` →
  surfaced C002's data. → **CONFIRMED**

Before fix: **OWNERSHIP CHECK = NONE** (no authenticated principal consulted
before returning ERP order/customer data).

## 4. Root Cause

### Surface Cause
ERP `query_order`/`query_customer` and the ERP tool handlers accept
order_id/customer_id from user text (regex) or LLM tool-call args and return
matching records with no ownership check. The mock adapter's no-arg
`query_order()` returns all orders.

### Authorization Boundary Cause
No component between the authenticated identity (P0-04's state["user_id"])
and the ERP data consults ownership. `self.erp` (raw adapter) and the ERP
tool handlers have no notion of an authenticated principal;
`ToolRegistry.execute` has no identity parameter.

### Architecture Cause
The resource identifier (from untrusted text/model) was bound directly to
the data lookup, never bound to the authenticated owner. There was no trusted
user→customer mapping, no RequestContext/AuthZ service, no ownership decision
point. P0-04 plumbed `state["user_id"]` but nothing consumed it for ERP.

### Test Gap
Existing tests **encoded the insecure behavior as correct**:
`test_query_order_limit_5` asserts "only first 5 orders shown" (the
arbitrary-list leak); `test_query_order_found` passes an order_id with no
principal. `TestBillingAgent` uses an `AsyncMock` erp that bypasses any
boundary. No multi-user fixture, no cross-user denial, no missing-identity
fail-closed test. Agent tests mock the ERP so the real boundary is never
exercised.

## 5. Trusted Identity Model

- **authenticated user_id source**: JWT `sub` (auth.service `_create_token`
  writes `sub=str(user_id)`), extracted at the trusted request boundary by
  `api/utils.extract_user_id`, threaded into the graph as `state["user_id"]`
  by P0-04 for REST/SSE/WebSocket/multimodal.
- **user_id → customer_id**: a new authoritative resolver
  `ERPProtocol.resolve_customer_by_user(user_id) -> str | None`.
  - Mock (`KingdeeMockAdapter._user_customer_map`): a server-trusted
    `{"user_001":"C001","user_002":"C002"}` mapping stored on the adapter
    (the authoritative ERP data owner in the test environment).
  - Base (`KingdeeAdapterBase`): default returns `None` (fail-closed) for any
    adapter without a verified authoritative mapping.
  - Real (`KingdeeRealAdapter`): inherits the base default → fail-closed
    until a verified Kingdee user↔customer field mapping is provided.
- **Why the mapping is trusted**: it lives on the ERP adapter (server-side
  authoritative data), NOT in prompt/LLM/tool-args/order-self-declaration.
  `ErpAuthorizationService` reads the principal from a ContextVar that P0-04
  sets at the trusted request boundary (`state["user_id"]`), never from the
  requested resource's own fields.

> **Honest scope note**: the AuthZ *boundary* and *ownership check* are PROVEN
> in the mock/test environment against server-trusted ERP data. Whether the
> real Kingdee ERP carries a linkable user↔customer field is a production
> business-data fact outside this repository's proof (EXTERNAL/UNVERIFIED).
> The code **fail-closes** (no resource disclosed) when the resolver cannot
> resolve, so production is safe even before the real mapping exists.

## 6. Authorization Architecture

```
RequestContext (transport-agnostic, P0-04)
        │  state["user_id"]
        ▼
BaseAgent.process_with_retry  ── set_erp_principal(state["user_id"])
        │                              (ContextVar, trusted boundary)
        ▼
ErpAuthorizationService  ◄── get_erp_principal()
        │  resolve_customer_by_user(principal)  → trusted customer_id
        │  requested resource (order_id/customer_id from prompt/model — NOT trusted)
        │  ownership decision (allow / deny)
        ▼
authorized data  (or safe empty — no enumeration)
        │  + structured security event on denial
        ▼
Agent / LLM / Response
```

BillingAgent, AftersalesAgent and the ERP tools all receive the same
`ErpAuthorizationService` instance (container wiring), so they share one
ownership policy.

## 7. Modified Files

| File | Change | Lines |
|---|---|---|
| `erp/authorization.py` | **NEW** — `ErpAuthorizationService` + `erp_principal`/`erp_entrypoint` ContextVars + `erp_principal()` ctx mgr + `_emit_denial()` structured security event. Enforces ownership on `query_order`/`query_customer`; delegates public `query_product`/`query_inventory`; fail-closed on missing/unresolvable principal; ignores prompt `customer_id` for ownership. | +196 |
| `erp/__init__.py` | Add `resolve_customer_by_user(user_id)` to `KingdeeAdapterBase` (default `None` = fail-closed). | +12 |
| `erp/kingdee_adapter.py` | Add server-trusted `_user_customer_map` + `resolve_customer_by_user` override. Customer/order dicts untouched (no key change → no regression). | +13 |
| `core/protocols.py` | Add `resolve_customer_by_user` to `ERPProtocol` so the typed service can call it. | +7 |
| `agents/base_agent.py` | `process_with_retry` sets `erp_principal`/`erp_entrypoint` from `state["user_id"]` (mirrors the existing blackboard ContextVar pattern). **No agent business logic changed.** | +9 |
| `core/container.py` | `_get_erp_authz()` lazy wrapper; agents (`set_erp`) and tools (`create_erp_tools`) receive the AuthZ-wrapped adapter. Returns `None` when `self.erp` is `None` (preserves un-initialized-ERP test behavior). | +30 |
| `tests/unit/test_erp_authorization.py` | **NEW** — 37 security + wiring tests (RED→GREEN). | +480 |

**Unchanged (by design)**: `agents/billing_agent.py`, `agents/aftersales_agent.py`,
`tools/erp_tools.py`, `tools/tool_registry.py` production logic — the boundary
is purely additive at the service layer; the agents/tools call the same methods
on the now-wrapped `self.erp`. No router, cache, RAG, or P1-05 tool-runtime
changes.

## 8. Tests Added (tests/unit/test_erp_authorization.py — 37)

- `TestTrustedCustomerResolution` (AUTHZ-1): known user→customer; unknown/empty→None.
- `TestMissingIdentityFailsClosed` (AUTHZ-1/6): no principal → `[]`/`None`;
  unresolvable principal → fail-closed.
- `TestResourceOwnership` (AUTHZ-2): owner reads own order; non-owner denied;
  `test_order_idor_denied` (symmetric cross-user); customer lookup scoped.
- `TestPromptAndModelCannotOverride` (AUTHZ-3): model args `customer_id=C001`
  cannot authorize C002's order; prompt `customer_id=C002` returns only own orders.
- `TestMissingOrderIdIsSafe` (AUTHZ-4): no-ID returns only principal's own orders
  (for both User A and User B).
- `TestNonEnumeration` (AUTHZ-5): unauthorized (exists, not yours) and not-found
  (doesn't exist) both return `[]`/`None` — indistinguishable.
- `TestToolAuthorizationBoundary` (AUTHZ-6/Step 14): tool owner reads own;
  tool non-owner denied before disclosure; **direct tool call without principal
  fails closed** (Attack F); tool model-args cannot override; tool customer scoped.
- `TestAgentAuthZParity` (AUTHZ-7): Billing owner reads; Billing non-owner denied;
  Billing no-ID safe; Billing prompt-override denied; Aftersales non-owner denied;
  Billing+Aftersales share policy.
- `TestSecurityEvent` (Step 11): denial emits structured `erp_authz` event
  (event_type/resource_type/resource_id/authorization_result/reason_code/
  principal/entrypoint/trace_id); no secrets/PII/order payload leaked.
- `TestTransportAgnosticAuthorization` (Step 12): denial/allow identical given
  principal (enforcement is service-layer, transport-agnostic).
- `TestContainerWiring` (Step 14): `_get_erp_authz` wraps raw; cached; re-wraps
  on adapter replacement; `None` when erp uninitialized; **agents receive the
  AuthZ boundary** (billing/aftersales/react); tool registry built on the boundary.

## 9. Tests Executed

### Security (test_erp_authorization.py)
37 passed, 0 failed.

### Agent / Tool / ERP unit
`test_base_agent_billing_coverage` (BillingAgent), `test_auth_tools_coverage`
(ERP tools), `test_erp_integration` (adapter), `test_modules.TestERPModule`,
`test_protocols_di` — all pass.

### Integration (test_integration.py)
`test_billing_agent_process` (raw-mock bypass path), container graph ainvoke
tests — pass.

### Transport / P0-04 parity
`test_sse_identity.py` (P0-04 identity parity) — **pass** (P0-04 contract
intact, not broken by P0-03). `test_api_routes.py` — pass.

### Affected + high-risk batch (9 files incl. security)
**495 passed, 0 failed** (51.8s).

### Full Regression (CI coverage command, unmasked)
```
python -m pytest tests/unit/ tests/integration/ tests/e2e/ \
  --cov=agents --cov=alerts --cov=api --cov=auth --cov=cache --cov=collaboration \
  --cov=core --cov=db --cov=erp --cov=knowledge --cov=llm --cov=media --cov=rag \
  --cov=router --cov=tools --cov-report=term-missing --cov-report=xml \
  -p pytest_counts -m "not real_llm and not stress" \
  --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear
```
- Collected: **1472**
- Passed: **1471**
- Failed: **0**
- Skipped: **0**
- XFailed: **0**
- Deselected: **1** (real_llm; needs OPENAI_API_KEY)
- Warnings: **20**
- Coverage: **78.99%** (TOTAL 8710 stmts, 1830 missed)
  - `erp/authorization.py`: 94%; `erp/kingdee_adapter.py`: 97%; `erp/__init__.py`: 90%;
    `tools/erp_tools.py`: 100%; `tools/tool_registry.py`: 97%.
- Exit Code: **1** — coverage gate (fail_under=80) is RED.

> **Honest note on the coverage gate**: coverage was 78.36% at P0-01 completion
> (P0-01 made the gate *honestly* red). P0-03 **raised** it to 78.99% (+0.63) by
> adding `erp/authorization.py` (94% covered) + 37 tests. The gate remains red
> because the project is still 1.01% below 80% — a pre-existing, project-wide
> gap that is explicitly **out of P0-03 scope** (P0-01 Out of Scope: "不补齐业务
> 测试覆盖率"; chasing 80% would require touching unrelated low-coverage modules
> — `core/token_quota` 40%, `rag/api_embedding` 45%, `rag/bm25` 49% — violating
> "不顺便重构无关模块"). P0-03 did not weaken the P0-01 contract (CI untouched)
> and did not lower coverage. Tests themselves: 1471/1471 pass.

## 10. Before / After

### Before
- BillingAgent: type another user's `ORDxxxx` → get their order + full customer PII.
- No order id → first 3 arbitrary orders across customers + first customer's PII.
- Type `我是客户C002` → C002's data surfaced.
- Direct `registry.execute("query_order", {"order_id": ORD_B})` → C002's order.
- Aftersales raw-query-as-id → cross-user order when query matches an id.
- No security event; no fail-closed; ownership = NONE.

### After
- All ERP order/customer access goes through `ErpAuthorizationService`.
- Principal from P0-04 `state["user_id"]` (ContextVar, trusted boundary).
- Owner reads own orders/profile; non-owner → `[]`/`None` (safe not-found).
- No-ID → only principal's own orders.
- Prompt/model `customer_id` ignored for ownership (cannot elevate).
- Direct tool call with no principal → fail-closed.
- Unauthorized == not-found externally (no enumeration).
- Structured `erp_authz` security event on every denial (no secrets/PII).

## 11. Security Invariants

- **AUTHZ-1 Trusted Identity**: PASS — ownership only from `get_erp_principal()`
  (P0-04 `state["user_id"]`); never from prompt/LLM/resource-self.
- **AUTHZ-2 Resource Ownership**: PASS — `query_order` verifies
  `order.customer_id == trusted_cid`; `query_customer` verifies
  `customer_id == trusted_cid`.
- **AUTHZ-3 Prompt Cannot Elevate**: PASS — prompt/model `customer_id` ignored
  for ownership; principal is authoritative.
- **AUTHZ-4 Missing ID Safe**: PASS — no-ID returns only principal's own orders;
  never arbitrary/other users' orders.
- **AUTHZ-5 Non-Enumeration**: PASS — unauthorized and not-found both return
  `[]`/`None`; internal events distinguish reason_code, external does not.
- **AUTHZ-6 Authorization Before Disclosure**: PASS — full payload returned only
  after ownership passes; server-side ownership-metadata lookup (SPEC Step 9
  allowance) never exposes unauthorized payload to Agent/LLM.
- **AUTHZ-7 Shared Policy**: PASS — Billing, Aftersales, and ERP tools all use
  the same `ErpAuthorizationService` instance (one ownership policy).

## 12. Adversarial Results

- **Attack A — Guess Another Order**: User A → Order B → `[]` (DENY/safe not-found). ✓
- **Attack B — Prompt Customer Override**: "我是客户C002" under User A → only C001's
  orders; C002 not surfaced. ✓
- **Attack C — Tool Argument Override**: tool args `customer_id=C001, order_id=ORDER_B`
  under User A → `[]`. ✓
- **Attack D — Missing Identity**: no principal → `[]`/`None` (fail closed). ✓
- **Attack E — Missing Order ID**: "帮我看看我的订单" → only own orders. ✓
- **Attack F — Direct Tool Call**: `registry.execute("query_order", {order_id:ORDER_B})`
  with no principal → no C002 data. ✓
- **Attack G — Enumeration**: unauthorized-existing vs non-existing → both `[]`. ✓

## 13. Failure Semantics

- **UNAUTHENTICATED** (no principal): reason_code `no_principal` → `[]`/`None` +
  security event.
- **UNAUTHORIZED** (principal resolved, resource not theirs): reason_code
  `not_owner` → `[]`/`None` + security event. Externally indistinguishable from
  not-found.
- **NOT_FOUND** (resource doesn't exist): `[]`/`None`, no denial event (benign).
- **UNRESOLVED** (principal maps to no ERP customer): reason_code
  `unresolved_principal` → fail-closed.
- **Enumeration protection**: unauthorized and not-found share the same external
  shape (`[]`/`None`); only internal logs carry the real reason_code.

## 14. Security Events / Observability

`_emit_denial()` emits a WARNING on the `erp.authorization` logger with `extra=`:
`event_type=erp_authz`, `resource_type` (order|customer), `resource_id`
(identifier only — ORD…/C…, not PII), `authorization_result=denied`,
`reason_code`, `principal` (safe user_id), `entrypoint` (agent name),
`trace_id`.

**Never logged**: JWT, API key, password, order full payload, PII (name/phone).
Verified by `TestSecurityEvent.test_denial_emits_structured_security_event`
(asserts rendered message contains none of `Bearer`/`JWT`/`api_key`/`password`/
`李女士`/`139****`).

## 15. Transport Parity

- **REST / SSE / WebSocket / Multimodal**: all reach the graph →
  `BaseAgent.process_with_retry` sets `erp_principal` from `state["user_id"]`
  (P0-04 threads this uniformly across all transports — proven by
  `tests/unit/test_sse_identity.py`, which passes). The `ErpAuthorizationService`
  boundary is transport-agnostic (reads the ContextVar). Therefore User A → Order B
  is **DENIED** identically on every transport; User A → Order A is **AUTHORIZED**
  identically. No transport is N/A — all reach ERP via the graph.
- P0-04 identity parity is the proven contract; P0-03 consumes it. No fabricated
  transport-specific test paths.

## 16. Regression Risk

Low. The boundary is additive (a wrapper service + ContextVar + 2-line
principal-setting in `process_with_retry`). No production agent/tool logic
changed. Existing tests that bypass AuthZ (raw `AsyncMock`/`KingdeeMockAdapter`
on `agent.erp`, or un-initialized `container.erp=None`) keep their behavior:
`TestBillingAgent`, `test_billing_agent_process`, and the container-graph
integration tests all pass unchanged. P0-04 identity tests pass. Full suite
1471/0. Coverage rose (78.36 → 78.99). `secrets/keys.json` is a pre-existing
uncommitted user change (documented in the Remediation Plan §0.1) — untouched.

## 17. Remaining Problems (NOT done by P0-03 — explicitly deferred)

- **Future P0-02** — Cache cross-user isolation (L1/L2/L3 scope). Untouched.
- **Future P1-04** — Agent reachability / Billing vs Aftersales routing boundary
  (which agent owns order_status / return_policy). P0-03 only guarantees that
  *whoever* accesses private ERP data cannot bypass ownership; it does not
  change routing.
- **Future P1-05** — Generic Tool Runtime (Schema→AuthZ→Risk→Timeout→Retry→
  Breaker→ToolResult). P0-03 implemented ERP ownership AuthZ only, via a
  dedicated service — **not** the generic runtime. `ToolRegistry.execute` still
  has no schema/timeout/retry/breaker. P0-03 did not pre-build P1-05.
- **Real ERP user↔customer mapping**: production linkage against live Kingdee
  is EXTERNAL/UNVERIFIED; code fail-closes until a verified mapping is provided.
- **Coverage ≥80%**: pre-existing project-wide gap (out of scope; P0-01 §Out of Scope).

## 18. Evidence

- Source: `erp/authorization.py`, `erp/__init__.py`, `erp/kingdee_adapter.py`,
  `core/protocols.py`, `agents/base_agent.py`, `core/container.py`.
- Tests: `tests/unit/test_erp_authorization.py` (37).
- Commands (run from repo root):
  - Security: `python3 -m pytest tests/unit/test_erp_authorization.py -q` → 37 passed.
  - Affected batch: 495 passed.
  - Full CI gate: command in §9 → 1471 passed, 0 failed; coverage 78.99%;
    REAL_EXIT=1 (coverage gate, pre-existing honest red).
- Diff: `git diff -- agents/base_agent.py core/container.py core/protocols.py erp/__init__.py erp/kingdee_adapter.py` (+71 lines production); new `erp/authorization.py`, `tests/unit/test_erp_authorization.py`.

## 19. Git Diff Review (Step 17 checklist)

1. Only P0-03? ✓ (no router/cache/RAG/ERP-write/P1-05).
2. No Router changes? ✓.
3. No P1-05 Tool Runtime? ✓ (no schema/timeout/retry/breaker — ERP ownership only).
4. No P0-02 cache scope? ✓ (cache untouched).
5. No ERP core business rules changed? ✓ (order/status/amount logic untouched;
   only wrapped by AuthZ).
6. Not only prompt-based? ✓ (boundary is `ErpAuthorizationService`; prompt
   `customer_id` ignored for ownership).
7. Not only string filtering? ✓ (ownership = `customer_id == trusted_cid`, not
   `sanitize_erp_input`).
8. No Agent-only authorization? ✓ (AuthZ in the service; agents don't implement
   ownership).
9. No Tool direct bypass? ✓ (tools close over AuthZ; direct call fail-closes — tested).
10. No missing fail-closed? ✓ (no principal / unresolved → deny).
11. No user-supplied `customer_id` trusted? ✓ (only `resolve_customer_by_user(principal)`).
12. No sensitive log leakage? ✓ (tested).
13. No enumeration via error diff? ✓ (tested).
14. No broken owner query? ✓ (owner reads own order — tested).
15. No test weakening? ✓ (+37 security/wiring tests; no skips/xfails for vulns;
    no coverage lowering).

## 19.1 Post-Completion Re-review Updates

The original completion snapshot above records the first P0-03 implementation
run. Subsequent independent review closed the remaining logging and boundary
hardening items without changing the authorization decision model:

- `sanitize_resource_id` is the shared policy for AuthZ and real-adapter
  `erp.*` logs. Pure-digit phone numbers and natural-language order inputs are
  hashed; valid letter-prefixed ERP identifiers remain auditable.
- Real-adapter ownership lookup requests only minimal owner metadata before any
  full order query; denial is fail-closed.
- `erp_principal` is reset on success, exception, and retry exhaustion.
- Real `user_id → customer_id` mapping is server-configured and fail-closed
  when absent or invalid.

Current targeted verification in this checkout:

```
python3 -m pytest tests/unit/test_erp_authorization.py \
  tests/unit/test_erp_user_mapping.py tests/unit/test_sse_identity.py -q --tb=short
87 passed, 1 warning
```

The project-wide regression run now includes P0-02 as well and is recorded in
the P0-02 report. Its current local result is 1587 passed, 0 failed, 1
deselected, coverage 80.32%, exit 0. The older 1471/78.99% figures above are
historical P0-03-only numbers, not the current repository result.

## 20. Commit Recommendation

```
fix(authz): enforce ERP resource ownership (P0-03)

Introduce ErpAuthorizationService as the Service/Tool authorization boundary
for ERP order/customer resources. Ownership is decided from the P0-04
authenticated principal (state["user_id"] via ContextVar), never from
prompt/LLM/tool-args. Non-owners are denied (safe not-found, no enumeration);
missing identity fail-closes; no-ID returns only the principal's own orders.
Billing, Aftersales, and the ERP tools share one boundary. Denials emit a
structured erp_authz security event (no secrets/PII).

Adds resolve_customer_by_user (authoritative user→customer mapping; mock
implemented, base/real fail-closed). Container wires the AuthZ service into
agents and tools.

Tests: tests/unit/test_erp_authorization.py (37 security + wiring).
Full regression: 1471 passed, 0 failed. Coverage 78.99% (up from 78.36%;
gate remains honestly red per P0-01, out of P0-03 scope).
```

---

**P0-03 DONE.**

Definition of Done: every item satisfied (see §11 invariants, §12 adversarial,
§9 tests). The only non-green CI signal is the pre-existing coverage gate
(78.99% < 80%), which P0-01 deliberately made honest and which is out of
P0-03's scope; P0-03 raised coverage and did not weaken the P0-01 or P0-04
contracts.
