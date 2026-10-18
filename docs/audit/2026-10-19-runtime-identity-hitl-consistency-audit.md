# Agent Runtime / Checkpoint / Celery / Redis Lease / 幂等工具 / HITL 一致性审计（2026-10-19）

> **性质**：CURRENT-vs-CODE 一致性审计。本文的**结论**（不是历史快照）以实时 `main`
> 为准；本文引用的测试计数是**执行时点快照**，复核请以命令当前输出为准
> （`pytest --collect-only -q`），不要把计数复制为永久事实。
>
> 证据状态词：`REPRODUCED` / `VERIFIED_LOCAL` / `CI VERIFIED`（真实 PG+Redis）/
> `NOT_VERIFIED` / `NOT PROVIDED`。**`CI VERIFIED` / `VERIFIED_LOCAL` ≠ 生产验证。**

---

## 0. 审计范围与方法

针对简历/文档中的 **AgentRuntime、PostgreSQL Checkpoint、Celery、Redis Lease、
幂等工具执行、HITL、故障恢复** 能力，做「宣称 → 代码符号 → 测试」三者一致性核对，
并在**真实 PostgreSQL + Redis** 上重跑故障矩阵。约束与结论同样重要：**不为了产出
PR 而改代码**；先确认是否仍有真实缺陷。

明确**不**重做的旧结论（本轮以实时 `main` 复核，不重复修复）：

- **Issue #12 Part A**（位置 ordinal 不是身份）已由 **PR #15** 用内容寻址键修复，并由
  **PR #17** 收敛「部分恢复」身份歧义；Issue #12 已于 2026-10-18 关闭。
- **Issue #13 / #14** 已由 PR #11 修复（非 PG 方言 takeover fail-closed；审批恢复崩溃窗口）。

---

## 1. 当前 HEAD 与同步记录

| 项 | 值 |
|---|---|
| 工作区 | `/home/dev/projects/customer-service-ai-agent` |
| 分支 | `main` |
| 本地 HEAD | `8e45f995f7362f1a281e9f1ad8fa3b97984569cc` |
| `origin/main` | `8e45f995f7362f1a281e9f1ad8fa3b97984569cc` |
| `git fetch --prune origin` | 成功，无新引用 |
| `git rev-list --left-right --count main...origin/main` | `0	0`（无分歧，**无需同步**） |
| 工作区（受跟踪文件） | 干净；仅 `artifacts/**` 未跟踪产物 |

本地与远程默认分支**完全一致**，因此本轮未执行任何合并/快进，未覆盖既有工作。

---

## 2. 能力 → 代码符号 → 测试 映射

