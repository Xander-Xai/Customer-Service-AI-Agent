# Code / Config / Tests / Scripts / Deployment / Documentation Alignment

> **HISTORICAL AUDIT SNAPSHOT**
> Baseline SHA: `18c927d7688957648955d749d61b3959b710277c`
> This document is valid only for that audit execution. It is not a permanent
> Current Truth entry and is not maintained as facts move forward.
> Current entry point: [docs/reference/current-state.md](../../reference/current-state.md).
> Snapshot SHA != Current HEAD (`git rev-parse HEAD`).
> Note: since this audit, `scripts/probe_provider_auth.py`,
> `scripts/run_production_evidence.py` and `scripts/audit_doc_consistency.py`
> all exist in the repository; statements elsewhere in this file claiming they
> are missing were true only for the audited checkout at that time.

> Audit baseline: checkout `rescue/local-work-20260929`, HEAD `18c927d7688957648955d749d61b3959b710277c`, audited 2026-09-29.
> The working tree already contained uncommitted runtime and test changes; this audit preserves them and does not treat them as committed release evidence.
>
> Classification: `STALE`, `CONTRADICTION`, `UNVERIFIED_CLAIM`, `BROKEN_REFERENCE`, `MISSING_DOCUMENTATION`, `HISTORICAL_SNAPSHOT`, `VALID_CURRENT`.

| File | Statement / Configuration | Current documented value | Current code/runtime truth | Evidence source | Classification | Action | Verification |
|---|---|---|---|---|---|---|---|
| `.env.example` / `core/config.py` | `REACT_MAX_ITERATIONS` | `5` | Runtime default is `3` | `core/config.py:302`, `.env.example` | CONTRADICTION | Align example to `3`; document override semantics | `audit_doc_consistency.py` |
| `.env.example` / `core/config.py` | Tool Result flags | Missing | Nine public flags exist; all default off except numeric budgets | `core/config.py:310-318` | MISSING_DOCUMENTATION | Add defaults, rollback, Redis/offload and scope boundaries | Config audit |
| `.env.example` / `core/config.py` | Vector DB mode | Test/example uses `qdrant_only` | Runtime still reads compatibility variable, default `qdrant_only` | `core/config.py:299`, `rag/*` | VALID_CURRENT | Keep only as compatibility setting; do not describe Chroma as active | grep + config audit |
| `README.md` | Current state | 2026-06-25 v6.3; old test/benchmark claims | Runtime version remains `6.3`, HEAD is later; current evidence is not re-run | `core/config.py`, git log, scripts | STALE / UNVERIFIED_CLAIM | Add Current HEAD section; downgrade unsupported numbers | README review |
| `README.md` | Cache architecture | Correctly says L1/L2/L3 but conflates mechanisms in places | Response Cache is separate from Tool Result cache/store/compression | `cache/response_cache.py`, `core/tool_result_*.py` | MISSING_DOCUMENTATION | Add explicit separation and current context flow | Link/path audit |
| `README.md` | Current RAG | Vector/reranker emphasis | Hybrid vector + BM25, lifecycle, contract, deterministic point IDs/migration are present in code | `rag/bm25_lifecycle.py`, `rag/retrieval_contract.py`, `rag/point_id*.py` | MISSING_DOCUMENTATION | Document current chain and evidence limits | Path audit |
| `CLAUDE.md` | Project facts | v6.3 sync, 1400+ tests, Chroma migration history in current overview | Runtime version 6.3; current HEAD state and test count need re-run | `core/config.py`, pytest collection | STALE | Replace current overview with runtime/HEAD/evidence wording | `pytest --collect-only -q` |
| `docs/README.md` | Index | June alignment/release described as latest | Active context/evidence/security docs exist; current alignment report is missing | `docs/design/*`, `docs/standards/*`, `docs/security/*` | STALE / MISSING_DOCUMENTATION | Add active index and current audit entry | Markdown link audit |
| `docs/design/architecture-design.md` | Tool calling flow | Partial/older flow | Cache policy → execution → compression → budget → store/reference → ToolMessage → history is current | `agents/base_agent.py`, `core/tool_result_*.py` | STALE | Rewrite current flow and terminology | Source review |
| `docs/design/architecture-design.md` | Retrieval flow | Older vector-centric wording | Rewrite/filter → vector+BM25 → contract → fusion → rerank → context | `rag/*` | STALE | Update flow and lifecycle/migration notes | Source review |
| `docs/decisions/005-dual-layer-cache.md` | ADR history | Reads like current architecture | It records an earlier design | file history + current cache modules | HISTORICAL_SNAPSHOT | Mark superseded; add ADR-006 | Link audit |
| `docs/checklists/production-readiness-checklist.md` | Readiness | June 27 verified claims | Provider auth is 401/unavailable; Redis/Qdrant/production evidence not verified | current scripts/docs and absence of provider scripts | UNVERIFIED_CLAIM | Rewrite as verified / not verified / required gates | Checklist review |
| `docs/operations/production-operations-guide.md` | Provider/evidence commands | Missing current evidence boundary; includes stale operational assumptions | `scripts/probe_provider_auth.py` and `scripts/run_production_evidence.py` do not exist | filesystem + `rg --files` | BROKEN_REFERENCE | Add only existing commands; mark missing commands not implemented | link/script audit |
| `docs/reports/releases/changelog.md` | Latest release | v6.3 at top | Aug-Sep work is unreleased on current HEAD | git log | STALE | Add Unreleased section; preserve historical sections | git/log review |
| `scripts/audit_doc_consistency.py` | Regression guard | Missing | Needed stable link/script/config checks | task requirement | MISSING_DOCUMENTATION | Add lightweight checker and pytest contract | script + pytest |
| `docs/evaluation/production-evidence.md` | Evidence taxonomy | Missing in this checkout | Provider auth, billing/token use, production latency are not verified | task requirement / current environment | MISSING_DOCUMENTATION | Add explicit evidence boundary and safe commands | link audit |

