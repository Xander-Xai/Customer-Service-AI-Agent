# Evidence Manifest — what was actually run, on what code, with what result

> Rule: a claim is only as strong as the command that produced it and the SHA it
> ran against. Anything not listed here is **NOT_VERIFIED**. No number in this
> repository may be quoted without its row.

---

## 1. Provenance of this manifest

| Field | Value |
|---|---|
| Code under test | PR #4 head, after the P0 lease-recovery fix |
| Branch | `fix/finalization-eval-runtime-observability` |
| Real infrastructure | PostgreSQL 15.18 (`infra-postgres-1`, port 5432), Redis 7.4.9 (`infra-redis-1`, port 6379) |
| Checkpoint backend | real `AsyncPostgresSaver` (postgres), not memory |
| Transport | real Celery prefork workers, `acks_late` + `task_reject_on_worker_lost` + `visibility_timeout` |

Artifacts under `artifacts/` are produced locally and are **not** committed;
`tested_code_sha` inside each artifact is the authoritative link back to code.

---

## 2. The P0 finding: stranded RUNNING runs

### 2.1 It was a real defect, not a timing flake

`tests/integration/test_worker_crash_recovery.py::test_worker_killed_mid_run_is_redelivered_and_succeeds`
failed **deterministically**:

| Build | Result |
|---|---|
| Pre-fix (`1c61ae9`) | **3 / 3 FAILED** |
| Post-fix (P0 commit) | **5 / 5 FAILED** (same signature) |

Failure signature, identical every time:

```
AssertionError: 未恢复成功: ('RUNNING', 2, 'Ydxx:3120657:437dc342', None)
 - 'RUNNING'
 + 'SUCCEEDED'
```

`attempt=2` proves worker B *did* take over. The run then never reached a
terminal state. Corroborating worker log:

```
runtime.executor - WARNING - 提交成功时 ownership 已丢失，放弃提交 run_id=crash-c23699fd
  expected_worker=Ydxx:3148898:918b8a3a current_status=RUNNING current_worker=Ydxx:3148898:918b8a3a
```

### 2.2 What the 252 stranded rows in the test database actually were

An initial reading of the shared test database looked like damning production
evidence — 252 rows stuck in `RUNNING`, the oldest with a lease expired since
**2026-10-03**, a week of silent accumulation:

```sql
SELECT count(*) FROM agent_runs WHERE status='RUNNING';  -- 252
```

**That reading was wrong, and the correction matters more than the original
claim.** Every one of those rows has `worker_id` in `('A','B','worker-A','worker-B')`
and `query = 'q'` — they are debris from
`tests/integration/runtime/test_worker_ownership_cas.py`, which *deliberately*
creates runs with expired leases to assert the fencing rules and never cleans
them up. They are not evidence that a production system accumulated orphans.

What they *are* evidence of is a second, real problem: **once stale `RUNNING`
becomes reclaimable, test debris becomes reclaimable too.** The scanner is
global (`ORDER BY lease_expires_at LIMIT 100`), so a backlog of 250+ stale rows
starved newer candidates out of the result set and made unrelated assertions fail
for the wrong reason. Two consequences, both acted on:

- The new integration tests register their runs and delete them
  (`cleanup_runs` fixture) — they must not depend on the shared table's history.
- Reconciler behaviour under a backlog is now a known operational consideration,
  not an untested edge: the scan is FIFO by `lease_expires_at`, so a permanent
  backlog drains oldest-first and never starves itself.

The P0 itself does **not** rest on these rows. It rests on the deterministic
reproduction in §2.1, which needs no historical data.

### 2.3 Mechanism

1. Ownership lease expires while the graph is still running (heartbeat starved by
   a blocking call on the event loop, a transient DB error, a GC pause).
2. Worker finishes the graph. `mark_succeeded(expected_worker_id=...)` is
   **correctly rejected**: `transition_owned` requires `lease_expires_at > now`.
   *This rejection is right and was not weakened.*
3. `execute_run` returns normally → Celery **ACKs** → broker never redelivers.
4. `list_recoverable_runs` scans only `RETRYING` / `QUEUED`. **Nothing ever looks
   at a stale `RUNNING` row again.**

Result: no terminal state, no DLQ record, no alert. The run is an orphan.

A second, quieter trigger was found in the same audit:
`_heartbeat_loop` caught only `CancelledError` and `ThreadLockBackendError`, so a
single exception from `heartbeat()` killed lease renewal permanently — and the
`finally` in `execute_run` suppressed the exception, so it never even logged.

### 2.4 Fix (two layers, neither weakens CAS or idempotency)

| Layer | Change | File |
|---|---|---|
| Shrink the window | Renewal survives a transient error; thread-lock refresh no longer skipped when the DB call raises | `runtime/executor.py::_heartbeat_loop` |
| Guarantee convergence | Reconciler reclaims expired-lease `RUNNING` runs via the existing atomic `takeover_running` | `runtime/repository.py`, `runtime/run_service.py`, `runtime/retry.py` |
| Bound the loop | Attempt-exhausted stale runs go to `DEAD_LETTER`, not infinite re-dispatch | `runtime/retry.py::reconcile_stuck_runs` |
| Observability | 4 new counters | `core/monitoring.py`, `runtime/metrics.py` |

