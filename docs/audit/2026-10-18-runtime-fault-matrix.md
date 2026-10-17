# 故障矩阵 — Agent Runtime 可靠性核验（2026-10-18）

> HISTORICAL AUDIT SNAPSHOT — 本快照只在执行时点有效。修复前/后行为以 git 历史
> 与回归测试为准；测试数量为执行时点快照，复核请以命令当前输出为准
> （`pytest --collect-only -q`），不要把本文件的计数复制为永久事实。

## 复核环境

- Python 3.10.12 · pytest 9.0.3 · Ruff
- 真实 PostgreSQL（`localhost:5432/csai_runtime_test`）+ 真实 Redis（`localhost:6379`）
- 分支：`fix/runtime-reliability-audit-20261016`（`git rev-parse HEAD` 取当前 SHA）
- 证据口径：`IMPLEMENTED` = 代码存在；`CI VERIFIED` = 真实基础设施 + 命令通过；
  `NOT_VERIFIED` = 未在对应环境验证。

## 全量回归（本快照执行结果）

| 命令 | 结果 |
|---|---|
| `pytest tests/unit -q` | 3432 passed, 5 skipped |
| `TEST_DISTRIBUTED_DB_URL=… TEST_REDIS_URL=… pytest tests/integration/runtime -q` | 183 passed |
| 目标场景定向（unit） | 194 passed |
| 目标场景定向（integration/runtime，真实 PG+Redis） | 92 passed |
| `python3 scripts/audit_doc_consistency.py` | OK（57 active docs） |
| `python3 scripts/check_public_repository_boundary.py` | OK |
| `python3 scripts/generate_openapi.py --check` | OK（63 paths） |

## 故障矩阵