| 简历/文档宣称 | 代码符号 | 回归测试（真实 PG+Redis 标 ✅） |
|---|---|---|
| 跨重试稳定操作身份（内容寻址） | `runtime/side_effects.py::build_replay_stable_operation_key`（`v2:` 键 = 作用域 + `sha256(canonical_json(args))` + 同内容 occurrence；或调用方 `logical_operation_id`） | `tests/unit/test_side_effect_stable_logical_identity.py`；✅ `tests/integration/runtime/test_side_effect_replay_identity_pg.py` |
| 同内容多次**合法**写不被吞 | `agents/base_agent.py::_process_with_tools` 的 `_logical_ordinals`（跨 LLM 轮次单调，声明在轮次循环**外**）；`LogicalCall.ordinal` | `tests/unit/test_side_effect_ordinal_scope.py` |
| 部分恢复身份歧义 fail-closed | `runtime/operation_identity.py::ContentOccurrenceResolver.assign`（缩短重放 → `OperationIdentityUnavailable`）；`SideEffectStore.reserve_operation`（执行前预占） | `tests/unit/test_side_effect_partial_resume_identity.py`；✅ `test_side_effect_replay_identity_pg.py` |
| ledger fencing（终态 CAS） | `SideEffectStore._commit_terminal_state`（单条条件 UPDATE：owner+status+lease+fingerprint）；`commit_result_or_raise` 抛 `SideEffectOwnershipLost` | ✅ `tests/integration/runtime/test_side_effect_commit_fencing_pg.py`；`test_side_effect_failure_observability_pg.py` |
| claim 抢占互斥 + 续租 | `SideEffectStore.claim` / `renew_claim` / `_ClaimRenewer`；`min_claim_ttl_seconds` 由真实执行路径传入 | ✅ `test_side_effect_claim_lease_pg.py`；`test_side_effect_claim_concurrency.py`；`tests/unit/test_side_effect_claim_floor_wiring.py` |
| Run 级/线程级/工具级三层幂等 | `runtime/run_service.py`（终态 CAS + `idempotency_key`）；`runtime/thread_lock.py`；`runtime/side_effects.py` | ✅ `test_duplicate_claim_lease_fencing_pg.py`；`test_worker_ownership_cas.py` |
| PostgreSQL checkpoint 跨进程恢复 | `core/checkpointer.py`（生产 fail-closed）；`runtime/bootstrap.py::invoke_graph_with_resume`（`ainvoke(None)` 续跑） | ✅ `test_worker_checkpoint_recovery.py`；`test_cross_process_checkpoint.py`；`test_sse_checkpoint_serialization.py` |
| Worker Lease 竞争 / 恢复所有权 | `runtime/repository.py::takeover_running` / `renew_lease_owned` / `list_stale_running_runs` / `dead_letter_stale_running` / `claim_executable`（谓词同一条 UPDATE） | ✅ `test_stale_run_reconciliation_pg.py`；`test_duplicate_claim_lease_fencing_pg.py`；`tests/unit/test_stale_run_reconciliation.py` |
| 失去所有权后执行 fencing | `runtime/executor.py::_install_execution_fence` + `_FenceGuard` + `_supervise_execution`；`ToolRegistry.set_run_execution_fence`（执行前只读复核） | ✅ `test_stale_worker_fencing_pg.py` |
| Celery 投递语义（实测，非推断） | `runtime/executor.py`（不把异常逃逸当恢复手段）；恢复闭环 = DB AgentRun + 周期 reconciler | ✅ `test_celery_delivery_semantics.py`（真实 Redis broker + 真实 worker 子进程） |
| HITL 审批治理（拦在执行前 / durable / CAS） | `core/hitl/gate.py::run_approval_gate` / `execute_approved_actions`；`core/hitl/approval_service.py::decide` / `consume_resume` / `peek_resume` / `expire_overdue`；`core/hitl/risk.py` | ✅ `test_hitl_approval_concurrency.py`；`test_hitl_approval_crash_window.py`；`test_hitl_resume_fault_injection.py`；`test_hitl_langgraph_interrupt.py` |
| 快路径无治理写操作 fail-closed | `core/hitl/gate.py::is_ungoverned_side_effect`；`ToolRegistry._ungoverned_side_effect_refusal`（唯一执行 choke point） | `tests/unit/test_hitl_real_graph_gate.py`；`tests/unit/test_hitl_fail_closed.py` |
| 缓存命中计数口径 | `cache/response_cache.py`（命中计数后置；`errors` 不入 hit_rate 分母） | `tests/unit/test_cache_hit_accounting.py`；`tests/unit/test_cache_metrics.py` |
| DLQ / 重放 | `runtime/repository.py::dead_letter_with_record`（状态+DLQ 同事务）；`scripts/replay_dead_run.py`（复用原 run_id） | `test_duplicate_claim_lease_fencing_pg.py`；`tests/unit/test_stale_run_reconciliation.py` |

---

## 3. 真实故障复现矩阵（本轮实跑）

环境：Python 3.10.12 · pytest 9.0.3 · **langgraph 1.2.2** · Celery 5.6.3 ·
真实 PostgreSQL（`localhost:5432/csai_runtime_test`）+ 真实 Redis（`localhost:6379`）。

