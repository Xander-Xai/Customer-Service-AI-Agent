# HITL Security Matrix — risk levels, approval, negative tests

> Sources: `core/hitl/risk.py`, `core/hitl/approval_service.py`,
> `core/hitl/gate.py`, `api/routes/approvals.py`, `core/config.py`.
> Tests: `tests/integration/runtime/test_hitl_*.py`,
> `tests/unit/test_hitl_real_graph_gate.py`.

---

## 1. Risk classification (`core/hitl/risk.py`)

Priority chain (first match wins):

```
tool-declared risk_level
  > HITL_HIGH_RISK_TOOLS name allowlist
  > amount threshold (HITL_HIGH_AMOUNT_THRESHOLD over amount/refund_amount/price/…)
  > HITL_MEDIUM_RISK_TOOLS name allowlist
  > side_effect=True and no rule claimed it  -> HIGH   (fail-closed coverage gap)
  > default LOW
```

Semantics:

- **Only HIGH requires approval.** MEDIUM is recorded; LOW is never gated.
- `classify_risk`'s `side_effect` parameter defaults to `False`, so a caller must
  **explicitly** declare side-effect-ness — "forgot to pass it" cannot silently
  mean "safe".
- `requires_approval` normalises case/whitespace before comparing, so a
  `"HIGH"` casing typo cannot fail-open.

| Tool (default config) | side_effect | risk | Gated? |
|---|---|---|---|
| `staging_refund` | true | high (allowlist) | ✅ |
| `staging_order_change` | true | high (allowlist) | ✅ |
| `staging_readonly_lookup` | false | low | ❌ (read-only) |
| ERP query tools (`erp_*`) | false | low | ❌ (read-only) |
| MCP tools | false (hard-coded) | low | ❌ (write impossible by construction) |

### Startup validation (fail-closed before `DEV_MODE` early-return)

`core.config.validate_hitl_settings`: `HITL_ENABLED=true` with **both** HIGH
rules empty (`HITL_HIGH_RISK_TOOLS=""` and `HITL_HIGH_AMOUNT_THRESHOLD<=0`) →
**refuse to start**. Otherwise every tool would classify LOW and the gate would
be a no-op with no alert. (`HITL_ENABLED` default is `false`.)

---

## 2. Approval state machine (`core/hitl/approval_service.py`)

```
PENDING ──► APPROVED   (terminal)   approve | edit
        └─► REJECTED   (terminal)   reject
        └─► EXPIRED    (terminal)   TTL elapsed (treated as reject, never allow)
```

| Property | Mechanism | Verified |
|---|---|---|
| Create is idempotent | unique `(run_id, action, proposal_fingerprint)` + `IntegrityError` convergence (no SELECT-then-INSERT) | `test_hitl_approval_flow.py` |
| `decide` is DB-level CAS | `UPDATE … WHERE status='PENDING'`; N concurrent decisions → exactly one winner | `test_hitl_approval_concurrency.py` (real PG) |
| `consume_resume` claims once | `UPDATE … WHERE resumed_at IS NULL` | `test_hitl_resume_fault_injection.py` |
| Reason separation | `reviewer_id != user_id` enforced in the **service layer** (`_guard_reviewer`), not only the API | `ApprovalService._guard_reviewer` |
| RBAC | only `admin` / `supervisor` may read/decide; `customer`/`agent` → 403 | `api/routes/approvals.py::_require_reviewer` |
| TTL | default 3600s; expiry converges to `EXPIRED` on decision, read and resume paths | `test_expired_approval_executes_nothing` |
| Sanitised persistence | `sanitize_proposal`/`sanitize_text` before storing | `core/hitl/sanitize.py` |

### Double idempotency (neither alone is sufficient)

- **Approval** stops "doing what should not be done".
- **Side-effect ledger** stops "doing once-done twice":
  approved actions use `tool_call_id = approval:{approval_id}` →
  `operation_key = run_id:approval:{approval_id}` (`core/hitl/gate.py:295`).

If a HIGH tool fails to declare `side_effect=True`, `execute_approved_actions`
returns `ToolNotDeclaredSideEffect` and logs an error — it does **not** execute
without ledger protection (`core/hitl/gate.py:297`).

---

## 3. Negative-test matrix

| Negative property | Test | Expected |
|---|---|---|
| HIGH action not executed without approval | `test_hitl_real_graph_gate.py::test_high_risk_action_is_deferred_and_graph_interrupts` | deferred, `__interrupt__`, 0 side effects |
| Read-only tool not gated | `...::test_read_only_tool_is_not_gated` | executes normally |
| No run context + write tool | `...::test_without_run_context_approval_is_unreachable_but_write_is_refused` | refused (fail-closed) |
| Reviewer == requester | `test_hitl_approval_flow.py` | `SelfApprovalForbidden` |
| Reject | `test_rejected_resume_executes_nothing` | 0 side effects |
| Expire | `test_expired_approval_executes_nothing` | 0 side effects |
| Duplicate resume | `test_terminal_replay_does_not_duplicate_side_effect` | side effect once |
| Concurrent decisions | `test_hitl_approval_concurrency.py` | exactly one winner |
| Crash after side effect | `test_crash_after_side_effect_does_not_duplicate_on_redelivery` | side effect once; run parks (W2) |
| Crash before execution | `test_crash_before_execution_leaves_run_parked_without_side_effect` | 0 side effects; run parks (W1) |
| Unclaimed side-effect tool under governance | `classify_risk(side_effect=True)` → HIGH | gated |
| `HITL_ENABLED=true` with empty HIGH rules | `validate_hitl_settings` | refuse to start |

Run them: `make runtime-e2e` (103 tests) + `make agent-eval-contract`
(`test_hitl_real_graph_gate.py`).

---

## 4. What is intentionally NOT claimed

| Statement | Truth |
|---|---|
| "Real ERP refund/order-change verified" | ❌ **NOT_VERIFIED**. `tools/hitl_staging_tools.py` verifies the *governance mechanism*, not Kingdee ERP correctness. No enterprise staging environment. |
| "Staging writes == ERP writes" | ❌ explicitly distinct; staging tools are deterministic local doubles. |
| "建议转人工 = human agent handoff implemented" | ❌ a session status label + prompt hint only. No ticket, queue, agent console or push. `handoff|ticket|human_handoff` = 0 hits in production code. |
| "Approvals notify reviewers proactively" | ❌ polling queue only; no push/email/IM. |
| "Every stuck approval auto-recovers" | ❌ W1/W2 park by design (see FINAL_RUNTIME_REVIEW §4). Manual runbook, no auto re-issue. |
| `/api/chat` fast path governed by HITL | ❌ fast path has no run context; write tools there are **refused**, not approved. |
| MCP write tools | structurally impossible: only `risk_level=low`, `side_effect=False` servers are registered. |
