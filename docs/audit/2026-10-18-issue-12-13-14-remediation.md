# Issue #12 / #13 / #14 缺陷复现与修复证据（2026-10-18）

> HISTORICAL AUDIT SNAPSHOT — 本快照只在执行时点有效。修复前/后行为以 git 历史与
> 回归测试为准；测试计数为执行时点快照，复核请以命令当前输出为准
> （`pytest --collect-only -q`），不要把本文件计数复制为永久事实。
>
> 三个缺陷均由 PR #11 review 发现（Issue #12/#13/#14），本文件记录
> **失败前 / 修复后** 的可复算证据。复用既有 Agent Runtime，未引入第二套。

## 复核环境

- Python 3.10.12 · pytest 9.0.3 · Ruff · langgraph 1.2.2 · Celery 5.6.3
- 真实 PostgreSQL（`localhost:5432/csai_runtime_test`）+ 真实 Redis（`localhost:6379`）
- 分支：`fix/runtime-reliability-audit-20261016`（Draft PR #11）
- 证据口径：`FAIL（修复前）` = 在撤销源码修复、保留新回归测试后复现；`PASS（修复后）`
  = 源码修复就位后通过。

## 复现方法（可复算）

```bash
# 修复前：只 stash 源码改动，保留新回归测试
git stash push -- agents/base_agent.py core/config.py core/hitl/approval_service.py \
  runtime/executor.py runtime/repository.py runtime/retry.py runtime/run_service.py \
  runtime/side_effects.py
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
  TEST_REDIS_URL=redis://localhost:6379 \
  python3 -m pytest tests/unit/test_side_effect_ordinal_scope.py \
                   tests/unit/test_side_effect_sqlite_claim_safety.py \
                   tests/integration/runtime/test_hitl_approval_crash_window.py -q
git stash pop   # 修复后重跑同一组
```

## 缺陷矩阵

| Issue | 根因 | 失败前证据 | 修复 | 修复后证据 | 残余风险 |
|---|---|---|---|---|---|
| **#12 Part B** | `_logical_ordinals` 在 `for round_num in range(max_tool_rounds)` **循环内部**初始化（`agents/base_agent.py`）。同一节点的两轮里，同名同参写操作都拿到 `ordinal=0` → 同一 `operation_key`：参数相同则第二次被 `CLAIM_SUCCEEDED` **静默吞掉**；参数不同则 `PermanentError` 被落 DLQ，合法的第二笔写被永久拒绝 | `test_side_effect_ordinal_scope.py::test_same_tool_across_two_rounds_executes_twice` — **1 failed**（两轮各退款一次，handler 只执行一次；日志两轮同一 `op=v1:e48831…`） | 计数器移到轮次循环之外（与 `_deferred` 同理）；节点重放时整个方法从头执行，序号仍与首次一致 | 该用例 **PASS**（handler 执行两次） | 无（跨轮已单调递增） |
| **#12 Part A** | `ordinal` 是**位置**而非身份：同一写工具的调用序列在重放时重排（插入/交换另一次同类写）会让同一笔写换槽位 → 新键 → 可能重复执行 | 结构上不可修；`build_replay_stable_operation_key` docstring 曾错误宣称「ordinal 漂移不影响不会重复执行」 | 仅修正文档与 Limitations，**未声称已修复**；跨任意重排的严格去重需调用方提供稳定逻辑身份（HITL 的 `approval:{approval_id}` 即是） | docstring + `docs/limitations.md` 已如实降级；xref Issue #12 | **未修复**：位置身份无法覆盖写序列重排；非 HITL 写工具目前仅 staging（HITL 门拦下） |
| **#13** | `SideEffectStore.claim` 的非 PostgreSQL 分支用「无锁 SELECT + 按主键 ORM UPDATE」做接管；SQLAlchemy 的 SQLite 方言**忽略 `FOR UPDATE`**。两个认领者可同时读到过期行、同时判定可接管、同时拿到 `CLAIM_EXECUTE` | `test_side_effect_sqlite_claim_safety.py` — **7 failed**（接管与 FAILED 重试都返回 `CLAIM_EXECUTE`；`validate_app_database_dsn` 尚不存在） | 非 PostgreSQL 方言的**接管**路径 fail-closed 返回 `in_progress`（首次插入的互斥仍由唯一约束提供）；新增生产启动校验 `validate_app_database_dsn` 拒绝非 PostgreSQL 应用库 | 该文件 **8 passed**（首次插入仍 `execute`；接管/重试为 `in_progress`；生产 SQLite/`postgresql+psycopg2` DSN 被拒） | 非 PG 方言上过期/失败的工具**不会自动重试**（fail-closed 进 DLQ）；生产由启动校验兜底 |
| **#14** | `runtime/executor.py` 在把 run 从 `WAITING_APPROVAL` 迁到 `RUNNING` **之前**就破坏性 `consume_resume`。worker 在两步间崩溃 → 载荷丢失、run 永久停在 `WAITING_APPROVAL`（不在 `EXECUTABLE_STATUSES`，无自动恢复） | `test_hitl_approval_crash_window.py` — **4 failed**（peek/reconcile/resume 均不可用）；`test_hitl_resume_fault_injection.py::TestCrashDuringResume` 旧断言钉死「永久滞留」 | 新增只读 `ApprovalService.peek_resume`（仅在无未消费 `PENDING` 时重建终态决策）；`_build_resume_command` 回退到它；reconciler 经 `list_resumable_waiting_approval_runs` 重新投递已决策滞留 run | 该文件 **5 passed**；`TestCrashDuringResume` 更新为**收敛断言**（已批准动作恰好执行一次 → SUCCEEDED） | 仍在等待人工决策的 run 不自动重投（正确）；`/api/chat` 快路径无 run 上下文 |

