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
6. Reports aggregate Token/cost as unknown when any attempted case lacks the corresponding valid metering; an observed partial sum is not a total. `agent_cases_attempted` is distinct from provider network-call count, which is also unknown without graph-wide request instrumentation.

This is **not a pre-call hard spending limit**: an already-started provider request may exceed the operator's quota. A strict bound requires provider-side per-request generation caps, actual usage propagation and explicit admission control at the complete graph request boundary. The previous estimate of **one provider request per case** was incorrect because a compiled Agent graph can issue several model calls. The plan now marks provider-request count, worst-case Token usage and maximum cost as unavailable rather than inventing upper bounds.

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


## Scoped chat transport accounting (candidate)

The actual `OpenAICompatibleClient` now records attempts immediately before
the HTTP call across ordinary, raw and streaming paths, including retries.
A context-local collector surrounds the compiled graph case, not an unused
helper. Parallel async tasks inherit the same case scope. Startup/teardown
health-check requests are accounted separately; aggregate case totals do not
include that lifecycle overhead. The collector accepts nonnegative integer
prompt/completion counts and rejects inconsistent total counts. Missing
usage, failed HTTP calls and interrupted streams remain unknown unless
provider usage was actually observed; no assumed zero for failed attempts.
A zero-attempt case is distinct from a provider-driven case. No prompts,
responses or keys are kept by the collector. Reports contain counts only.

This covers the instrumented chat HTTP client, not independent billing,
embedding, reranker, tool transports or detached background work. Streaming
providers may not return usage; the implementation does not change their
request protocol. Existing case-capped/unmetered authorization remains in
force until full spending admission control and all external paths are
verified. TTFT is not measured here. No real external calls are executed
by the regression tests.
