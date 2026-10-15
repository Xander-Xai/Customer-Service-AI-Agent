# Data Provenance & Public-Repository Boundary

> 本文件是本仓库的**数据来源与公开边界声明**，也是
> `scripts/check_public_repository_boundary.py` 的人工对照面。
> 机器检查只能保证「没有被规则覆盖的越界内容回流」；它不能证明本文件
> 之外的任何事实。声明与代码冲突时，以代码 + `make` 命令的实际输出为准。

---

## 1. 项目定位

本仓库是**独立维护的 LangGraph 多智能体客服工程参考实现**，定位为：

> **LangGraph-based Customer Service AI Agent — multi-agent orchestration,
> hybrid RAG, tool execution, durable runtime, HITL and observability.**

它**不代表任何特定企业的官方客服系统**，也不是任何生产系统的等价镜像。
仓库中不包含、也不寻求包含：真实客户数据、企业内部订单、商业秘密、生产日志、
数据库凭据，或未经授权公开的第三方资产。

本项目的测试与运行验证，仅对**已执行的测试环境和测试条件**负责；
不代表生产高可用、安全认证或实际业务正确性得到验证。
证据分级与不可宣称清单见 [docs/limitations.md](docs/limitations.md)
与 [docs/production-readiness.md](docs/production-readiness.md)。

---

## 2. 仓库内数据清单与来源

下表列出**所有被 git 跟踪的数据文件**及其来源分类。
分类含义：

| 分类 | 含义 |
|---|---|
| `SYNTHETIC` | 人工撰写或确定性脚本生成的虚构内容，不对应任何真实主体 |
| `DERIVED-SYNTHETIC` | 从 `SYNTHETIC` 内容派生（如检索评测查询集、标注工作清单） |
| `EVIDENCE` | 本仓库 CI / 本地运行产生的验证产物，不含业务数据 |
| `CONFIG` | 配置与示例，不含凭据 |

### 2.1 示例知识库（`data/seed/`）

| 文件 | 记录数 | 分类 | 说明 |
|---|---:|---|---|
| `data/seed/product_knowledge.json` | 50 | `SYNTHETIC` | 化妆品成分/品类/护理通用科普与虚构商品线 |
| `data/seed/faq.json` | 45 | `SYNTHETIC` | 虚构品牌的售前/售后问答 |
| `data/seed/complaint_knowledge.json` | 38 | `SYNTHETIC` | 行业通用客服处置**示例话术**，非任何企业的真实售后政策 |
| `data/seed/tech_support.json` | 35 | `SYNTHETIC` | 护肤成分安全与使用方法的通用科普 |
| `data/seed/image_knowledge.json` | 5 | `SYNTHETIC` | 通用品类词（无品牌），指向本地占位图片路径 |

这些语料用于**功能验证**（检索、路由、协作模式、缓存、审批链路的演示与
测试），**不构成真实客服政策、退款承诺或交易约定**，也**不代表任何真实企业**
的退款、订单或客服系统。

语料中出现的虚构品牌名 `Aurelane` 是为本仓库**虚构**的示例品牌，与任何真实
品牌或注册商标无关，也不指向任何真实商品。语料中出现的第三方名称
（如物流承运商、支付渠道、其他化妆品品牌）仅为**事实性引用**，不表示任何
商业关联或授权。

### 2.2 检索评测资产（`tests/eval/`）

| 文件 | 规模 | 分类 | 说明 |
|---|---:|---|---|
| `tests/eval/rag_benchmark.json` | 649 queries | `DERIVED-SYNTHETIC` | 检索评测查询集。`expected_doc_ids` **不是**人工相关度判定 |
| `tests/eval/queries/single_condition_200.json` | 200 queries | `DERIVED-SYNTHETIC` | 单条件查询子集 |
| `tests/eval/gold_labels/*.jsonl` | — | `DERIVED-SYNTHETIC` | gold-label 契约数据集，`CONSTRUCTED` / `DRAFT_UNVERIFIED` / `JUDGED` / `EXCLUDED` 状态由 `rag-gold-label/v1` 契约定义 |
| `tests/eval/gold_labels/review_worklist.jsonl` | — | `DERIVED-SYNTHETIC` | 待人工标注工作清单（生成物） |

正式 649-query 检索指标状态为 **`NOT_VERIFIED`**：现有 gold 集不含人工相关度
判定。在出现带 provenance 的正式证据 artifact 之前，本仓库**不生成、不引用、
不传播任何 Hit@K / MRR / NDCG / Recall 百分比**。见
[docs/reference/rag-evaluation.md](docs/reference/rag-evaluation.md)。