| 场景 | 命令 | 结果 |
|---|---|---|
| 全部 runtime 集成（含 checkpoint/lease/幂等/HITL/celery 投递） | `TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… pytest tests/integration/runtime -q` | **199 passed** |
| 单元全量 | `pytest tests/unit -q` | **3481 passed, 5 skipped** |
| 稳定身份 / 部分恢复 / ordinal 作用域（离线） | `pytest tests/unit/test_side_effect_*.py -q` | **44 passed** |
| 幂等 ledger 真实 PG（replay/claim/commit/observability/fencing） | 5 个 `test_side_effect_*_pg.py` | **48 passed** |
| HITL + 恢复 + 卡死收敛（真实 PG+Redis） | 6 个 `test_hitl_*` / `test_stale_*` / `test_worker_checkpoint_recovery.py` | **38 passed** |
| Celery 投递语义（真实 broker + worker 子进程） | `pytest tests/integration/runtime/test_celery_delivery_semantics.py` | **8 passed** |
| Worker SIGKILL 崩溃恢复（chaos） | `python scripts/test_worker_crash_recovery.py` | **[PASS]**（artifact `artifacts/runtime/chaos-audit-*.json`） |
| 分布式 Runtime 能力证据 | `python scripts/verify_distributed_runtime.py` | `overall_status: PASS`（artifact `artifacts/distributed-runtime/<ts>/report.json`） |
| 文档一致性守卫 | `python scripts/audit_doc_consistency.py` | **OK（57 active docs）** |

重点场景复核（均在**真实 PG**，非 mock）：

- **同一业务写操作的 replay reorder**：`test_side_effect_stable_logical_identity.py`
  的重排用例 + `test_side_effect_replay_identity_pg.py` —— 同一笔写重排后命中**同一**
  `operation_key`，不重复执行。
- **相同参数的双次合法调用**：`test_side_effect_partial_resume_identity.py` ——
  attempt1 `[A, A]` 只执行 A₁ 后崩溃；恢复**完整重放** `[A₁, A₂]` 时 A₁ 去重、A₂ 执行一次；
  恢复**只提出单条 A**（无显式业务 ID）时以 `OperationIdentityUnavailable` **fail-closed**，
  不静默复用 A₁ 结果，也不猜测性执行 A₂。
- **Worker 崩溃 / Lease 过期 / 恢复所有权竞争**：`test_duplicate_claim_lease_fencing_pg.py`、
  `test_stale_run_reconciliation_pg.py` —— 并发竞争者**恰好一个**接管（同一条 UPDATE 的
  谓词），到达 `attempt == max_attempts` 的最后一次正常执行不被重复投递误打 DLQ。
- **HITL 并发审批**：`test_hitl_approval_concurrency.py` —— N 个并发决策**恰好一个赢家**
  （`UPDATE ... WHERE status='PENDING'`），`consume_resume` 的 `resumed_at IS NULL`
  保证**恰好一份**载荷；**无决策时不恢复**（重试/崩溃不能绕过审批）。
- **多审批顺序恢复**：`test_hitl_langgraph_interrupt.py::test_multiple_pending_actions_each_get_approval`
  —— 两个高风险动作各自 interrupt、逐个决策、各执行一次。
- **DLQ**：`test_duplicate_claim_lease_fencing_pg.py` 的 DLQ 原子性用例（状态迁移 + DLQ 行
  同一事务；注入 DLQ 写失败则整体回滚）。

---

## 4. 快路径 vs 持久化运行时授权边界（重点核对）

**结论：边界成立，且是 fail-closed 的。** 证据链：

1. `set_run_context(...)` 在**生产代码中只有一处**调用 —— `runtime/executor.py:405`
   （worker 执行 `AgentRun` 时）。`/api/chat` / SSE / WS 快路径**不设置** run 上下文。
2. 所有工具执行（REST / SSE / WS / 多模态 / worker / HITL 批准后）**汇聚到同一 choke point**
   `ToolRegistry.execute_raw`：
   - 已声明 `side_effect=True` 且**无 run 上下文** → `is_ungoverned_side_effect` 返回真 →
     返回可解释拒绝文案（`ungoverned_side_effect_message`），**handler 不执行**；
   - 有 run 上下文 → 走 `runtime.side_effects` ledger（+ HIGH 风险走 HITL）。
3. `execute_approved_actions`（HITL 批准后）同样经 `execute_raw`，并显式传
   `tool_call_id="approval:{approval_id}"` → `operation_key = run_id:approval:{approval_id}`，
   重投递/崩溃可去重。
4. **不能因 AgentRun 已有审批就假设所有 FC 路径都有同一保证**：审计确认**恰恰相反** ——
   有 run 上下文才可能有审批与 ledger；无 run 上下文的写操作被**拒绝**，而不是被「同样的
   审批」保护。这是有意的设计取舍，不是遗漏。

---

## 5. 本轮发现

### 5.1 真实代码缺陷：**未发现新的可复现缺陷**

对以下高风险面逐一核对源码 + 真实 PG 回归，未发现新缺陷：

