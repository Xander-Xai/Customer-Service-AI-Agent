# Agent Evaluation Report — offline / real-model / human-labelled lanes

> This document is a **status report**, not a marketing number. It states, per
> lane, what was measured, what was not, and why.
>
> Canonical sources: `docs/reference/agent-evaluation.md`, `docs/limitations.md`,
> `evaluation/agent_eval/contract.py`. Where they disagree with this file, they
> win.

---

## 0. The three lanes (never blended)

| Lane | What it measures | Status | Artifact |
|---|---|---|---|
| **A. Scripted-LLM regression** | orchestration / governance / routing of the **real graph**, driven by a deterministic in-process double | **MEASURED** (`LEVEL_2_APPLICATION_MEASURED`) | `artifacts/agent-eval/<ts>/report.json` |
| **B. Real-provider** | real model's route/tool/answer behaviour | **entry IMPLEMENTED, metrics NOT_MEASURED** (no credential in this env) | `artifacts/agent-eval-real/<ts>/report.json` |
| **C. Human-labelled route accuracy** | end-to-end route correctness vs independently confirmed labels | **NOT_MEASURED** (0 human-confirmed labels) | inside Lane A artifact (`route_accuracy = NOT_MEASURED`) |

**The scripted-LLM `task_completion_rate` is NOT a real Qwen/DeepSeek success
rate.** It is already written verbatim into every Lane A artifact's
`evidence_boundaries.task_completion_rate.does_not_measure`.

---

## 1. Lane A — what was actually run

`make agent-eval` (real compiled graph, zero network, 115 cases):

| Metric | Value | n / denominator | excluded | Meaning |
|---|---|---|---|---|
| `route_accuracy` | **NOT_MEASURED** | 0 / 0 | 0 | no human-confirmed labels; denominator has no referent |
| `tool_selection_accuracy` | 1.000 | 17 / 17 | 2 | governance fidelity of the scripted plan, **not** model capability |
| `tool_argument_schema_pass_rate` | 1.000 | 22 / 22 | 0 | args pass the tool's declared JSON-schema subset |
| `expected_parameter_match_rate` (diag) | 0.950 | 19 / 20 | 0 | expected params preserved; 1 deliberate negative control drops it |
| `forbidden_tool_rate` | 0.000 | 0 / 5 | 0 | target 0, achieved |
| `hitl_trigger_accuracy` | 1.000 | 17 / 17 | 0 | HIGH deferred & not executed; low-risk not gated |
| `hitl_gate_propagation` (diag) | 1.000 | 10 / 10 | 0 | deferral → `__interrupt__` connectivity |
| `governance_outcome_match_rate` | 1.000 | 115 / 115 | 0 | graph reached expected terminal state (incl. `WAITING_APPROVAL`) |
| `task_completion_rate` | 0.962 | 101 / 105 | 10 | business completion (excl. `WAITING_APPROVAL`); fault cases stay in denominator |
| `task_completion_evidence_coverage` | 0.069 | 7 / 101 | 0 | completions backed by an actually-executed tool |
| `workflow_execution_rate` | 1.000 | 115 / 115 | 0 | no unhandled exception |
| `response_delivery_rate` | 1.000 | 115 / 115 | 0 | non-empty reply delivered (delivery ≠ resolution) |
| `fallback_rate` | 0.035 | 4 / 115 | 0 | cases that hit a real degradation marker |
| `fallback_detection_accuracy` | 1.000 | 4 / 4 | 0 | `expect_fallback` vs observed markers |
| `rule_route_agreement` (diag) | 0.243 | 28 / 115 | 0 | rule classifier vs labels (labels are LLM candidates) |
| `step_count` (diag) | 555 total | 115 cases | 0 | node executions; all within per-case budget |

`overall_status = INCONCLUSIVE` — every measurable gate passes, but
`route_accuracy` is `NOT_AVAILABLE`, so the run is **not** `PASS`. `INCONCLUSIVE`
and `PASS` are mutually exclusive by construction.

The dataset grew from 114 → **115** cases in this finalization: one
`tool_repeat_readonly_001` resilience case (the same read-only tool scripted
twice) covers the repeated-invocation/retry boundary. Its schema and selection
gates still pass; write-idempotency is **not** re-measured here (that is
`make runtime-e2e`).

### Orchestration coverage (honest, machine-readable)