### 2.3 运行期生成的知识库（未跟踪）

`data/knowledge_base/` 由 `scripts/generate_knowledge_base.py` **确定性生成**
（固定随机种子），属于 `SYNTHETIC`，且被 `.gitignore` 排除，不随仓库分发。
规模以脚本 `--validate` 的当前输出为准，不在任何文档中硬编码。

`scripts/import_real_docs.py` 提供**导入自有文档**的能力。它是一个通用导入器，
仓库内**不附带**任何待导入的真实业务数据；使用者必须自行确保导入数据已脱敏
且具备合法使用权限。

### 2.4 证据产物（`artifacts/`）

`artifacts/` 中的 JSON 均为本仓库自身测试/演练的验证产物（分布式 runtime、
崩溃恢复、Prometheus 告警端到端、RAG preflight / gold provenance、
flake 调查等）。按 [artifacts/README.md](artifacts/README.md) 的规则生成，
其中：

- 数据库连接串只出现 `localhost` 且口令已脱敏；
- 不包含 token、密钥或任何业务数据；
- **provenance 覆盖是部分的，不是逐份齐备。** 以
  `git ls-tree -r main -- artifacts/` 的当前输出为准，已跟踪的 **354** 份 JSON
  构成如下**互斥**分组（合计 354）：

  | 分组 | 份数 | 位置 / 说明 |
  |---|---:|---|
  | 带 `tested_code_sha` + `artifact_commit_sha` + `generated_at` | 9 | `artifacts/distributed-runtime/*/report.json`（schema 值 `distributed-runtime-evidence/v2`） |
  | 带 schema 标识但不带 `tested_code_sha` | 16 | 含上表 9 份中余下 7 份带 `generated_at` 的，以及 9 份仅带 schema 的 |
  | 无任何 schema 标识 | 329 | `flake-investigation/` 320、`runtime/chaos-*` 8、`observability/` 1 |

  带 schema 的 25 份由 `distributed-runtime-evidence/v2`×9、`v1`×1、
  `runtime-suite-diagnostics/v1`×4、`crash-kill-gate-probe/v1`×2 及 8 个单例构成；
  **在被跟踪的这 354 份中，带 `code_provenance` 块的是 0 份**。329 份无 schema 的
  **生成于 provenance 契约之前**，它们**按原样保留、不重写**：可追溯性由提交历史
  本身保证，而不是由 artifact 内的自述字段保证。
- **`code_provenance` 由生成器写入，尚未成为全库强制不变量。** 调用
  `core/code_provenance.collect_code_provenance()` 的生成器共 7 个：
  `scripts/report_distributed_runtime.py`（`make runtime-report`）、
  `evaluate_agent.py`（`make agent-eval`）、`evaluate_agent_real.py`、
  `evaluate_rag_reviewed_gold.py`、`measure_performance.py`、
  `run_rag_ablation.py`，以及 `scripts/test_worker_crash_recovery.py`
  （`make runtime-chaos`）。这些会写入 `publication_status` / `dirty` /
  `tracked_diff_sha256` / `untracked_source_sha256`。
  - **`make runtime-chaos` 的缺口已于 2026-10-15 补齐**（`scripts/
    test_worker_crash_recovery.py::main` 现在把 `code_provenance` 放在报告顶层）。
    验证口径：①`tests/unit/test_runtime_chaos_provenance.py` 覆盖 PASS / FAIL /
    脏工作区 / git 不可用 / 旧产物兼容；②在真实 PostgreSQL + Redis + 多进程
    Celery 上执行 `make runtime-chaos` 得到 `result=PASS`、8 个步骤、
    `code_provenance.publication_status=DIRTY_WORKTREE_NOT_REPRODUCIBLE`
    （当次运行确有未提交改动，如实标注）。
  - **该声明只覆盖"经此修改后的生成器所产生的报告"**：上述 8 份历史
    `artifacts/runtime/chaos-*.json` **仍然没有** `code_provenance`，也**不会**被
    补写或重新生成。仓库内不存在"所有 artifact 都带溯源"这一事实。
  - 采集器与混沌判定严格解耦：采集失败只会让 `code_provenance.publication_status`
    变成 `PROVENANCE_UNAVAILABLE` 并附 note，**不会**改变 `result` 或退出码。
  - `publication_status` 的取值语义（`core/code_provenance.py`）：
    `REPRODUCIBLE`（已提交且干净）/ `DIRTY_WORKTREE_NOT_REPRODUCIBLE`（有未提交
    改动，`commit_sha` 不足以指代被测代码）/ `UNPUBLISHABLE_GENERATOR_MUTATED_SOURCES`
    （生成器改动了被测源）/ `PROVENANCE_UNAVAILABLE`（读不到 `commit_sha`，例如
    非 git 目录或无 HEAD —— 此时**不得**声称可复现）。

