# Repository Truth Alignment — Audit Report (2026-10-01)

> **HISTORICAL AUDIT SNAPSHOT** — records the alignment executed on 2026-10-01.
> Snapshot SHA != Current HEAD (`git rev-parse HEAD`).
> Current entry point: [docs/reference/current-state.md](../../reference/current-state.md).

## Baseline

| Field | Value |
|---|---|
| Starting SHA (`main`, `origin/main`, `upstream/main`) | `25b484ecb4a65620e53984d5f067cd47b5a390d8` |
| Branch | `fix/dependency-and-repository-truth-alignment` |
| Date | 2026-10-01 |
| Code SHA at report authorship | `5a43f9c28a6fd377e44665f2df0bbc0a60e9990b` |
| Ending SHA | report commit on this branch — `git rev-parse HEAD` |
| Canonical repo | `Xander-Xai/customer-ai-agent` (`origin` and `upstream` both resolve here) |

## GitHub state

| Object | State |
|---|---|
| PR #19 / #20 / #21 | MERGED (PR #21 merge commit = `25b484e`, the starting SHA) |
| PR #21 Codex review | 1 × P1 inline on `requirements-dev.txt:7` — raise floor to `pytest-asyncio>=0.23.4`; addressed here |
| Issue #7 | **OPEN** — body restructured into a production-evidence backlog (Updated, not Closed) |
| Remote branches | only `main` (`origin/main` == `upstream/main`); `git remote prune` dry-run empty |
| Local branch cleanup | deleted merged `eval/rag-649-formal-evidence` (0 unique commits); retained worktree/unknown branches (see below) |

Branch cleanup result:

| Branch | Merged? | Open PR? | Unique commits | Action |
|---|---|---|---|---|
| `eval/rag-649-formal-evidence` | yes | no | 0 | **deleted** |
| `chore/ruff-baseline` | yes | no | 0 | retained (dirty worktree `/tmp/opencode/ruff-baseline`) |
| `fix/evidence-gap-remediation` | no | no | 22 | retained (unmerged recovery value) |
| `worktree-agent-*` (6) | yes | no | 0 | retained (dirty worktrees — unknown work, not overwritten) |
| `worktree-agent-ad6e567dfd4f2182c` | no | no | 1 | retained (unique commit) |

## Drift matrix

| File | Previous state | Current truth | Action |
|---|---|---|---|
| `requirements-dev.txt` | `pytest-asyncio>=0.23.0` + "floor 0.23" comment | must be `>=0.23.4` for pytest 8 | floor raised; comment corrected |
| `.github/workflows/ci.yml` | one implicit pytest contract | LANE A (pinned coverage) vs LANE B (dev compat) | lanes documented; `dev-compat` job added |
| `requirements-lock.txt` | claimed "生产部署" install entry but unused by Docker; missing 9 direct deps; stale Jinja2/MarkupSafe | non-authoritative snapshot; `requirements.txt`+Dockerfile canonical | reclassified SUPERSEDED / NON-AUTHORITATIVE |
| `Makefile` `lock` | "生成依赖锁定文件" (authoritative framing) | writes an untracked local snapshot; never rewrites the frozen file | relabeled non-authoritative; output → `requirements-lock.local.txt` |
| `Dockerfile` | installs `requirements.txt` (undocumented) | canonical deployment contract | comment documents contract |
| `pyproject.toml` | `6.0` | `6.3` | synchronized |
| `package.json` | `6.1` + description claims v6.1 | `6.3` | synchronized |
| `package-lock.json` | root `6.0` | `6.3` | synchronized |
| `auth/__init__.py`, `auth/router.py` | PBKDF2 described as primary | Argon2id primary + PBKDF2 fallback | comments corrected |
| `docs/audit/CODEX_PROJECT_REMEDIATION_SPEC.md` | "本文件是项目整改的唯一执行规格" | superseded by current-state + evidence pipeline | SUPERSEDED banner + Current Truth links |
| `docs/superpowers/**` (11) | no lifecycle marker | dated 2026-06 plans/specs | historical banners added |
| `TOOL_RESULT_CACHE_REUSE_AUDIT.md`, `TOOL_RESULT_V2_AUDIT_REPORT.md` | root, no contract requiring root path | historical snapshots | moved → `docs/reports/audit/` |
| `docs/design/governance-audit.md` | dated 2026-06 audit misfiled as active design | historical audit | moved → `docs/reports/audit/` + banner |
| `docs/README.md` | partial lifecycle coverage | every directory has a lifecycle | legend + evaluation/audit/superpowers sections |
| `docs/reports/releases/changelog.md` | no entry for this work | Unreleased entry | appended |

## Dependency contract (final definition)

