# Phase 1 Independent Gate Report

> **Reviewer:** Codex — Independent Phase Gate Reviewer + Security/Correctness Red Team  
> **Date:** 2026-08-16  
> **Review rule:** `READ → TRACE → ATTACK → VERIFY → ACCEPT / REJECT`  
> **Scope:** P0-01, P0-04, P0-03, P0-02, P0-05 only.  
> **Write boundary:** No production code, tests, fixtures, CI configuration, coverage threshold, or historical acceptance report was modified.

## 1. Verdict

**FAIL**

The positive P0 test matrix and the CI false-green contract pass at the reviewed
HEAD, but independent red-team probes found three explicit current-HEAD gate
failures:

1. P0-04: a valid WebSocket JWT identity is lost when the supported
   `session_manager=None` app state is used.
2. P0-03: constructing the ERP tool registry with a raw adapter bypasses the
   authorization service and discloses another user's order.
3. P0-05: the public `query_with_vector()` path sends a wrong-dimension vector
   to Qdrant instead of rejecting it.

These are current-checkout security/correctness failures, not claims inferred
from the completion report. The first two are not introduced by the
`5be0668..HEAD` Phase diff, but Phase 1 is a current-HEAD gate and the required
contracts still fail on those paths.

**PHASE 1 GATE = FAIL**

**PHASE 1 SECURITY & CORRECTNESS = NOT ACCEPTED**

`P1-03 Stable Qdrant IDs` is **not eligible to start**. No P1 work was started
in this review.

## 2. Checkout

| Field | Value |
|---|---|
| Branch | `agent/p0-02-p0-03-remediation` |
| Upstream | `origin/agent/p0-02-p0-03-remediation` is gone |
| HEAD | `089cefbeb91950c2bd60bda734b127dd0325b208` (`fix(rag): fail closed on embedding unavailability (P0-05)`) |
| Phase Base | `5be0668` (`docs: add CI remediation audit spec and plan`) |
| Phase End | `089cefb` |
| Phase diff | `5be0668..HEAD`: 39 files, `+6554/-247`; all committed changes map to P0-01..05 by static scope review |
| Working tree before this report | `M secrets/keys.json`, `M tests/data/csai.db-shm`, `M tests/data/csai.db-wal`, `?? docs/audit/PHASE1_COMPLETION_REPORT.md` |
| Working tree treatment | The secret file was not read or staged. SQLite WAL churn and the completion report were left untouched. This report is the only file created by this review. |

The Phase diff passed `git diff --check`. No unrelated user file was staged or
published.

## 3. Individual Issue Re-validation

### P0-01 — CI False Green: **PASS**

Current source and CI-equivalent probes prove that the blocking coverage lane
preserves failure status:

- `TestFalseGreenMechanism::test_pipefail_propagates_failure_through_tail` — exit 0.
- `TestExitCodePropagation::test_coverage_step_preserves_pytest_exit_code` — exit 0.
- `TestCoverageGate::test_fail_under_threshold_is_80` — exit 0; `pyproject.toml:85` remains `fail_under = 80`.
- Intentional pytest failure — direct exit **4**.
- The same failure through `tail` without `pipefail` — exit **0**.
- The same failure through `tail` with `set -o pipefail` — exit **4**.
- A real sub-threshold coverage invocation (`--cov-fail-under=99`) — exit **1**.
- The exact CI coverage invocation at the end of this review — exit **0**.

The blocking pytest/coverage step has no `continue-on-error`. The summary,
gradual mypy, and pip-audit steps are explicitly informational; the latter
does not use `|| true`.

**Evidence level:** CODE VERIFIED + UNIT VERIFIED + CI-equivalent subprocess
VERIFIED. This is not a remote GitHub Actions runner result.

### P0-04 — SSE/REST/WebSocket Identity Context: **FAIL**

The normal existing fixture path passes:

