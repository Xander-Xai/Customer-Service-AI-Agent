# Evaluation

> 🟢 CURRENT — 本文回答："这个项目的效果怎么衡量？你测过什么？测出来的数字是多少？"
>
> **核心立场**：把 `VERIFIED` / `NOT_VERIFIED` / `NOT_MEASURED` / `UNKNOWN`
> 当作**一等公民**。本文里的每个数字都标注证据来源；没有证据的地方直接写
> "未验证"，不写"应该还行"。
>
> 口径与契约的 canonical 位置是
> [reference/rag-evaluation.md](reference/rag-evaluation.md)；
> 本文是入口与解释层。

---

## 1. 证据分级（本项目最重要的一个约定）

| 级别 | 含义 | 本项目现状 |
|---|---|---|
| **Level 1 — IMPLEMENTED** | 代码存在且可读，能指出 `file:line` | 图编排、工具循环、RAG、分布式 Runtime、HITL、OTel |
| **Level 2 — CI VERIFIED** | 真实基础设施 + 命令 + 可复查 artifact | `make runtime-e2e`（真实 PG + Redis + 多进程 Celery）、`make runtime-chaos`（SIGKILL worker 恢复）在 CI lane 上跑；OTel Collector 传输链路（`make otel-collector-smoke`）为 **LOCALLY VERIFIED**，不在 CI lane |
| **Level 3 — 生产验证** | 真实生产集群 / 多副本 / 真实流量 | **全部 NOT_VERIFIED** |

> **Level 2 ≠ 生产验证。** 本仓库任何文档都不得把 Level 2 说成"生产集群已验证"。

`NOT_MEASURED` 与 `NOT_VERIFIED` 也不是同义词：

- **NOT_MEASURED** = 没跑过，将来能跑
- **NOT_VERIFIED** = 跑了/审查了，结论是"无法确立"（例如根因是数据集缺陷）
- **UNKNOWN** = 不知道该测什么

机器守卫：`scripts/rag_evidence_status.py` 从 artifact **派生**状态，
文档不能自证；`scripts/audit_doc_consistency.py::check_unproven_current_metrics`
禁止在没有 provenance 的情况下写百分比。

---

## 2. 测了什么：四类评测

| 类别 | 测什么 | 状态 | 入口 |
|---|---|---|---|
| **A. 检索质量（RAG）** | Hit@K / Recall@K / Precision@K / NDCG@K / MRR@K，4 配置消融 | 流水线 **Implemented**；**正式指标 NOT_VERIFIED 且当前不可测** | `make rag-eval-649` |
| **B. 分布式 Runtime** | 跨进程 checkpoint、同 thread 串行、跨 thread 并行、工具幂等、崩溃恢复 | **Level 2 已验证**（有 artifact） | `make runtime-e2e` / `make runtime-chaos` / `make runtime-verify` |
| **C. 可观测性链路** | 5 个语义 span 是否真实到达 Collector | **Level 2 本地已验证** | `make otel-collector-smoke` |
| **D. 响应质量** | 完整性/准确性/简洁性/礼貌/相关性 | **Implemented，但是启发式**（关键词打分，非 LLM 判官） | `agents/evaluator.py` |
| **E. Agent 行为回归** | Agent 选择、工具选择、路由正确性 | **未实现** | — |

---

## 3. A. RAG 检索评测：流水线是真的，数字现在拿不出来

### 3.1 实现了什么

- **语料**：`scripts/generate_knowledge_base.py` 生成 5000+ 化妆品知识文档；
  `make rag-eval-import` 幂等导入 Qdrant + 重建 BM25 + 产出 manifest
  + **gold 覆盖率审计**。
- **数据集**：`tests/eval/rag_benchmark.json`，metadata 声明 649 条 query，
  按类别与难度分层。
- **指标**：`hit@{k}` / `recall@{k}` / `precision@{k}` / `ndcg@{k}` / `mrr@{k}`，
  multi-K = 1/3/5/8（`scripts/eval_contract.py:113-118`）。