The artifact's `orchestration_coverage` section reports:

- **covered**: `sequential` (96), `react` (18);
- **uncovered**: `parallel`, `consultation`, `hierarchical`;
- **cases with ≥2 agents (cross-agent hand-off): 0**.

This is a **declared gap**, not a pass. The scripted harness pins tool cases to
`react` and does not force mode selection; mode-*selection quality* needs a
human-labelled route set.

---

## 2. Lane B — real-provider entry (default OFF, fail-closed)

`scripts/evaluate_agent_real.py` / `make agent-eval-real` — schema
`agent-eval-real-provider/v1`:

- credential **only** from env (`AGENT_EVAL_REAL_PROVIDER_API_KEY` →
  `OPENAI_API_KEY`); never from the CLI, never logged (`plan.redacted()`);
- **four simultaneous conditions** required before any request:
  credential + `AGENT_EVAL_REAL_PROVIDER_AUTHORIZED=1` +
  `--i-authorize-external-calls` + plan printed first;
- configurable `--max-queries / --concurrency / --timeout /
  --max-tokens-per-request / --max-total-tokens / --max-cost-usd /
  --cost-per-1k-tokens`;
- pre-flight **worst-case** estimate (requests, tokens, cost-or-NOT_MEASURED)
  printed before execution;
- per-case evidence: route / mode / tools / args / response-nonempty / exception
  / latency / tokens / cost (tokens & cost stay `NOT_MEASURED` when the app's
  client does not surface them — never estimated);
- a failing case is recorded as `ERROR` for that case; **no** mock fallback;
- separate artifact directory and schema → cannot be merged with Lane A.

**This environment has no credential → the lane writes `NOT_MEASURED` with
`network_calls_made: 0`.** Verified by `make agent-eval-real` and by
`tests/unit/test_agent_eval_real_provider_lane.py` (14 tests: both switches
individually insufficient, credential env-only, no secret in the redacted plan,
`max_queries`/token/cost budget enforcement, per-case error recording, no
fallback).

### Preparing the reviewed business set

Lane B can consume any `--dataset`. The candidate set in
`tests/eval/agent_cases.jsonl` is `llm_candidate` (0 human-confirmed). A
30–50 case independently-reviewed set is a **data-authoring task requiring a
human reviewer** and is intentionally **not** fabricated here. The contract for
that set already exists: only `annotation.provenance=human_confirmed` with a
`confirmed_by` enters `route_accuracy`.

---

## 3. Lane C — human labels (the blocking item)

- `make agent-eval-annotation-status` → **0 / 115 confirmed**.
- `make agent-eval-real` → `NOT_MEASURED`.
- RAG reviewed-gold: `make rag-gold-reviewed-status` → `NOT_MEASURABLE
  (GOLD_LABELS_FILE_ABSENT)`; **0 JUDGED** labels; Recall/NDCG stay
  `NOT_MEASURABLE`. The shipped 649-query benchmark is never used for formal
  quality (its gold are not relevance judgements).

No Recall/NDCG/Hit@K number is published anywhere, by design.

---

## 4. Blocking lists

### 4.1 Human-labelling blockers

1. Independent reviewers to confirm `expected_route` for ≥ N cases (target 30–50)
   through `scripts/approve_agent_eval_annotations.py`.
2. A reviewer-authored `reviewed_gold.jsonl` with JUDGED records for RAG
   (grade + evidence span + `judgment_completeness`).
3. Inter-annotator agreement (e.g. Cohen's κ) so "human-confirmed" means more
   than "one person typed it".

### 4.2 Real-model blockers

1. A provider credential in the environment (never committed).
2. Both authorization switches.
3. A human-confirmed 30–50 case subset to score against.

### 4.3 Production blockers

1. Real provider SLO / p95 (currently `NOT_VERIFIED`, `make perf-evidence`).
2. LLM-as-a-Judge (currently heuristics only).
3. Real traffic / user satisfaction (`NOT_MEASURED`).

---

## 5. Reproduce

```bash
make agent-eval                    # Lane A + INCONCLUSIVE artifact
make agent-eval-contract           # metric/boundary/marker guards
make agent-eval-annotation-status  # 0 human-confirmed (expected)
make agent-eval-real               # Lane B -> NOT_MEASURED, 0 network calls
make rag-gold-reviewed-status      # RAG -> NOT_MEASURABLE (no JUDGED)
```