- `test_sse_identity.py` — 11 passed, exit 0.
- Selected transport/Graph chain probes — 7 passed, 4 deselected, exit 0.
- REST, SSE, multimodal REST, multimodal SSE, WebSocket, client-body spoofing,
  and `_run_graph` state capture all passed under the existing fixtures.

The source chain is present for the normal path:

`extract_user_id()` → `AuthenticatedSession.user_id` →
`_build_sse_stream_context(..., user_id=...)` → `run_graph(..., user_id=...)` →
`AgentState["user_id"]`.

The independent negative probe exposed a missing boundary case. With a valid
JWT payload `{ "sub": "jwt-user-without-session-manager" }` and the current
route state `session_manager=None`, the WebSocket route calls:

```text
run_graph(..., user_id="")
```

The probe result was `FAIL_IDENTITY_LOST`, process exit 0. The cause is the
current conditional in `api/routes/ws.py`:

```python
if session_manager and ws_jwt_payload:
    ws_uid = ws_jwt_payload.get("sub", "")
```

`api.app.create_app()` accepts `session_manager=None`, so this is a reachable
current-code construction, not a client spoof. The normal `api.app_factory`
entrypoint injects a session manager, which explains why the existing parity
test passes; it does not make the identity contract unconditional.

**Minimum required remediation — do not implement in this review:** derive the
trusted WS identity from the authenticated JWT independently of the optional
session-storage dependency; explicitly fail closed if the authenticated JWT
has no usable subject; add a regression case for authenticated JWT plus
`session_manager=None`.

**Evidence level:** CODE VERIFIED + UNIT/LOCAL-FIXTURE VERIFIED; the normal
production entrypoint was not E2E-verified. The official app lifespan also
hit a separate pre-existing `httpx` `NameError` at `api/app.py:252`, so no
production-startup claim is made.

### P0-03 — ERP IDOR / Authorization: **FAIL**

The wrapped AuthZ path passes the expected positive and negative tests:

- owner → own order allowed — pass;
- User A → User B order denied — pass;
- missing identity — fail closed — pass;
- prompt/customer/tool argument override — denied — pass;
- Billing/Aftersales share the AuthZ boundary — pass;
- direct tool call using an **AuthZ-wrapped** adapter without a principal —
  denied — pass.

However, the required “Direct ERP Tool bypass = denied” attack fails at the
public tool-factory boundary. This current probe:

```text
registry = create_erp_tools(KingdeeMockAdapter())
registry.execute("query_order", {"order_id": "ORD20260530002"})
```

returned:

```text
订单: ORD20260530002 | 客户: 李女士 | 状态: 待发货 | 金额: 486.0元 ...
```

The raw-adapter probe verdict was `FAIL_RAW_ADAPTER_BYPASS`, process exit 0.
`ServiceContainer` correctly wires `create_erp_tools(self._get_erp_authz())`,
but `tools/erp_tools.py:create_erp_tools()` accepts and directly uses any raw
adapter supplied by a caller. The service-layer authorization is therefore
not an invariant of the tool boundary itself.

**Minimum required remediation — do not implement in this review:** make the
tool factory enforce or require the AuthZ boundary for private ERP tools, and
retain a direct raw-adapter bypass regression test. Public product/inventory
tools may remain separately scoped.

**Evidence level:** CODE VERIFIED + UNIT VERIFIED + MOCK ERP attack VERIFIED.
Real Kingdee ownership mapping was not exercised.

### P0-02 — Cache Cross-User Leakage: **PASS**

The current CachePolicy and ResponseCache code apply the same scope/version/TTL
policy to L1 Redis, L3 Jaccard, and L2 semantic lookup. Independent tests pass
for:

- same-query private response isolation at L1;
- L3 lexical isolation;
- L2 Qdrant scope isolation;
- semantic-equivalent cross-user miss;
- all-three-layer personalized isolation;
- missing identity / unknown intent fail-closed;
- public FAQ shared-cache positive behavior;
- TTL parity and version mismatch invalidation.

The required P0 cache suite passed `87/87`; the full P0 matrix also passed.