- 内容寻址身份与 `ContentOccurrenceResolver` 的 occurrence 分配 / 缩短重放判定 / 预占；
- `claim` / `renew_claim` / `_commit_terminal_state` 的原子谓词（owner+status+lease+fingerprint）；
- `takeover_running` / `dead_letter_stale_running` / `claim_executable` 的「判定与写入同一
  条 UPDATE」；
- `consume_resume` / `peek_resume` / `expire_overdue` / `decide` 的 CAS 与崩溃窗口；
- 执行 fencing（协作式取消 + 执行前只读复核）与快路径 fail-closed。

Issue #12（含 Part A/B 与部分恢复残差）、#13、#14 的修复在本轮真实基础设施上**复现为 PASS**。

### 5.2 事实过期文档（已修）：langgraph 版本声明

**性质**：证据一致性（可复现性）问题，非代码缺陷。

**根因 / 复现**：`core/hitl/gate.py`、`runtime/bootstrap.py`、`runtime/executor.py`、
`CLAUDE.md`、`docs/design/human-in-the-loop.md` 及测试 docstring 均声明 HITL 语义
「实测于 **langgraph 1.2.12**」，`docs/limitations.md` §9 亦称「本地 venv 确实是 1.2.12」。
但实时事实是：

```
$ .venv/bin/python -m pip show langgraph | head -2
Name: langgraph
Version: 1.2.2
$ grep -n '^langgraph==' requirements-lock.txt
144:langgraph==1.2.2
$ python -c "import json; print(json.load(open('<fresh chaos artifact>'))['environment']['langgraph'])"
1.2.2
```

即：**lockfile 锁 1.2.2、本地 venv 装 1.2.2、新生成的 chaos artifact 记录 1.2.2**，
只有文档/注释还写着 1.2.12。按 lockfile 全新安装得到的是 1.2.2，因此「实测于 1.2.12」
对该仓库**不可复现**。

**修复**：把 6 处**活文档/源码注释**与 1 处测试 docstring 统一为 **1.2.2**（lockfile 与
实际安装一致的版本），并在 `core/hitl/gate.py` 注明该版本即 lockfile 锁定版本；在
`docs/limitations.md` §9 将该债务条目标记为**已修复**（附复测证据）。历史快照
（`docs/reports/audit/FINAL_CHECK_REPORT.md`、`PROJECT_FINALIZATION_PLAN.md`）按
「快照不可回填」约定**保留原文**，不改写。

**回归证据**：HITL 的 LangGraph 语义断言测试
（`tests/integration/runtime/test_hitl_langgraph_interrupt.py`）在 **1.2.2** 上通过
（`interrupt()` 注入 `__interrupt__`；`ainvoke(None)` 不解除 interrupt；
`Command(resume=...)` 恢复；已完成的 run 不再带 `__interrupt__`；多审批逐个恢复）。

**改动前后测试**：

| | 改动前 | 改动后 |
|---|---|---|
| `pytest tests/integration/runtime -q` | 199 passed | 199 passed |
| `pytest tests/unit -q` | 3481 passed, 5 skipped | 3481 passed, 5 skipped |
| `python scripts/audit_doc_consistency.py` | OK | OK |

> 本改为**文档/注释级**，不改任何运行期行为；上述测试用于证明「1.2.2 上语义成立」这一
> 新声明本身有据。

---

## 6. 未解决风险与边界（不得越界宣称）

- **正确性语义是 at-least-once，不是 exactly-once。** `acks_late` +
  `reject_on_worker_lost` + `visibility_timeout`；正确性由 run / thread / 工具三层幂等承担。
- **外部系统端到端 exactly-once `NOT PROVIDED`。** ledger 只保证「本仓不重复发起同一笔」；
  下游若需去重必须接受同一把 idempotency key 或人工对账。
- **真实 ERP 写操作 `NOT_VERIFIED`。** 现有 `side_effect=True` 工具仅 `tools/hitl_staging_tools.py`
  的确定性 staging 工具，验证的是**治理机制**，不是金蝶 ERP 的正确性。
- **生产集群 / 多副本长期稳定 / 真实用户流量 / 大规模 backlog / K8s autoscaling / multi-region
  `NOT_VERIFIED`。** Level 2（真实依赖 CI 验收）**不等于**生产验证。
