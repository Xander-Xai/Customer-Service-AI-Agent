# Phase 1 Remediation Addendum (BF-01 / BF-02 / BF-03)

> **Status:** Fix-cycle output in response to `PHASE1_INDEPENDENT_GATE_REPORT.md` (FAIL).
> **Scope:** Address the three blocking findings BF-01 (P0-04), BF-02 (P0-03), BF-03 (P0-05)
> and add the corresponding regression coverage. No P1/P2 work. No historical acceptance
> report modified.
> **Rule:** TDD (RED first → minimal fix → GREEN), scoped to each Issue.
> **Date:** 2026-08-16.

## 1. Context

The independent Phase 1 Gate returned **FAIL** with three current-HEAD contract failures
reproducible by red-team probes (not test-suite gaps alone). This addendum records the
fix-cycle: reproduce each bypass (RED), apply the minimal scoped fix (GREEN), and re-verify
the full regression. The completion report's earlier "no P0 regression at HEAD" conclusion
was wrong precisely because it relied on the existing test suites, which did not exercise
these bypass paths.

## 2. Blocking Findings — Root Cause & Remediation

### BF-01 — P0-04 WebSocket trusted identity lost when `session_manager=None`

- **Root cause:** `api/routes/ws.py` gated JWT-sub extraction on
  `if session_manager and ws_jwt_payload:`. Identity derivation (the JWT `sub`) was
  coupled to the *optional* session-storage dependency. `create_app(...,
  session_manager=None)` is a supported construction, so a valid authenticated JWT
  subject was dropped to `user_id=""` before `run_graph`.
- **Fix (minimal):** extract identity into a pure `derive_ws_user_id(ws_jwt_payload)`
  function, independent of `session_manager`. `None` payload (api-key / DEV_MODE
  anonymous path) → `""` (allowed anonymous system-auth). A presented JWT with a usable
  `sub` → the subject. A presented JWT (dict, incl. empty `{}`) with no usable `sub` →
  raise `ValueError` (fail-closed; the route closes the connection 4001). `set_user_id`
  remains gated on `session_manager` (session storage, legitimately optional).
  - **TDD catch:** the first implementation used `if not ws_jwt_payload:` (empty-dict
    falsy), which let `derive_ws_user_id({})` return `""` instead of failing closed.
    The `{}` regression test caught it; fixed to `is None` to distinguish "no JWT
    presented" (anonymous) from "JWT presented, no subject" (fail-closed).
- **Files:** `api/routes/ws.py` (new `derive_ws_user_id`, route uses it).
- **RED→GREEN:** 4 new tests in `test_sse_identity.py::TestWSIdentityIndependentOfSessionManager`
  + `::TestDeriveWsUserId`. RED confirmed (`user_id=""` not the sub; missing-sub did not
  raise). GREEN: all pass.

### BF-02 — P0-03 direct ERP tool raw-adapter bypass

- **Root cause:** `tools/erp_tools.py::create_erp_tools(erp_adapter)` duck-typed any
  adapter; `_query_order` / `_query_customer` called `erp_adapter.query_order(...)` /
  `query_customer(...)` directly. `core.container` wires the `ErpAuthorizationService`,
  but the factory itself did not enforce it — so `create_erp_tools(KingdeeMockAdapter())`
  disclosed another user's order with no ownership check.
- **Fix (minimal):** the private handlers (`_query_order`, `_query_customer`) fail closed
  (safe not-found, indistinguishable from `not_found` / `non_owner` per AUTHZ-5) unless
  `isinstance(erp_adapter, ErpAuthorizationService)`. Public tools (`query_product`,
  `query_inventory`) remain separately scoped and still accept a raw adapter. Registration
  (`list_tools`) is unchanged.
- **Files:** `tools/erp_tools.py` (import + 2 guards).
- **Existing-test update:** `tests/unit/test_auth_tools_coverage.py::TestERPTools` (3
  formatting tests) previously exercised the now-closed bypass via a raw `AsyncMock`. They
  are updated to build the registry from the AuthZ-wrapped adapter (the container's real
  wiring) with an authenticated `principal` fixture + passing ownership mapping, so they
  exercise the *authorized* formatting path. `test_query_order_found` mock now includes
  `customer_id` to pass the AuthZ defensive scope filter. (Public-tool + registration
  tests unchanged.)
- **RED→GREEN:** 2 new tests in `test_erp_authorization.py::TestRawAdapterBypassDenied`.
  RED confirmed (`李女士 / 486.0元` and `139****5678` disclosed). GREEN: all pass.

### BF-03 — P0-05 wrong-dimension precomputed vector reaches Qdrant

- **Root cause:** `rag/qdrant_knowledge_base.py::query_with_vector()` dispatched
  `query_vector` to `self._client.search(...)` without calling `validate_embedding_vector`.
  The internal caller (`query_multiple`) pre-validates via `_embed_texts`, so the gap was
  only on the external precomputed-vector entrypoint.
- **Fix (minimal):** call the shared `validate_embedding_vector(query_vector,
  _EMBEDDING_DIM, model=...)` at the top of `query_with_vector`, before any Qdrant
  dispatch. Invalid input raises `EmbeddingDimensionError` (consistent with `_embed_texts`);
  no-op on the internal path (already-validated vector). (Earlier completion-report note
  that this was "low-risk" was incorrect — the gate correctly retained it as P0-05.)