**Evidence level:** CODE VERIFIED + UNIT VERIFIED with `FakeRedis`,
`FakeQdrant`, and injected deterministic test embedding. No real Redis cache
write/read or Qdrant server semantic query was performed.

### P0-05 — Random Embedding Fallback: **FAIL**

The provider-failure contract passes for the covered paths:

- no random/pseudo-random vector;
- no fake Qdrant query on embedding failure;
- BM25-ready → explicit degraded result;
- no channel → explicit degraded result;
- semantic cache embedding failure → no L2 query/upsert;
- malformed provider vectors (wrong dimension, NaN/Inf, non-numeric) rejected;
- normal available embedding path remains non-degraded.

The target P0-05/Qdrant suite passed `50/50`, and the prior Codex re-review #3
claim that malformed semantic-cache values reach Qdrant is not reproduced by
the current `_embed_query()` path.

The broader required contract still fails at the public precomputed-vector
boundary. An independent probe called:

```text
await kb.query_with_vector("c", [0.1, 0.2], n_results=1)
```

with a two-element vector. The mocked Qdrant `search()` was called with that
two-element vector. Result: `FAIL_WRONG_DIMENSION_REACHED_QDRANT`, process exit
0. `rag/qdrant_knowledge_base.py:394-415` does not call
`validate_embedding_vector()` before dispatching the vector.

This is an explicit violation of the requested “wrong dimension rejected” and
“no fake/wrong vector query” boundary, even though the normal embedding
provider path validates its own output.

**Minimum required remediation — do not implement in this review:** validate
all externally supplied precomputed vectors at `query_with_vector()` before
Qdrant dispatch; on invalid input, reject or return an explicit degraded/error
result without querying Qdrant; add a direct wrong-dimension regression test.

**Evidence level:** CODE VERIFIED + UNIT/LOCAL-FIXTURE VERIFIED with a mocked
Qdrant client. No real Qdrant server or embedding provider was exercised.

## 4. Required Gate Tests

The specification names five functions. Three exact functions exist; two exact
names do not exist in the current test tree, so their semantic equivalents are
reported explicitly rather than being silently treated as present.

| Test | Result | Exit Code | Evidence |
|---|---|---:|---|
| `test_ci_fails_when_coverage_below_threshold` | PASS — exact name absent; sub-threshold subprocess equivalent passed | `1` intentional coverage failure; contract test `0` | `TestCoverageGate::test_fail_under_threshold_is_80`, `TestBuildSummary::test_surfaces_all_counts_and_subthreshold_coverage_gate_fail`, real `--cov-fail-under=99` probe |
| `test_cross_user_cache_isolation` | PASS — exact name absent; layer-specific equivalents passed | 0 | `test_all_three_layers_isolate_personalized`, L1/L2/L3 tests, 87/87 P0-02 suite |
| `test_order_idor_denied` | PASS as wrapped-service test | 0 | `TestResourceOwnership::test_order_idor_denied`; separate raw-tool attack is a P0-03 FAIL |
| `test_sse_rest_identity_parity` | PASS in existing fixture; FAIL on missing-session-manager WS edge | 0 for fixture; probe result `FAIL_IDENTITY_LOST` | Current function is `test_rest_sse_identity_parity`; direct WS negative probe exposes the uncovered boundary |
| `test_embedding_failure_degrades_to_bm25` | PASS | 0 | `TestDegradedRetrieval::test_embedding_failure_degrades_to_bm25`; separate direct wrong-vector attack is a P0-05 FAIL |

## 5. Cross-Issue Red-Team Results

### Identity → AuthZ

**PASS for the wrapped path.** Fifteen targeted tests passed (exit 0): owner
positive, non-owner denial, missing principal, prompt/model override, tool
argument override, Billing/Aftersales parity, and principal lifecycle reset.
The trusted principal is `state["user_id"]` → `bound_erp_request()` →
`erp_principal` ContextVar → server-side customer mapping.

**FAIL for the independent WS edge** described in P0-04: the trusted JWT
identity can be reduced to an empty principal before the graph runs.

