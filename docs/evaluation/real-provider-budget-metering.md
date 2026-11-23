# Real-provider evaluation: metering and fail-stop contract

## Scope

This document describes the opt-in `evaluation/agent_eval/real_provider.py` runner. It is a technical evaluation module, not proof of deployment readiness or of real-model task success.

The existing scripted graph harness and real-provider lane are separate evaluation populations. Scripted replay measures orchestration and governance only.

## Risk and protection

A graph may return an outcome without provider-reported input/output Token usage or monetary cost. An operator-supplied `max_total_tokens` or `max_cost_usd` is not a reliable spending limit unless that usage is available and validated.

The runner now:

1. Requires separate explicit authorization and credential presence before external calls.
2. Validates Token counters as non-negative integer values excluding booleans, and monetary values as finite non-negative numbers.
3. Uses provider-reported monetary cost when present, otherwise derives an **estimate** only if a declared Token rate and valid input/output usage exist. An estimate is not an invoice.
4. If a requested budget cannot be reconciled after a case, reports `provider_token_usage_unavailable`, `provider_cost_usage_unavailable`, or `provider_usage_unknown_after_error` and stops scheduling further cases.
5. Keeps each case in the evidence report, including errors, instead of switching to scripted completion.

This is **not a pre-call hard spending limit**: an already-started provider request may exceed the operator's quota. A strict bound requires provider-side per-request generation caps, actual usage propagation and explicit admission control at the complete graph request boundary.

## Verification

```bash
pytest -q tests/unit/test_real_provider_missing_usage_guard.py
pytest -q tests/unit/test_agent_eval_real_provider_lane.py
```

No credential is required for these injected-runner tests. The production-like real-provider lane remains opt-in and must not run in CI with real credentials merely to demonstrate a green result.

## Open measurement gaps

- Actual provider usage is not currently surfaced by `scripts/evaluate_agent_real.py::_runner`; cost/Token usage may remain unavailable.
- A valid task-completion score requires independently reviewed business outcomes and a frozen, appropriate dataset.
- This module does not validate production ERP write operations, network SLA, or customer satisfaction.