- **消融** 4 个配置（`scripts/eval_contract.py:36-41`）：
  `vector_only` / `bm25_only` / `hybrid_no_rerank` / `hybrid_rerank`。
- **三种统计口径**（分母运行时动态计算，禁止硬编码）：
  `all_queries`（主口径，end-to-end）/ `retrieval_eligible` / `full_gold_covered`。
- **失败分类**：`TIMEOUT` / `PROVIDER_ERROR` / `GOLD_NOT_INDEXED` / `MISS_ALL` /
  `LOW_RANK`——保证"没召回"和"跑挂了"不会混进同一个平均数。
- **preflight gate**：`make rag-eval-649-preflight` 在全量跑之前先验
  provider auth / Qdrant 可用性 / BM25 索引，**不通过就不产生 artifact**。

### 3.2 为什么现在没有数字（这是重点）

**当前 649-query 正式指标状态：`NOT_VERIFIED`。**
而且不是"暂时没跑"，是**当前用这份数据集测不出来**：

> 根因审计（Issue #99，`scripts/rag_gold_label_provenance.py`）结论：
> `FORMAL_RETRIEVAL_METRICS_NOT_MEASURABLE_FROM_CURRENT_GOLD`

| 事实 | 含义 |
|---|---|
| 649 条 query 中仅 49 条能复现仓库唯一记录过的生成方式（同类别随机抽样） | 其余 600 条 gold 来源无仓库记录 |
| **没有任何一条 gold 是 relevance judgement** | gold 是"随机同类别文档"，不是"人工判定相关" |
| 另有 160 个 gold id 不在 5000 条语料中 | 40 条 query 全部 gold 缺失 |
| 上一轮 preflight 记录 `EMBEDDING_PROVIDER_AUTH`（401）+ `VECTOR_INDEX_EMPTY` | 那次是**环境问题**；当前主 blocker 是**数据契约问题** |

**为什么必须坚持不发数字？** 用"随机同类别文档"当 gold 算出来的
Hit@5 / MRR 是一个**看起来合理、实际无意义**的数字——
它衡量的是"检索器能不能召回随机同类的文档"，不是"能不能找到正确答案"。
**发这种数字比不发更糟**：它会在评审时被当作已验证结论，然后误导后续所有优化。

### 3.3 修复路径

已完成的部分：

- **Issue #99**（已关闭）：静态 provenance / root-cause 审计已落地。
- **Issue #119**（已关闭，PR #128）：版本化、provenance-enforced 的 gold 标注契约
  `rag-gold-label/v1` + 确定性离线校验器（`scripts/gold_label_contract.py`、
  `scripts/validate_gold_labels.py`）。新的 label 必须带
  `annotator` / `method` / `agreed` 等 provenance，且能被离线校验。

还缺的一步：**人工相关性标注 + 标注一致性（Kappa）度量**。
这是路线图项，不是本次收尾范围（需要标注预算）。

### 3.4 历史数字的正确用法

仓库里存在一份 2026-06 的 **30-query ChromaDB 历史快照**
（`docs/reference/rag-evaluation-report.json`）。
它**只能作为历史对比叙事**，不得当作当前事实；
且它评测的是已被 Qdrant 取代的旧向量库，不反映当前检索架构。

---

## 4. B. 分布式 Runtime：这是本项目证据最硬的部分

### 4.1 测什么

| 检查 | 断言 |
|---|---|
| `checkpoint_cross_process` | worker A 写下的 checkpoint，worker B 能读到并续跑 |
| `same_thread_serialization` | 同 thread 第二个获取者在持有期间**拿不到**锁，释放后能拿到 |
| `different_thread_parallelism` | 不同 thread 能**同时**持锁（不误串行） |
| `tool_idempotency` | 重复投递同 `operation_key` 的副作用工具，**实际执行次数 = 1** |
| 崩溃恢复 | `SIGKILL` worker → 未 ACK 任务经 `visibility_timeout` 重投 → lease 过期后接管（attempt+1）→ **从 checkpoint `next` 续跑** → 副作用仍然只发生一次 |