- **Files:** `rag/qdrant_knowledge_base.py` (1 validation call).
- **RED→GREEN:** 1 new test in
  `test_p005_embedding_failclosed.py::TestQueryWithVectorValidatesPrecomputedVector`.
  RED confirmed (`DID NOT RAISE`; `search` called with length-2 vector). GREEN: raises
  `EmbeddingDimensionError`, `search.call_count == 0`.

## 3. Post-Fix Verification (CI-pinned toolchain: pytest 7.4.4 / coverage 7.4.4)

> Reproduced via a temporary in-place downgrade of the local `.venv` (which had drifted to
> pytest 9.0.3 / coverage 7.14.1). The `.venv` was **restored to 9.0.3 / 7.14.1** afterward
> — left as found. CI reinstalls the pinned versions fresh each run, so the gate is
> unaffected by the local drift.

### New BF regression tests (7 total)
- BF-01: `test_ws_authenticated_jwt_identity_reaches_graph_without_session_manager`,
  `test_extracts_subject_from_authenticated_jwt`,
  `test_no_jwt_payload_is_anonymous_allowed`,
  `test_authenticated_jwt_without_subject_fails_closed` — 4 PASS.
- BF-02: `test_create_erp_tools_raw_adapter_denies_private_order`,
  `test_create_erp_tools_raw_adapter_denies_private_customer` — 2 PASS.
- BF-03: `test_query_with_vector_rejects_wrong_dimension` — 1 PASS.

### 5 Required Gate Tests (named semantics, post-fix)
- GATE-01 CI: 4/4 PASS (pipefail, fail_under=80, toolchain pin, subthreshold-gate-fail).
- GATE-02 Identity: 9 PASS (incl. WS session_manager=None + derive_ws_user_id).
- GATE-03 ERP AuthZ: 5 PASS (incl. raw-adapter denial).
- GATE-04 Cache: 5 PASS (L1/L2/L3 + public FAQ).
- GATE-05 Embedding: 3 PASS (incl. wrong-dimension rejection).

### Cross-issue composition (post-fix)
- CROSS-01 identity→AuthZ: 4 PASS. CROSS-02 identity→cache: 3 PASS.
- CROSS-04 embedding→cache: 3 PASS. (CROSS-03/05 covered by GATE-03/01.)

### Full repository regression (exact ci.yml command)
| Metric | Value |
|---|---:|
| Collected | 1641 (+7 new BF tests vs 1634 pre-fix) |
| Passed | 1640 |
| Failed | **0** |
| Skipped | 0 |
| XFailed | 0 |
| Deselected | 1 (`@pytest.mark.stress`) |
| Warnings | 18 (unchanged) |
| Coverage | **81.57%** (TOTAL 9055 / 1669 missed; `fail_under=80` gate PASS) |
| Exit Code | **0** |
| Runtime | 156.89s (no hang — the removed flaky WS test is gone) |

### Lint
- `ruff` on changed files: the one introduced finding (SIM117 nested `with`) was fixed
  (combined context managers). Three remaining findings (B007, B905 in
  `rag/qdrant_knowledge_base.py:93,346`; F401 `asyncio` in
  `tests/unit/test_auth_tools_coverage.py:5`) are **pre-existing at HEAD** (confirmed via
  `git show HEAD:<file> | ruff check`) in lines untouched by this remediation — left per
  "不顺便重构无关模块". ruff is not a CI gate step.

## 4. Files Changed

Production (3): `api/routes/ws.py`, `tools/erp_tools.py`, `rag/qdrant_knowledge_base.py`.
Tests (4): `tests/unit/test_sse_identity.py`, `tests/unit/test_erp_authorization.py`,
`tests/unit/test_p005_embedding_failclosed.py`, `tests/unit/test_auth_tools_coverage.py`.

Unrelated working-tree changes left untouched: `secrets/keys.json` (rotation-metadata
timestamp, no secret values), `tests/data/csai.db-shm`/`-wal` (SQLite WAL churn).

## 5. Scope Integrity

No P1/P2 work started or accepted. No RAG unification, BM25 rebuild, stable Qdrant IDs,
agent routing, tool runtime, global deadline, router/circuit-breaker rewrite, or
benchmark/claim-matrix work. The fixes are additive boundary checks + a pure helper — no
algorithm or architecture change.

## 6. Deferred (NOT Phase 1 — unchanged from gate report)

- Real Redis/Qdrant-server/ERP/embedding/LLM verification → Phase 3.
- `api/app.py:252` `httpx` `NameError` (official app startup) and the two unawaited
  `AsyncMock` `RuntimeWarning`s → separate hygiene/runtime work (out of P0 scope).
- P0-01/P0-04 standalone acceptance reports and P0-05 re-review #3 record → acceptance-
  record gaps (the gate supplies a fresh independent record; the three code blockers
  above are now closed).
- Pre-existing ruff findings (B007/B905/F401) → separate lint hygiene.

## 7. Recommendation

Commit the remediation (suggested one commit per Issue — P0-04, P0-03, P0-05 — to keep
the commit boundary clean per the P0-02 AC25 discipline), then **rerun the independent
Phase 1 Gate at the relocked HEAD**. The three BF blockers are closed and covered by
regression tests; the full regression is 1640 passed / 0 failed / 81.57% / exit 0. P1
work remains forbidden until the re-gate passes.