- **Canonical deployment contract:** `requirements.txt` (security floors: Jinja2>=3.1.6, MarkupSafe>=2.1.5, starlette>=0.47.2, python-jose>=3.4.0, pillow). The `Dockerfile` builder stage installs it.
- **`requirements-lock.txt`:** 2026-06-06 local `pip freeze` snapshot, **NOT** used by Docker/CI/install. Labeled `SUPERSEDED / NON-AUTHORITATIVE`. Not a reproducible lock (no resolve-from-clean-env, no hashes; missing direct deps).
- **`requirements-optional.txt`:** optional media extras (subset of `requirements.txt`).
- **`requirements-dev.txt`:** LANE B dev compatibility surface.
- **Decision:** Plan B (no tooling for a hermetic transitive lock). Recorded in file headers + `make lock` (writes an untracked `requirements-lock.local.txt`, never rewrites the frozen historical file) + guard tests.
- **CI lanes:** LANE A pinned coverage toolchain (`test` job); LANE B supported dev compatibility (`dev-compat` job), which also pins and exercises the exact `pytest-asyncio==0.23.4` floor (P2 review on PR #22).

## Version contract (final definition)

Single product version source: `core/config.py::VERSION` default = **6.3**.

| Location | Value |
|---|---|
| `core/config.py` | 6.3 (env-overridable `APP_VERSION`) |
| `pyproject.toml` | 6.3 |
| `package.json` / `package-lock.json` (root + `packages[""]`) | 6.3 |
| `docs/openapi.json` | 6.3 |

Divergence is disallowed until a formal decision is written; guarded by `tests/unit/test_repository_contract.py`. Historical `v6.0/v6.1/v6.2/v5.x` references in README/changelog/release-notes are preserved intentionally.

## Documentation lifecycle

- **CURRENT** — `docs/reference/current-state.md` (single entry), `docs/reference/rag-evaluation.md`, `docs/evaluation/production-evidence.md`, `docs/README.md`.
- **DESIGN / ADR** — `docs/design/**`, `docs/decisions/**`.
- **RUNBOOK** — `docs/operations/**`, `docs/checklists/**`.
- **EVIDENCE** — `docs/evaluation/**`.
- **HISTORICAL AUDIT** — `docs/reports/**`, `docs/audit/**`, `docs/superpowers/**`, moved snapshots.
- **SUPERSEDED** — `docs/audit/CODEX_PROJECT_REMEDIATION_SPEC.md`.
- **ARCHIVE** — `docs/archive/**`.

## Verification (commands actually run)

| Command | Result |
|---|---|
| `python3 scripts/project_facts.py` | runtime 6.3, openapi 53, rag_formal_metrics_status **NOT_VERIFIED** |
| `python3 scripts/rag_evidence_status.py` | **NOT_VERIFIED**, artifact path/SHA/timestamp `null` |
| `python3 scripts/generate_openapi.py --check` | OK — 53 paths match |
| `python3 scripts/audit_doc_consistency.py` | OK — 33 active documents, 0 errors |
| `pytest tests/unit/test_doc_consistency.py tests/unit/test_ci_contract.py -q` | 100 passed |
| `pytest tests/unit/test_repository_contract.py -q` | 14 passed |
| `pytest --collect-only -q` | 1889 tests collected |
| `pytest tests/unit/ -q -m "not real_llm and not stress"` | **1508 passed** |
| `npm test` | 60 passed (7 files) |
| `npm run build` | built (vite) |
| `pip install --dry-run --ignore-installed -r requirements.txt` | exit 0; all direct deps + security floors resolved |
| `pip check` (local env) | conflicts only from unrelated globally-installed tooling (`embedchain`/`streamlit`/`mcp`/`fastmcp`); clean-env venv unavailable (`ensurepip` missing) |
| `docker build -t customer-ai-agent:truth-alignment .` | image built; smoke test `import fastapi, qdrant_client, argon2, alembic, gunicorn, pdfplumber, docx` → **deps-ok** |
| `docker compose -f …docker-compose.yml -f …docker-compose.prod.yml config -q` | exit 0 (warnings: missing `OPENAI_API_KEY`, obsolete `version` key) |

## Remaining blockers

| Item | Status |
|---|---|
| Provider authentication | **NOT_VERIFIED** (HTTP 401 / BLOCKED_BY_AUTHENTICATION) |
| Provider token usage / billing | **NOT_MEASURED** |
| Formal 649-query RAG metrics (Hit@K/Recall@K/Precision@K/NDCG@K/MRR@K) | **NOT_VERIFIED** |
| Production P50/P95/P99 latency | **NOT_MEASURED** |
| Production cache evidence | **NOT_MEASURED** |
| Tool Result Context production evidence (real Redis) | **NOT_MEASURED** |
| Real ERP integration/pagination + call reduction | **NOT_MEASURED** |
| Clean-env `pip check` | **NOT_MEASURED** (venv/ensurepip unavailable; dry-run resolve passed) |
| PR review state | pending Codex review + CI on this HEAD |

## Final status

**READY_FOR_FOUNDER_REVIEW**

- No `v6.4` release, tag, or auto-merge is created.
- Issue #7 remains OPEN and is only *Updated* by this work.
- RAG/production metrics remain NOT_VERIFIED / NOT_MEASURED — no numbers fabricated.
