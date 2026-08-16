# P1-03 Completion Report

## 1. Issue

P1-03 — Stable Qdrant Point IDs. The Qdrant storage Point ID was derived from
Python's `hash(id_) & 0x7FFFFFFFFFFFFFFF`, which is randomized per process via
`PYTHONHASHSEED`. The same logical `doc_id` therefore mapped to a *different*
storage Point ID on every process start — breaking idempotent upsert (ghost
duplicates) and any cross-process / cross-restart Point-ID reference. This
issue fixes the deterministic mapping, the upsert/delete/update identity
contract, the collision policy, and the legacy-data migration safety.

## 2. Baseline (current code, verified — not assumed from the spec)

**Current ID generation:** single site, `rag/qdrant_knowledge_base.py` `add_documents`:

```python
id=hash(id_) & 0x7FFFFFFFFFFFFFFF          # process-seed-dependent
payload={"doc_id": id_, "content": doc, **meta}   # logical id preserved
```

Type: `int`, positive int63. The only Category-C (persistent-storage) `hash()`
site in the repo (full-repo scan via Explore agent + manual confirmation).

**Current delete behavior:** payload filter on `doc_id`
(`models.Filter(must=[FieldCondition(key="doc_id", ...)])`) — already
independent of the unstable hash, and uniquely capable of cleaning up legacy
ghost duplicates.

**Current update behavior:** none explicit; re-`add_documents` upserts by
Point ID — idempotent within a process, but cross-process the stored Point ID
≠ the recomputed one, so re-ingestion created ghost duplicates.

**Current persistence behavior:** logical `doc_id` is always stored in
payload; `_parse_query_result` exposes `id = payload.doc_id` (the logical id,
NOT the storage Point ID). BM25 stores logical doc_ids only — it never touches
storage Point IDs. Benchmark/golden files (`expected_doc_ids.json`,
`rag_benchmark.json`) hold logical string doc_ids and never assert Qdrant
Point IDs.

## 3. Reproduction (Step 2)

`hash("product_knowledge_42") & 0x7FFFFFFFFFFFFFFF` across processes:

| Process / seed | Point ID |
|---|---|
| PYTHONHASHSEED=0 run 1 | 6853863427414565189 |
| PYTHONHASHSEED=1       | 1487969802233063677  ← different |
| PYTHONHASHSEED=0 run 2 | 6853863427414565189 (deterministic within seed) |
| unset run 1            | 8586886230805367694  |
| unset run 2            | 7218065077892718963  ← different across starts |

**Before: UNSTABLE.** Same logical id → different Point IDs across processes.

Candidate stable algorithm verified seed-independent:
`SHA-256("coll:doc")[:8] & 0x7FFFFFFFFFFFFFFF` = `5079228239274416136`
identical across all seeds.

## 4. Root Cause

No stable mapping boundary existed; `add_documents` used Python's
process-randomized `hash()` directly for a persistent identity. No collision
guard existed. Delete happened to be stable only because it filters on the
logical `doc_id` payload rather than the Point ID.

## 5. Stable ID Decision

- **Algorithm:** `SHA-256("{collection_name}:{doc_id}")` → first 8 bytes
  big-endian → `& 0x7FFFFFFFFFFFFFFF` (positive int63).
- **Input:** `(collection_name: str, doc_id: str|Any)`.
- **Output type:** `int` in `[0, 0x7FFFFFFFFFFFFFFF]` — same type + mask as
  the legacy code → no Qdrant schema change; legacy and stable ids share one
  int ID space (enables in-place detection + cleanup, not a forced cutover).
- **Why:** seed-independent (verified), deterministic across processes/restarts,
  collision bound < 1e-8 for ≤ 10^6 docs.
- **Cross-collection semantics:** collection participates in the namespace →
  same `doc_id` in two collections → two different Point IDs (defense-in-depth;
  consistent with the existing `"{coll}_{count}"` auto-id convention).
- **Collision policy:** read-before-write via `client.retrieve(ids=...)` before
  every upsert; if an existing point's `payload.doc_id` differs from the doc_id
  being written for the same Point ID → raise `PointIdCollisionError` (refuse
  silent overwrite). Retrieval failure fails closed (propagates, no upsert).

## 6. Identity Model

- **Logical Document ID** = business/corpus identity (e.g. `"pr_000"`), carried
  in `payload.doc_id` — unchanged, preserved on every write.
