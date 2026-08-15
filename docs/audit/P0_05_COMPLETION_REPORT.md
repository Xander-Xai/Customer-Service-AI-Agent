# P0-05 Completion Report

## 0. Re-review Remediation (post Codex PARTIAL)

> **Re-verification note (2026-08-16):** Codex re-review #3 returned **FAIL**,
> claiming `ResponseCache._embed_query()` "only checks length" and lets
> NaN/Inf/None reach Qdrant (`query_points_calls=1`, `upsert_calls=1`). An
> independent spy replicating that methodology against the **current working
> tree** contradicts the finding: for NaN/Inf/None/non-numeric (each a
> 1024-length vector) `_embed_query` **raises** `EmbeddingDimensionError`
> (`reason=non_finite`/`non_numeric`), and `get()`/`set()` produce
> `query_points_calls=0` and `upsert_calls=0`. The re-review #3 FAIL rationale
> describes the pre-re-review-#2 state (committed HEAD `62ec44b` still has the
> `random.Random(query)` fallback); the working tree already contains the
> re-review-#2 remediation documented below. Evidence:
> `/tmp/p005_spy_verify.py` (independent spy) + 35 target tests pass. Numbers
> below refreshed: 35 target / 1656 collected / 1651 passed / 81.70% coverage
> (was 30 / 1651 collected / 1646 passed).

Codex Independent Reliability Acceptance returned **PARTIAL** with 4 required
fixes. All four are addressed and re-verified in the canonical `.venv` env:

