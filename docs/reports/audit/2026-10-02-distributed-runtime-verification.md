# 分布式 Runtime 故障恢复验证报告

- **日期**：2026-10-02
- **分支**：`test/distributed-runtime-recovery`
- **范围**：Celery worker + Redis（broker/thread-lock/event-stream）+ PostgreSQL
  （AgentRun / LangGraph Checkpoint）+ Qdrant 的分布式执行**故障恢复与可观测性**。
- **证据分级**：本报告只记录**实际执行过**的结果。成功项标 `LOCALLY VERIFIED`；
  未执行/无真实环境项标 `NOT_VERIFIED`。**不使用 “PASS” 作为生产结论。**
- **说明**：`LOCALLY VERIFIED` ≠ 生产验证。生产多副本 / Kubernetes / 真实 HA 均未验证。

## 0. 执行环境

| 项 | 值 |
|----|----|
| Python | 3.10 |
| 默认测试 DB | 独立 SQLite（`StaticPool`，每用例隔离） |
| 真实 Redis（gated） | `redis://localhost:6379/0`（本地容器 `infra-redis-1`） |
| 真实 PostgreSQL（gated） | `postgresql://…@localhost:5432/csai_ckpt_test`（本地容器 `infra-postgres-1`） |
| 说明 | 以上是**本地开发容器**，不是生产环境 |

## 1. 执行命令与结果（真实输出）

| 命令 | 结果 |
|------|------|
| `pytest tests/unit -q --no-cov` | `1590 passed` |
| `pytest tests/integration -q --no-cov` | `111 passed, 5 skipped`（gated 无外部服务时 skip） |
| `TEST_REDIS_URL=… TEST_POSTGRES_CHECKPOINT_URL=… pytest tests/integration/distributed_runtime/test_cross_worker_postgres.py tests/integration/test_run_events_redis.py tests/integration/test_thread_lock_redis.py tests/integration/test_checkpoint_postgres.py -q` | `5 passed` |
| `pytest tests/integration/distributed_runtime -q` | `11 passed, 1 skipped`（cross-worker PG 需 env） |
| `pytest tests/integration/distributed_runtime/test_recovery_scenarios.py -q` | `6 passed` |

## 2. 故障场景矩阵

| # | 场景 | 测试 | 证据状态 |
|---|------|------|---------|
| 1 | Worker crash → checkpoint 恢复（同进程共享 checkpointer） | `distributed_runtime/test_recovery_scenarios.py::test_worker_crash_resumes_from_checkpoint` | `LOCALLY VERIFIED` |
| 1b | Worker crash → 跨 worker/进程（真实 PG，全新 saver+pool） | `distributed_runtime/test_cross_worker_postgres.py::test_checkpoint_survives_worker_process_restart` | `LOCALLY VERIFIED`（gated，已用真实 PG 执行） |
| 2 | API restart 不丢 run（新 app/service 仅共享 DB） | `test_recovery_scenarios.py::test_api_restart_does_not_lose_run` | `LOCALLY VERIFIED`（进程内模拟重启，非容器级） |
| 3 | Duplicate delivery 不重复执行（RUNNING + lease） | `test_recovery_scenarios.py::test_duplicate_delivery_does_not_repeat_execution`；`tests/unit/test_run_reliability.py::test_duplicate_celery_delivery_does_not_repeat_success` | `LOCALLY VERIFIED` |
| 4 | Same thread 串行 / Different thread 并行 | `test_recovery_scenarios.py::test_same_thread_serial_different_thread_parallel`；`test_run_reliability.py::{test_same_thread_serial_execution,test_different_thread_parallel_execution}` | `LOCALLY VERIFIED` |
| 4b | 真实 Redis 锁 owner/TTL/Lua compare-and-delete | `tests/integration/test_thread_lock_redis.py` | `LOCALLY VERIFIED`（gated，已用真实 Redis 执行） |
| 5 | Side-effect tool 崩溃重放不重复执行 | `test_recovery_scenarios.py::test_side_effect_not_repeated_after_crash_replay`；`test_run_reliability.py::test_side_effect_not_repeated_after_worker_replay` | `LOCALLY VERIFIED` |
| 6 | SSE disconnect + `Last-Event-ID` reconnect | `test_recovery_scenarios.py::test_sse_reconnect_with_last_event_id`；`tests/unit/test_run_events.py::test_sse_resume_from_last_id` | `LOCALLY VERIFIED` |
| 6b | 真实 Redis Streams XADD/XREAD/MAXLEN/TTL/跨客户端 | `tests/integration/test_run_events_redis.py` | `LOCALLY VERIFIED`（gated，已用真实 Redis 执行） |
| 7 | 可观测性（trace 关联 / 指标注册 / 不泄露 query） | `distributed_runtime/test_observability.py`（4 用例） | `LOCALLY VERIFIED` |

