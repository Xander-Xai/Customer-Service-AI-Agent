# Phase 1 Completion Report

> **Report type:** Phase 1 Pre-Gate Consolidation (evidence + readiness only).
> **Author:** Consolidator (this session). **No production code modified this round.**
> **Rule followed:** VERIFY FIRST — current checkout (code/tests/config/reproducible commands)
> is the source of truth; historical reports are auxiliary. Evidence before claims.
> **Date:** 2026-08-16.

## 1. Phase

**Security & Correctness** (P0-01 → P0-04 → P0-03 → P0-02 → P0-05), per
`CODEX_REMEDIATION_PLAN.md` §3 Phase 1.

## 2. HEAD / Baseline

| Field | Value |
|---|---|
| Branch | `agent/p0-02-p0-03-remediation` (upstream gone) |
| HEAD | `089cefbeb91950c2bd60bda734b127dd0325b208` |
| Phase Start Commit | `5be0668` docs: add CI remediation audit spec and plan (Phase 0 output) |
| Phase End Commit | `089cefb` fix(rag): fail closed on embedding unavailability (P0-05) |
| Working Tree | 3 unrelated uncommitted changes: `secrets/keys.json` (rotation-metadata timestamp 2026-06-25→2026-08-16, **no secret values**, pre-existing, untouched by P0 commits and by this session), `tests/data/csai.db-shm` / `csai.db-wal` (SQLite WAL churn from test runs). No P0 commit touches these. |
| Phase 1 diff scope | `5be0668..HEAD` = 39 files, +6554/−247. All changes map to P0-01..05 (see §9). |

### Phase 1 Baseline

| Item | Value |
|---|---|
| Python | 3.10.12 |
| CI-pinned toolchain (ci.yml L44) | pytest 7.4.4 / pytest-asyncio 0.21.1 / pytest-cov 5.0.0 / coverage 7.4.4 |
| Local `.venv` toolchain at session start | **DRIFTED** — pytest 9.0.3 / pytest-asyncio 1.4.0 / coverage 7.14.1 / pytest-cov 7.1.0 (does NOT match CI pin) |
| Verification toolchain used | CI-pinned (pytest 7.4.4/coverage 7.4.4) via temporary in-place downgrade of `.venv`; **restored to 9.0.3/7.14.1 after** — `.venv` left as found |
| Key deps | fastapi 0.137.1, langgraph 1.2.2, qdrant-client 1.18.0, redis 5.2.0, SQLAlchemy 2.0.35 |
| Redis availability | **none** (`redis-cli: command not found`; no server). Tests use FakeRedis. |
| Qdrant availability | **no server** (port 6333 closed). Tests use local file-mode `QdrantClient` + FakeQdrant. |
| ERP availability | `ERP_MODE=mock` → KingdeeMockAdapter. No real ERP. |
| Embedding provider | `EMBEDDING_API_KEY=sk-placeholder-embedding-test-key` → mock `_SemanticEmbedding`. No real embedding API. |
| LLM | `OPENAI_API_KEY=sk-placeholder-test-key-do-not-use` → MockLLM. No real LLM. |
| External services | **All MOCK / LOCAL-FIXTURE.** See §8. |

## 3. Issue Status

Acceptance-record status as found in the repository's committed artifacts (not from session memory):

| Issue | Dedicated completion report? | Dedicated acceptance report? | Last documented Codex verdict | Status this consolidation |
|---|---|---|---|---|
| P0-01 CI False Green | **NO** (cross-referenced in P0-05 §17, P0-03) | **NO** | Implied ACCEPTED (CI gate green); no standalone acceptance record committed | **ACCEPTED-AT-HEAD (re-verified)** / **ACCEPTANCE-RECORD GAP** |
| P0-04 SSE Identity | **NO** (cross-referenced in P0-03 §15, P0-05 §17) | **NO** | Implied ACCEPTED (test_sse_identity suite); no standalone acceptance record committed | **ACCEPTED-AT-HEAD (re-verified)** / **ACCEPTANCE-RECORD GAP** |
| P0-03 ERP AuthZ | YES (`P0_03_COMPLETION_REPORT.md`) | embedded §11 invariants (self-declared) | Acceptance "by independent reviewer" per P0-02 §16 framing | **ACCEPTED-AT-HEAD (re-verified)** |
| P0-02 Cache Isolation | YES (`P0_02_COMPLETION_REPORT.md`) | §14 matrix (self-declared); §16 "final ACCEPTED by independent reviewer, not self-declared" | Re-review #2 PARTIAL → #3 PARTIAL (3 findings) → fixed; no in-report Codex ACCEPTED | **ACCEPTED-AT-HEAD (re-verified)** |
| P0-05 Embedding Fail-Closed | YES (`P0_05_COMPLETION_REPORT.md`) | §15 invariants (self-declared) | **Re-review #3 = FAIL**, contradicted by implementer's independent spy against current working tree; **no post-#3 Codex ACCEPTED in report** | **ACCEPTED-AT-HEAD (re-verified)** / **CODEX VERDICT UNRESOLVED** |