### Identity → Cache

**PASS.** Nine targeted cache-composition tests passed (exit 0). Graph cache
read uses `state.get("user_id")`; non-public/missing-intent responses do not
fall back to GLOBAL/shared scope. Public FAQ remains explicitly shareable.

### AuthZ → Cache

**PASS for the implemented cache boundary.** L1/L2/L3 private scope tests,
semantic-equivalent cross-user miss, and cache invalidation scope tests pass.
No tested cache path returned User A's private response to User B before ERP
authorization.

**Separate FAIL:** the ERP tool boundary itself can bypass AuthZ when a raw
adapter is passed to `create_erp_tools()`. This is P0-03, not a demonstrated
cache leak.

### Embedding Failure → Cache

**PASS for embedding failure.** Eight targeted tests passed (exit 0): L2 is
skipped without fake query/upsert, L1 remains available, and user scope is
preserved.

**Separate FAIL:** a caller-supplied wrong-dimension vector reaches Qdrant via
`query_with_vector()`. This is a P0-05 vector-boundary failure even though the
provider-failure semantic-cache path is fail-closed.

### CI → Security Tests

**PASS for tests currently present:** a failing pytest command and a failing
coverage gate make the blocking CI-equivalent command non-zero; the exact full
lane exited 0 only when all selected tests and coverage passed.

**Evidence gap:** the three newly exposed boundary attacks are not represented
by current required tests, so the current CI suite can remain green while
those attacks exist. Adding tests is part of minimum remediation, but is
forbidden in this review.

## 6. Security Invariant Matrix

| Invariant | Result | Evidence level / note |
|---|---|---|
| CI pytest failure propagates | PASS | Shell probe + CI contract unit |
| Coverage below threshold propagates | PASS | Real sub-threshold pytest/coverage invocation exits 1 |
| REST/SSE identity parity | PASS | Current route tests |
| WebSocket identity reaches graph in normal fixture | PASS | Current WebSocket test |
| WebSocket identity unconditional when session manager is absent | **FAIL** | Current route-level red-team probe |
| Client/body identity cannot override authenticated identity | PASS | SSE spoof test and AuthZ prompt/tool tests |
| ERP owner can read own order | PASS | Mock ERP wrapped AuthZ test |
| ERP non-owner cannot read order | PASS | Wrapped-service test |
| Missing ERP principal fails closed | PASS | Wrapped-service/direct wrapped-tool tests |
| Raw ERP tool cannot bypass AuthZ | **FAIL** | `create_erp_tools(KingdeeMockAdapter())` returned User B order |
| L1 private cache is user-scoped | PASS | FakeRedis current tests |
| L2 private semantic cache is user-scoped | PASS | API-faithful FakeQdrant current tests |
| L3 private lexical cache is user-scoped | PASS | Current tests |
| Public FAQ remains shareable | PASS | Positive L1/L2/L3 policy tests |
| Missing identity cannot become GLOBAL for private data | PASS | Unknown/missing intent tests |
| Embedding failure creates no synthetic vector | PASS | Current P0-05 tests |
| Embedding failure does not query semantic Qdrant | PASS | Fake-Qdrant call counters |
| Provider wrong-dimension output is rejected | PASS | `EmbeddingDimensionError` tests |
| Precomputed wrong-dimension vector is rejected | **FAIL** | `query_with_vector()` dispatched length 2 |
| BM25 fallback/no-channel semantics are explicit | PASS | P0-05 degraded-result tests |

## 7. Full Regression

### P0 regression matrix

CI-pinned overlay: pytest 7.4.4, pytest-asyncio 0.21.1, pytest-cov 5.0.0,
coverage 7.4.4. The existing `.venv` remains at its original drifted versions
(pytest 9.0.3, pytest-asyncio 1.4.0, pytest-cov 7.1.0, coverage 7.14.1).

