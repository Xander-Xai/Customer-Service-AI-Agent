# Final Runtime Review — state machine, recovery semantics, idempotency boundaries

> Scope: `runtime/`, `core/hitl/`, `core/checkpointer.py`, `db/` — **audit only**.
> No runtime was re-implemented. Every claim below is tied to a command or a
> `file:line`.
>
> Evidence state vocabulary: **IMPLEMENTED** (code exists) / **LEVEL 2** (real
> PG+Redis CI-tested) / **NOT_VERIFIED** (no production evidence) /
> **NOT_MEASURED** (never run).

---

## 1. What was actually executed for this review

Commit under test: PR #3 head `4944044` + this branch's working-tree changes
(uncommitted at run time → artifacts recorded `dirty`/`tested_code_sha`).

| Command | Result | Evidence |
|---|---|---|
| `make runtime-e2e` (real PostgreSQL 16 + Redis 7) | **PASS** — 103 tests, 90.85s | `tests/integration/runtime/` |
| `make runtime-chaos` (SIGKILL worker → lease expiry → checkpoint resume) | **PASS** — 8 steps, recovered `attempt=2`, side effect once | `artifacts/runtime/chaos-<ts>.json` |
| `make runtime-verify` | **PASS** (`distributed-runtime-evidence/v2`) | `artifacts/distributed-runtime/<ts>/report.json` |
| `make runtime-report` | `production_status: NOT_VERIFIED`, 7 fault scenarios, 2 remaining risks | `artifacts/distributed-runtime-summary/<ts>.json` |
| `make test` | **3659 passed, 120 skipped** | full suite |

`make runtime-report` explicitly prints `production_status: NOT_VERIFIED`.
Level 2 (local/CI real infra) is **not** production verification.

---

## 2. Canonical state machine (`runtime/statuses.py`)

```
PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED            (terminal)
                      │           ├──► FAILED               (terminal, permanent)
                      │           ├──► RETRYING ──► RUNNING (transient; attempt+1)
                      │           ├──► WAITING_APPROVAL ──► RUNNING (human decision; attempt unchanged)
                      │           └──► DEAD_LETTER          (terminal, retries exhausted)
                      └──► CANCELLED                        (terminal)
```

Key invariants (enforced at the DB layer via atomic conditional updates in
`runtime/run_service.py`, not merely in Python):

- `EXECUTABLE_STATUSES = {QUEUED, RETRYING}` — `WAITING_APPROVAL` is **not**
  executable, so generic polling cannot busy-loop on an unattended approval.
- `APPROVAL_RESUMABLE_STATUSES = {WAITING_APPROVAL}` — only the approval API
  explicitly re-dispatches.
- `WAITING_APPROVAL → RUNNING` via `mark_resumed_running()` does **not**
  increment `attempt` (waiting for a human is not a failed try).
- Terminality is closed: `SUCCEEDED/FAILED/DEAD_LETTER/CANCELLED` have no
  outgoing transitions.

---

## 3. Three-layer idempotency (the real exactly-once story)

Delivery is **at-least-once** (`acks_late` + `reject_on_worker_lost` +
Redis `visibility_timeout`). Exactly-once *effects* are reconstructed by three
independent layers:

| Layer | Mechanism | Key | File |
|---|---|---|---|
| Run | terminal run re-delivery is a no-op; `idempotency_key` unique constraint | `run_id` | `runtime/executor.py:256`, `runtime/run_service.py` |
| Thread | Redis per-thread lock, owner token + TTL + Lua CAS delete | `agent:thread-lock:{thread_id}` | `core/concurrency/distributed_lock.py` |
| Tool side effect | atomic claim (`INSERT … ON CONFLICT` + `SELECT … FOR UPDATE`), `mark_succeeded` owner-guarded | `operation_key = run_id:tool_call_id`; approved actions use `run_id:approval:{approval_id}` | `runtime/side_effects.py:140`, `core/hitl/gate.py:295` |

The HHITL approved-action deliberately derives a **stable** `tool_call_id` from
`approval_id` (`core/hitl/gate.py:295`), so the ledger key is constant across
every retry of the same approval — a different approval of the same order gets a
different key and is not suppressed.

---

## 4. Claim-then-crash liveness boundary (the requested deep-dive)

### 4.1 How resume works

On `WAITING_APPROVAL`, `execute_run` calls `_build_resume_command()` →
`ApprovalService.consume_resume(run_id)`. `consume_resume` performs:

```sql
UPDATE human_approvals
   SET resumed_at = now
 WHERE approval_id = :id AND resumed_at IS NULL    -- atomic claim
```

Only one caller can win. If it returns a decision, the graph resumes with
`Command(resume=decision)`; the node replays from `interrupt()` and calls
`execute_approved_actions`, which executes through the idempotency ledger.

### 4.2 Crash windows