- **Storage Point ID** = Qdrant storage identity, the stable int from
  `document_id_to_point_id(collection, doc_id)`.
- **Relationship:** one-way deterministic mapping; logical id is never derived
  from the Point ID. The two concepts are kept distinct.

## 7. Modified Files

- `rag/qdrant_knowledge_base.py` (+18/-2): import the boundary; replace
  `hash(id_) & 0x7FFFFFFFFFFFFFFF` with `document_id_to_point_id(...)`; call
  `assert_no_point_id_collision(...)` before upsert. Payload `doc_id`
  preserved. Pre-existing formatting untouched (minimal diff).
- `rag/point_id.py` (NEW, 171 lines): the single mapping boundary —
  `document_id_to_point_id`, `PointIdCollisionError`,
  `assert_no_point_id_collision`. Module docstring is the design record.
- `rag/point_id_migration.py` (NEW, 226 lines): read-only
  `discover_legacy_points` (LegacyReport) + safe in-place
  `rebuild_collection_point_ids` (RebuildReport, `dry_run=True` default,
  reuses stored vectors — no re-embedding, never deletes a whole collection,
  protects stable ids that collide with legacy ids).
- `scripts/migrate_point_ids.py` (NEW, 176 lines): CLI. `--dry-run` (default)
  / `--execute` (requires `--i-understand-this-is-destructive`). Connects via
  `core.config` Qdrant env vars. `--collection` (repeatable) or `--all`.
- `tests/unit/test_qdrant_point_id.py` (NEW, 334 lines, 14 tests): the 8 ID
  invariants incl. real cross-process subprocess tests.
- `tests/unit/test_point_id_migration.py` (NEW, 209 lines, 13 tests):
  discovery classification, rebuild dry-run/execute/safety, CLI safety.

## 8. Upsert Behavior (ID-2)

Same logical `(collection, doc_id)` → same stable Point ID → Qdrant overwrites
the existing point in place. Verified by
`test_duplicate_upsert_is_idempotent`: two `add_documents` calls produce
identical Point IDs. Ghost duplicates can no longer accumulate across restarts.

## 9. Delete Behavior (ID-3)

Canonical delete contract = **payload filter on `doc_id`** (preserved). This is
mapping-consistent (operates on the logical id) and uniquely capable of
removing legacy ghost duplicates (delete-by-point-id would miss them, since
their stored ids are process-random). Locked by
`test_delete_uses_payload_filter_on_logical_doc_id`.

## 10. Update Behavior

No explicit update method exists; re-`add_documents` is the update path. With
stable ids it now targets the SAME Point ID → Qdrant overwrites the old point
(no ghost duplicate). Same-Point-ID + same-`doc_id` is treated as a legitimate
idempotent re-upsert (collision guard allows it).

## 11. Cross-Process / Restart Results

`test_point_id_stable_across_processes`: two fresh Python processes with
PYTHONHASHSEED 0 and 1 (plus a default-randomized third) compute the SAME
Point ID for `("product_knowledge","pr_000")`. `test_mapping_does_not_depend_on_python_hash_seed`:
seeds {0,1,2,42,default} all agree.

## 12. Collision Handling (ID-5)

Two layers: (a) algorithm — SHA-256 truncation, 63-bit space, astronomically
rare (verified no collision in 8000 distinct inputs); (b) storage —
`assert_no_point_id_collision` retrieves existing points at the computed ids
before upsert and raises `PointIdCollisionError` if a Point ID already stores a
*different* `doc_id`. Adversarially verified: a `retrieve()` exception
propagates and blocks the upsert (fail-closed), and the guard allows an
idempotent same-`doc_id` re-upsert.

## 13. Legacy Data Strategy

### Detection
`discover_legacy_points` (read-only scroll): classifies each point as
stable / legacy (has `doc_id`, stored id ≠ stable id) / unmappable (no
`doc_id`); reports `duplicate_logical_ids` (ghost duplicates) and
`potential_conflicts` (a stable id claimed by 2+ distinct doc_ids — a SHA-256
collision, surfaced for manual resolution).

### Dry Run
`rebuild_collection_point_ids(..., dry_run=True)` (default) reports what would
be upserted/deleted without writing. CLI `--dry-run` (default) does the same.

