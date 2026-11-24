# Customer Service Agent — Memory architecture and employment boundary

> 2026-11-24 repository hardening plan. This does not retroactively prove these
> enhancements ran at the previous employer during the original project period.

## Which project is this?

This repository and `Beauty-Industry-RAG-QA-System` are from the **same earlier
employer/time period**; `Gaobao-Advisor` is from a **later, different employer**.
Do not combine their metrics, dates, model deployments or runtime claims.

## Memory contract (why this is the right place for task memory)

| Memory layer | Source of truth | Existing verified code | Engineering priority |
|---|---|---|---|
| Working / conversation | `EnhancedSessionManager` recent turns and bounded summary | `core/session/session_manager.py` | P0: label untrusted summary, avoid privilege upgrade and leaked exceptions (PR #47) |
| Episodic / workflow | LangGraph checkpoints, per-run state, actor handoff | graph runtime, PostgreSQL checkpoint integration | P1: resume after worker restart without lost work |
| Action / receipt | durable tool execution results, approval decisions, idempotency keys | tool/governance and AgentRun/worker paths | P0: never treat natural-language history as authorization or a completed side effect |
| Procedural | versioned code, AGENTS.md, policies and tests | root `AGENTS.md` | P1: codify recovery invariants |

Unlike a coding agent, a customer service system **cannot** make the summary
authoritative for refunds, order writes, approval decisions or tool completion.
A compacted handoff should contain only objective, current blocker, stable
run/task IDs, evidence *references* and next safe read-only action. Actual
effects and approvals must be replayed from the durable ledger.

## What to learn from Pi and OpenCode

- Pi's versioned compaction entries and branch summaries demonstrate how to
  preserve a handoff boundary without silently deleting historical records.
- OpenCode's structured "Objective / Next Move" compaction is a good model for a
  **task handoff summary**, but not for SQL receipts or approvals.
- For customer service, structured `TaskEpisode` handoff + checkpoint recovery
  is a **future proposal**; it is not a shipped runtime capability unless
  source/tests/CI on the default branch demonstrate it.

## Test and release gates

1. Inject a hostile instruction into old dialogue; verify neither summary nor
   graph state upgrades it to a system message or calls a side-effect tool.
2. Stop/restart a worker before and after a side effect; confirm task/run state
   and idempotent tool ledger prevent a second execution.
3. Verify approvals are still required after history compaction, checkpoint
   resume and timeout, with expired decisions rejected.
4. Confirm session deletion clears both conversational state and associated
   checkpoint data according to the established retention policy.
5. Keep all test results labeled: mocked, local integration, real provider,
   actual production. Do not infer an SLA from mock CI.

## Evidence ledger

- **Existing implementation:** session windows/summarizer, LangGraph runtime,
  checkpoint retention and tool governance have separate source paths.
- **Added hardening:** [PR #47](https://github.com/Xander-Xai/Customer-Service-AI-Agent/pull/47)
  lowers historical summary trust and removes provider-exception string logging;
  status must be checked against current GitHub PR/CI, not this dated document.
- **Not yet proven:** specific crash-recovery end-to-end results, measured
  summary accuracy and worker-restart metrics.

Pi: https://docs.pi.dev/latest/compaction
OpenCode: https://opencode.ai/docs/compaction

## Incremental implementation: episode ownership at the checkpoint boundary

The worker stamps the server-issued AgentRun ID into the graph's
`episode_run_id` channel. Recovery inspects the latest checkpoint before
calling the graph: a pending checkpoint for another run (including an
unbound legacy checkpoint) is rejected; the worker does not restart the
graph when the checkpoint backend cannot be inspected. Explicit approval
resume requires a pending checkpoint belonging to the same run. The API
fast path refuses to adopt a pending worker episode. A completed foreign
checkpoint may precede a new turn.

This is the first TaskEpisode safety slice, not a persisted natural-language
handoff or full receipt projection. Deployment needs a controlled drain or
reconciliation of unfinished pre-upgrade checkpoints. A pending foreign
episode requires operator reconciliation, not arbitrary checkpoint deletion.
Tests: `pytest -q tests/test_episode_checkpoint_ownership.py` (mocked graph
and a real in-memory LangGraph checkpointer; CI and real PostgreSQL recovery
must be verified independently). Tool execution, approval decisions and
side-effect truth remain in the existing durable ledgers. Full TaskEpisode
receipt projection, approval/side-effect crash injection, completed-result
reconciliation and production verification remain open in #49.