### 4.2 artifact（可复查）

| artifact | schema | 结论 |
|---|---|---|
| `artifacts/distributed-runtime/<ts>/report.json` | `distributed-runtime-evidence/v2` | `overall_status: PASS`，带 `tested_code_sha` + `generated_at` |
| `artifacts/runtime/chaos-<ts>.json` | — | `result: PASS`，8 步含 `sigkill_worker_group` / `restart_worker` / 恢复后 `attempt: 2` / `worker_switched: true` / `ledger_hits: 1` |
| `tests/integration/runtime/` | — | 91 个真实基础设施测试（真实 PG + Redis + 多进程 Celery） |

`make runtime-e2e` 始终注入 `TEST_DISTRIBUTED_DB_URL` / `TEST_REDIS_URL`，
**基础设施缺失时是硬 FAIL，不静默 skip**。这是有意的：
一个会因为"环境没配"而变绿的测试，比没有测试更危险。

### 4.3 没测的（Level 3）

真实生产集群 / 多副本长期运行 / 真实用户流量 / 真实 ERP 写操作 /
大规模 queue backlog / K8s autoscaling / multi-region。
**不宣称。**

---

## 5. C. 可观测性链路验证

`make otel-collector-smoke` 对**真实运行的** OTel Collector
（`otel/opentelemetry-collector:0.162.0`，OTLP/gRPC）验证：

- `setup_tracing(app)` 真的装了 TracerProvider
- 应用真的 `force_flush` 成功
- **5 个语义 span 全部到达**：`csai.agent.execute` / `csai.agent.execute.resume` /
  `csai.rag.retrieve` / `csai.llm.chat_completion` / `csai.tool.execute`
- 隐私 canary **未出现**（敏感属性确实被过滤掉了）

artifact：`artifacts/observability/otel-collector-<ts>/report.json`
（`otel-collector-evidence/v1`，`status: VERIFIED_LOCAL`，带 git sha 与 dirty 标记）。

**未验证**：持久化/可查询 trace 后端（collector 目前只有 `debug` exporter）、
真实流量下的 span 采样与保留策略。

---

## 6. D. 响应质量：启发式，不是判官

`agents/evaluator.py` 的 `ResponseEvaluator` 对五个维度打分：
完整性 / 准确性 / 简洁性 / 礼貌 / 相关性。

**它怎么打分？** 关键词与句式启发式（例如"您好"计入礼貌、
长度阈值计入简洁性）。**不是 LLM-as-judge。**

| 优点 | 局限 |
|---|---|
| 无模型调用、完全确定性 | 测不了语义质量（"答非所问但用词礼貌"会给高分） |
| 可作 CI 门禁，无 API 依赖 | 分数与人工判断的相关性未验证 |
| 无出网，适合离线 demo | 无法泛化到开放式回答 |

**它真正的用途是"要不要升级重试"的触发器**，不是质量度量。
在 `agents/response_agent.py:316` 低分时升级协作模式重跑一次。

### 6.1 Agent 行为回归评测：未实现（当前最大评测缺口）

这是当前最大的评测缺口。企业 Agent 项目的评测重心应该是
"Agent 选择是否回归 / 工具选择是否正确 / 路由是否走偏"，
而本项目**没有**这套评测：

- 仓库里 `evaluation/` 只覆盖检索证据与 provider 证据；
- 曾有过 `evaluation/agent_eval/` 的设计（源码从未提交，
  只在 gitignore 的 `.pyc` 里留下痕迹），**当前不可运行**；
- 没有 agent 行为的 golden 用例集。