### 2.5 凭据与配置

| 文件 | 分类 | 说明 |
|---|---|---|
| `.env.example` · `.env.test` | `CONFIG` | 仅占位符；`.env.test` 全部 Mock 化并随仓库分发 |
| `.env` · `.env.dev` · `.env.prod` · `.env.prod.generated` | — | **不跟踪**（`.gitignore`），本机生成 |
| `deploy/` · `Dockerfile` · `docker-compose*.yml` | `CONFIG` | 生产凭据由 `scripts/generate_prod_env.py` 生成，不入库 |

凭据扫描由 `scripts/check_secrets.py`（工作区 + 可选 `--history` 模式）执行，
并有独立的
[Historical secret scan](.github/workflows/secret-history-scan.yml) workflow。

---

## 3. 工具执行与 ERP 边界

`erp/` 提供金蝶（Kingdee）ERP 的 Mock / Real 适配器。**仓库不包含任何真实
ERP 凭据、租户标识或接口资料**；`ERP_MODE=real` 需要使用者自行提供
`ERP_BASE_URL` 与凭据。

退款、订单修改等**有副作用的工具调用**，在本仓库中用于演示：

- 工作流编排与风险分级（`core/hitl/risk.py`）；
- 拦截时机（执行**之前**摘出到 `pending_actions`）；
- 人工审批与职责分离（`core/hitl/`）；
- 幂等与副作用记账（`runtime/side_effects.py` ledger）；
- 故障注入与崩溃恢复（`make runtime-chaos`）。

**这些不应被理解为已接入真实企业 ERP，或具备真实交易执行能力。**
副作用治理机制是在确定性 staging 工具（`tools/hitl_staging_tools.py`）上验证的；
真实 ERP 写操作状态为 **`NOT_VERIFIED`**——本仓库没有企业 staging 环境，
详见 [docs/design/human-in-the-loop.md](docs/design/human-in-the-loop.md)。

除非使用者拥有一个**明确、可核验且已获授权的独立环境**，本项目不会、也不应
连接真实 ERP 写入路径或任何真实客户数据。

---

## 4. 已执行的公开内容审计（结果，非承诺）

以下为本仓库在 2026-10 治理变更中**实际执行**的审计结论。它是对已检查范围的
陈述，不是对未来状态的绝对保证。

| 检查范围 | 结论 |
|---|---|
| `data/seed/*.json`（173 条记录，全量通读） | 未发现真实姓名、手机号、地址、账号、订单号、退款单号、交易流水、URL/IP、内部接口资料；未发现成本/毛利/供应商等内部经营数据 |
| `artifacts/**`（356 个文件） | 未发现凭据或业务数据；DSN 仅 `localhost` 且口令脱敏 |
| `.env.example` / Docker / CI / Compose | 仅占位符；无真实凭据 |
| 跟踪文件全量关键词扫描 | 已移除内部产品代号、自评验收材料、背诵式问答卡与对外业务指标数值 |
| 评测资产（`tests/eval/`） | 全部为合成/构造数据，无人工真实标注来源的企业数据 |
| Git 历史（**仅公开引用**：普通 `git clone` 的引用集） | 2026-10-15 复核：`--history` 报告 **1025** 处命中，全部为**文字类**残留（内部代号 / 职业措辞 / 自评评分 / 正式材料措辞 / 对外指标措辞）；凭据扫描 **0** 命中 |
| Git 历史（含本机私有分支与 backup tag） | 同一命令在本机 clone 上报告 **1218** 处。**命中数不等于泄漏数**：差额来自未发布的本地引用，不构成公开暴露 |