Deliberately **not** reclaimed: `WAITING_APPROVAL`. Auto-resend after a
claim-then-crash cannot be proven free of duplicate side effects, so the manual
runbook and the TTL boundary are preserved.

### 2.5 A test harness was also masking this

`tests/integration/fake_runtime_provider.py` used `time.sleep()` inside
`async def run()`. That blocks the event loop, so the lease heartbeat cannot
run at all. With the suite's `AGENT_RUN_LEASE_SECONDS=3` and a 6s runtime, the
stranding was *guaranteed* — the test was measuring event-loop starvation while
claiming to measure broker redelivery. Changed to `await asyncio.sleep()`; no
assertion was relaxed. The recovery test then passes and runs ~6x faster
(100s → 17s).

---

## 3. Regression coverage added

| File | Tests | Pre-fix | Post-fix |
|---|---|---|---|
| `tests/unit/test_stale_run_reconciliation.py` | 13 | **9 fail** | 13 pass |
| `tests/integration/runtime/test_stale_run_reconciliation.py` (real PostgreSQL) | 7 | **5 fail** | 7 pass |

Covered: lease race, stale-worker write-back rejection, renewal survival,
reclaim → takeover → terminal state, attempt-exhaustion DLQ, DLQ replay
identity, `WAITING_APPROVAL` non-reclamation, renewed-lease protection,
concurrent reconcilers, concurrent challengers.

`tests/integration/runtime/test_stale_run_reconciliation.py` sits **inside**
`tests/integration/runtime/`, so `make runtime-e2e` now covers this path. It did
not before — which is a large part of why the defect survived.

---

## 4. Multi-round real-infrastructure runs

Each round is an independent full run against real PostgreSQL 15.18 + Redis 7.4.9
with real Celery prefork workers: SIGKILL crash recovery
(`tests/integration/test_worker_crash_recovery.py`) plus checkpoint resume
(`tests/integration/runtime/test_worker_checkpoint_recovery.py`) plus the chaos
script (`scripts/test_worker_crash_recovery.py`).

**All 8 rounds are recorded. None was dropped, re-run, or reported selectively.**

| Round | recovery tests | chaos script |
|---|---|---|
| 1 | 2 passed (53.99s) | PASS |
| 2 | 2 passed (54.20s) | PASS |
| 3 | 2 passed (54.59s) | PASS |
| 4 | 2 passed (54.17s) | PASS |
| 5 | 2 passed (54.25s) | PASS |
| 6 | 2 passed (54.74s) | PASS |
| 7 | 2 passed (54.57s) | PASS |
| 8 | 2 passed (54.64s) | PASS |

### What the rounds are and are not worth

They are worth exactly this: after the fix, the crash-recovery and
checkpoint-resume paths completed on **8/8** independent real-infra runs, where
the crash-recovery leg failed **3/3 and 5/5** before it.

They are **not** proof of production behaviour. Same as every other Level 2
result: local/CI real infrastructure, one machine, no multi-replica cluster, no
SLO, no real ERP writes.

### Honest note on how the rounds were obtained

- Rounds 1–2 ran against the working tree *before* the P0 fix was committed;
  rounds 3–8 ran against committed SHA `92aac78`.
- The first multi-round attempt was aborted and restarted because two harness
  errors of mine produced unusable data: a wrong test path (`rc=4`, pytest usage
  error) and a background job killed by a tool timeout. Those aborted attempts
  are **not** counted in the 8 rounds above.
- One round was measured while the working tree was being committed. That round
  is excluded; the 8 counted rounds each ran against a fixed SHA or a tree that
  was not being modified during the run.

---

## 5. Evidence-level boundaries (unchanged and still binding)

| Area | Level | What it does NOT mean |
|---|---|---|
| Distributed runtime, PG checkpoints, Redis locks, Celery, DLQ | **Level 2 CI-verified** | Not production multi-replica HA |
| HITL approval governance, fail-closed, CAS concurrency | **Level 2** | Real ERP refunds/order edits: **NOT_VERIFIED** |
| Agent orchestration/tool-governance regression (scripted LLM) | **Level 1–2** | **Not** real-model task success rate |
| Real-model eval lane | Guarded, opt-in, credential-gated | Not run in this pass (no budget/credential authorization) |
| RAG 649-query formal metrics | **NOT_MEASURABLE** | No human JUDGED labels; no Hit@K/MRR/NDCG may be quoted |
| Production SLO / multi-region / K8s autoscaling | **NOT_VERIFIED** | — |

`make runtime-report` prints `production_status: NOT_VERIFIED`. Level 2 is not
production verification and must not be described as such.