- **无单调 fencing token。** 失去所有权后的中止是**协作式取消**（heartbeat 取消图协程）+
  执行前**只读** ownership 复核；取消前已发出的外部调用无法回收；卡在同步 C 调用的执行
  由 `AGENT_RUN_TASK_TIME_LIMIT` 兜底。
- **同参、无显式 `logical_operation_id` 的「缩短重放」是设计上的 fail-closed 边界**：系统
  不猜测非确定 LLM 输出的业务意图。单条无歧义恢复须由调用方提供持久化 `logical_operation_id`。
- **`logical_operation_id` 目前无生产调用方**（Agent 工具循环从不下发；HITL 走
  `approval:{approval_id}`）。该参数是**为编排层预留的接口**，不是当前已接线能力。
- **`/api/chat` 快路径明确不在 HITL 治理边界内**，补偿措施是**拒绝**无治理的副作用调用。
- **RAG 正式检索指标 `NOT_VERIFIED`，缓存命中率 / 端到端延迟收益 `NOT_MEASURED`。**
  本轮**未**从确定性测试推导任何企业历史百分比（如「42% 命中」「27.8% 延迟改善」）——
  这类数字在本仓库**没有任何 provenance-bearing 证据**，不得引用。
- **DLQ 告警通知投递 `NOT_VERIFIED`**（指标可达 + 规则表达式成立已验证；「有人真的收到
  通知」未验证）。

---

## 7. 面试可使用的准确措辞

**可以这么说：**

- 「异步 Agent Runtime 建在 **真实 PostgreSQL checkpoint + Redis** 上：run 的真相源是
  数据库 `agent_runs` 表，Celery result backend 不是真相源；崩溃后从 checkpoint 的 `next`
  续跑，跨 worker 恢复。」
- 「信号只保证 **at-least-once**（`acks_late` + `reject_on_worker_lost`）。我用**三层幂等**
  兜住重复：run 级终态 CAS、thread 级 Redis 锁、工具级 `tool_side_effects` ledger。**不宣称
  exactly-once**。」
- 「深度 bug 是**去重身份**本身：最初用 LLM 的 `tool_call_id`，节点重放会变；改成位置 ordinal
  后，重放时同类写重排又会换槽位——两者都会**重复执行外部写**。最终改成**内容寻址**身份
  （作用域 + 参数内容指纹 + 同内容 occurrence），并把 occurrence 锚定到**持久化预占**，解决
  「同参数两笔合法写，部分恢复时被错误合并」的问题；无法判定的缩短重放**fail-closed**。」
- 「审批恢复用 LangGraph `interrupt()` / `Command(resume=...)`：`decide()` 与 `consume_resume()`
  都是数据库级 CAS，并发审批恰好一个赢家、恰好一份载荷；**没有决策时重试与崩溃恢复都不能
  绕过审批**。审批与 ledger 是两道独立防线：审批防『不该做的被做了』，ledger 防『做了一次被
  重做』。」
- 「工具层有一个统一执行 choke point：**无 durable run 上下文的写操作 fail-closed 拒绝**，
  不靠提示词劝退；这条边界由真实编译图的测试钉住。」
- 「我知道这套系统**没有**的东西：无单调 fencing token、无工具级熔断/重试/并行、真实 ERP 写
  未在生产验证、生产集群与多副本未验证、RAG 正式指标因缺人工标注而不可测。」

**不能这么说（会被追问并证伪）：**

- 「端到端 exactly-once」「生产集群已验证」「真实 ERP 退款已验证」。
- 任何具体的缓存命中率 / 延迟改善百分比（如 42% / 27.8%）或当前 Hit@K / MRR / NDCG。
- 「有 fencing token」——只有**协作式取消 + 执行前只读复核**。
- 「所有 Function Calling 路径都受审批保护」——快路径是**拒绝**，不是审批。

---

## 8. 复现本报告

```bash
# 单元全量
pytest tests/unit -q

# 真实 PG + Redis 全量 runtime 集成（含 checkpoint / lease / 幂等 / HITL / celery 投递）
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 pytest tests/integration/runtime -q

# 故障矩阵 / 证据
TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… make runtime-chaos
TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… make runtime-verify
TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… make runtime-delivery

# 文档一致性
python scripts/audit_doc_consistency.py
```