### Migration / Rebuild
In-place rebuild (same collection, same int id type — enabled by keeping the
mask): scroll all points with vectors; re-upsert each at its stable Point ID
(reusing the stored vector, no re-embedding); delete orphaned legacy ids.
Idempotent; a partial failure leaves both stable and legacy points present
(no data loss).

### Duplicate Resolution
`duplicate_logical_ids` are detected and reported. The rebuild upserts each at
its stable id (ghost duplicates collapse onto the same stable point) then
deletes the orphaned legacy ids.

### Rollback
Keep a pre-rebuild backup of payloads+vectors. The rebuild never deletes a
whole collection — only individual orphaned legacy points after stable points
are in place — so rollback = re-ingest from backup/seed into a fresh
collection.

## 14. Migration Safety

- **Destructive migration executed:** NO (only mocked-client unit tests).
- **Production/unknown collection modified:** NO. The CLI defaults to
  `--dry-run`; `--execute` requires `--i-understand-this-is-destructive`.
  No real Qdrant was contacted during this issue.

## 15. Benchmark / Golden Impact

None. `evaluate_rag.py` compares `retrieved[].id` (= `payload.doc_id`, the
logical id) against `expected_doc_ids` (logical strings like `"derm_001310"`).
No golden file or benchmark metric references a Qdrant Point ID. Locked by
`test_benchmark_expected_ids_are_logical_doc_ids_not_point_ids` and
`test_parse_query_result_exposes_logical_doc_id_as_id`. The P1-03 change is
invisible to the benchmark identity contract.

## 16. BM25 Impact

Compatibility-only. `BM25Retriever` stores and returns the logical `doc_id`
string and never references a Qdrant storage Point ID — confirmed by reading
`rag/bm25_retriever.py`. No BM25 algorithm or lifecycle change.
**P1-02 (BM25 restart rebuild) remains deferred** — not started here.

## 17. Tests Added

`test_qdrant_point_id.py` (14):
- `test_same_logical_id_produces_same_qdrant_point_id`
- `test_point_id_is_valid_for_qdrant`
- `test_distinct_doc_ids_produce_distinct_point_ids`
- `test_collection_namespace_semantics`
- `test_point_id_stable_across_processes` (real subprocess)
- `test_mapping_does_not_depend_on_python_hash_seed` (real subprocess)
- `test_add_documents_upserts_with_stable_point_id`
- `test_duplicate_upsert_is_idempotent`
- `test_refuses_overwrite_when_existing_point_has_different_doc_id`
- `test_allows_idempotent_reinsert_when_same_doc_id`
- `test_delete_uses_payload_filter_on_logical_doc_id`
- `test_knowledge_base_source_uses_stable_mapping_not_hash` (AST guard)
- `test_benchmark_expected_ids_are_logical_doc_ids_not_point_ids`
- `test_parse_query_result_exposes_logical_doc_id_as_id`

`test_point_id_migration.py` (13):
- `test_classifies_stable_legacy_and_unmappable`
- `test_detects_duplicate_logical_doc_ids`
- `test_detects_potential_stable_id_conflicts`
- `test_discovery_is_read_only`
- `test_dry_run_default_makes_no_writes`
- `test_execute_upserts_at_stable_id_and_deletes_legacy`
- `test_execute_skips_unmappable_without_doc_id`
- `test_execute_never_deletes_a_stable_id_even_if_legacy_collides`
- `test_default_mode_is_dry_run_and_needs_no_confirmation`
- `test_execute_without_acknowledgement_is_refused`
- `test_execute_with_acknowledgement_is_accepted`
- `test_execute_and_dry_run_are_mutually_exclusive`
- `test_requires_at_least_one_target`

## 18. Tests Executed

Target (P1-03): 27 passed
Qdrant KB unit: 15 passed
Persistence/migration: 13 passed
P0 regression surface (cache/erp/sse/ws/embedding/auth): 472 passed
Full regression (local toolchain): 1672 passed, 6 deselected, 0 failed
Full regression (CI-pinned toolchain, canonical command): 1667 passed,
1 deselected, 0 failed

Collected (CI-pin canonical, via junit): 1668
Passed: 1667
Failed: 0
Skipped: 0 (1 deselected: real_llm)
XFailed: 0
Deselected: 1
Warnings: 18
Coverage: 81.74% (gate ≥80% PASS)
Exit Code: 0