| Suite | Collected | Passed | Failed | Skipped | XFailed | Deselected | Exit |
|---|---:|---:|---:|---:|---:|---:|---:|
| P0-01 CI contract + summary | 40 | 40 | 0 | 0 | 0 | 0 | 0 |
| P0-04 SSE/identity | 11 | 11 | 0 | 0 | 0 | 0 | 0 |
| P0-03 ERP AuthZ + mapping | 77 | 77 | 0 | 0 | 0 | 0 | 0 |
| P0-02 cache isolation + policy | 87 | 87 | 0 | 0 | 0 | 0 | 0 |
| P0-05 embedding + Qdrant | 50 | 50 | 0 | 0 | 0 | 0 | 0 |
| **Total** | **265** | **265** | **0** | **0** | **0** | **0** | **0** |

### Exact CI coverage invocation

```text
python -m pytest tests/unit/ tests/integration/ tests/e2e/ -v --tb=short \
  --cov=agents --cov=alerts --cov=api --cov=auth --cov=cache \
  --cov=collaboration --cov=core --cov=db --cov=erp --cov=knowledge \
  --cov=llm --cov=media --cov=rag --cov=router --cov=tools \
  --cov-report=term-missing --cov-report=html --cov-report=xml \
  --junit-xml=pytest-report.xml -p pytest_counts \
  -m "not real_llm and not stress" \
  --ignore=tests/e2e/test_e2e_real_llm.py --cache-clear
```

| Metric | Result |
|---|---:|
| Collected | 1634 |
| Passed | 1633 |
| Failed | 0 |
| Skipped | 0 |
| XFailed | 0 |
| Deselected | 1 (`@pytest.mark.stress`) |
| Warnings | 18 |
| Coverage | 81.57% (`coverage.xml`, 9035 statements / 1665 missed) |
| Coverage threshold | 80%, reached; this is separate from CI gate correctness |
| Exit Code | **0** |

The P0-05 report headline `1656/1651/5 skipped/81.70%` came from the
non-CI `pytest tests/ --cov=.` command. The authoritative current CI command
is `1634/1633/1 deselected/81.57%`.

## 8. Warning / Hidden Failure Review

| Finding | Classification | Evidence |
|---|---|---|
| Starlette `httpx` TestClient deprecation | NON-BLOCKING | 1 warning in full lane |
| Qdrant remote compatibility warning | NON-BLOCKING / MOCK ENVIRONMENT | 15 warnings; no Qdrant server was available |
| Two unawaited `AsyncMock` `RuntimeWarning`s | NON-BLOCKING, DEFERRED hygiene | `test_base_agent_streaming.py::test_process_with_tools_no_tool_call_no_extra_events` and `test_modules.py::TestStreamingLLM::test_process_with_llm_stream_uses_callback`; each still exits 0 under `-W error::RuntimeWarning`, but the interpreter emits the warning at shutdown |
| Unknown pytest markers | PASS / none observed | No `PytestUnknownMarkWarning`; marker contract passed |
| `skip` / `xfail` | PASS for selected CI lane | 0 skipped, 0 xfailed; real-LLM file is intentionally ignored and one stress test is deselected by the CI expression |
| CI `continue-on-error` | PASS for blocking tests | Only summary, gradual mypy, and pip-audit are informational; pytest/coverage steps are not softened |
| CI `|| true` | PASS for workflow gate | No `|| true` in CI run steps. `scripts/backup.sh` contains unrelated cleanup fallbacks and is not the Phase 1 blocking lane |
| Official `create_app` lifespan | DEFERRED / OUT OF P0 SCOPE but blocks official-app E2E evidence | `api/app.py:252` references `httpx.AsyncClient` without an import; the startup probe raised `NameError: httpx is not defined` |

No warning was promoted to a P0 blocker above the three explicit red-team
failures. The official app startup issue must not be described as production
E2E verification.

## 9. Scope Integrity

**PASS — no P1/P2 implementation was accepted or started.** Static diff review
and current source search found no Phase-diff implementation of:

