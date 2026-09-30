# 2026-09-30 Documentation Convergence v3 — Full-Repo Audit

> **HISTORICAL AUDIT SNAPSHOT**
>
> 本文件是 2026-09-30 当轮执行的审计快照，只在该时点有效；它**不是 Current Truth**，
> 以后也不会成为 Current Truth。当前事实入口仍是
> [docs/reference/current-state.md](../../reference/current-state.md) + `core/` 运行时代码
> + 当前验证命令（`python3 scripts/project_facts.py`、`python3 scripts/audit_doc_consistency.py`、
> `pytest --collect-only -q` 等）。

## 1. 审计基本信息

- **BASE_HEAD**: `4c97406c638825e5bd50d49cf6c0e2a5a2dc8f1e`（fetch 后的 `origin/main`，
  即 PR #19 "eval: prepare reproducible 649-query RAG evidence pipeline"；PR #18
  `0ad32ed0a84986ca2ab7e2b94d1d52cd537dfbb4` 为其前身，均以当前代码为准复核）
- **FINAL_HEAD**: 见本分支最新提交（`git rev-parse HEAD` 现场获取；本文件不硬编码）
- **分支**: `docs/convergence-v3-20260930`（独立 git worktree，基线为 origin/main）
- **审计日期**: 2026-09-30
- **审计版本口径**: runtime version 仍为 **6.3**（`core/config.py::VERSION`）；未创建 v6.4。

## 2. 审计范围

- Runtime/config：`core/config.py`、`core/container.py`、`core/graph_builder.py`、
  `api/app_factory.py`、`.env.example`、`.env.test`、`deploy/compose/*.yml`、`Makefile`
- RAG 实现：`rag/knowledge_base.py`、`rag/retrieval_contract.py`、`rag/bm25_retriever.py`、
  `rag/bm25_lifecycle.py`、`rag/reranker.py`、`rag/api_embedding.py`、`rag/point_id.py`、
  `rag/point_id_migration.py`
- RAG evidence pipeline：`scripts/evaluate_rag.py`、`scripts/import_eval_corpus.py`、
  `tests/unit/test_rag_eval_harness.py`、`tests/eval/rag_benchmark.json`、
  `artifacts/evaluation/rag-649/**`、`docs/reference/rag-evaluation.md`
- 文档治理：`scripts/audit_doc_consistency.py`、`scripts/project_facts.py`、
  `scripts/generate_openapi.py`、`tests/unit/test_doc_consistency.py`