| # | 问题 | 触发条件 | 修复前行为 | 修复后行为 | 测试路径 | 结果 | 残余风险 |
|---|---|---|---|---|---|---|---|
| 1 | Worker 崩溃（工具执行前/中/后） | SIGKILL worker 进程；DB/Redis lease 到期 | 无持久 checkpoint 时从头重跑；已完成节点副作用可能重复 | 从 PG checkpoint 的 `next` 续跑（`ainvoke(None)`），已完成节点不重跑；副作用经 ledger 去重 | `tests/integration/runtime/test_worker_checkpoint_recovery.py`；`test_tool_idempotency.py::test_gate11_…across_worker_kill`；`make runtime-chaos` | PASS（CI VERIFIED） | 无 fencing token：外部节点副作用仍依赖 ledger；端到端 exactly-once NOT PROVIDED |
| 2 | ACK 后 Run 意外滞留（RETRYING/QUEUED 无后继消息） | 外层 `except` 分支决定被 `contextlib.suppress` 吞掉 | `return FAILED` = ACK 一个永不再投递的 run；告警照报但恢复是死的 | 决定移到 suppress 之外：状态仍 RUNNING→终态收敛，RETRYING/QUEUED→`raise`（不被当作成功 ACK） | `tests/unit/test_runtime_review_findings.py::TestStrandedRunMustNotBeAcked`；`tests/unit/test_stale_run_reconciliation.py` | PASS | 实测 broker 不重投普通异常，恢复依赖周期 reconciler；beat 停摆则需人工 `reconcile_stuck_runs.py --apply` |
| 3 | Lease 过期与所有权转移 | worker pause 超过 TTL；竞争 worker 接管 | 旧 worker 迟到写回可覆盖新 owner 的终态 | `transition_owned` / `takeover_running` / `renew_lease_owned` 均为单条原子 `UPDATE`（`run_id+status+worker_id+lease 未过期`）；失去所有权抛 `RunOwnershipLost`，executor 不记失败/不重试/不消耗 attempt | `tests/integration/runtime/test_worker_ownership_cas.py`；`test_duplicate_claim_lease_fencing_pg.py`；`test_stale_worker_fencing_pg.py`；`test_stale_run_reconciliation_pg.py` | PASS（真实 PG） | 严格 lease 过期，但无 fencing token；旧 worker 协程可能继续跑完（其提交被条件更新挡下） |
| 4 | 重复投递导致工具副作用重复 | at-least-once 重投 / 节点重放 | LLM `tool_call_id` 不稳定 → 重放拿到新键 → 同一笔写执行两次 | `build_replay_stable_operation_key(run,node,agent,tool,ordinal)`（不含 LLM 输出）+ ledger `claim` 去重；指纹不一致 = `PermanentError`（fail-closed） | `tests/integration/runtime/test_tool_idempotency.py`；`test_side_effect_replay_identity_pg.py`；`test_side_effect_claim_concurrency.py`；`tests/unit/test_side_effect_replay_wiring.py` | PASS（真实 PG） | 本仓不重复发起；外部系统端到端 exactly-once NOT PROVIDED（需下游接受同一幂等键） |
| 5 | 审批恢复绕过 HITL | retry / worker 崩溃恢复尝试直接恢复 | 无决策时可能盲执行 | `consume_resume` 用 `WHERE resumed_at IS NULL` 原子认领，**无决策返回 None**；治理开启时未认领风险的 side-effect 工具判 HIGH | `tests/integration/runtime/test_hitl_approval_concurrency.py::TestRecoveryCannotBypassApproval`；`test_hitl_resume_fault_injection.py` | PASS（真实 PG） | `/api/chat` 快路径无 run 上下文，明确不在该治理边界内；真实 ERP 写 NOT_VERIFIED |
| 6 | 工具超时后丢失幂等状态 | 副作用工具执行超过 `TOOL_EXECUTION_TIMEOUT_SECONDS` | 若把超时转成字符串返回，ledger 会把"结果未知"的写记成 SUCCEEDED 并缓存 → 重投直接返回该字符串 | 只读工具超时降级为可解释错误串；副作用工具超时**冒泡** `ToolExecutionTimeout`（`is_tool_timeout`）→ TIMEOUT → 可重试；认领租约 TTL 下界强制覆盖工具超时 | `tests/unit/test_tool_execution_reliability.py`；`tests/integration/runtime/test_side_effect_claim_lease_pg.py::test_claim_ttl_must_cover_tool_timeout`；`test_no_false_success_when_result_commit_is_rejected` | PASS | 超时对副作用是"未知执行结果"，最终靠下游幂等键；外部 exactly-once NOT PROVIDED |
| 7 | 不同 Session 的缓存/上下文污染 | 查询经缓存层跨用户/跨会话 | 个性化回答可能落入 SHARED 槽位被跨用户读取 | CachePolicy 统一 scope/version/ttl；无身份 fail-closed 不写共享槽；读取端 OR 探测 SHARED + 调用方作用域（个性化永不落 SHARED）；黑板 ContextVar 按 session 隔离 | `tests/unit/test_cache_cross_user_isolation.py`；`tests/unit/test_cache_policy.py` | PASS | 缓存命中率/延迟生产数值 NOT_MEASURED |
| 8 | 测试通过但真实工具从未执行 | ReActMode 丢弃 `pending_actions` | HITL 闸门在真实图上从未触发；高风险副作用被静默丢弃而 run 报成功 | `ReActMode.execute` 保留 `pending_actions`；闸门在协作模式与 `final_response` 之间逐个 `interrupt()`；注册表声明 `side_effect` 的判据由 `ToolRegistry.is_side_effect` 提供 | `tests/unit/test_hitl_real_graph_gate.py`（真实编译图）；`tests/unit/test_hitl_gate.py` | PASS | 只有 `react` 模式持有 `tool_registry`；其余模式仍防御性透传 |
| 9 | 缓存命中统计口径错误 | L1 条目损坏（合法 JSON 缺 `response`）；后端读取异常 | `l1_hits` 在解析前自增 → 同一请求既记命中又记未命中；后端异常被计成 miss，污染命中率分母 | 命中计数移到成功取出响应之后（单请求只贡献 hit 或 miss/error 之一）；新增 `errors` 口径：所有已配置层异常时记 `errors` 而非 miss，hit_rate 分母只含 命中+未命中 | `tests/unit/test_cache_hit_accounting.py`（新增） | PASS（回归在修复前 5 failed） | 命中率 L1/L2/L3 的生产数值仍 NOT_MEASURED |
| 10 | 编排评测被误称真实 LLM 能力评测 | 用脚本化 LLM 跑真实图 | 把编排/治理层准确率当成模型意图识别/工具选择能力 | `evaluation/agent_eval/contract.py` 逐指标写 `measures` / `does_not_measure` 并原样写入 artifact；`route_accuracy` 无 `human_confirmed` 标注时 = `NOT_MEASURED`，`overall_status = INCONCLUSIVE`（≠ PASS） | `scripts/evaluate_agent.py`；`tests/unit/test_agent_eval_contract.py` | PASS（route_accuracy NOT_MEASURED / INCONCLUSIVE） | 真实模型 lane 默认零外网；LLM-as-judge 未实现 |

## 结论与残余风险

- 上述 1–6、8、10 的修复位于 `fix/runtime-reliability-audit-20261016`（对准 `main`
  的 Draft PR）；条目 9 与本快照同批新增。
- **能力层级**：1–6、8 = CI VERIFIED（真实 PG + Redis，部分真实多进程 Celery）；
  7 = CI VERIFIED（离线逻辑）；9 = 本地单测；10 = 本地脚本化评测。
- **NOT_VERIFIED**：真实生产集群 / 多副本长期稳定 / 真实用户流量 / 真实 ERP 写操作 /
  大规模 backlog / K8s autoscaling / multi-region。Level 2 ≠ 生产验证。
- **不得越界宣称**：端到端 exactly-once 不提供；事件流是 best-effort 观测通道非真相源；
  无 fencing token，取消是协作式的。