1. **Typed invalid contract** — `validate_embedding_vector` now wraps
   non-numeric elements (`ValueError`/`TypeError`) into
   `EmbeddingDimensionError(reason="non_numeric")` instead of propagating a bare
   `ValueError`. Added `EmbeddingStatus.from_error()` so `INVALID` is reachable
   (returned for `EmbeddingDimensionError`; `UNAVAILABLE` for
   `EmbeddingUnavailableError`). Added `degraded_reason_for(exc)`; the KB
   `query`/`search`/`query_multiple` typed-error catches now set
   `degraded_reason` to `embedding_invalid` vs `embedding_unavailable`
   accurately. *(fixes "INVALID not returned" + "metadata mislabels dim errors
   as unavailable" + "non-numeric bare ValueError".)*
2. **Stale log text** — `_create_embedding_function` no longer says
   "回退到随机向量"; it now says embedding is unavailable and the vector
   channel will be disabled (no random vector generated). `_embed_fn_name`
   fallback changed from `"default(fallback)"` to `"default(unavailable)"`.
3. **`prometheus_client`** — was undeclared (only in user-site, invisible to
   the isolated `.venv`), so the observability test **SKIPPED** in the canonical
   env. Now declared in `requirements.txt` (`prometheus-client>=0.18.0`) and
   installed into `.venv` (0.25.0). The observability test now **RUNS and
   PASSES** (no longer skipped).
4. **Authoritative test statistics** — re-run in `.venv/bin/python` (the env
   Codex uses). See §14.

### Re-review #2 (post Codex FAIL) — semantic cache malformed-value fail-closed

Codex re-review #2 returned **FAIL**: `ResponseCache._embed_query()` validated
only `len(vec)`, so a **length-correct but malformed** (NaN / Inf / None /
non-numeric) vector passed through and reached Qdrant (`query_points` on read,
`upsert` on write) — violating EMB-5/EMB-6. Fixed:

- `_embed_query` now calls the **shared `validate_embedding_vector`** (the same
  validator the KB uses) instead of a manual length check. Empty /
  wrong-dimension / non-finite (NaN/Inf) / non-numeric vectors raise
  `EmbeddingDimensionError`.
- `_qdrant_get`/`_qdrant_set` already catch `EmbeddingDimensionError` → skip L2
  (no `query_points`, no upsert). Verified by spy: NaN/Inf/None →
  `query_points_calls=0`, `upsert_calls=0`.
- 5 new target tests: cache rejects NaN / Inf / None-element;
  malformed embedding never reaches Qdrant; malformed →
  `semantic_cache_embedding_failures_total` metric incremented.

Authoritative `.venv` numbers (re-review #2): **1651 passed, 5 skipped,
0 failed, coverage 81.70%**; P0-05 target **35 passed, 0 skipped** (was 30;
+5 semantic-cache malformed-value fail-closed tests). Re-verified against the
current working tree (2026-08-16): full regression 1651 passed / 5 skipped /
0 failed / coverage 81.70%; target 35 passed / 0 skipped.

## 1. Issue

**P0-05 — Random Embedding Fallback / 随机 Embedding 降级**

Embedding dependency failure was being coerced into a synthetic vector
(`random.random()` in `QdrantKnowledgeBase._embed_texts`, and
`random.Random(query)` deterministic-random in `ResponseCache._embed_query`),
so an embedding-provider outage masqueraded as normal (low-recall) vector
retrieval and semantic-cache lookup. This is failure masking, not graceful
degradation.

## 2. Baseline Failure Matrix

### RAG Query Embedding
- Path: `query` / `search` / `query_multiple` → `_embed_texts` → `_embed_fn.encode`
- Provider: `ApiEmbedding` (BAAI/bge-large-zh-v1.5 via SiliconFlow)
- Failure source: `_create_embedding_function` returns `None` when
  `EMBEDDING_API_KEY` missing or ctor raises; `encode()` raises at runtime.
- Current fallback: `[[random.random() for _ in range(1024)] for _ in texts]`
  (non-deterministic).
- Random vector? **YES**
- Caller sees failure? **NO** (silent)
- Metadata: none
- Risk: HIGH — random vector queries Qdrant, returns noise as candidates.

### Document Embedding / Ingestion
- Path: `add_documents` → `_embed_texts` → upsert
- Current fallback: random vectors upserted to Qdrant permanently.
- Random vector? **YES** (persisted)
- Caller sees failure? **NO**
- Risk: **CRITICAL** — permanent vector pollution.

### Semantic Cache
- Path: `_qdrant_get` / `_qdrant_set` → `_embed_query`
- Current fallback: `random.Random(query)` deterministic-random (both the
  `_embed_fn is None` path AND the `encode()`-raises path).
- Random vector? **YES** (deterministic)
- Caller sees failure? **NO** (warning log only)
- Risk: HIGH — L2 cache queries AND writes fake vectors; same query → same
  fake vector → deterministic semantic "hit" with no real embedding.

### Dimension Validation
- Current: none — a wrong-dimension vector is fed straight to Qdrant.
- Safe? **NO** (Qdrant errors → caught → `[]`, indistinguishable from low recall).

## 3. Reproduction

`/tmp/p005_repro.py` proved the OLD behavior (all CONFIRMED):
- RAG query path emits **non-deterministic random** vector
  (`_embed_fn=None` → `random.random()`). Same query → different vector each call.
- Runtime `encode()` raise → `query()` returns `[]` indistinguishable from low
  recall (no degraded metadata).
- Semantic cache emits **deterministic-random** vector (`random.Random(query)`)
  on both the `_embed_fn=None` path AND the runtime encode-raise path.
- Cache `_qdrant_set` upserts a fake vector; `_qdrant_get` calls `query_points`
  with a fake vector.
- Dimension mismatch (dim 7 returned) is NOT validated.
- Ingestion persists a random vector to Qdrant.

After the fix, the same repro script crashes at the first `_embed_texts` call
with `EmbeddingUnavailableError: provider_unavailable` — the random vector is
never produced.

## 4. Root Cause

- **Surface:** `_embed_texts` (qdrant_knowledge_base.py) and `_embed_query`
  (response_cache.py) coerce embedding-unavailable into a synthetic vector;
  callers proceed as if a real embedding existed.
- **Contract:** No typed embedding status. `_create_embedding_function`
  returns `None` (ambiguous: unavailable vs not-configured); failure is
  flattened into `list[float]`. There was no AVAILABLE / UNAVAILABLE /
  INVALID distinction.
- **Architecture:** Callers (`query`, `search`, `query_multiple`,
  `add_documents`, `_qdrant_get`, `_qdrant_set`) assumed `_embed_texts`
  always returns a vector; dependency failure was absorbed at the lowest
  layer instead of propagated as a channel-disabled signal.
- **Observability:** Only `logger.warning("…回退到随机向量")` and a `[]`
  return — no metric, no `retrieval_degraded` flag, no `degraded_reason`.
  "Embedding provider down" was indistinguishable from "low recall".
- **Test contract:** `test_embed_texts_fallback` asserted `_embed_fn=None`
  returns a `_EMBEDDING_DIM`-length vector — codifying the random fallback as
  expected behavior. The cache edge-path test asserted a failing embedding
  returns a 1024-dim vector. Both valued "doesn't crash" over "doesn't lie".

## 5. New Failure Contract

Minimal typed contract in `rag/embedding_status.py` (types/helpers only — no
behavioral fallback):

- **`EmbeddingStatus`** enum: `AVAILABLE` / `UNAVAILABLE` / `INVALID`, with
  `from_error(exc)` classmethod mapping `EmbeddingDimensionError`→INVALID,
  `EmbeddingUnavailableError`→UNAVAILABLE.
- **`EmbeddingUnavailableError`** (UNAVAILABLE): no embed_fn (`provider_unavailable`)
  or `encode()` raised (`encode_failed`). Carries `reason`, `provider`, `model`.
- **`EmbeddingDimensionError`** (INVALID): wrong dimension, empty, non-finite
  (NaN/Inf), or non-numeric elements. Carries `expected`, `actual`, `reason`.
- **`validate_embedding_vector()`**: rejects empty/wrong-dim/non-finite/
  non-numeric (never pads/truncates/random-fills; wraps `ValueError`/`TypeError`
  into the typed contract).
- **`degraded_reason_for(exc)`**: `embedding_invalid` vs `embedding_unavailable`.
- **`RetrievalResultList(list)`**: a result list carrying per-query `.meta`
  (backward compatible — IS-A list; callers that iterate are unaffected).

Mapping to invariants:
- SUCCESS → real embedding returned; normal hybrid retrieval; `meta.retrieval_degraded=False`.
- UNAVAILABLE → vector channel disabled; BM25-only if ready else no-channel;
  `meta.retrieval_degraded=True`, `degraded_reason=embedding_unavailable|no_retrieval_channel`.
- INVALID → `EmbeddingDimensionError` raised; metric incremented; no Qdrant query;
  `meta.degraded_reason=embedding_invalid`.

## 6. Modified Files

- `rag/embedding_status.py` **(NEW)** — typed contract (exceptions, enum +
  `from_error`, `validate_embedding_vector`, `degraded_reason_for`,
  `RetrievalResultList`, reason constants).
- `rag/qdrant_knowledge_base.py` — `_embed_texts` raises typed errors (no
  random); `embedding_available`/`embedding_status()`; `_degraded_meta`/
  `_degraded_result` helpers; `query`/`search` disable vector channel on
  failure; `query_multiple` skips vector channel → BM25-only/no-channel
  degraded result with `.meta`; `add_documents` skips Qdrant upsert on
  failure (no pollution), still indexes BM25; instance provider/model attrs.
- `cache/response_cache.py` — `_embed_query` raises typed errors (no
  deterministic-random); `_qdrant_get`/`_qdrant_set` catch typed errors and
  skip L2 (no fake-vector query/write); `import random` removed;
  `_RANDOM_VECTOR_DIM` → `_L2_VECTOR_DIM`; `semantic_cache_embedding_failures`
  metric.
- `core/monitoring.py` — 6 P0-05 counters (`embedding_provider_failures_total`,
  `embedding_dimension_errors_total`, `vector_channel_disabled_total`,
  `bm25_fallback_used_total`, `retrieval_no_channel_total`,
  `semantic_cache_embedding_failures_total`) + noop fallbacks.
- `agents/base_agent.py` — `_retrieve_knowledge` captures `RetrievalResultList.meta`
  before rerank and surfaces `state["retrieval_degraded"]` / `state["degraded_reason"]`
  to the graph/agent (no behavior change to retrieval itself).
- `requirements.txt` — declare `prometheus-client>=0.18.0` (was undeclared; the
  observability metrics depend on it and the test SKIPPED in the isolated `.venv`).

## 7. RAG Query Behavior

### Embedding available
Normal hybrid path: vector + BM25 parallel → RRF → rerank. Returns
`RetrievalResultList(reranked, meta={retrieval_degraded: False,
vector_channel_used: True, lexical_channel_used: bool(bm25), provider, model})`.

### Embedding unavailable + BM25 ready
Vector channel skipped entirely (no `_embed_texts` call, no Qdrant query).
BM25-only results, reranked. `vector_channel_disabled_total` + `bm25_fallback_used_total`
incremented. Returns `RetrievalResultList(bm25_results, meta={retrieval_degraded: True,
degraded_reason: "embedding_unavailable", vector_channel_used: False,
lexical_channel_used: True, ...})`.

### Embedding unavailable + no fallback (BM25 empty/unavailable)
`retrieval_no_channel_total` incremented. Returns `RetrievalResultList([], meta={
retrieval_degraded: True, degraded_reason: "no_retrieval_channel",
vector_channel_used: False, lexical_channel_used: False, ...})`. Upper layer
decides safe-answer/refusal/escalation.

## 8. Semantic Cache Behavior

`_embed_query` raises `EmbeddingUnavailableError`/`EmbeddingDimensionError` (no
deterministic-random). It calls the **shared `validate_embedding_vector`** (same
as the KB) — so empty / wrong-dimension / non-finite (NaN/Inf) / non-numeric
vectors raise `EmbeddingDimensionError` and are never used to query or upsert
Qdrant (not just a length check). `_qdrant_get` catches → returns None (L2
miss, no `query_points` call). `_qdrant_set` catches → skips L2 write (no fake
vector upsert). `get`/`_set` fall through to L1 (Redis exact) / L3 (Jaccard)
which are embedding-independent — P0-02 scope isolation preserved.
`semantic_cache_embedding_failures` metric incremented on every skip (incl.
malformed values).

## 9. Ingestion Behavior

`add_documents` with embedding unavailable: skips the Qdrant vector upsert (no
fake-vector pollution), still updates the BM25 lexical index (embedding-free),
logs `embedding_unavailable` + increments `vector_channel_disabled_total`.
Runtime encode failure during an available embedding raises
`EmbeddingUnavailableError` (explicit write failure — no partial/pollution).

## 10. Dimension Validation

`validate_embedding_vector` rejects empty, wrong-dimension, non-finite
(NaN/Inf), and non-numeric elements (string/None/…) via
`EmbeddingDimensionError` — non-numeric elements are wrapped into the typed
INVALID contract (`reason="non_numeric"`) rather than propagating a bare
`ValueError`. Never pads, truncates, or random-fills. `_embed_texts` and
`_embed_query` both validate; Qdrant is never queried with a malformed vector.
`embedding_dimension_errors_total` incremented.

## 11. Degraded Metadata

`RetrievalResultList.meta` carries: `retrieval_degraded`, `degraded_reason`,
`vector_channel_used`, `lexical_channel_used`, `embedding_provider`,
`embedding_model`. `degraded_reason` is **distinguished**: `embedding_invalid`
(wrong dim / non-finite / non-numeric, via `EmbeddingDimensionError`) vs
`embedding_unavailable` (no embed_fn / encode raised, via
`EmbeddingUnavailableError`) vs `no_retrieval_channel` (vector down + BM25
empty). `EmbeddingStatus.from_error(exc)` makes `INVALID` reachable
(`AVAILABLE` is the pre-check state; `UNAVAILABLE`/`INVALID` are per-call
outcomes derived from the typed error). Reaches direct KB callers (benchmark,
tests, prefetch) via `.meta`, and reaches the agent/graph via
`_retrieve_knowledge` writing `state["retrieval_degraded"]` +
`state["degraded_reason"]`. Captured before `simple_rerank` (which slices to a
plain list) so it is not lost.

## 12. Metrics / Logging

Counters added (labels carry only reason codes / provider / model name — never
query text, API key, or cached response content; P0-02 privacy boundary intact):
`embedding_provider_failures_total{reason}`, `embedding_dimension_errors_total`,
`vector_channel_disabled_total`, `bm25_fallback_used_total`,
`retrieval_no_channel_total`, `semantic_cache_embedding_failures_total`.
Structured WARNING logs at each degradation point (embedding unavailable /
vector channel disabled / no retrieval channel / semantic cache skipped).

## 13. Tests Added / Changed

**NEW** `tests/unit/test_p005_embedding_failclosed.py` (35 tests):
EMB-1/2 no-synthetic-vector + no-fake-vector-query; EMB-3/4/7 BM25 fallback /
no-channel / degraded meta / normal-not-degraded; EMB-6 dimension + non-finite +
empty + **non-numeric + None-element** rejection; EMB-7 metric+log (RUNS in
`.venv`, not skipped — `prometheus_client` now installed); EMB-8 benchmark
honesty; EMB-5 semantic cache fail-closed (no deterministic-random, no semantic
hit, L1 preserved, scope isolation intact); ingestion fail-closed; adversarial
runtime-encode-failure + repeated-failures; **invalid-vs-unavailable contract**
(`EmbeddingStatus.from_error` → INVALID; `embedding_invalid` vs
`embedding_unavailable` metadata reason); **+5 semantic-cache malformed-value
fail-closed tests** (re-review #2): `_embed_query` rejects NaN / Inf /
None-element (length-correct but malformed) via the shared
`validate_embedding_vector`; a NaN embedding never reaches Qdrant
(`upsert_calls == 0` on write, `query_points_calls == 0` on read); malformed →
`semantic_cache_embedding_failures_total` incremented.

**Rewritten contracts (NOT weakened):**
- `test_embed_texts_fallback` → `test_embed_texts_fails_closed_when_unavailable`:
  asserts `EmbeddingUnavailableError` instead of a random vector.
- `test_embedding_and_internal_edge_paths` (cache): `_embed_query` with no
  embed_fn / failing embed → `EmbeddingUnavailableError`; wrong-dim →
  `EmbeddingDimensionError` (was: asserted a 1024-dim random vector).
- `tests/unit/test_cache_cross_user_isolation.py` `_cache` fixture: uses real
  `_SemanticEmbedding()` instead of `embedding_model=None` (was: relied on the
  deterministic-random fallback to exercise L2). Isolation logic is what's
  tested, not the embedding — L2 now works with a real mock embedding.

## 14. Tests Executed

All executed with `.venv/bin/python` (the canonical env; `prometheus_client`
now installed there).

| Hierarchy | Collected | Passed | Failed | Skipped |
|---|---|---|---|---|
| P0-05 target | 35 | 35 | 0 | 0 (observability now RUNS) |
| P0-02/03/04 combined | 175 | 175 | 0 | 0 |
| P0-01 CI contract | 43 | 43 | 0 | 0 |
| **Full regression** | **1656** | **1651** | **0** | **5** |

- Collected: 1656 (1651 passed + 5 skipped; verified via `pytest --collect-only`)
- Passed: 1651
- Failed: 0
- Skipped: 5 (real-LLM tests, no OPENAI_API_KEY — observability test no longer skipped)
- XFailed: 0
- Deselected: 0
- Warnings: 18 (pre-existing jieba/pkg_resources/Starlette deprecations)
- Runtime: ~171–174s across re-verification runs (nondeterministic wall-clock; not a contract)
- Coverage: **81.70%** (gate 80%)
- Exit Code: **0**

## 15. Reliability Invariants

- **EMB-1 No Synthetic Vector:** PASS — `_embed_texts`/`_embed_query` raise
  typed errors; no random/pseudo/hash vector.
- **EMB-2 Disable Vector Channel:** PASS — `embedding_available` pre-check +
  typed-error catch skip the vector channel; Qdrant never queried with a fake
  vector (asserted `search.call_count == 0`).
- **EMB-3 BM25 Controlled Fallback:** PASS — BM25-only when ready, flagged
  `lexical_channel_used=True`.
- **EMB-4 No Retrieval Channel:** PASS — `no_retrieval_channel` degraded empty
  result when vector down + BM25 empty.
- **EMB-5 Semantic Cache Fail Closed:** PASS — L2 skipped (no `query_points`/
  upsert), L1/L3 + P0-02 scope preserved.
- **EMB-6 Dimension Safety:** PASS — wrong-dim/empty/non-finite/non-numeric
  all rejected via `EmbeddingDimensionError` (no bare ValueError).
- **EMB-7 Observable Degradation:** PASS — 6 metrics (now verified RUNNING in
  `.venv`, not skipped) + structured logs + `.meta` to caller +
  `state["retrieval_degraded"]`. Invalid vs unavailable distinguished.
- **EMB-8 Benchmark Honesty:** PASS — degraded run `meta.retrieval_degraded=True`,
  `vector_channel_used=False`; no fake vectors → no fake-high recall.

## 16. Adversarial Results

- A (ctor fails → None): raises `EmbeddingUnavailableError`. PASS
- B (API raises timeout/error at runtime): `query_multiple` catches the typed
  error mid-flight, disables vector channel, degrades to BM25; Qdrant unsearched. PASS
- C (empty embedding): `EmbeddingDimensionError(reason=empty_vector)`. PASS
- D (wrong dimension): `EmbeddingDimensionError`. PASS
- E (NaN/Inf): `EmbeddingDimensionError(reason=non_finite)`. PASS
- F (semantic cache embedding fails): L2 skipped, no semantic hit. PASS
- G (embedding fails + BM25 ready): BM25-only degraded. PASS
- H (embedding fails + BM25 unavailable): `no_retrieval_channel` empty. PASS
- I (repeated failures): every run raises/flags degraded; no varying random vector. PASS
- J (document ingestion embedding failure): no Qdrant upsert; BM25 indexed. PASS

## 17. Prior P0 Regression (`.venv`)

- P0-01 CI Gate: PASS — full suite exit 0; coverage 81.70% ≥ 80% gate; 43 CI-contract tests pass.
- P0-04 Identity Context: PASS — SSE-identity tests pass (in combined 175).
- P0-03 ERP AuthZ: PASS — ERP authz+mapping tests pass (in combined 175).
- P0-02 Cache Isolation: PASS — cache isolation+policy tests pass; L2 skip
  preserves GLOBAL/USER/NONE scope (L1/L3 embedding-independent).

## 18. Regression Risk

Low. The retrieval return type is now `RetrievalResultList` (a `list` subclass)
— backward compatible (`len`, iteration, indexing, `isinstance(list)` unchanged).
Existing tests that mock `_embed_texts` bypass the new validation. The cache
`_embed_query` now raises on failure where it previously returned a fake vector;
only the two cache tests that asserted the fake-vector behavior were rewritten.
No call-site signature changes to public methods.

## 19. Remaining Problems

### P0-05 Remaining
- Historical random vectors already persisted in any pre-existing Qdrant
  collections are NOT migrated/scrubbed by this change (the fix only prevents
  NEW fake vectors). A one-off collection rebuild would purge them; that is
  out of scope (touches P1-02/P1-03 territory) and not required for the
  fail-closed contract.
- `query_with_vector()` accepts an externally precomputed vector; it does not
  generate a fallback vector (no fake-vector risk), but it also does not
  re-validate the caller-supplied vector dimension. Callers obtain vectors via
  `_embed_texts` (which validates), so this is not a fake-vector path.

### P1-01 Future (NOT started)
RAG Pipeline Unification — no `RetrievalRequest`/`RetrievalTrace`/unified
`retrieve()` introduced. `query`/`search`/`query_multiple` remain separate.
The `.meta` channel is compatible with a future unified entrypoint.

### P1-02 Future (NOT started)
BM25 restart/readiness lifecycle — P0-05 only CONSUMES the existing
`BM25Retriever.collection_size()` readiness signal; it does not rebuild BM25
from Qdrant on restart or add a readiness state machine. If BM25 is empty
after restart, P0-05 correctly reports `no_retrieval_channel` (honest), but
the underlying "BM25 empty after restart" is P1-02.

### P2-03 Future (NOT started)
Benchmark provenance — `evaluate_rag.py` does not tag runs as normal/degraded.
P0-05 ensures degraded runs produce honest results (no fake-high recall) and
expose `.meta.retrieval_degraded` for a future benchmark to read; full
provenance tagging is P2-03.

## 20. Evidence

- Source: `rag/embedding_status.py`, `rag/qdrant_knowledge_base.py`,
  `cache/response_cache.py`, `core/monitoring.py`, `agents/base_agent.py`,
  `requirements.txt`.
- Tests: `tests/unit/test_p005_embedding_failclosed.py` (35 tests),
  rewritten `tests/unit/test_qdrant_knowledge_base.py`,
  `tests/unit/test_cache_cross_user_isolation.py`.
- Commands (canonical `.venv`): `make env-test && .venv/bin/python -m pytest
  tests/ -q --cov=. --cov-report=term`; target
  `.venv/bin/python -m pytest tests/unit/test_p005_embedding_failclosed.py -q`.
- Actual output (re-verified 2026-08-16): `1651 passed, 5 skipped, 18 warnings
  in ~171–174s` (1656 collected, verified via `--collect-only`); `Required test
  coverage of 80.0% reached. Total coverage: 81.70%`. Target: `35 passed`
  (observability test RUNS, not skipped).
- Reproduction: `/tmp/p005_repro.py` (old: random vectors CONFIRMED; after fix:
  raises `EmbeddingUnavailableError` at first `_embed_texts` call).
- git diff: 7 files modified + 3 new files; `import random` removed;
  no `random`/`np.random`/deterministic-random in added production lines.
  Modified: `rag/qdrant_knowledge_base.py`, `cache/response_cache.py`,
  `core/monitoring.py`, `agents/base_agent.py`, `requirements.txt`,
  `tests/unit/test_qdrant_knowledge_base.py`,
  `tests/unit/test_cache_cross_user_isolation.py`.
  New: `rag/embedding_status.py`,
  `tests/unit/test_p005_embedding_failclosed.py`,
  `docs/audit/P0_05_COMPLETION_REPORT.md`.
  (Note: `secrets/keys.json` and `tests/data/csai.db-shm`/`-wal` are test/rotation
  artifacts, NOT part of P0-05 — verified unchanged in content scope.)

## 21. Git Diff Review

- [x] 1. All random embedding fallback deleted (no `random.random` in
      `_embed_texts`; no `random.Random` in `_embed_query`).
- [x] 2. No deterministic-random cache fallback.
- [x] 3. No zero-vector "new random fallback" (the zero-vector in the test
      helper `_SemanticEmbedding` is a deterministic TEST mock, not production).
- [x] 4. No swallowed exceptions (typed catches precede generic; `_qdrant_get`/
      `_qdrant_set` skip, don't swallow into success).
- [x] 5. BM25 not-ready does not claim fallback (`no_retrieval_channel`).
- [x] 6. Degraded metadata is not dead (`.meta` to caller + `state`).
- [x] 7. Semantic cache does not query a fake vector (`query_points` not called).
- [x] 8. Ingestion does not write a fake vector (upsert skipped).
- [x] 9. Dimension mismatch not silently fixed (rejected).
- [x] 10. P1-01 RAG pipeline unification NOT started.
- [x] 11. P1-02 BM25 rebuild NOT started.
- [x] 12. Embedding model NOT swapped.
- [x] 13. RRF/reranker NOT changed.
- [x] 14. Benchmark NOT rewritten (only ensured honest degraded results).
- [x] 15. P0-02 cache isolation NOT broken (87 P0-02 tests pass).
- [x] 16. Old tests rewritten (not weakened) — fail-closed contract.

## 22. Commit Recommendation

```
fix(rag): fail closed on embedding unavailability (P0-05)

Eliminate random/deterministic-random vector fallbacks in
QdrantKnowledgeBase._embed_texts and ResponseCache._embed_query.
On embedding dependency failure the vector channel is explicitly
disabled; BM25-only retrieval runs when ready (retrieval_degraded=true)
or an explicit no_retrieval_channel degraded result is returned.
Wrong-dimension / non-finite / non-numeric vectors are rejected via a
typed EmbeddingDimensionError (never padded; invalid vs unavailable
distinguished in metadata). Semantic cache skips L2 on failure (no
fake-vector query/write), preserving P0-02 scope isolation via L1/L3.
Declare prometheus-client (was undeclared; observability test skipped
in the isolated venv). Degraded metadata + 6 metrics reach callers;
AgentState surfaces retrieval_degraded.

.venv: 1651 passed / 0 failed / 5 skipped (real-LLM); coverage 81.70%.
```