(Count variance local-vs-CI-pin is the documented toolchain-induced variance
the project's AC8 pinning exists to control; the CI-pinned number is
authoritative. Toolchain temp-downgraded to pytest==7.4.4 /
pytest-asyncio==0.21.1 / pytest-cov==5.0.0 / coverage==7.4.4 for the gate,
then restored to pytest==9.0.3 / coverage==7.14.1.)

## 19. ID Invariants

- ID-1 Deterministic: PASS (cross-process subprocess test)
- ID-2 Idempotent: PASS
- ID-3 Delete: PASS (payload-filter contract locked)
- ID-4 Qdrant Valid: PASS (int63, same type as legacy)
- ID-5 Collision: PASS (refuse-on-conflict + fail-closed on retrieve error)
- ID-6 Migration Safety: PASS (dry-run default, read-only discovery,
  acknowledged-execute, idempotent rebuild, rollback plan)
- ID-7 Collection Semantics: PASS (collection in namespace, tested)
- ID-8 Single Mapping Boundary: PASS (AST guard: no builtin `hash()` in KB;
  `document_id_to_point_id` is the only entry point)

## 20. P0 Regression

No Phase-1 breakage. The collision guard added a `client.retrieve()` call
inside `add_documents`; verified to degrade cleanly under the existing bare-
`MagicMock` test fixtures (`list(MagicMock()) == []` → no existing points →
no collision → upsert proceeds). Full P0 security/correctness surface
(cache isolation, ERP AuthZ, embedding fail-closed, SSE/WS identity, tool
AuthZ): 472 passed, 0 failed. Secrets untouched.

## 21. Regression Risk

Low. The only production-code change is in `add_documents`'s Point ID
derivation + a pre-upsert read. The mask and output type are unchanged, so
new writes land in the same id space as legacy writes. Existing collections
with legacy hash-based ids are not silently corrupted — they are detected by
`discover_legacy_points` and rebuilt via the CLI (off by default). Delete and
query paths are untouched.

## 22. Remaining Problems

### P1-03 Remaining
None for the identity contract. Operational note: production collections with
pre-existing legacy (hash-based) points should be run through
`scripts/migrate_point_ids --dry-run` then `--execute` by an operator with a
backup — this is intentionally a human step, not auto-run.

### P1-02 Future
BM25 restart/rebuild — not started. BM25 uses logical doc_ids only (no
storage-id coupling), so P1-03 imposes no constraint on P1-02's design.

### P1-01 Future
RAG pipeline unification — not started.

### P2 Future
Benchmark provenance — not started. (No benchmark identity change was needed
or made for P1-03.)

## 23. Evidence

- `rag/point_id.py`, `rag/point_id_migration.py`, `scripts/migrate_point_ids.py`
- `tests/unit/test_qdrant_point_id.py`, `tests/unit/test_point_id_migration.py`
- `rag/qdrant_knowledge_base.py` (diff: +18/-2)
- Reproduction output (§3)
- Adversarial checks: fail-closed on `retrieve()` exception (upsert not
  called); CLI `--dry-run` end-to-end classification (1 stable + 1 legacy)
- CI-pinned coverage gate: `1667 passed, coverage 81.74%, exit 0`
- Commit: see `git log` for `fix(rag): use deterministic qdrant point ids`

## 24. Git Diff Review (Step 23 checklist)

1. old persistent hash() removed — YES (AST-guarded)
2. no second stable-id algorithm — YES (single `document_id_to_point_id`)
3. logical id vs Point ID not conflated — YES (payload.doc_id preserved)
4. duplicate upsert idempotent — YES (tested)
5. delete/update use stable identity — YES (payload-filter delete; update = same stable id)
6. destructive migration default — NO (dry-run default)
7. dry-run present — YES
8. rollback/rebuild plan — YES (in-place idempotent rebuild + backup rollback)
9. silent collision overwrite — NO (raises `PointIdCollisionError`)
10. legacy duplicate identification — YES (`discover_legacy_points`)
11. document chunking modified — NO
12. embedding modified — NO
13. BM25 rebuild started — NO
14. RAG unification started — NO
15. benchmark metrics changed — NO (identity contract preserved)
16. P0 broken — NO (472 P0-surface tests pass)

## 25. Commit Recommendation

```
fix(rag): use deterministic qdrant point ids (P1-03)
```

---

NEXT_REQUIRED_ACTION = CODEX P1-03 INDEPENDENT ACCEPTANCE

P1-02 remains deferred.