- P1-01 unified `RetrievalRequest`/`RetrievalTrace`/`retrieve()`;
- P1-02 BM25 restart rebuild;
- P1-03 stable Qdrant IDs;
- P1-04 Agent reachability/routing boundary;
- P1-05 full Tool Runtime contract;
- P1-06 global deadline/recursion/retry budget;
- P1-07 router/circuit-breaker semantic rewrite;
- P2 benchmark, FCR, labor-efficiency, dataset, or claim-matrix work.

The `query_with_vector()` finding is retained as P0-05 because it is the
embedding/vector safety boundary explicitly required by this gate; it is not
reclassified as P1 retrieval unification.

## 10. Evidence Levels

| Area | Highest accepted evidence | Not established |
|---|---|---|
| P0-01 | CODE + UNIT + local subprocess/CI-equivalent | Remote GitHub runner result |
| P0-04 | CODE + UNIT/LOCAL-FIXTURE route and Graph-state capture | Official app startup/E2E; WS optional-dependency edge fails |
| P0-03 | CODE + UNIT + KingdeeMockAdapter attack | Real Kingdee ERP/user-customer mapping |
| P0-02 | CODE + UNIT + FakeRedis/FakeQdrant + injected test embedding | Real Redis cache semantics and real Qdrant server |
| P0-05 | CODE + UNIT + fake Qdrant/mock embedding | Real embedding provider and real Qdrant |
| Full regression | Local CI-pinned overlay, raw exit 0, XML/JUnit/counts inspected | Multi-version remote matrix |

Redis at `127.0.0.1:6379` answered a read-only `PING`, but no cache writes or
reads were issued against it. Qdrant ports 6333/6334 were unavailable. The
test environment declares `ERP_MODE=mock` and uses placeholder embedding/LLM
configuration. All security conclusions above remain mock/local-fixture
evidence, not production proof.

## 11. Claim Freeze Review

**FROZEN — PASS.** This review did not unfreeze or upgrade any P99, FCR, Hit
Rate, Token savings, labor-efficiency, or full-RAG-pipeline claim.

The 81.57% number is a current CI-lane coverage result only; it is not a
resume metric. Historical `81.70%` and other non-CI headlines remain
historical snapshots and were not substituted for the exact CI result.

Per the remediation specification, resume/interview numbers remain frozen
until the later Phase 4 evidence and Claim Matrix work is complete.

## 12. Phase Gate Matrix

| Gate | Result | Reason |
|---|---|---|
| PG1 pytest non-zero fails CI | PASS | Direct exit 4 and `pipefail` exit 4 |
| PG2 coverage threshold failure fails CI | PASS | Real sub-threshold invocation exit 1; `fail_under=80` intact |
| PG3 REST/SSE/WS identity contract consistent | **FAIL** | Existing fixtures pass, but valid JWT identity is lost on the supported missing-session-manager WS path |
| PG4 client identity cannot override authenticated identity | PASS | Body/prompt/model/tool override tests pass; the WS edge loses identity rather than accepting client identity |
| PG5 non-owner ERP resource fails closed | PASS for wrapped path | Wrapped AuthZ denies owner mismatch |
| PG6 model/tool owner parameter cannot override auth identity | **FAIL** | Raw ERP tool factory bypasses the AuthZ boundary and returns another user's order |
| PG7 private response not cross-user cached | PASS | L1/L2/L3 isolation tests pass |
| PG8 L1/L2/L3 scope policy consistent | PASS | Shared `CachePolicy`, scope key, version, and TTL tests pass |
| PG9 public FAQ can share | PASS | Positive shared-cache tests pass |
| PG10 cache cannot bypass ERP AuthZ | PASS in tested cache path | No cache cross-user read observed; raw tool bypass is tracked under PG6/P0-03 |
| PG11 embedding failure does not create random/fake vector | PASS for provider-failure path | No synthetic vector emitted in tested failure paths |
| PG12 semantic cache failure fails closed/degrades safely | PASS | L2 query/upsert skipped; L1 behavior preserved |
| PG13 BM25 available gives explicit degraded fallback | PASS | `retrieval_degraded=true`, BM25-only result |
| PG14 no retrieval channel is explicit degraded/error | PASS | `no_retrieval_channel` metadata path |
| PG15 five formal required gate tests pass | **FAIL** | Three exact names exist; two specified names are absent and only semantic equivalents exist |
| PG16 P0 regression suite passes | PASS as executed suite | 265/265 passed; newly found direct boundary attacks are not represented in that suite |
| PG17 full repository regression executed and reported honestly | PASS | 1634 collected / 1633 passed / 1 deselected / 18 warnings / 81.57% / exit 0 |
| PG18 no skip/xfail/threshold weakening for green | PASS | No blocking-step weakening; no workflow `|| true` |
| PG19 no explicit P0 security/correctness regression | **FAIL** | P0-04, P0-03, and P0-05 red-team failures are reproducible at HEAD |
| PG20 no P1/P2 result incorrectly upgraded to proven claim | PASS | Scope clean and Claim Freeze remains FROZEN |