| Window | Where it crashes | `resumed_at` | Side effect happened? | Redelivery behaviour | Outcome |
|---|---|---|---|---|---|
| **W0 — crash-before-claim** | before `consume_resume` | NULL | no | decision re-consumed; side effect executes once | **converges** ✅ |
| **W1 — crash-after-claim, before execution** | after `resumed_at` set, before `execute_approved_actions` | set | no | `consume_resume` returns None → graph re-interrupts | **parks in WAITING_APPROVAL** (safe, no duplicate) ⚠️ |
| **W2 — crash-after-side-effect, before SUCCEEDED commit** | after external write, before `mark_succeeded`/run commit | set | yes | `consume_resume` returns None → graph re-interrupts; run stays WAITING_APPROVAL | **parks** (safe from *duplicate here*; residual external-write risk) ⚠️ |
| **W3 — crash during ledger commit** | external write done, `mark_succeeded` lost | set | yes | if a future re-issue re-claims after claim-lease TTL, the external write could repeat | **at-least-once boundary** (inherent, see §4.4) |

W1/W2 are **liveness** boundaries, not duplicate-side-effect bugs. W3 is the
inherent at-least-once window of any external write.

### 4.3 Why automatic re-issue is NOT enabled (safety argument, per the brief)

The obvious "fix" — a reconciler that resets `resumed_at = NULL` for runs stuck
in `WAITING_APPROVAL` so redelivery re-consumes the decision — is safe **only if
the external write is idempotent given `operation_key`, or if the ledger commit
always landed**. W3 shows the latter cannot be guaranteed: a crash between the
external call and `mark_succeeded` loses the commit. A re-claim after the
claim-lease TTL would then repeat the external write.

Because that cannot be *proved* free of duplicate side effects without the
downstream API accepting an idempotency key, the brief's own rule applies:

> 如果无法证明自动恢复不引入重复副作用，保留人工 Runbook 和明确告警，不要强行实现。

Therefore:

- **No auto re-issue** is implemented. The run parks (safe). This is pinned by
  `tests/integration/runtime/test_hitl_resume_fault_injection.py`
  (`TestCrashDuringResume`, both W1 and W2).
- **Manual runbook** + operator action (re-decide or cancel) is documented in
  `docs/operations/`. The existing `scripts/reconcile_stuck_runs.py` only covers
  `RETRYING/QUEUED` and deliberately does **not** touch approval-claimed runs.
- **Alerting**: `remaining_risks` in `make runtime-report` names this boundary
  with a reproduction command. Adding a dedicated Prometheus alert on
  "runs in WAITING_APPROVAL older than TTL with a non-null `resumed_at`" is the
  recommended next step (NOT_MEASURED — no such series exists yet).

### 4.4 Residual external-write risk (documented, not hidden)

`runtime/side_effects.py` docstring already states: the ledger only prevents
*the same Agent re-issuing the same side effect*. End-to-end idempotency of the
downstream write requires the downstream API to accept the idempotency key.
This is an **at-least-once** boundary of the external system, independent of
HITL, and it is recorded here rather than papered over.

---

## 5. Fault-injection test inventory

All real-PostgreSQL, in `tests/integration/runtime/`:

| Scenario | Test | Expected side effects |
|---|---|---|
| approval suspend | `test_hitl_approval_flow.py` / `test_hitl_langgraph_interrupt.py` | 0 |
| approve → resume | `test_hitl_resume_fault_injection.py::test_terminal_replay_does_not_duplicate_side_effect` | 1 |
| reject | `...::TestRejectAndExpireDoNotExecute::test_rejected_resume_executes_nothing` | 0 |
| expire | `...::test_expired_approval_executes_nothing` | 0 |
| duplicate resume delivery | `...::TestDuplicateResumeDelivery` | 1 |
| decision consumed exactly once | `...::test_decision_is_consumed_exactly_once` | n/a |
| crash after side effect (W2) | `...::test_crash_after_side_effect_does_not_duplicate_on_redelivery` | 1 |
| crash before execution (W1) | `...::test_crash_before_execution_leaves_run_parked_without_side_effect` | 0 |
| concurrent decisions (one winner) | `test_hitl_approval_concurrency.py` | n/a |
| side-effect claim concurrency | `test_side_effect_claim_concurrency.py` | n/a |
| worker SIGKILL → checkpoint resume | `test_worker_checkpoint_recovery.py` (chaos) | 1 |

**Crash-before-claim (W0)** is exercised indirectly (approve→resume path starts
with `resumed_at IS NULL`); a dedicated W0 unit is a reasonable follow-up but the
behaviour is already covered by the approve→resume and duplicate-delivery cases.

---

## 6. Boundaries that must not be over-claimed

| Claim | Truth |
|---|---|
| "Exactly-once delivery" | ❌ at-least-once + three-layer idempotency. Not exactly-once. |
| "Level 2 = production HA" | ❌ Level 2 = local/CI real PG+Redis. `production_status: NOT_VERIFIED`. |
| "Auto-recovers every stuck approval" | ❌ W1/W2 park by design; manual runbook + alert. |
| "Real ERP writes verified" | ❌ staging tools only; `NOT_VERIFIED`. |
| "Multi-replica long-run stable" | ❌ `NOT_VERIFIED` (no production cluster). |
| Run event stream is a source of truth | ❌ best-effort observability; source of truth is `agent_runs`. |