## 全量回归（本快照执行结果）

| 命令 | 结果 |
|---|---|
| `pytest tests/unit -q` | 3447 passed, 5 skipped |
| `TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… pytest tests/integration/runtime -q` | 188 passed |
| `python3 scripts/test_worker_crash_recovery.py`（`make runtime-chaos`） | PASS |
| `python3 scripts/verify_distributed_runtime.py`（`make runtime-verify`） | PASS（`overall_status: PASS`） |
| `python3 scripts/evaluate_agent.py`（`make agent-eval`） | `overall_status = INCONCLUSIVE`（`route_accuracy` NOT_AVAILABLE，无人工确认标注） |
| `python3 scripts/audit_doc_consistency.py` | OK（57 active docs） |
| `python3 scripts/check_public_repository_boundary.py` | OK |

## 语义边界（不得混淆）

- **单次 Claim 获胜** ≠ **消息至少一次投递** ≠ **数据库状态一致** ≠ **外部系统端到端
  exactly-once**。ledger 只保证**本仓不重复发起**；外部系统 exactly-once
  **NOT PROVIDED**。
- #14 的「恰好执行一次」是**本仓发起次数**；外部系统端到端仍取决于下游幂等。
- #13 的 fail-closed 是**拒绝执行**，不是「已安全去重」。

## 结论与残余风险

- #12 Part B、#13、#14 修复位于 `fix/runtime-reliability-audit-20261016`（对准 `main`
  的 Draft PR #11）。
- **能力层级**：#12 Part B / #13 = 本地单测（离线逻辑）+ PG 回归；#14 = 真实 PG
  的集成故障注入。三者均为 CI VERIFIED，**不是**生产验证。
- **NOT_VERIFIED**：真实生产集群 / 多副本长期稳定 / 真实用户流量 / 真实 ERP 写操作 /
  大规模 backlog。Level 2 ≠ 生产验证。
- **未修复（如实记录）**：#12 Part A（位置身份 vs 写序列重排）。