## 13. Blocking Findings

### BF-01 — P0-04 WebSocket trusted identity loss

- `OWNER_ISSUE = P0-04`
- Reproduction: valid JWT `sub`, `session_manager=None`, current WS route.
- Observed: `run_graph(..., user_id="")`.
- Impact: authenticated identity does not reach Graph state/runtime context.
- `MINIMUM_REQUIRED_REMEDIATION`: make JWT-sub propagation independent of
  optional session storage; define missing-sub behavior; add the missing
  negative regression test. Do not patch during this gate review.

### BF-02 — P0-03 direct ERP tool raw-adapter bypass

- `OWNER_ISSUE = P0-03`
- Reproduction: `create_erp_tools(KingdeeMockAdapter())` followed by direct
  `query_order` for User B's order with no principal.
- Observed: full mock order disclosure.
- Impact: Tool/Service authorization is not invariant at the public tool
  boundary; container-only wiring is bypassable.
- `MINIMUM_REQUIRED_REMEDIATION`: enforce the AuthZ-wrapped adapter or reject
  raw private-resource adapters at tool construction; add direct raw-factory
  denial coverage. Do not patch during this gate review.

### BF-03 — P0-05 wrong-dimension precomputed vector reaches Qdrant

- `OWNER_ISSUE = P0-05`
- Reproduction: `query_with_vector("c", [0.1, 0.2])`.
- Observed: Qdrant `search()` called with vector length 2.
- Impact: The requested wrong-dimension rejection boundary can be bypassed,
  allowing invalid/fake vector queries.
- `MINIMUM_REQUIRED_REMEDIATION`: validate precomputed vectors before Qdrant
  dispatch and reject/degrade without querying; add direct-path coverage. Do
  not patch during this gate review.

The missing standalone P0-01/P0-04 acceptance reports and the historical P0-05
re-review #3 record are acceptance-record issues, but this report supplies a
fresh independent gate record. They are not used to hide or replace the three
current code-level blockers above.

## 14. Deferred Findings

These are not additional P0 blockers and must not be used to start P1:

- Real Redis/Qdrant-server/ERP/embedding/LLM verification is deferred to the
  planned real-run phase; current mock evidence must not be called production
  proven.
- P1-03 stable Qdrant IDs, P1-02 BM25 restart rebuild, P1-01 retrieval
  unification, P1-04 reachability, P1-05 Tool Runtime, P1-06 global deadline,
  and P1-07 router semantics remain untouched.
- P2-01..P2-06 FCR, labor efficiency, benchmark provenance, RAG metric
  semantics, dataset provenance, and claim-matrix work remain untouched.
- The two unawaited AsyncMock warnings and the `api/app.py` `httpx` startup
  NameError require separate hygiene/runtime work; no fix was made here.

## 15. Evidence Index

### Source and configuration

- `docs/audit/CODEX_PROJECT_REMEDIATION_SPEC.md` — Phase 1 scope, required test
  semantics, acceptance criteria, and claim freeze.
- `docs/audit/CODEX_REMEDIATION_PLAN.md` — implementation order and P0/P1/P2
  boundaries.