> **复核方法（可重放）**：结果**随引用集变化**。`--history` 走
> `git rev-list --all`，因此凡是额外抓取了 `refs/pull/*`、未发布的本地分支或
> backup tag 的 clone，都会得到更大的数字。要复现「公开克隆者实际能看到什么」，
> 用一次**普通 clone**（默认只抓 `refs/heads/*` 与 `refs/tags/*`，不含
> `refs/pull/*`）再运行：
>
> ```bash
> git clone https://github.com/Xander-Xai/Customer-Service-AI-Agent.git
> cd Customer-Service-AI-Agent
> python3 scripts/check_public_repository_boundary.py --history --skip-secrets
> ```
>
> 注意 `--mirror` **不可**用于此复现：它是裸仓库、没有检出工作树，
> `make` 与脚本都无法直接运行。CI（`Historical secret scan`）用的就是这个形态；
> `--new-only` 门禁在公开引用集上通过，在带本地私有分支的 clone 上会误报。
> 上表 1025 是**删除陈旧分支之后**的实测值；删除前为 1034，差额 9 处正是
> `docs/clarify-independent-reproduction-20261014` 带来的。

**已知的例外与残留**（如实声明）：

1. **Git 历史仍包含已删除的旧文件。** `docs/finalization/INTERVIEW_GUIDE.md`
   与 `docs/finalization/RESUME_CLAIMS.md` 曾在公开分支上提交过，其对象在
   历史中仍可通过已知 SHA 访问。本仓库**未**执行历史重写（未经授权）。
2. **历史 tag 指向的提交仍包含旧的内部产品代号。** `v1.0.0` / `v1.0.1` /
   `v6.3` tag 指向的提交未做重写。**Release 描述已于 2026-10-15 修正**：
   `v1.0.0` 描述中的内部代号改为中性项目名（版本号、发布时间、tag 指向与
   技术变更事实均未改动），`v1.0.1` 描述经检查原本就不含代号。
3. **本文件不覆盖外部系统。** 本仓库的检查不扫描第三方 SaaS、镜像仓库、
   包注册表或已分发的容器镜像内容。
4. **GitHub 对已关闭 PR 保留 `refs/pull/N/head`。** 2026-10-15 审计发现：
   已关闭且从未合并的分支 `docs/clarify-independent-reproduction-20261014`
   （PR #7，tip `b407f24`）仍在发布状态，并以 HTTP 200 直接提供上述两份
   职业材料（`main` 与三个 release tag 均为 404）。该分支**已删除**，
   `git ls-remote --heads` 与 GitHub API 均确认引用消失。提交本身仍可通过
   `refs/pull/7/head` 取得，因此本条与第 1 条一样，只能由**经授权的**历史
   重写彻底消除。

如需彻底消除第 1、4 项，需要一次**经授权的**历史重写或 tag 迁移操作；
该操作会影响所有已有 clone，属于破坏性变更，不在常规治理范围内自动执行。

---

## 5. 机器守卫

| 守卫 | 作用 |
|---|---|
| `scripts/check_public_repository_boundary.py` | 阻止个人职业档案与口述稿、内部代号、自评验收话术、对外业务指标数值、未经授权的凭据与示例数据来源问题回流 |
| `scripts/check_secrets.py` | 凭据扫描（工作区 / `--history`） |
| `scripts/audit_doc_consistency.py` | 文档与代码事实一致性（证据状态、指标声明、链接、配置） |

运行方式：

```bash
python3 scripts/check_public_repository_boundary.py                  # 当前代码树：违规即失败
python3 scripts/check_public_repository_boundary.py --history        # 全量历史清点：含已知遗留，退出码 2
python3 scripts/check_public_repository_boundary.py --history --new-only  # 基于已审快照的增量历史门禁
python3 scripts/check_secrets.py --history --new-only                # 同一快照的新增历史凭据门禁
```


历史增量门禁的固定基线为 `scripts/public_history_baseline.json` 中经审核的 Git
对象引用快照。既有的历史残留**不被宣布为安全或被清除**：`--history` 仍可以完整
列出它们；`--history --new-only` 仅允许这批已知对象保持现状，而**新的可达
提交／删除后仍留在历史中的违规内容**会使定期 workflow 失败。基线不得随意
更新为最新主分支或通过 `continue-on-error` 绕过；确需刷新时必须独立审查
新增对象及其来源。历史扫描范围以执行环境实际获取的 Git refs 为限，
不等于已清除 GitHub 上所有旧 PR、Release 或第三方副本。

守卫的规则是**可枚举**的：新增一条边界规则，必须同时在
`tests/unit/test_public_repository_boundary.py` 中补上正例与反例，
并在本文件中更新对应说明。守卫不会打印命中的敏感原文，只报告
`文件:行号` 与规则 ID。