- 活文档：README、CLAUDE.md、docs/reference/current-state.md、docs/reference/rag-evaluation.md、
  docs/evaluation/production-evidence.md、docs/design/architecture-design.md、
  docs/operations/e2e-verification-guide.md、docs/checklists/*、docs/README.md、changelog
- 面试材料：interview-intro / interview-deep-dive / interview-questions-final /
  interview/context-engineering-interview / reports/resume-description（最后一份冻结，未改）
- 仓库卫生：git 跟踪 × .gitignore 冲突、SQLite sidecar 文件

## 3. Fact matrix 摘要

| FACT | CODE/CONFIG 源 | 状态 |
|---|---|---|
| Runtime version | `core/config.py::VERSION`（os.getenv("APP_VERSION", "6.3")） | CURRENT_VERIFIED（`scripts/project_facts.py` 动态） |
| Default LLM provider/model/base_url | `LLM_PROVIDER=siliconflow` / `Qwen/Qwen3-8B` / `https://api.siliconflow.cn/v1`（code fallback == .env.example == compose） | CURRENT_VERIFIED（canonical config guard） |
| Embedding model/dim/provider | `BAAI/bge-large-zh-v1.5` / 1024 / HTTP API（`rag/api_embedding.py`）；`EMBEDDING_API_KEY` 未单独配置时回退 `OPENAI_API_KEY`（`core/config.py:259-262`） | CURRENT_VERIFIED |
| Reranker model/provider | `BAAI/bge-reranker-v2-m3` / HTTP API；`RERANKER_API_KEY` 独立读取，不回退 LLM key；失败回退原顺序（`rag/reranker.py`），preflight 探针识别 silent_fallback | CURRENT_VERIFIED |
| `VECTOR_DB_MODE` | `qdrant_only`（唯一有效值，ChromaDB 已移除） | CURRENT_VERIFIED |
| `HYBRID_SEARCH_ENABLED` | `true`（向量 + BM25 + RRF k=60） | CURRENT_VERIFIED |
| Agent role count | 9 个运行时角色（`core/container.py::_init_agents` 由 project_facts AST 计数） | CURRENT_VERIFIED |
| OpenAPI path/method surface | 53 paths（快照 vs `app.openapi()` surface guard） | CURRENT_VERIFIED |
| RAG benchmark query count | 649（metadata == len(queries)，动态校验；不要复制数字进新文档） | CURRENT_VERIFIED |
| RAG retrieval chain | rewrite/filter → vector + BM25 → retrieval contract → RRF → rerank → context | CURRENT_VERIFIED |
| RRF | k=60，多通道一致偏好（测试） | CURRENT_VERIFIED |
| Reranker | ApiReranker；BM25 词法重排器已移除（历史）；API 失败回退原顺序 | CURRENT_VERIFIED |
| BM25 lifecycle | `rag/bm25_lifecycle.py`，重启 rebuild | CURRENT_VERIFIED |
| Deterministic Qdrant point ID | `rag/point_id.py` + 迁移工具 | CURRENT_VERIFIED |
| Evaluation harness 配置（canonical experiments） | `vector_only` / `bm25_only` / `hybrid_no_rerank` / `hybrid_rerank`（由 project_facts 从源码 AST/regex 动态抽取） | CURRENT_VERIFIED |
| 支持的评测指标 | Hit@K / Recall@K / Precision@K / NDCG@K / MRR@K，K ∈ {1,3,5,8}；实测阶段延迟（not derived） | CURRENT_VERIFIED（harness 能力） |
| Evaluation populations | `all_queries`（主口径）/ `retrieval_eligible` / `full_gold_covered`，运行时动态计算 | CURRENT_VERIFIED（harness 能力） |
| Failure taxonomy | TIMEOUT / PROVIDER_ERROR / GOLD_NOT_INDEXED / MISS_ALL / LOW_RANK（+ 通道诊断） | CURRENT_VERIFIED（harness 能力） |
| Artifact schema | `rag-eval-evidence/v2`（v1 历史原样保留） | CURRENT_VERIFIED |
| **当前 649-query 正式指标** | 无正式 artifact | **NOT_VERIFIED** |
| Provider authentication | 已提交 preflight v1 evidence：embedding 探针 401、reranker silent_fallback（401 口径复盘 FAIL-CLOSED 正确） | NOT_VERIFIED（401 blocker 留档） |
| Provider token/billing | 无 provider 直接回报字段 | NOT_AVAILABLE |
| Production latency/P99/SLA 达标 | 无带 provenance 的生产 artifact | NOT_MEASURED |
| 缓存命中率（L1/L2/总） | 只有观察端点与指标暴露，无生产观测 | NOT_MEASURED（历史"70%"说法已从正文移除） |
| LLM 调用量节省 | 设计机制存在，无当前测量 | NOT_MEASURED |
| Deployment readiness | 部署组件（Compose 6 变体等）在 repo；真实环境复核未完成 | NOT_VERIFIED |

## 4. 发现的 drift（本轮修复项）

### P0（current-truth 语义错误）

1. **README**：`RAG 有数据` 行把 `python scripts/evaluate_rag.py` 描述为"输出当前指标"
   → 已改为 4 步 evidence pipeline（import → preflight → smoke → formal），明确 smoke
   非正式证据、正式指标 NOT_VERIFIED、历史 30-query 快照只作历史口径。
2. **production-readiness-checklist §1**：硬编码 `npm test = 60/60` 作为"当前已验证"
   → 已改为动态口径（"数量以命令输出为准"）。
3. **production-readiness-checklist §0**：gates 未包含 RAG evidence preflight
   → 已加入 `make rag-eval-import` / `make rag-eval-649-preflight` gate 及
   project_facts/openapi 检查；补充"smoke 不是正式证据"。
4. **production-readiness-checklist §2**："secrets/keys.json 已添加到 .gitignore，
   下次提交后将从跟踪中移除" 而文件实际仍在 git index；"`.env` 文件中仍存在明文
   SiliconFlow API Key" 属本机状态，不应作为公共仓库静态事实
   → 本轮已 `git rm --cached`；条目改为部署环境风险描述并链接 secret policy。
5. **architecture-design.md**：无 provenance 的当前化数字（60-70% 高频重复、5-15s→<10ms、
   LLM 调用减少 60%+、L1≈30%/L2≈40%/组合≈70%）→ 全部改为设计目标/机制描述并显式标注
   NOT_MEASURED；补 RAG evaluation architecture 小节（ablation + evidence chain，
   未与 production request path 混写）。
6. **e2e-verification-guide.md**：2026-06-20"与当前代码一致"复审口径、固定 5 PASSED
   输出、1352 collected、"可直接用于面试展示"→ 全部改为动态口径 + 显式
   **Historical Evidence** 区（§8 重命名），价格标注历史估计，加入 provider 先探针
   （`probe_provider_auth.py`）与 rag preflight 指引。

### P1（补充/完善）

7. **current-state.md**：新增 "RAG evaluation / evidence state" 小节（4 实验、指标族、
   populations、preflight blocker、canonical 命令链）。
8. **CLAUDE.md**：当前 HEAD 能力补 RAG evidence pipeline；常用命令补 4 个 RAG make 目标；
   增加禁止无 provenance 传播当前 RAG 百分比的 Agent 规则。
9. **production-evidence.md**：明确两类证据族（provider/production vs RAG retrieval
   evaluation）、metric contract 扩为 multi-K + populations + taxonomy + stage latency、
   local benchmark ≠ production outcome、RAG 行标注 NOT_VERIFIED。
10. **面试材料**：interview-intro（部署架构措辞、缓存 Q2 改估算口径、新增 Q8 RAG 证据
    口径）；interview-deep-dive（新增 Q10：ablation/为什么不能只报 Recall/populations/
    fail-closed/reranker silent fallback/provenance，两处 <10ms 移除）；interview-questions-
    final（口径头、Q4/Q5 追问弹药、Q9 SLA 配置口径、准备表更新）；
    context-engineering-interview（与 RAG 评测链的边界小节）。
11. **changelog**：Unreleased 补 PR #19（完整性描述 + NOT_VERIFIED 状态 + hygiene）与本轮
    convergence v3；未创建 v6.4。
12. **docs/README.md**：新增"真相层级速查"表（current-state = current facts 入口、
    rag-evaluation = canonical、production-evidence、reports/audit = historical snapshots、
    旧 releases 永不重写）。
13. **quick-launch-checklist.md**（P1）：新增 embedding/reranker 凭据、RAG preflight、
    Qdrant corpus/index readiness 检查项；凭据语义以代码为准（embedding 可回退
    OPENAI_API_KEY；reranker 独立 `RERANKER_API_KEY`，未配置时重排不可用而非静默成功）。
14. **Makefile**：`eval-rag` 改为 `rag-eval-649` 的显式兼容 alias（递归 `$(MAKE) --no-print-directory`），
    唯一 canonical formal command = `rag-eval-649`，两套逻辑不再并存。

## 5. 修改的文件

- README.md
- CLAUDE.md
- Makefile
- scripts/audit_doc_consistency.py（guard 扩展 A–F）
- scripts/project_facts.py（evaluation facts 抽取 + --check 增强 + formal status 标记）
- tests/unit/test_doc_consistency.py（新增回归测试：test-count framing、canonical RAG 引用、
  unproven metric claims、make target 依赖、tracked+ignored hygiene、project_facts 评测事实）
- docs/reference/current-state.md
- docs/evaluation/production-evidence.md
- docs/design/architecture-design.md
- docs/operations/e2e-verification-guide.md
- docs/checklists/production-readiness-checklist.md
- docs/checklists/quick-launch-checklist.md
- docs/design/interview-intro.md
- docs/design/interview-deep-dive.md
- docs/interview-questions-final.md
- docs/interview/context-engineering-interview.md
- docs/reports/releases/changelog.md（Unreleased）
- docs/README.md
- docs/reports/audit/2026-09-30-documentation-convergence-v3.md（本文件）
- 仓库卫生：`git rm --cached secrets/keys.json tests/data/csai.db-shm tests/data/csai.db-wal`
  （本地文件保留；`CLAUDE.md` 虽也在 ignore 列表，但作为仓库明文使用的 AI 指令文件被有意跟踪，
  与 docs/standards 交叉引用一致，本次保留并写入 guard 的 ALLOWED 白名单）

## 6. 未修改的历史文件（保护清单）

以下历史快照**原样保留**，未做 schema 回填或数字重算：

- `artifacts/evaluation/rag-649/preflight-20260929T191128Z/report.json`（v1 preflight evidence）
- `artifacts/evaluation/rag-649/import_manifest_import-20260929T190455Z.json`
- `docs/reference/rag-evaluation-report.json`（2026-06 30-query 快照）
- `docs/archive/**`、`docs/reports/milestone/**`、`docs/reports/plans/**`
- 旧 release notes（release-notes-v5.x/v6.0）与 changelog 的旧版本章节
- `docs/reports/resume-description.md`（evidence freeze 标注，按任务规则不重写历史证据）
- `docs/superpowers/**` specs（历史工作单）

## 7. 基线验证结果（修改前）

| 命令 | 结果 |
|---|---|
| `python3 scripts/project_facts.py` | OK（6.3 / Qwen3-8B / bge-large-zh-v1.5 / bge-reranker-v2-m3 / qdrant_only / 53 paths / 9 agents / 649 queries，metadata consistent） |
| `project_facts.py --check docs/reference/current-state.md` | OK |
| `python3 scripts/audit_doc_consistency.py` | OK（36 active docs，guards v2） |
| `python3 scripts/generate_openapi.py --check` | OK（53 paths surface 一致） |
| `pytest tests/unit/test_doc_consistency.py -q` | 26 passed |
| `pytest tests/unit/test_rag_eval_harness.py -q` | 30 passed |
| `pytest --collect-only -q` | 1823 tests collected |
| `npm test` | 60 passed（7 files） |
| `npm run build` | OK |
| `git diff --check` | OK |
| `git ls-files -ci --exclude-standard` | CLAUDE.md（有意保留）、secrets/keys.json、tests/data/csai.db-shm、tests/data/csai.db-wal（后三者本轮清理） |

## 8. Final 验证结果（PR #20 closeout 时实测）

最终环境：PR #20 最终 HEAD（`git rev-parse HEAD` 现场获取，收口 commit
`docs: finalize convergence v3 audit metadata`；治理收口前的基线为
`ca4636f6d571d80f83fd2c2214f00cdcfd2c8bca`）。

| 命令 | 结果 |
|---|---|
| `python3 scripts/rag_evidence_status.py` | rag_formal_metrics_status=**NOT_VERIFIED**，`rag_formal_artifact_*` 全部 null |
| `python3 scripts/project_facts.py` | OK，`rag_formal_metrics_status=NOT_VERIFIED`（artifact 推导，非 Markdown） |
| `project_facts.py --check docs/reference/current-state.md` | OK（含 canonical rag-evaluation.md 双向治理比对） |
| `python3 scripts/audit_doc_consistency.py` | OK（36 active docs，guards A–M，rule K 走 artifact 推导状态） |
| `pytest tests/unit/test_doc_consistency.py -q` | 60 passed |
| `pytest tests/unit/test_rag_eval_harness.py -q` | 30 passed |
| `ruff check`（治理脚本 + 测试） | clean |
| `git diff --check` | clean |
| CI（PR #20 push 后：tests 3.10/3.11/3.12 + security） | run 36638725103 SUCCESS |
| 总 pytest collected 数 | 以 `pytest --collect-only -q` 现场输出为准，不在本文硬编码 |

## 9. 当前 evidence boundary

- 当前 649-query 正式 RAG 指标（Hit@K/Recall@K/Precision@K/NDCG@K/MRR@K）：**NOT_VERIFIED**
  ——provider 凭据恢复并按 `make rag-eval-import` → `make rag-eval-649-preflight` →
  `make rag-eval-649` 产生正式 artifact 前，任何文档/面试材料不得出现"当前"百分比。
- 历史指标（2026-06 30-query：Hit@3 80% / MRR 0.778）只能以历史口径引用，不能作为当前结果。
- Provider auth / billing / 生产延迟 / FCR / 人效：NOT_VERIFIED / NOT_AVAILABLE / NOT_MEASURED。
- 缓存命中率与 LLM 调用节省：机制存在、观测端点存在，生产数值 NOT_MEASURED。
- 本地/fixture benchmark ≠ 生产 customer outcome（production-evidence.md）。

## 10. 剩余 UNKNOWN / NOT_VERIFIED / NOT_MEASURED

- 当前 RAG 正式指标：NOT_VERIFIED（primary blocker：provider credential 401，已留档）
- provider 真实凭据状态（expired/revoked/account-mismatch）：UNKNOWN（401 无法进一步归因）
- provider token/billing：NOT_AVAILABLE
- 生产延迟/SLA/吞吐：NOT_MEASURED
- Qdrant/Redis/PostgreSQL 真机复核：NOT_VERIFIED
- ERP real 模式：NOT_VERIFIED（mock 为当前默认）
- CLAUDE.md 的 tracked+ignored 状态：有意保留（见 §5 清单），属于仓库政策而非本轮 drift。

## 11. 当前 RAG formal evaluation 状态

分管线已在 repo 落地（PR #19）：`scripts/evaluate_rag.py`（4-config ablation、multi-K、
实测 stage latency、fail taxonomy、provenance、v2 blockers）、`scripts/import_eval_corpus.py`
（幂等导入 + BM25 rebuild + gold 覆盖审计 + manifest）、Make 目标 4 个、hermetic 回归测试
30 个。**评测从未完成一次正式运行**：preflight gate 因 provider 401 fail closed（正确行为）。
recover 路径 = 凭据 → `make rag-eval-import` → `make rag-eval-649-preflight` → `make rag-eval-649`
→ 引用带 git SHA + benchmark sha256 的新 artifact。

## 12. Repository hygiene 处理结果

- `secrets/keys.json`：从 index 移除（本地文件保留）。该文件仅含轮换台账 metadata
  （时间戳/周期/风险级，无凭据材料），但 `.gitignore` 已忽略 `secrets/`，且仓库 secret
  policy 要求公共仓库不跟踪任何 secrets/ 下文件。
- `tests/data/csai.db-shm` / `csai.db-wal`：从 index 移除。SQLite WAL/SHM 是运行时
  sidecar（主库 `csai.db` 本身未被跟踪），无代码/测试依赖（`grep` 证实），符合
  `*.db-shm`/`*.db-wal` ignore 规则。
- `CLAUDE.md`：tracked+ignored，有意保留（ALLOWED 白名单 + guard 注释）。

## 13. 后续建议

1. provider 凭据恢复后走 §11 复现路径；正式 artifact 产生后**无需改 Python 代码**——
   `scripts/rag_evidence_status.py` 会从 artifact 推导 VERIFIED，只需按 guard 提示
   把 current-state/rag-evaluation 文档行重渲染并绑定 artifact provenance
   （`project_facts.py --check` / `audit_doc_consistency.py` 双向强制），同时刷新面试材料中的指标引用。
2. 为 `scripts/import_eval_corpus.py` 补 30 个 `scene_0008xx` gold 文档（影响 80 条查询、
   主口径 GOLD_NOT_INDEXED 记账），或在 benchmark 备注该 gap 后再做正式评测。
3. 后续轮次可考虑把 `evaluation_populations` 的动态计数也纳入 project_facts
   （从最新 artifact 读取，带 artifact_path/timestamp/git_sha/schema_version 的 provenance），
   本轮克制地只取 harness 能力事实，未硬编码任何一次 401 状态。
4. 若未来决定 CLAUDE.md 不再需要跟踪，应先移除 docs/standards 中对根目录 CLAUDE.md 的
   引用，再调整 guard 白名单。

## 14. 治理收口（PR #20 后半轮，本轮新增）

PR #20 前半轮曾把 formal status 从 canonical Markdown 文本读取
（`project_facts.current_formal_status`），这属于 evidence-direction inversion
（docs → facts → guard）。后半轮已修复为：

```
artifact/evidence
  → scripts/rag_evidence_status.py        # 唯一推导入口（module）
  → scripts/project_facts.py              # machine-readable fact projection
  → docs                                  # 渲染投影，不拥有状态
  → scripts/audit_doc_consistency.py      # 一致性 verifier（双向）
```

要点：

- **Markdown 不拥有 formal evidence state**：formal status 只由
  `artifacts/evaluation/rag-649/**/report.json` 中通过 formal full-run
  contract 的 provenance-bearing artifact 推导；preflight-only / smoke / subset
  artifact 与文档声明结构上即被拒绝。
- **VERIFIED contract**：`status == VERIFIED_FULL` 且 4-config ablation
  （vector_only / bm25_only / hybrid_no_rerank / hybrid_rerank）全部执行完成
  （metrics + run_summary，n_success == executed，benchmark sha256 绑定当前
  benchmark，`evaluation_populations.primary_view == all_queries`）。
- `VERIFIED_FULL_NO_RERANK`（reranker ablation 腿缺失）**不提升** formal
  VERIFIED，一律 NOT_VERIFIED（fail-closed）；`SUBSET_SMOKE` / `PARTIAL` /
  preflight BLOCKED 同样 NOT_VERIFIED。
- 文档双向守卫：无 artifact 时文档不得 claim VERIFIED（不能自我提级）；有
  VERIFIED artifact 时仍写 NOT_VERIFIED 的文档判 stale；VERIFIED 行内必须绑定
  artifact provenance。
- 历史证据保护清单（§6）与 `evaluate_rag.py` schema / benchmark 数据本轮
  **未改动**；历史 artifact 无任何回填或重算。
- 回归测试：`tests/unit/test_doc_consistency.py` 最终 **60 passed**
  （含 artifact-driven 治理断言：
  `test_no_formal_artifact_is_not_verified`、
  `test_preflight_artifact_does_not_verify_metrics`、
  `test_smoke_subset_artifact_does_not_verify_metrics`、
  `test_valid_formal_artifact_marks_verified`、
  `test_verified_full_no_rerank_does_not_promote_formal_metrics`、
  `test_doc_cannot_self_promote_to_verified`、
  `test_verified_artifact_with_stale_not_verified_doc_fails`、
  `test_not_verified_artifact_with_fake_verified_doc_fails`、
  `test_metric_check_uses_artifact_state_not_doc_state`、
  `test_metric_claim_guard_uses_artifact_state_not_doc_state` 等）；
  `tests/unit/test_rag_eval_harness.py` **30 passed**。CI：run 36638725103。
- **当前 formal status：NOT_VERIFIED**；`rag_formal_artifact_path / timestamp /
  git_sha / schema_version` 全部 null（当前仓库尚无正式 649 full-run artifact）。

> 本节与 §8 为 PR #20 最终收口时点的实测快照；治理实现以当前仓库代码为准。

## 15. Final Review / Final Closeout（最后一轮，2026-10-01）

- **PR20_PRE_FINAL_HEAD**: `4f07746518c8f73584438c898755c199d414aebf`
- **FINAL_HEAD**: PR #20 最终合并提交（squash 后见 `git rev-parse origin/main`；
  本快照遵循既有约定不预写最终 SHA）。
- **WORKTREE_PATH**: `/home/dev/projects/csaa-convergence-v3`（`docs/convergence-v3-20260930`
  独立 worktree；主 checkout 的本地未提交改动未被触碰）。

### 15.1 本轮对既有修复的复核（对照当前代码验证，非复述 PR 描述）

| 既有结论 | 复核证据 | 结论 |
|---|---|---|
| Cache「本地/进程内/亚毫秒」表述已修正 | grep 全部 active docs：`亚毫秒/进程内读/本地读` 仅存于历史 snapshot 与 ADR 正文；`architecture-design.md`/`interview-deep-dive.md` 已改为「跳过 Router/Agent/LLM 链路 + Redis/Qdrant 网络存储 + 延迟未测量」 | VERIFIED |
| `cache/response_cache.py` 无随机向量 fail-open docstring | module/class/Args docstring（L233-234）与实现一致：`EmbeddingUnavailableError -> skip L2`（L710-722, L760, L824）；Guard P + 回归测试在位 | VERIFIED |
| `production-operations-guide.md` 引用的脚本/负向声明与仓库一致 | `scripts/probe_provider_auth.py`、`scripts/run_production_evidence.py` 存在；`scripts/restore.sh`/`smoke_test.sh`/`analyze_query_diversity.py`/`cleanup_expired_sessions.py` 确实不存在；负向 env 声明（`CACHE_TTL_PRODUCT` 等）与 `core/config.py` 一致 | VERIFIED |
| E2E 文档 CURRENT/HISTORICAL 分离 | `e2e-verification-guide.md` L95-105（当前无「5 个全部 PASS」证据声明）+ L240-251（历史 58.28s 快照标注）+ L295-303（面试话术：401/BLOCKED_BY_AUTHENTICATION 为当前 blocker） | VERIFIED |
| 面试材料量化 claim | Argon2id「100 倍+」已删（改为 memory-hard 定性）；WCAG 表述为「按 AA/AAA 对比度要求设计，完整合规认证未单独完成」；「生产级」均改为 production-oriented 框架；`resume-description.md` evidence-freeze 未触碰 | VERIFIED |

### 15.2 本轮新发现的 drift（修复）

1. **P1 — provider 切换配方失真**（`production-operations-guide.md` L110-113）：
   `export DEEPSEEK_API_KEY=...` 引用了仓库中不存在的变量。运行时恒读
   `OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL`（`core/container.py:240-274`），
   `LLM_PROVIDER` 仅作标签。已改为正确的 DeepSeek 切换配方。
2. **P1 — 连接池「环境变量」失真**（同文件原 L753-754）：声称可在 `.env.prod` 设置
   `DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW`——两者不存在，池大小硬编码于
   `db/database.py`（pool_size=10, max_overflow=20），且与同文档 L215 的正确
   负向声明自相矛盾。已改为指向代码事实。
3. **P2 — Gunicorn 调参表述失真**（同文件原 L840-842）：`GUNICORN_THREADS/TIMEOUT/
   KEEPALIVE` 被当作 env 可调项；实际仅 `GUNICORN_WORKERS`/`GUNICORN_BIND` 经
   `os.getenv` 读取，timeout=120/keepalive=5 硬编码于 `gunicorn.conf.py`。已改写。
4. **P2 — README 矛盾词对计数**：「40+ 否定/矛盾词对」与代码 `NEGATION_PAIRS = 40`
   不符，改为代码派生计数并链接来源。
5. **P2 — README「极致级生产就绪/超越99.9%」**：改为 production-oriented 框架 +
   estimate/历史自评快照标注（Guard R 精神，生产验证 NOT_VERIFIED）。

### 15.3 新增 Guard S（env 引用可解析性）

`scripts/audit_doc_consistency.py` 新增 `check_env_references` +
`collect_known_env_keys`：active docs 中 env 形态 token 必须能解析到机器可读的
config 面（`core/config.py` 模块常量、任意 Python 模块的 env 读取、
`.env.example`/`.env.test` key、compose/deploy/workflow env），或该行具备
env 使用语境（`KEY=value`/yaml 赋值形态/「环境变量/.env/export/getenv」）且
未被同线 negative（不存在/无/已移除…）或 historical 语境豁免。
提取面 over-collection 时 fail toward allowing（不误伤）。回归测试
`tests/unit/test_doc_consistency.py` 新增 5 例 + real-repo 不变量纳入 Guard S。

### 15.4 本轮验证（现场实测）

| 命令 | 结果 |
|---|---|
| `python3 scripts/rag_evidence_status.py` | NOT_VERIFIED，artifact 字段 null |
| `python3 scripts/project_facts.py` | OK（6.3 / Qwen3-8B / 53 paths / 9 agents / 649 queries / formal NOT_VERIFIED） |
| `python3 scripts/project_facts.py --check docs/reference/current-state.md` | OK |
| `python3 scripts/audit_doc_consistency.py` | OK（36 active docs，含 Guard S） |
| `python3 scripts/generate_openapi.py --check` | OK（53 paths） |
| `pytest tests/unit/test_doc_consistency.py -q` | 78 passed |
| `pytest tests/unit/test_rag_eval_harness.py -q` | 30 passed |
| `pytest -q -m "not real_llm"` | 见 PR body 的最终轮数据（以实际输出为准） |
| `ruff check`（changed py files） | clean |
| `npm test` / `npm run build` | 见 PR body |
| `git diff --check` / `git ls-files -ci --exclude-standard` | clean / 仅 CLAUDE.md（政策性白名单） |

### 15.5 剩余 evidence boundary（未变）

- RAG 正式 649 指标：**NOT_VERIFIED**（blocker：provider credential 401 留档）
- provider token/billing：**NOT_AVAILABLE**；生产延迟/SLA/吞吐：**NOT_MEASURED**
- provider 凭据失效根因：**UNKNOWN**（401 无法进一步归因）
- Qdrant/Redis/PostgreSQL 真机部署复核、ERP real 模式：**NOT_VERIFIED**
- 本节为 HISTORICAL AUDIT SNAPSHOT 的最后一轮补充；current truth 入口仍是
  `docs/reference/current-state.md`。