- `.github/workflows/ci.yml:31-111` — pinned toolchain, explicit coverage
  sources, `pipefail`, blocking coverage command.
- `pyproject.toml:46-89` — coverage sources and `fail_under = 80`.
- `api/routes/chat.py:88-168,286-306` — authenticated session and SSE
  identity propagation.
- `api/routes/chat_multimodal.py:76-145` — multimodal REST/SSE propagation.
- `api/routes/ws.py:155-161,246-250` — WS identity extraction and failing
  optional-session condition.
- `api/app.py:233-311` — `create_app(..., session_manager=None)` and startup
  lifecycle.
- `core/graph_builder.py:204-216,332-359` — trusted cache identity read/write.
- `erp/authorization.py:206-289` — wrapped ownership checks and fail-closed
  behavior.
- `core/container.py:486-505,528-539` — container AuthZ wiring.
- `tools/erp_tools.py:15-100` — raw adapter accepted by tool factory.
- `cache/cache_policy.py` and `cache/response_cache.py:301-415,641-963,1067-1117` —
  cache scope/version/TTL behavior.
- `rag/embedding_status.py`, `rag/qdrant_knowledge_base.py:225-259,394-415,533-668`,
  and `cache/response_cache.py:702-874` — embedding failure and vector paths.

### Tests and commands

- `tests/unit/test_ci_contract.py`, `tests/unit/test_ci_summary.py` — P0-01
  contract and summary tests.
- `tests/unit/test_sse_identity.py` — REST/SSE/WS/multimodal/Graph-state
  fixture tests.
- `tests/unit/test_erp_authorization.py`, `tests/unit/test_erp_user_mapping.py` —
  P0-03 wrapped AuthZ tests.
- `tests/unit/test_cache_cross_user_isolation.py`,
  `tests/unit/test_cache_policy.py` — P0-02 L1/L2/L3 and public-positive tests.
- `tests/unit/test_p005_embedding_failclosed.py`,
  `tests/unit/test_qdrant_knowledge_base.py` — P0-05 tests.
- CI-pinned overlay versions: pytest 7.4.4, pytest-asyncio 0.21.1,
  pytest-cov 5.0.0, coverage 7.4.4.
- Generated artifacts inspected after the exact CI invocation:
  `pytest-counts.json`, `pytest-report.xml`, `coverage.xml`.
- Raw full-run log/status: `/tmp/phase1-full-status.log`,
  `/tmp/phase1-full-exit-code` (`0`).
- Direct attack probes: `/tmp` command outputs for the WS missing-session
  identity, raw ERP tool, and wrong-dimension vector cases; no repository test
  file was added.

### Commit and worktree evidence

- HEAD: `089cefbeb91950c2bd60bda734b127dd0325b208`.
- Phase base: `5be0668`.
- `git diff --stat 5be0668..HEAD`: 39 files, `+6554/-247`.
- `git diff --check 5be0668..HEAD`: exit 0.
- Final status after verification: only the pre-existing secret/WAL changes,
  the pre-existing completion report, and this independent report are
  uncommitted.

## 16. Final Conclusion

The current checkout demonstrates substantial passing P0 behavior, including
honest CI failure propagation, cache scope isolation, wrapped ERP ownership
checks, and embedding failure degradation. That is not sufficient for Phase 1
acceptance because the independent red-team found three explicit current-HEAD
contract failures:

- P0-04 loses a valid WebSocket identity on an accepted optional-dependency
  construction;
- P0-03 exposes a private ERP order through the raw tool factory;
- P0-05 dispatches an invalid precomputed vector to Qdrant.

**PHASE 1 GATE = FAIL**

**OWNER_ISSUE = P0-04, P0-03, P0-05**

**MINIMUM_REQUIRED_REMEDIATION =** address BF-01, BF-02, and BF-03; add only
the corresponding regression coverage; rerun this independent gate at a
relocked HEAD. Until then, do not start P1-03 or any other P1/P2 work.