**Honest summary:** all five P0 **code contracts** hold at HEAD `089cefb` (proven in §4–§7). But the
**acceptance records** have gaps: P0-01 and P0-04 have **no dedicated** completion/acceptance report
(acceptance only cross-referenced inside other P0 reports), and P0-05's committed report documents its
last Codex re-review as **FAIL** (contradicted by the implementer's own spy + 35 target tests) with no
clean post-#3 Codex **ACCEPTED** recorded. Session-memory claims of "ACCEPTED durable" for P0-05 are
**not corroborated** by the committed report.

## 4. Required Gate Tests

All five required gates re-proved at HEAD under the CI-pinned toolchain (pytest 7.4.4/coverage 7.4.4).
Each gate's named semantic test was run individually and **PASSED**.

### GATE-01 CI Blocking — **PASS**
Core semantics: TEST/COVERAGE FAIL → CI STEP FAIL (not FAIL→pipe/tail→GREEN); `fail_under` not lowered.
- `test_ci_contract.py::TestFalseGreenMechanism::test_pipefail_propagates_failure_through_tail` PASSED — `set -o pipefail; false 2>&1 | tail -1` returns non-zero (the fix); without pipefail returns 0 (the bug).
- `test_ci_contract.py::TestExitCodePropagation::test_coverage_step_preserves_pytest_exit_code` PASSED.
- `test_ci_contract.py::TestCoverageGate::test_fail_under_threshold_is_80` PASSED — `fail_under = 80` intact at `pyproject.toml:85`.
- `test_ci_contract.py::TestToolchainReproducibility::test_install_step_pins_full_coverage_toolchain` PASSED — full toolchain pinned at ci.yml L44.
- `test_ci_summary.py::TestBuildSummary::test_surfaces_all_counts_and_subthreshold_coverage_gate_fail` PASSED.
- Config evidence: `set -o pipefail` at `.github/workflows/ci.yml:110`; no `|| true` anywhere; `continue-on-error` only on informational lanes (ci_summary L133, gradual mypy L168, pip-audit L177 — the latter explicitly "replaces old `|| true` to surface findings as ⚠️").

### GATE-02 Identity Parity — **PASS**
Core semantics: REST / SSE / WebSocket / Multimodal share one trusted identity contract; no regression.
- `test_sse_identity.py::TestSSEIdentityPropagation::test_rest_sse_identity_parity` PASSED.
- `test_sse_identity.py::TestWSIdentityPropagation::test_rest_sse_ws_identity_parity` PASSED (REST+SSE+WS).
- `test_sse_identity.py::TestSSEIdentityPropagation::test_sse_user_id_is_not_from_client_body` PASSED.
- `test_sse_identity.py::TestSSEIdentityPropagation::test_rest_identity_parity_baseline` PASSED.
- `test_sse_identity.py::TestWSIdentityPropagation::test_websocket_passes_authenticated_user_id_to_graph` PASSED.

### GATE-03 ERP Authorization — **PASS**
Core semantics: User A → User B private order = DENIED/safe not-found; legal owner path still works.
- `test_erp_authorization.py::TestResourceOwnership::test_order_idor_denied` PASSED.
- `test_erp_authorization.py::TestResourceOwnership::test_non_owner_cannot_read_order` PASSED.
- `test_erp_authorization.py::TestResourceOwnership::test_order_owner_can_read_own_order` PASSED (positive path).

### GATE-04 Cache Isolation — **PASS**
Core semantics: User A private response not obtainable by User B via L1/L2/L3; public FAQ shared cache still works per CachePolicy.
- `test_cache_cross_user_isolation.py::TestCrossUserCacheIsolation::test_l1_user_a_order_not_served_to_user_b` PASSED (L1).
- `test_cache_cross_user_isolation.py::TestCrossUserCacheIsolation::test_l2_qdrant_user_isolation` PASSED (L2).
- `test_cache_cross_user_isolation.py::TestCrossUserCacheIsolation::test_l3_jaccard_user_isolation` PASSED (L3).
- `test_cache_cross_user_isolation.py::TestL2SemanticIsolation::test_l2_semantic_cross_user_miss` PASSED (semantic).
- `test_cache_cross_user_isolation.py::TestPublicFaqCanBeShared::test_public_jaccard_shared_across_users` PASSED (public FAQ shared — positive).

### GATE-05 Embedding Degradation — **PASS**
Core semantics: Embedding unavailable does NOT produce random/pseudo/dummy vector → semantic query; BM25 ready → explicit degraded fallback; no channel → explicit degraded/error.
- `test_p005_embedding_failclosed.py::TestNoSyntheticVector::test_embedding_failure_never_generates_random_vector` PASSED (EMB-1).
- `test_p005_embedding_failclosed.py::TestNoFakeVectorQuery::test_embedding_failure_does_not_query_qdrant_with_fake_vector_query` PASSED (EMB-2).
- `test_p005_embedding_failclosed.py::TestDegradedRetrieval::test_embedding_failure_degrades_to_bm25` PASSED (EMB-3).
- `test_p005_embedding_failclosed.py::TestDegradedRetrieval::test_embedding_failure_returns_explicit_degraded_result` PASSED (EMB-4).
- `test_p005_embedding_failclosed.py::TestDegradedRetrieval::test_normal_retrieval_not_marked_degraded` PASSED.

## 5. Full Regression Results

### Per-P0 suite matrix (CI-pinned toolchain)

| Suite | Collected | Passed | Failed | Skipped | XFailed | Deselected | Exit |
|---|---:|---:|---:|---:|---:|---:|---:|
| P0-01 (test_ci_contract + test_ci_summary) | 40 | 40 | 0 | 0 | 0 | 0 | 0 |
| P0-04 (test_sse_identity) | 11 | 11 | 0 | 0 | 0 | 0 | 0 |
| P0-03 (test_erp_authorization + test_erp_user_mapping) | 77 | 77 | 0 | 0 | 0 | 0 | 0 |
| P0-02 (test_cache_cross_user_isolation + test_cache_policy) | 87 | 87 | 0 | 0 | 0 | 0 | 0 |
| P0-05 (test_p005_embedding_failclosed + test_qdrant_knowledge_base) | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| **P0 matrix total** | **265** | **265** | **0** | **0** | **0** | **0** | **0** |

### Full repository regression (EXACT ci.yml L108 command, CI-pinned toolchain)

Command: `python -m pytest tests/unit/ tests/integration/ tests/e2e/ --cov=agents --cov=alerts --cov=api --cov=auth --cov=cache --cov=collaboration --cov=core --cov=db --cov=erp --cov=knowledge --cov=llm --cov=media --cov=rag --cov=router --cov=tools -p pytest_counts -m "not real_llm and not stress" --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear`

| Metric | Value |
|---|---|
| Collected | **1634** (pytest-counts.json sidecar) |
| Passed | **1633** |
| Failed | **0** |
| Skipped | **0** |
| XFailed | **0** |
| Deselected | **1** (`@pytest.mark.stress` at `tests/e2e/test_all.py:1643`, deselected by `-m "not stress"`) |
| Warnings | **18** (see §10) |
| Coverage | **81.57%** (TOTAL 9035 stmts, 1665 missed; `fail_under=80` gate PASS) |
| Exit Code | **0** |
| Runtime | 158.11s |

> **Honest discrepancy vs P0-05 report headline:** the report claims `1656 collected / 1651 passed /
> 5 skipped / 81.70%`. That was produced by `.venv/bin/python -m pytest tests/ -q --cov=.` — **a
> non-CI command** (`tests/` all dirs + `--cov=.`). The **actual ci.yml gate command** yields
> `1634 collected / 1633 passed / 1 deselected / 81.57%`. Both pass the gate (≥80%, 0 failed, exit 0);
> the report's specific numbers do not match the CI command. Coverage is toolchain-sensitive
> (ci.yml L36-43 explicitly documents 78.08/78.65/78.72% as three different toolchains), so the
> CI-faithful 81.57% under coverage 7.4.4 is the authoritative gate figure.

> **Coverage trajectory (honest):** 78.36% (P0-01, honestly RED, exit 1) → 78.99% (P0-03, RED,
> 1.01% gap explicitly deferred as out-of-P0-03-scope per P0-03 report §9) → 80.91% (P0-02) →
> 81.57% (P0-05, this verification). The coverage gap was closed by P0-02/P0-05 **adding tests**,
> not by P0-01/P0-03. CI Gate Correctness (P0-01, the gate-is-honest contract) and Coverage Target
> Achievement (≥80%) are **separate claims** — both now hold.

## 6. Cross-Issue Composition

### Identity → AuthZ (CROSS-01) — **PASS**
P0-04 identity is the trusted principal for P0-03 AuthZ; prompt/model cannot override.
- `test_erp_authorization.py::TestPromptAndModelCannotOverride::test_model_cannot_override_request_identity` PASSED.
- `test_erp_authorization.py::TestPromptAndModelCannotOverride::test_customer_id_from_prompt_cannot_override_identity` PASSED.
- `test_erp_authorization.py::TestToolAuthorizationBoundary::test_tool_model_args_cannot_override_identity` PASSED.
- `test_erp_authorization.py::TestAgentAuthZParity::test_billing_prompt_customer_cannot_override` PASSED.
- `test_erp_authorization.py::TestTransportAgnosticAuthorization::test_denial_identical_regardless_of_principal_source` PASSED.
- Code: `ErpAuthorizationService` reads principal from `get_erp_principal()` (P0-04 `state["user_id"]` ContextVar), never from prompt/LLM/resource-self.

### Identity → Cache Scope (CROSS-02) — **PASS**
Missing authenticated identity does NOT fall back to GLOBAL for private cache; it fail-closes.
- `test_cache_cross_user_isolation.py::TestCrossUserCacheIsolation::test_unknown_intent_no_identity_fail_closed` PASSED.
- `test_cache_cross_user_isolation.py::TestCacheTTLPolicy::test_disabled_not_cached` PASSED.
- `test_cache_cross_user_isolation.py::TestExplicitPolicyAndTenantScope::test_set_with_explicit_disabled_policy_skips_write` PASSED.
- `test_cache_cross_user_isolation.py::TestExplicitPolicyAndTenantScope::test_personalized_disabled_does_not_pollute_shared_l3` PASSED.
- Policy: `cache/cache_policy.py` uses an **allowlist** (not blocklist); unknown/missing/illegal intent → DISABLED (fail-closed), never GLOBAL, for non-public intents.

### ERP AuthZ → Cache (CROSS-03) — **PASS**
User A's cached ERP order response cannot be served to User B via any cache tier (cache does not bypass P0-03 AuthZ).
- `test_cache_cross_user_isolation.py::TestCrossUserCacheIsolation::test_l1_user_a_order_not_served_to_user_b` PASSED (L1 order cross-user).
- `test_l2_qdrant_user_isolation` / `test_l3_jaccard_user_isolation` / `test_l2_semantic_cross_user_miss` PASSED (L2/L3).
- Scope enforced by `read_scope_keys(user_id)` + `version` consumed identically by L1/L2/L3 (actual order L1→L3→L2, per P0-02 §15.2).

### Embedding Failure → Semantic Cache (CROSS-04) — **PASS**
P0-05 embedding failure does not make P0-02 semantic cache use a fake vector, return a cross-user entry, or bypass CachePolicy.
- `test_p005_embedding_failclosed.py::TestNoFakeVectorQuery::test_embedding_failure_does_not_query_qdrant_with_fake_vector_query` PASSED.
- `test_p005_embedding_failclosed.py::TestNoFakeVectorQuery::test_embedding_failure_does_not_query_qdrant_with_fake_vector_multiple` PASSED.
- `test_p005_embedding_failclosed.py::TestSemanticCacheFailClosed::test_cache_l2_skip_preserves_l1_exact_hit` PASSED (L2 skip preserves L1 scope).
- `test_p005_embedding_failclosed.py::TestIngestionFailClosed::test_ingestion_does_not_persist_fake_vector_when_embedding_down` PASSED (no fake-vector upsert).

### CI → All Security Tests (CROSS-05) — **PASS**
A failing test/coverage condition propagates and is not swallowed by CI; subsequent security-test failures cannot be masked.
- Covered by GATE-01 (`test_pipefail_propagates_failure_through_tail`, `test_coverage_step_preserves_pytest_exit_code`, `test_surfaces_all_counts_and_subthreshold_coverage_gate_fail`). The blocking test/coverage step has no `continue-on-error`; informational lanes are explicitly separated.

## 7. Security Invariant Matrix

| ID | Invariant | Verdict | Evidence |
|---|---|---|---|
| CI-1 | pytest failure propagates | **PASS** | `test_pipefail_propagates_failure_through_tail`; `set -o pipefail` ci.yml:110 |
| CI-2 | coverage failure propagates | **PASS** | `test_surfaces_all_counts_and_subthreshold_coverage_gate_fail`; `test_fail_under_threshold_is_80`; `fail_under=80` pyproject:85; full run 81.57%≥80%→exit 0 |
| ID-1 | trusted identity reaches graph | **PASS** | `test_sse_passes_authenticated_user_id_to_graph`; `test_run_graph_writes_user_id_to_state` |
| ID-2 | client identity cannot override | **PASS** | `test_sse_user_id_is_not_from_client_body`; CROSS-01 prompt-override tests |
| AUTHZ-1 | owner allowed | **PASS** | `test_order_owner_can_read_own_order` |
| AUTHZ-2 | non-owner denied | **PASS** | `test_non_owner_cannot_read_order`; `test_order_idor_denied` |
| AUTHZ-3 | direct tool cannot bypass | **PASS** | `test_direct_tool_call_without_principal_fails_closed`; `test_tool_non_owner_denied_before_disclosure` |
| CACHE-1 | private response not globally shared | **PASS** | `test_l1_user_a_order_not_served_to_user_b` + L2/L3 equivalents; CROSS-03 |
| CACHE-2 | L1/L2/L3 scope parity | **PASS** | `test_l2_qdrant_user_isolation`; `test_l3_jaccard_user_isolation`; single `_resolve_policy` scope_key/version/ttl consumed by all tiers |
| CACHE-3 | missing identity fails closed | **PASS** | `test_unknown_intent_no_identity_fail_closed`; CROSS-02; no USER→GLOBAL fallback |
| CACHE-4 | public FAQ still shareable | **PASS** | `test_public_jaccard_shared_across_users` |
| EMB-1 | no fake vector | **PASS** | `test_embedding_failure_never_generates_random_vector`; static grep: no `random`/`np.random`/pseudo-vector in production diff |
| EMB-2 | vector disabled on embedding failure | **PASS** | `test_embedding_failure_does_not_query_qdrant_with_fake_vector_query/multiple`; `query_points`/upsert call_count==0 |
| EMB-3 | BM25 fallback explicitly degraded | **PASS** | `test_embedding_failure_degrades_to_bm25`; `test_normal_retrieval_not_marked_degraded` |
| EMB-4 | no-channel result not marked normal | **PASS** | `test_embedding_failure_returns_explicit_degraded_result`; `test_degraded_run_is_not_reported_as_normal_vector_run` |

**All 15 invariants PASS.**

## 8. External Dependency Evidence Level

| Dependency | Evidence level this round | Note |
|---|---|---|
| Redis | **MOCK VERIFIED** | `redis-cli` absent; no server. Tests use FakeRedis. L1 contract logic verified, not real Redis. |
| Qdrant | **LOCAL INTEGRATION VERIFIED** (library, no server) | Port 6333 closed; no server. Tests use local file-mode `QdrantClient` + FakeQdrant. `query_points` API verified against real qdrant-client 1.18.0 in file-mode (P0-02 §16.2 probe), but no server round-trip. |
| ERP | **MOCK VERIFIED** | `ERP_MODE=mock` → KingdeeMockAdapter. Real Kingdee user↔customer field mapping is EXTERNAL/UNVERIFIED (P0-03 §5 honest note); code fail-closes when resolver cannot resolve. |
| Embedding | **MOCK VERIFIED** | `EMBEDDING_API_KEY=sk-placeholder`; mock `_SemanticEmbedding`. No real embedding API call. |
| LLM | **MOCK VERIFIED** | `OPENAI_API_KEY=sk-placeholder`; MockLLM. Real-LLM E2E deselected (no key). |

> **Phase 1 is mock-verified.** Real-run verification (Redis server, Qdrant server, real ERP mapping,
> real embedding provider, real LLM) is **Phase 3** per `CODEX_REMEDIATION_PLAN.md` §3 and is **not
> done**. Mock-pass is NOT production-verified; no P0 claim is elevated to "production proven" on
> the strength of these runs.

## 9. Scope Integrity

Phase 1 diff `5be0668..HEAD` = 39 files / +6554/−247. All changes map cleanly to P0-01..05:

- **P0-01:** `.github/workflows/ci.yml` (pipefail, pinned toolchain, counts plugin, junit/coverage artifacts, informational-lane split), `pyproject.toml` (markers, coverage source/omit, fail_under=80), `scripts/pytest_counts.py` + `scripts/ci_summary.py`, `tests/unit/test_ci_contract.py`, `tests/unit/test_ci_summary.py`.
- **P0-04:** `api/routes/chat.py`, `api/routes/chat_multimodal.py`, `api/routes/ws.py`, `core/state.py`, `agents/base_agent.py` (principal plumbing), `tests/unit/test_sse_identity.py`.
- **P0-03:** `erp/authorization.py` (NEW), `erp/__init__.py`, `erp/kingdee_adapter.py`, `erp/kingdee_real_adapter.py`, `tools/erp_tools.py`, `tests/unit/test_erp_authorization.py` (NEW), `tests/unit/test_erp_user_mapping.py` (NEW).
- **P0-02:** `cache/cache_policy.py` (NEW), `cache/response_cache.py`, `cache/__init__.py`, `core/config.py`, `core/container.py`, `core/graph_builder.py`, `core/logger.py`, `tests/unit/test_cache_cross_user_isolation.py` (NEW), `tests/unit/test_cache_policy.py` (NEW).
- **P0-05:** `rag/embedding_status.py` (NEW), `rag/qdrant_knowledge_base.py`, `cache/response_cache.py` (L2 fail-closed), `core/monitoring.py`, `agents/base_agent.py`, `core/protocols.py`, `core/state.py`, `requirements.txt` (prometheus-client), `tests/unit/test_p005_embedding_failclosed.py` (NEW), `tests/unit/test_qdrant_knowledge_base.py`.

**No P1/P2 work sneaked in.** Static grep for `RetrievalRequest`/`RetrievalTrace`/unified `retrieve()`/BM25 restart-rebuild/stable Qdrant IDs/global deadline/retry_budget in the production diff returned **nothing**. `rag/qdrant_knowledge_base.py` +245 lines is P0-05 embedding-validation + degradation-surfacing (typed `EmbeddingDimensionError`, channel-disable, `.meta`), NOT P1-01 RAG unification — `query`/`search`/`query_multiple` remain separate (P0-05 §19 confirms P1-01/P1-02 NOT started; embedding model NOT swapped; RRF/reranker NOT changed; benchmark NOT rewritten).

**Unrelated working-tree changes left untouched:** `secrets/keys.json` (rotation-metadata timestamp only, no secret values), `tests/data/csai.db-shm`/`-wal` (SQLite WAL churn). Neither is touched by any P0 commit or by this session.

## 10. Warning Review

18 warnings in the full regression. Classification:

| Class | Count | Warning | Verdict |
|---|---:|---|---|
| NON-BLOCKING | 1 | `qdrant_client/qdrant_remote.py:290 UserWarning: Failed to obtain server version...` | Expected — no real Qdrant server (mock env, §8). |
| NON-BLOCKING | — | `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2` (fastapi/testclient.py:1) | Pre-existing deprecation (P0-05 report notes jieba/pkg_resources/Starlette). |
| FUTURE ISSUE | 2 | `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` (unittest/mock.py:2135, :2181) | Test hygiene — async mocks leaking coroutines. Not a P0 contract. |
| FUTURE ISSUE | — | Resource warnings (tests/e2e/test_production_features.py: 4; per-file: test_modules 3, test_api_routes 2, test_v4_production 1, test_multimodal 1, test_all 4) | Unclosed resources in e2e. Not a P0 contract. |

> Exit code = 0 does **not** mean warnings are ignored. None are BLOCKING. The unawaited-coroutine
> and resource warnings are flagged as FUTURE ISSUE (test hygiene) — appropriate for a later
> cleanup, not a Phase 1 gate blocker.

## 11. Remaining P0 Risks

Only items that are genuinely still P0 (not P1/P2 deferred):

1. **P0-05 re-review #3 verdict unresolved.** The committed P0-05 report documents Codex re-review #3 as **FAIL** (claiming `_embed_query` "only checks length" lets NaN/Inf/None reach Qdrant). The implementer's independent spy contradicts this against the current working tree (`_embed_query` raises `EmbeddingDimensionError(reason=non_finite/non_numeric)`; `get()`/`set()` → `query_points_calls=0`, `upsert_calls=0`). This session's re-verification confirms the fail-closed contract holds at HEAD (EMB-1..4 + CROSS-04 PASS). **But there is no clean, post-#3 Codex ACCEPTED recorded.** A Codex re-review against HEAD `089cefb` is required to close this.
2. **P0-01 / P0-04 acceptance-record gap.** Neither has a dedicated completion/acceptance report in `docs/audit/`; acceptance is only cross-referenced inside P0-02/03/05 reports. The code contracts hold (re-verified), but the Phase 1 DoD item "five P0 acceptance reports exist and PASSed" is not literally satisfied for these two. Either accept the cross-referenced acceptance as sufficient, or require standalone reports.

## 12. Deferred P1/P2 Work (NOT Phase 1 Done-conditions)

Explicitly **not** started in Phase 1 (confirmed via static diff + P0-05 §19):

- **P1-03** Stable Qdrant IDs — no `point_id` uuid/stable-ID migration.
- **P1-02** BM25 Restart Rebuild — P0-05 only *consumes* `BM25Retriever.collection_size()` readiness; no rebuild-on-restart state machine.
- **P1-01** RAG Pipeline Unification — no `RetrievalRequest`/`RetrievalTrace`/unified `retrieve()`; `query`/`search`/`query_multiple` remain separate.
- **P1-04** Agent Reachability / Billing↔Aftersales routing — untouched.
- **P1-05** Tool Runtime boundary — untouched.
- **P1-06** Global Deadline / Retry Budget — untouched.
- **P1-07** Router/CircuitBreaker semantics — untouched.
- **P2-01..P2-06** FCR/labor-efficiency/benchmark-provenance/RAG-metrics/dataset-provenance/claim-matrix — untouched.
- **P2-03** Benchmark provenance — `evaluate_rag.py` does not tag runs normal/degraded (P0-05 only exposes `.meta.retrieval_degraded` for a future benchmark to read).

None of these are Phase 1 Done-conditions. They must not be treated as P0-DONE prerequisites.

## 13. Claim Freeze Status

**FROZEN.** No Phase 1 violation.

- No resume/interview production number (P99 / FCR / Hit Rate / Token Cost / Coverage-as-resume-claim) was upgraded to PROVEN in the Phase 1 diff. `grep` for `PROVEN|P99|FCR|Hit Rate|Token Cost` in the diff returned only: (a) P0-03 §5 "AuthZ *boundary* and *ownership check* are PROVEN **in the mock/test environment**... real Kingdee mapping is **EXTERNAL/UNVERIFIED**... code fail-closes" — a **scoped security-contract** claim, not a resume number; (b) P0-05 §17 "P0-01 CI Gate: PASS — coverage 81.70% ≥ 80% gate" — a CI fact, not a resume claim.
- `docs/reports/resume-description.md` still asserts "流式首字 P99 | < 1.8s" — this is **pre-existing** (NOT in the Phase 1 diff; untouched) and remains UNVERIFIED-by-artifact. A pre-existing concern, not a Phase 1 violation.
- Per `CODEX_PROJECT_REMEDIATION_SPEC.md` §0 + `CODEX_REMEDIATION_PLAN.md` §3 Phase 4/5, resume/interview claims stay frozen until Phase 1–4 complete **and** the Claim Matrix is updated. Phase 1 alone does not unfreeze them.

## 14. Evidence Index

| Gate / item | Source | Test | Command | Output | Commit SHA |
|---|---|---|---|---|---|
| GATE-01 | `.github/workflows/ci.yml:110`, `pyproject.toml:85` | `test_pipefail_propagates_failure_through_tail`, `test_fail_under_threshold_is_80`, `test_install_step_pins_full_coverage_toolchain`, `test_surfaces_all_counts_and_subthreshold_coverage_gate_fail` | `.venv/bin/python -m pytest tests/unit/test_ci_contract.py tests/unit/test_ci_summary.py -k "pipefail_propagates or fail_under_threshold or install_step_pins or subthreshold_coverage or coverage_step_preserves"` | 5 passed, exit 0 | `089cefb` |
| GATE-02 | `api/routes/chat.py`, `api/routes/ws.py`, `agents/base_agent.py` | `test_rest_sse_ws_identity_parity`, `test_sse_user_id_is_not_from_client_body`, `test_websocket_passes_authenticated_user_id_to_graph` | `pytest tests/unit/test_sse_identity.py -k "identity_parity or rest_sse_ws or websocket_passes or sse_user_id_is_not_from"` | 5 passed, exit 0 | `089cefb` |
| GATE-03 | `erp/authorization.py` | `test_order_idor_denied`, `test_non_owner_cannot_read_order`, `test_order_owner_can_read_own_order` | `pytest tests/unit/test_erp_authorization.py -k "order_idor_denied or order_owner_can_read_own or non_owner_cannot_read"` | 3 passed, exit 0 | `089cefb` |
| GATE-04 | `cache/cache_policy.py`, `cache/response_cache.py` | `test_l1_user_a_order_not_served_to_user_b`, `test_l2_qdrant_user_isolation`, `test_l3_jaccard_user_isolation`, `test_l2_semantic_cross_user_miss`, `test_public_jaccard_shared_across_users` | `pytest tests/unit/test_cache_cross_user_isolation.py -k "user_a_order_not_served or l3_jaccard or l2_qdrant or l2_semantic_cross_user or public_jaccard_shared"` | 5 passed, exit 0 | `089cefb` |
| GATE-05 | `rag/embedding_status.py`, `rag/qdrant_knowledge_base.py`, `cache/response_cache.py` | `test_embedding_failure_degrades_to_bm25`, `test_embedding_failure_never_generates_random_vector`, `test_embedding_failure_returns_explicit_degraded_result`, `test_normal_retrieval_not_marked_degraded` | `pytest tests/unit/test_p005_embedding_failclosed.py -k "embedding_failure_degrades_to_bm25 or embedding_failure_never_generates_random or embedding_failure_returns_explicit_degraded or normal_retrieval_not_marked"` | 4 passed, exit 0 | `089cefb` |
| P0 matrix | (5 suites above) | (per §5 table) | per-suite file runs | 265 passed / 0 failed | `089cefb` |
| Full regression | ci.yml L108 (exact) | (whole suite) | `PYTHONPATH=scripts .venv/bin/python -m pytest tests/unit/ tests/integration/ tests/e2e/ --cov=agents ... --cov=tools -p pytest_counts -m "not real_llm and not stress" --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear` | 1634 collected / 1633 passed / 0 failed / 1 deselected / 18 warnings / 81.57% / exit 0 | `089cefb` |
| CROSS-01..04 | (cross-issue tests) | (per §6) | per-test `-k` runs | 5+4+4+4 passed | `089cefb` |
| Toolchain pin | ci.yml L44 | `test_install_step_pins_full_coverage_toolchain` | (GATE-01) | PASS under pytest 7.4.4/coverage 7.4.4 | `089cefb` |

> **Reproducibility note:** the local `.venv` had drifted to pytest 9.0.3/coverage 7.14.1 (not the
> CI pin). The pinned-toolchain numbers above were produced by a temporary in-place downgrade of
> `.venv` to pytest 7.4.4/coverage 7.4.4; the `.venv` was **restored to 9.0.3/7.14.1** afterward and
> left as found. CI itself installs the pinned versions fresh each run (ci.yml L44), so the drift
> does not affect the CI gate — only local reproduction claims.

## 15. Phase Readiness Recommendation

**NOT_READY_FOR_INDEPENDENT_GATE**

There is **no P0 security/correctness regression at HEAD `089cefb`** — all five code contracts hold
(GATE-01..05 PASS; P0 matrix 265/265 PASS, 0 failed; full regression 1633 passed / 0 failed /
81.57% / exit 0; cross-issue composition CROSS-01..05 PASS; all 15 security invariants PASS; no
test-weakening, no `|| true`, `fail_under=80` intact; scope clean; claim freeze intact).

However, the Phase 1 DoD is **not literally satisfied** on the acceptance-record dimension:

### BLOCKING ISSUES (acceptance-record / evidence-level — NOT code regressions)

1. **P0-01 and P0-04 lack dedicated completion/acceptance reports.** Acceptance is only
   cross-referenced inside P0-02/03/05 reports. DoD item 1 ("五个 P0 Acceptance Report 都存在且曾
   PASS") is not literally met for these two. Likely owner: the original P0-01/P0-04 implementer.
   Evidence: `docs/audit/` contains only `P0_02/03/05_COMPLETION_REPORT.md`; no `P0_01`/`P0_04`
   reports exist on any branch.
2. **P0-05 Codex re-review #3 verdict unresolved.** The committed report documents its last Codex
   verdict as **FAIL** (contradicted by the implementer's independent spy against the current working
   tree — corroborated by this session's re-verification). There is **no clean post-#3 Codex ACCEPTED**
   recorded. Likely owner: Codex (independent reviewer). Evidence: `P0_05_COMPLETION_REPORT.md` §0.
3. **Evidence level = MOCK/LOCAL-FIXTURE only.** No real Redis/Qdrant-server/ERP/Embedding/LLM was
   exercised (§8). Phase 3 real-run verification is not done. This is **expected for Phase 1** per
   the plan, but must be stated so the gate reviewer does not mistake mock-pass for
   production-verified. Likely owner: Phase 3.

### NON-BLOCKING OBSERVATIONS (reproducibility — for the reviewer to note)

- The local `.venv` had **drifted** from the CI-pinned toolchain (pytest 9.0.3 vs CI 7.4.4). CI
  reinstalls pinned versions each run, so the gate is unaffected, but the drift means local runs
  outside CI are not automatically CI-faithful.
- The P0-05 report's headline numbers (1656 collected / 1651 passed / 81.70%) came from a
  **non-CI command** (`pytest tests/ --cov=.`). The actual ci.yml command yields 1634/1633/81.57%.
  Both pass; the report's figures should be reconciled to the CI command.

### NEXT_REQUIRED_ACTION

**CODEX PHASE 1 INDEPENDENT GATE REVIEW.** Recommended scope for the reviewer:
1. Re-run the P0-05 re-review #3 against **HEAD `089cefb`** and issue a clean verdict (ACCEPTED or
   specific FAIL with repro) — the implementer's spy + this session's EMB-1..4/CROSS-04 evidence
   indicate ACCEPTED, but the committed record does not state it.
2. Adjudicate the P0-01/P0-04 acceptance-record gap: either accept the cross-referenced acceptance
   (P0-05 §17, P0-03 §15) as sufficient, or require standalone acceptance reports.
3. Note the mock/local-fixture evidence level and defer real-run verification to Phase 3.
4. Note the `.venv` drift + non-CI headline-number discrepancy (reproducibility hygiene).

**PHASE 1 PRE-GATE = BLOCKED (acceptance-record + evidence-level), NOT a code regression.**
P1 work remains **forbidden** until the gate is cleared. The consolidator did **not** fix anything
this round (per VERIFY FIRST): no production code, tests, coverage threshold, or historical
acceptance report was modified.