补齐方向见 [PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P2-1 / P2-2。

---

## 7. 质量门禁：CI 里到底卡什么

| 门 | 内容 | 阻塞性 |
|---|---|---|
| `lint` | 固定版本的 `ruff check` + `ruff format --check` | **阻塞** |
| `test` | 3 个 Python 版本矩阵；MCP 契约**禁止静默 skip**；覆盖率 `--fail_under=80` | **阻塞** |
| `dev-compat` | pytest 8 时代兼容性通道；`pytest-asyncio` 下限断言 | 阻塞 |
| `runtime-e2e` | 真实 PG + Redis + 多进程 Celery + chaos 脚本，**断言 `result == "PASS"`** | 阻塞 |
| `security` | 严格 mypy（7 个新模块）、`bandit`、`check_secrets.py` + 硬编码 key grep | 部分阻塞 |
| `build-and-push` | main 推送 → GHCR | — |
| `deploy` | 需 `vars.DEPLOY_HOST`；容器内健康探测 + **Celery 原生 worker 就绪断言** | 条件 |

> **`make audit-docs` 未接入 CI workflow**：它是**本地/提交前**的文档一致性
> 守卫（链接/配置/OpenAPI/生命周期/指标声明/Mermaid 结构/HITL 默认值/状态机/
> 生产声明），不是 CI lane。不要把它列进"CI 卡什么"。

**为什么 MCP 契约不允许 skip？** 一个会因为"依赖没装"而变绿的测试
证明了不了任何事，却会给人"已验证"的错觉。所以 CI 里 MCP SDK 是**硬安装失败**，
契约测试跑不起来就是**硬失败**。

**为什么 `deploy` 要用 Celery 原生就绪探测？** 原来的
`docker compose ps worker` 只要 compose 文件能解析就返回 0——
worker 实际没起来也算"成功"。换成 `inspect ping` → `pong` 才是真检查。

---

## 8. 怎么自己复核这些结论

```bash
# 当前事实（版本/模型/路径数/评测状态），不硬编码任何数字
make facts

# 文档一致性守卫（链接/配置/OpenAPI/生命周期/指标声明/Mermaid 结构/HITL 默认值…）
make audit-docs

# 真实基础设施验收（需要 PG + Redis；缺失时硬 FAIL）
make runtime-e2e
make runtime-chaos
make runtime-verify

# RAG 评测流水线（preflight 不通过则不产生 artifact）
make rag-eval-649-preflight
make rag-eval-649-smoke      # 前 16 条，冒烟，不是正式证据
make rag-eval-649            # 正式全量 4-config 消融

# gold 标注契约离线校验
python3 scripts/validate_gold_labels.py

# 测试规模（以当前输出为准，不要从文档抄数字）
pytest --collect-only -q
```

---

## 9. 明确不测 / 测不了的东西

| 项 | 状态 | 理由 |
|---|---|---|
| 649-query 正式检索指标 | **当前不可测** | gold 非相关度标注（数据契约缺陷） |
| 真实用户满意度 / NPS | 无 | 无真实流量 |
| P99 延迟 / 容量基线 | 无 | 有 8 个 benchmark 脚本，但**无 SLO 定义、无生产基线** |
| 生产 SLA | **不声明** | 无生产环境，无证据 |
| LLM-as-judge 语义质量 | 未实现 | 现为启发式 |
| Agent 行为回归 | 未实现 | 见 6.1 |
| 真实 ERP 写操作正确性 | **未验证** | 只用 `tools/hitl_staging_tools.py` 验证了治理机制 |

---

## 10. 延伸阅读

| 你想知道 | 去 |
|---|---|
| RAG 评测口径与契约（canonical） | [reference/rag-evaluation.md](reference/rag-evaluation.md) |
| gold 标注契约与校验器 | [reference/rag-gold-label-contract.md](reference/rag-gold-label-contract.md) |
| gold provenance 审计结论 | [reference/rag-gold-label-provenance.md](reference/rag-gold-label-provenance.md) |
| 分布式 Runtime 证据边界（Level 1/2/3） | [evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md) |
| provider / 生产证据语义 | [evaluation/production-evidence.md](evaluation/production-evidence.md) |
| 事实驱动工程标准 | [standards/evidence-driven-engineering-loop.md](standards/evidence-driven-engineering-loop.md) |
| 测试分层与新增测试落位 | [reference/testing-guide.md](reference/testing-guide.md) |
| 已知不宣称项 | [limitations.md](limitations.md) |
