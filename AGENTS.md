# Repository instructions

## Reporting current project status

1. Read the live repository metadata for the default branch (currently `main`). Resolve its remote head SHA, then read the README, [implementation reference](docs/reference/current-state.md), relevant source, tests and [evidence](docs/production-readiness.md) at that exact SHA. Report the branch, SHA and check time. A local checkout may lag the remote.
2. Read current PR and Issue metadata from GitHub when reporting their state. Use the PR's actual base, draft flag and merged status. Verify that a claimed merge is present in the default branch before counting it as delivered. Use PR bodies, archived reports and conversation notes for historical context.
3. Check Actions for the exact SHA, and state what ran, passed, failed, was skipped or is pending. Report release/tag and deployment evidence separately. When a source is unavailable, say which claim could not be verified.
4. State the environment and dataset behind each finding. Distinguish implemented code, test evidence, measured results and production validation. Keep public-repository evidence separate from personal or employment history.
5. When changing a feature or interface, update its current documentation and existing generated contracts in the same PR. Keep dated audits labelled with the snapshot date.

## Project history and Memory engineering
- This customer service Agent and Beauty-Industry-RAG-QA-System belong to the SAME earlier employer/employment phase. Gaobao-Advisor is a later employer's project. Never combine their dates, performance metrics, delivery claims, or PR progress.
- Current November 2026 governance patches are not retroactive proof of previous employment-time deployment. Verify actual main-branch code, CI, runtime results and production evidence.
- The main Memory advantage here is **episode/run state**: LangGraph checkpoint + durable AgentRun + authorized tool-call receipt + approval/HITL + idempotency. These records are more authoritative for resume/replay than an LLM summary, and must never be overwritten by one.
- `EnhancedSessionManager` recent-history and summary are only conversational context. Summaries/user history MUST NOT become system instructions, order facts, approvals, or tool execution evidence. Preserve provenance of results, business identifiers, task/run ID and verified next step separately.
- On restart or checkpoint restoration: never infer that a side-effect tool has run or not run solely from summary text; inspect the run ledger and durable receipts, then retry only if the operation ID is idempotent and authorized.
- Test user-authored prompt injections in older sessions, failed summary handling, session erasure, checkpoint restart, replay and duplicate side effects. Do not copy user preference stores or Pi/OpenCode branch summaries wholesale into customer-order state.
- Read `docs/reference/memory-engineering-positioning-20261124.md` and existing `docs/reference/current-state.md` before modifying Runtime or Memory.

## Architecture change gate
- Follow [architecture decision freeze](docs/architecture.md#architecture-decision-freeze--2026-11-25). No new autonomous role, framework or remote boundary without a concrete business need, simpler baseline, failure contract and regression evidence. ResponseAgent is a deterministic postprocessor; never claim ERP exactly-once from local CAS/ledger. Separate architecture simplification from capability-gap work.

- Response text cache must not short-circuit durable work or replace tool/approval receipts. Keep response cache eligibility shared by the actual graph read and normal finalization write; fallback finalization must not cache. Do not claim semantic matching safety without independent evaluation.