## Current-state rules

- Runtime version is `6.3`; this audit does not invent `6.4`.
- `UNKNOWN`, `NOT_MEASURED`, and `NOT_VERIFIED` remain explicit states.
- Historical reports remain snapshots. They are not rewritten into the current architecture.
- A passing local test, synthetic benchmark, or mock provider run is not production evidence.

## Final verification (2026-09-29)

| Check | Result |
|---|---|
| `pytest -q` | `1768 passed, 5 skipped` |
| `pytest --collect-only -q` | `1773 tests collected` |
| OpenAPI | `53 HTTP paths` |
| `npm test` | `60 passed` |
| `npm run build` | PASS |
| `python3 -m compileall .` | PASS |
| `python3 scripts/audit_doc_consistency.py` | PASS, warnings only for explicitly historical/missing references |
| Task-changed Python Ruff | PASS for `scripts/audit_doc_consistency.py`, `core/tool_result_optimizer.py`, and task test contract files; `agents/base_agent.py` retains pre-existing findings |
| Full-repository Ruff | FAIL: `139` baseline findings before the final pre-commit attempt; no full cleanup performed |
| `pre-commit run --all-files` | NOT PASS: original YAML was invalid; after minimal YAML repair, existing `.env` hook rejected `.env.example` and `.env.dev`, and Ruff reported baseline findings |

## Change classification for delivery

| File/group | Classification | Reason | Commit policy |
|---|---|---|---|
| `README.md`, `CLAUDE.md`, `.env.example`, `docs/**` active docs, ADR-006, production evidence doc | TASK_GENERATED | Directly changed for this convergence audit | Commit |
| `scripts/audit_doc_consistency.py` | TASK_GENERATED | New regression guard required by this task | Commit |
| `core/tool_result_optimizer.py`, task contract tests | TASK_GENERATED / task-related | Minimal fixes required by current Tool Result and embedding contracts | Commit selectively |
| `agents/base_agent.py`, `core/monitoring.py`, `tests/unit/test_qdrant_knowledge_base.py` | MIXED / UNCERTAIN | Pre-existing worktree changes plus task-related hunks | Do not commit wholesale; stage only reviewed hunks |
| Existing RAG/runtime/test changes present at task start, audit reports, chat records, migration scripts | PRE_EXISTING | Present in initial `git status --short` | Leave uncommitted |
| `secrets/keys.json` | TEST_SIDE_EFFECT_REVERTED | Test-side rotation timestamp changed it; restored | Do not commit |

## Delivery boundary

No production credentials, provider responses, real user data, merge, or main-branch mutation was used. Provider authentication, billing/token usage, production latency, FCR, and human-efficiency outcomes remain `NOT_VERIFIED` / `NOT_MEASURED`.
