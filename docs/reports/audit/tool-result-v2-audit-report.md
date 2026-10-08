# Tool Result Context Engineering V2 Audit Report

> **HISTORICAL AUDIT SNAPSHOT**
> This document does not describe current runtime state. It records the audit executed at the SHA printed below (Snapshot SHA != Current HEAD).
> Current entry point: `docs/reference/current-state.md`.


## CURRENT_HEAD

- Current checkout: `504b954efd7dc51c30d1d9fead217eaedfc499ca`
- Main at audit time: `517ef1beb971c870c7c25dc8e6e3c12d76a45801`
- Main was fetched from `origin/main` before this report was written.

## CURRENT_BRANCH

- Audited checkout: `feat/tool-result-context-engineering`
- V2 implementation checkout: `feat/tool-result-context-engineering-v2`, created from `origin/main` in a separate worktree.

## MAIN_HEAD

`517ef1b chore: harden public repository secret handling`

## WORKTREE_STATUS

The original checkout was dirty before work began. It contains modified source,
tests, SQLite WAL/SHM files, `secrets/keys.json`, and untracked Phase 1 audit,
retrieval lifecycle, and chat-record files. These changes were not reset,
stashed, or overwritten. The V2 worktree is clean apart from this audit report.

## V1_IMPLEMENTED

- `core/tool_result_optimizer.py`: deterministic structured filtering, field
  allow/deny lists, empty-value removal, deduplication, max-items, estimated
  token budget, per-tool policy, and old `ToolMessage` compaction.
- `tools/tool_registry.py`: `execute_raw()` preserves structured tool output;
  `execute()` remains the compatibility string API.
- `agents/base_agent.py`: enabled path calls `execute_raw()`, optimizes before
  constructing `ToolMessage`, preserves `tool_call_id`, message order, and
  compacts historical tool results.
- Feature flags default to disabled and existing Prometheus metrics cover raw /
  optimized size, estimated tokens, processed results, and truncation.
- Unit/integration coverage and a deterministic local benchmark exist.

## V1_GAPS

- No `ToolResultStoreProtocol`, in-memory store, Redis offload, TTL metadata, or
  controlled raw-result recovery.
- No specialized search or HTML compressor strategy.
- No optional semantic-summary interface or failure fallback.
- No explicit tool-result reference scope enforcement.
- ERP mock/real adapters expose query methods but no pagination contract.
- Existing benchmark has no offload/recovery, search, HTML, or mixed-tool rows.
- CI is defined in GitHub Actions but this audit does not treat local results as
  CI results; a new PR run is required.

## REUSABLE_INFRASTRUCTURE

- `REDIS_URL` is the existing Redis configuration. `ServiceContainer` already
  creates a synchronous Redis client for `ResponseCache`; session storage,
  token quota, metrics snapshots, and cache use the same infrastructure.
- `core.session.token_counter._count_tokens` is the existing estimator.
- `SharedBlackboard` and `EnhancedSessionManager` provide session-scoped state,
  but neither is a raw tool-result store.
- Existing ERP authorization wraps private order/customer access and must remain
  the security boundary.
- Existing Prometheus helpers in `core.monitoring` support bounded metric labels.

## RISKS

- Redis must remain an optional acceleration/offload dependency; an outage must
  not fail the agent path.
- Reference IDs must be opaque and scoped; a reference alone must not authorize
  cross-user or cross-session reads.
- Tool results, especially search/HTML content, are untrusted data and must not
  be promoted to instructions.
- Historical compaction must retain valid tool-call pairing.
- Existing dirty work in the original checkout is unrelated to V2 and must not
  be included in the feature branch.

## PROPOSED_V2_SCOPE

1. Add a store protocol with deterministic in-memory implementation and Redis
   implementation reusing `REDIS_URL`, opaque IDs, schema/version, TTL, metadata,
   scope checks, and fail-soft behavior.
2. Add reference-based offload and bounded preview to the optimizer/agent path,
   plus application-layer recovery.
3. Add JSON, search-result, and malformed-HTML compressors behind a small strategy
   registry; keep deterministic compression as the default.
4. Add optional, disabled-by-default semantic summarization with timeout and
   deterministic fallback.
5. Define and test a deterministic ERP pagination contract in the mock layer;
   do not claim real ERP pagination without backend evidence.
6. Extend metrics, benchmark, tests, design docs, README, and engineering material.

## NON_GOALS

- No main-history rewrite, force push, automatic merge, or unrelated cleanup.
- No second Redis configuration or microservice.
- No default semantic LLM call, production API-latency claim, or fabricated ERP
  integration.
- No deletion of `ToolMessage` entries and no exposure of raw sensitive results
  in logs or metric labels.

## BRANCH_AND_PR_AUDIT

- PR #4 (`feat/tool-result-context-engineering`) is closed.
- PR #5 (`feat/tool-result-context-engineering-clean`) is merged and contains
  the clean V1 implementation now present on main.
- No open PR or evidence was found requiring the old branch to remain for V2;
  the old remote branch is intentionally retained until final review because
  safe deletion and dependency ownership cannot be proven from this checkout.
