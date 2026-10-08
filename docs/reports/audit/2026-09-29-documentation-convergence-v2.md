# Documentation / Configuration / Runtime Truth Convergence v2 — Final Audit Report

> **AUDIT SNAPSHOT**
> Audited 2026-09-30 on branch `docs/convergence-v2-20260929`.
> BASE_SHA: `555835d845673d57de141d00806648aa71241bd0` (= origin/main at task start)
> FINAL_SHA: see `git rev-parse HEAD` (do not trust a hardcoded value in this file after the audit date; Snapshot SHA != Current HEAD).
> This document is valid only for this audit execution. It is not a permanent Current Truth entry.
> Current entry point: [docs/reference/current-state.md](../../reference/current-state.md).

---

## 1. Scope

Full consistency audit over: README.md, CLAUDE.md, `.env.example`/`.env.test`, Makefile, deploy/compose, docs/** (lifecycle-classified), scripts/**, OpenAPI, RAG evaluation docs, 已移除的内部材料 material, ADRs, and root audit snapshots. Classification per task §3: CANONICAL_CURRENT / GENERATED / HISTORICAL_SNAPSHOT / ADR.

## 2. Drift table (old claim → runtime truth → evidence → action)

| File | Old claim | Runtime truth | Evidence | Classification | Action |
|---|---|---|---|---|---|
| `deploy/compose/docker-compose.yml` | `OPENAI_MODEL:-Qwen/Qwen2.5-7B-Instruct` | `OPENAI_MODEL` default `Qwen/Qwen3-8B` | `core/config.py:49` | CONTRADICTION | Unified to canonical default; `LLM_ROUTER_TIMEOUT` now `${LLM_ROUTER_TIMEOUT:-8.0}` with runtime-fallback note |
| `docs/operations/llm-provider-switch.md` | recommended model `Qwen2.5-7B-Instruct`; `/health` endpoint; provider SLA table stated as current facts | default `Qwen/Qwen3-8B`; health endpoint `/api/health`; provider SLA/latency/pricing = `NOT_VERIFIED`/`NOT_MEASURED` | `core/config.py`, `api/routes/monitoring.py`, absence of artifacts | STALE / UNVERIFIED_CLAIM / BROKEN_REFERENCE | Runbook rewritten against current runtime; added `scripts/probe_provider_auth.py` + `scripts/run_production_evidence.py` sections; SLA table replaced with explicit NOT_VERIFIED statement |
| `README.md` | "Audited checkout rescue/local-work-20260929, HEAD 18c927d..." | HEAD rots by design; must be `git rev-parse HEAD` | git | STALE | Removed hardcoded HEAD block; linked `docs/reference/current-state.md`; v5.x–v6.3 per-version block compressed to changelog pointer |
| `README.md` | `npm test` = 60 cases; OpenAPI output `53`; Hit Rate@3 = 80% | counts drift; benchmark is dated dataset | `npm test` (60/60 on 2026-09-30), `app.openapi()` = 53 paths / 55 ops | UNVERIFIED_CLAIM / STALE | Verification table now command-based; RAG row cites dataset + dated historical snapshot |
| `README.md` | Per-file pytest table with 39 files × fixed counts | counts change every commit | `pytest --collect-only -q` (1767 on 2026-09-30) | STALE | Replaced with directory-level table + collection command |
| `docs/reference/api-reference.md` | "HTTP 路径 55" | 53 paths / 55 operations (49 `/api/*` ops + `/metrics/prometheus` + 5 pages) | `app.openapi()` diff vs doc rows (55 rows verified 1:1) | CONTRADICTION | Fixed counts; added `make openapi-check` drift guard note |
| `docs/reference/model-comparison.md` | "Qwen3-8B 从 Qwen3-8B 升级"; duplicate Qwen3-8B rows; "7B 参数量"; bge-small-zh-v1.5 as current production embedding; L1 MD5 + L2 Jaccard as current cache; `rag/knowledge_base.py:61/114/145` line links; provider latency/price as current | default LLM `Qwen/Qwen3-8B`; embedding `BAAI/bge-large-zh-v1.5` via HTTP API (`rag/api_embedding.py`); reranker `BAAI/bge-reranker-v2-m3` (`rag/reranker.py`); Response Cache L1 Redis + L2 Qdrant + L3 Jaccard; knowledge_base.py is an 8-line compat alias | `core/config.py`, `rag/*.py`, `rag/knowledge_base.py` | STALE / CONTRADICTION / BROKEN_REFERENCE | Full rewrite: Current implementation vs Historical estimate sections; latency/price labelled historical; links re-pointed to `rag/qdrant_knowledge_base.py` etc. without fragile line numbers |
| `scripts/evaluate_rag.py` | header/summary hardcoded "500+ 基准查询" / "500 基准查询" | prints `f"{total} 基准查询"` from metadata | code | STALE | Dynamic count; docstring updated |
| `docs/decisions/003-qwen-default-llm.md` | Status "已采纳" while runtime default changed | Superseded | `core/config.py` + ADR-007 | HISTORICAL | Status → `Superseded by ADR-007` (decision body preserved) |
| `docs/decisions/004-rag-embedding-selection.md` | Old chain reads as current | Qdrant + API embedding + ApiReranker + BM25 hybrid | `rag/*.py`, ADR-008 | PARTIALLY_SUPERSEDED | Status → `Partially Superseded by ADR-008` (decision body preserved) |
| `docs/decisions/007-current-default-llm.md` (new) | — | ADR-007: current default LLM, OpenAI-compatible interface, env vars, evidence boundary (quality/latency/cost NOT_VERIFIED), rollback | `core/config.py` | NEW | Added |
| `docs/decisions/008-current-rag-retrieval-architecture.md` (new) | — | ADR-008: current retrieval chain, embedding/reranker models, hybrid config, evidence boundary, rollback flags | `core/config.py`, `rag/*.py` | NEW | Added |
| `docs/reports/plans/2026-09-29-code-doc-alignment.md` | Treated as permanent current truth; internally contradictory about script existence | HISTORICAL AUDIT SNAPSHOT (baseline 18c927d) valid only for that audit run | git log; file existence check | CONTRADICTION / HISTORICAL | Banner with baseline SHA + explicit "scripts now exist" reconciliation note |
| `docs/reports/plans/2026-06-*` | Dated plans read as live | Historical | dates | HISTORICAL | Banner added |
| `TOOL_RESULT_CACHE_REUSE_AUDIT.md`, `TOOL_RESULT_V2_AUDIT_REPORT.md`, `docs/audit/P0_02/P0_03/CODEX_REMEDIATION_PLAN` | Root/audit-dir reports without status markers | Historical snapshots (no inbound references found via grep) | grep | HISTORICAL_SNAPSHOT | Banners added; not moved (moving adds churn without value) |
| `docs/checklists/quick-launch-checklist.md` | "1,361+ tests"; `curl localhost:6333` for prod Qdrant | "pytest must pass for current checkout"; Qdrant verified from compose network (`exec qdrant curl`, consistent with compose healthcheck; prod compose only `expose`, no host `ports`) | compose file | STALE / BROKEN_REFERENCE | Fixed; added optional `audit_doc_consistency.py` gate |
| `docs/checklists/production-readiness-checklist.md` | "48 API + 5 页面"; stale ChromaDB lines | 47 `/api` + `/metrics/prometheus` + 5 pages; historical fix lines framed (v6.3 历史修复记录) | `app.openapi()` | STALE | Fixed |
| `docs/checklists/acceptance-checklist.md`, `step-by-step-plan.md`, `audit-execution-plan.md` | v5.3.1-era execution plans referencing deleted files (`api/middleware.py`, `web/src/utils/chart.js`, `tests/unit/test_token_quota.py`) | Snapshot artifacts of the v5.3.1 audit run | content dates | HISTORICAL | Banner added (kept as evidence, not "fixed" to current state) |
| `docs/standards/conventions.md` | "tests/ 共 5000+ 条" (mixing pytest cases with KB docs) | tests/ = pytest suites + eval assets; 5000 documents live in `data/knowledge_base/knowledge_base_5000.jsonl` (verified: 5000 lines) — a dataset, not test cases | `pytest --collect-only -q`, `wc -l` | CONTRADICTION | Fixed; agent-role semantics clarified |
| `CLAUDE.md` | "26 个 unit test 文件 / 3 integration / 4 e2e"; "15 条 warm queries"; points to dated plan as Current Truth | dirs only; counts via commands (actual: unit=47 files, integration=6, e2e=7 — why hardcoded counts keep rotting) | `ls`, `scripts/warm_cache.py` (14 queries) | STALE | Counts removed; current-state entry; warm-cache count de-hardcoded |
| `docs/design/architecture-design.md` | BM25Reranker fallback still described; widget DOMPurify "CDN 依赖"; per-layer test counts; Qwen3-8B attribution in v4.2 story | ApiReranker only (BM25 reranker removed — historical change); DOMPurify vendored locally; test table command-based; v4.2 story uses "当时的历史模型" | `rag/reranker.py`, `web/static/vendor/` | STALE | Fixed |
| `docs/operations/e2e-verification-guide.md` | `tests/test_e2e_real_llm.py` path | `tests/e2e/test_e2e_real_llm.py` | file system | BROKEN_REFERENCE | Fixed (+ `../../` link fixes) |
| `docs/operations/production-operations-guide.md` | host `curl localhost:6333` in two sections | compose-network exec commands | compose file | BROKEN_REFERENCE | Fixed |
| `scripts/pre_deploy_check.sh` | checks `chromadb` import for RAG | runtime needs `qdrant_client` | `requirements.txt` | STALE | Fixed |
| `docs/reference/current-state.md` (new) | — | Current-facts entry: runtime facts via `scripts/project_facts.py`, verification commands, evidence boundary, config semantics (runtime fallback ≠ template value) | scripts | NEW | Added |
| `docs/openapi.json` | content identical but missing trailing newline; no generator | regenerated deterministically via `scripts/generate_openapi.py` (UTF-8, indent=2, trailing newline); `--check` mode guards drift | diff = 1 line | GENERATED | Regenerated + generator added |
| `.env.example` | `LLM_MAX_TOKENS=8192` / `HTTP_TIMEOUT=30` / `LLM_ROUTER_TIMEOUT=8.0` read as "defaults" vs runtime 4096/15/4.0 | semantics split: runtime fallback (code) vs deployment recommended value (template), both annotated | `core/config.py` | MISSING_DOCUMENTATION | Comments added at each override |
| `rag/*.py`, `core/config.py` comments | `v7.0`/`v7.1` feature labels read like released versions | no v7.x release; labels are internal feature milestones | `core/config.py::VERSION`=6.3 | STALE | Rephrased ("internal feature milestone" / "已移除——历史变更"); no business code touched |

## 3. Verification evidence (2026-09-30)

| Check | Result |
|---|---|
| `python3 -m compileall api auth cache collaboration core db evaluation knowledge llm media rag router scripts tools` | PASS |
| `python3 scripts/audit_doc_consistency.py` | PASS (exit 0, 36 active documents) |
| `python3 scripts/generate_openapi.py --check` | PASS (`53` paths match `app.openapi()`) |
| `python3 scripts/project_facts.py --check docs/reference/current-state.md` | PASS |
| RAG benchmark metadata | PASS (`total_queries == len(queries) == 649`) |
| `pytest --collect-only -q` (final state) | `1789 tests collected` (incl. 22 new `test_doc_consistency.py` guards) |
| `pytest -q` (full, final state) | `1784 passed, 5 skipped` (~202s; 5 skips = `real_llm` markers, need API key) |
| `npm test` | `60 passed (7 files)` |
| `npm run build` | PASS (`web/static/dist/`) |
| `ruff check scripts/audit_doc_consistency.py tests/unit/test_doc_consistency.py scripts/project_facts.py scripts/generate_openapi.py scripts/evaluate_rag.py` | PASS |
| `ruff check .` (whole repo) | 131 findings — **PRE_EXISTING_BASELINE** (conc.: alerts/notifier 26, collaboration/orchestrator 14, auth/service 14, core/monitoring 10, auth/router 10, agents/base_agent 10 …). TASK_INTRODUCED = 0 |
| `npm run lint` (biome) | FAIL — **PRE_EXISTING_BASELINE**: `web/styles/theme-a11y.css` `!important` rules (a11y prefers-reduced-motion pattern) flagged by `noImportantStyles`; verified identical on unmodified base checkout |
| Full-repo stale-term scan | see §4 |

## 4. Stale-term scan classification (`rg` per §14)

- `ChromaDB` in README/CLAUDE/Makefile → **VALID_HISTORICAL** (framed v6.0/v6.2 migration records + migration tooling `scripts/migrate_chroma_to_qdrant.py` which still must mention ChromaDB).
- `scripts/pre_deploy_check.sh` chromadb check → **STALE** → fixed to `qdrant_client`.
- `Qwen2.5-7B-Instruct` in compose → **CONTRADICTION** → fixed (now only in ADR-003/007 rollback sections = VALID_HISTORICAL).
- `bge-small-zh-v1.5` / `text2vec` / `all-MiniLM-L6-v2` in reference docs → **VALID_HISTORICAL** (all under "历史实验/历史记录" sections) — the guard enforces the framing.
- `1,361+` in quick-launch-checklist → **STALE** → replaced by command-based gate.
- `1400+` in README/CLAUDE → **VALID_CURRENT** as advisory ("do not copy historical numbers").
- `5000+ 知识库文档` → **VALID_CURRENT** (verified: `data/knowledge_base/knowledge_base_5000.jsonl` = 5000 lines; distinct from pytest counts).
- `30 条`/`500 基准`/`649` → all historical-or-derived now; benchmark count is metadata-driven in script and docs.
- `18c927` (old HEAD) → **VALID_HISTORICAL** only inside the marked 2026-09-29 plan snapshot + docs/README index description; removed from README.
- `55 HTTP` in api-reference → **CONTRADICTION** → fixed (53 paths / 55 operations distinction documented).
- `v7.0` in code comments → **STALE** → rephrased as internal feature milestone.
- `docs/superpowers/**`, `docs/archive/**`, `docs/reports/{audit,milestone,releases,plans}/**` → excluded from active scan by directory semantics; banners/markers added where content could mislead.

## 5. Facts at audit time

- runtime_version: `6.3` (`core/config.py::VERSION`)
- llm_model: `Qwen/Qwen3-8B` (base `https://api.siliconflow.cn/v1`, provider `siliconflow`)
- embedding_model: `BAAI/bge-large-zh-v1.5` (1024 dims, HTTP API)
- reranker_model: `BAAI/bge-reranker-v2-m3`
- vector_db: `qdrant_only`
- agent_roles: 9 (7 domain + ReActAgent + ResponseAgent; evaluator separate; BaseAgent not counted)
- rag_benchmark_queries: 649 (metadata-consistent)
- openapi_paths: 53 (operations 55; `/api/*` operations 49)
- pytest collected: 1789 (1784 passed / 5 skipped on full run)
- frontend tests: 60 passed / 7 files

## 6. Unverified / NOT_MEASURED (unchanged by this task)

- Provider authentication end-to-end, provider token usage/billing, provider latency/SLA
- Production FCR / human efficiency / production SLA / real-user outcomes
- Current Hit Rate/MRR on the 649-query benchmark (needs a fresh `scripts/evaluate_rag.py` run artifact)
- Qdrant point-id migration against a real production cluster (dry-run required per docs)
- Docker-based verification commands (compose exec) were derived from the compose healthcheck definitions and are documented as such; this audit machine has no Docker daemon to execute them

## 7. Remaining debt

- `pytest-counts.json` sidecar is stale (`1680`); it is CI output, not documentation — left untouched.
- Full-repo Ruff baseline (131 findings) is pre-existing; not task-introduced.
- `docs/reference/rag-evaluation-report.json` remains the 2026-06 30-query snapshot; a fresh 649-query artifact should replace/annotate it in a future evaluation run.
- `npm run lint` (biome) fails on the pre-existing `theme-a11y.css` `!important` pattern (accessibility reduced-motion); fixing it requires either a Biome rule override or a CSS rework — left as baseline debt, not touched by this task.

## 8. Delivery boundary

No production credentials, `.env` files, benchmark gold data, or main-branch mutations. Historical evidence was not rewritten into current evidence; supersession is carried by Status lines and banners.

---

## 9. Merge-gate addendum (2026-09-30, post-snapshot)

CI on the original PR HEAD `71c52f8` failed (`test 3.10/3.11/3.12`):
`check_openapi_snapshot` compared the full serialized `paths` JSON, which is
fastapi/pydantic-version sensitive (requirements.txt floats both packages;
CI resolved fastapi 0.141.1 + pydantic 2.13.5 vs snapshot-time 0.137.1/2.9.0
with an identical path→method surface). Reproduced locally by shadowing the
CI package versions (paths dict equal=False, surface equal=True) —
TASK_INTRODUCED, not baseline. Fix (commit `8f609e0`): the guard and
`generate_openapi.py --check` now compare the portable API surface + version;
canonical-config extraction uses `ast` (`extract_runtime_env_defaults`);
`.env.example` coverage is a strict contract (EMBEDDING_DIM,
HYBRID_SEARCH_ENABLED, VECTOR_DB_MODE, QDRANT_HOST, QDRANT_PORT added);
4 new non-vacuous regression tests. CI-equivalent local runs with the pinned
toolchain: unit 1412 / integration 99 / e2e(mock) 260 / stress 12 passed;
coverage lane 1770 passed, 82.26% (gate 80%). GitHub Actions run
`36608222496` on HEAD `8f609e0`: test (3.10/3.11/3.12) + security = PASS.