### 2.1 各场景断言要点

- **场景 1**：node1→node2→node3；node2 首次抛 `TransientError`（模拟 worker 被杀）。
  断言 run 先 `QUEUED`（retry）后 `SUCCEEDED`；`node_log.count("node1")==1`
  （已完成节点不重跑）；checkpoint 可被读取。跨 worker 版本用**两个独立
  `build_postgres_checkpointer`（各自连接池）**，证明 checkpoint 落 PostgreSQL 且
  新 worker 可恢复。
- **场景 2**：worker 在独立 task 中执行；模拟“停止 API”后 worker 继续；用**全新
  app + RunService 实例**（仅共享 SQLite）`GET /api/runs/{id}` 返回 `SUCCEEDED` + result。
- **场景 3**：两个 worker_id 对同 run 投递；第二个见 `RUNNING` + 有效 lease 直接返回
  `RUNNING`，runtime 调用次数保持 1。
- **场景 4**：同 thread 第二个 run 保持 `QUEUED`（未执行）；不同 thread B 在 A 阻塞时
  进入执行，`max_concurrent==2`。
- **场景 5**：node2 先调用 fake refund（调用计数 1），再抛异常；重放后 refund 调用
  计数仍为 1（`tool_side_effects` 唯一约束 + 请求指纹）。
- **场景 6**：连接 1 收 2 帧后断开，记录 `id:`；用 `Last-Event-ID` 重连收剩余帧；
  合并后 sequence `[1,2,3,4,5]`，无重复/倒序，终帧 `run_completed`。
- **场景 7**：trace_id 从 AgentRun 传播到 worker/graph（`get_trace_id()` 一致）；
  要求的 Prometheus 指标全部注册；`agent_run_total` label 不含 `query`/`run_id`；
  run event payload 不含 `TOP_SECRET_QUERY`；`checkpoint_operation_seconds` 有样本。

## 3. 本次发现并修复的真实缺陷

| 缺陷 | 影响 | 修复 |
|------|------|------|
| `RunService.mark_running` 在 SQLite 下比较 `lease_expires_at`（naive）与 `now`（aware）抛 `TypeError` | 重复投递路径在 SQLite 环境崩溃 | 新增 `_ensure_aware()`，统一 UTC aware 后比较（`runtime/run_service.py`） |

该缺陷由 `test_duplicate_delivery_does_not_repeat_execution` 首次暴露，修复后用例通过。

## 4. 未验证项（NOT_VERIFIED）

以下**没有**真实执行，故明确标 `NOT_VERIFIED`，不得据此宣称生产就绪：

- `docker compose up` 后 **API 容器停止不影响 worker 中已开始的 run** 的**容器级**验证
  （当前仅在进程内模拟 API 重启）。
- **Kubernetes / 多副本 / 真实生产 HA** 下的 thread lock、checkpoint、事件流行为。
- **真实网络分区 / Redis 或 PostgreSQL 宕机**下的端到端行为（仅做了单元/受控集成级
  故障注入，未做真实基础设施故障演练）。
- **持续故障下的 DLQ 增长曲线**、事件流 backlog/慢消费者的**负载压测**。
- **端到端 exactly-once**：未实现，也未验证；仅 at-least-once + 应用层幂等。
- 生产级 SLO/延迟数字：`NOT_MEASURED`。

## 5. 结论

在**本地/受控**环境（SQLite + 真实 Redis + 真实 PostgreSQL 单实例）下，下列能力
已由自动化测试证明：worker crash 的 checkpoint 恢复（含跨 worker）、API 重启不丢 run、
重复投递不重复执行、同 thread 串行/不同 thread 并行、副作用崩溃重放去重、SSE 断线
续读、trace 关联与指标注册且不泄露 query。

这些是 `LOCALLY VERIFIED`；**生产多副本 HA 仍未验证（NOT_VERIFIED）**，不得夸大。
