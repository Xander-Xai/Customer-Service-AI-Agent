# DISTRIBUTED_RUNTIME_AUDIT

> **HISTORICAL AUDIT SNAPSHOT — 2026-10-02.** 本文件是**时点审计结果**，只在该
> 审计执行的 HEAD 上有效，**不是 Current Truth**，不得与
> [docs/reference/current-state.md](../../reference/current-state.md) 竞争。
>
> 文件内容按审计时点原样冻结，**不随后续代码更新而重写**。
> 其中若干结论已被后续 commit 推翻 —— 典型是本报告 §9"仅设计（不在本 Foundation
> 实现）"里的 **"DLQ 运维闭环（replay / requeue / 告警）"** 与
> **"cross-process SSE event bridge"**：两者在当前 HEAD 均已实现
> （`scripts/replay_dead_run.py` 复用原 `run_id`；`runtime/events.py` Redis Stream
> + `GET /api/runs/{run_id}/events` SSE 续读）。**读当前事实请以 `current-state.md`
> 为准。**
>
> 审计对象：`Xander-Xai/customer-ai-agent` · 审计时 HEAD `912ad49`（`feat/distributed-agent-runtime`）
> 审计原则：以当前代码为事实源；先审计后修改；不夸大验证状态。
> 说明：本仓库此前已落地一轮分布式运行时工作（`runtime/` 包、Postgres
> checkpointer、Run 模型、Celery worker、thread lock、side-effect ledger）。
> 本次审计据实区分「已外部化 / 仍在进程内 / 仍缺失」，避免重复造轮子。

---

## 1. 状态归属总览（当前代码事实）

| State | Scope | 当前 Storage | 跨 Worker | Durable | 判定 |
|---|---|---|---|---|---|
| LangGraph checkpoint | thread | **PostgreSQL**（`core/checkpointer.py`, `AsyncPostgresSaver`；dev/test `MemorySaver`） | Yes | Yes | 已外部化 |
| Session history | thread/user | Redis（`SESSION_STORAGE_BACKEND=redis` 时）；**否则进程内 dict** | 视配置 | TTL | **部分外部化（生产强制缺失）** |
| Run metadata (`agent_runs`) | run | PostgreSQL | Yes | Yes | 已外部化 |
| Tool side-effect ledger (`tool_side_effects`) | operation | PostgreSQL | Yes | Yes | 已外部化（未被真实写工具使用） |
| Thread execution lock | thread | Redis（worker 路径）；**API 快路径未加锁** | worker: Yes / API: No | TTL | **部分外部化（API 缺口）** |
| Response cache L1 | query/user | Redis | Yes | TTL | 已外部化 |
| Response cache L2 | query/user | Qdrant | Yes | TTL | 已外部化 |
| SharedBlackboard | session/run | **进程内 dict**（`core/shared_blackboard.py`） | No | No | 进程内（见 §6） |
| MessageBus | run-local | **进程内 asyncio.Queue**（`core/message_bus.py`） | No | No | 进程内（见 §6） |
| SSE event queue | request/run | 进程内 `asyncio.Queue`（`api/routes/chat.py`） | No | No | 进程内（预期） |
| Metrics collector counters | process | 进程内 + Redis 快照（`core/monitoring.py`） | 部分 | 部分 | 可接受 |
| Circuit breaker | process | 进程内（`core/monitoring.py`） | No | No | 进程内（每 worker 独立，可接受） |

## 2. 仍在进程内的状态（需判断，不必然要改）

1. **SharedBlackboard**（`core/shared_blackboard.py`）：`{session_id: {key: value}}`
   进程内 dict + `asyncio.Lock`。写入/读取均在单次 `graph.ainvoke` 生命周期内，
   由 `set_blackboard_session_id()`（contextvar）隔离。
2. **MessageBus**（`core/message_bus.py`）：`asyncio.Queue` + subscriber dict，
   用于同一 run 内多 Agent 通信与 SLA 告警广播。
3. **SSE chunk queue**（`api/routes/chat.py`）：请求内流式事件队列。
4. **CircuitBreaker / MetricsCollector 计数器**：每 worker 独立。
5. **`asyncio.Lock`**（`core/container.py::ServiceContainer._lock`、
   `MessageBus`/`SharedBlackboard`）：只保证单进程内并发安全。

## 3. Gunicorn 多 Worker 下会发生什么

| 场景 | 现状 | 后果 |
|---|---|---|
| 同一 thread 两个请求落到不同 worker | **API 快路径无 per-thread 锁** | 两个 worker 并发 `ainvoke` 同一 `thread_id`，checkpoint 竞争/覆盖，对话状态串扰 |
| Checkpoint 读写 | 生产已是 Postgres（`LANGGRAPH_CHECKPOINT_BACKEND=postgres`） | 若被显式配成 memory → 各 worker 各自一份，断点续传失效（config 已 fail-closed） |
| Session | 生产若 `SESSION_STORAGE_BACKEND=memory`（模板默认值） | 会话历史随 worker 分片、重启丢失；**当前只 warning，不 fail-fast** |
| Worker 崩溃 | 异步路径有 lease + broker redelivery；**同步路径无 checkpoint resume 保证** | 同步请求中途崩溃，客户端需重试；重试可能重复副作用 |
| Tool 写副作用 | 目前 ERP 工具**全是只读**；ledger 已建但无写工具接入 | 一旦新增写工具且未接 ledger，重试会重复执行 |
| Blackboard/MessageBus | 进程内 | 跨 worker 不共享；当前用法均为 run-local，暂无跨进程误用 |
| SSE | 进程内 | 跨 worker 不共享；多副本下客户端需 sticky 或后续 Redis Streams |

## 4. 非幂等副作用工具审计

- `tools/erp_tools.py`：`query_product` / `query_inventory` / `query_order` /
  `query_customer` —— **全部只读**（`erp_adapter` 无写方法）。
- 结论：**当前没有已实现的非幂等写工具**。`runtime/side_effects.py` +
  `tool_side_effects` 表已提供通用幂等基础设施，但尚无真实写工具调用它。
- 风险：未来新增 `refund` / `create_ticket` / `modify_order` 等写工具时，
  必须走 `SideEffectStore.claim/mark_succeeded`，否则重试重复副作用。

## 5. 需要修改的模块（本次范围）

| 模块 | 缺口 | 处理 |
|---|---|---|
| `api/app.py::_run_graph` | 统一执行边界未加 per-thread 锁 | 加入 Redis 分布式锁（REST/SSE/WS/multimodal 共用） |
| `core/concurrency/distributed_lock.py` | 不存在（推荐路径） | 新增 API 层 async context manager（复用 `runtime.thread_lock` 原语） |
| `core/config.py` | 生产 memory session 只 warning；多 worker 一致性无 gate | 改为 fail-fast；新增 acquire timeout；多 worker 一致性校验 |
| `core/container.py` | Redis session 初始化失败静默回退 memory | 生产 fail-fast |
| `core/monitoring.py` | 缺 `agent_thread_lock_acquire_total` / `agent_runs_active` / `agent_run_failures_total` / `checkpoint_errors_total` | 补齐并接线 |
| `core/checkpointer.py` | 无 checkpoint error 指标 | 接线 `checkpoint_errors_total` |
| `runtime/side_effects.py` | 缺通用「包裹执行」helper | 新增 `execute_idempotent_operation` |
| `deploy/compose/*.yml` | base/canary 默认 `SESSION_STORAGE_BACKEND=memory`；worker 未设 session backend | 明确 redis |
| `.env.example` | session 模板默认 memory | 改为 redis（注释 dev 可 memory） |
| 文档 | 缺 ownership/ADR/async-worker 设计/README Mermaid/审计 | 新增 |

## 6. SharedBlackboard / MessageBus 架构判断

- **SharedBlackboard**：所有 key 的读写都发生在单次 `graph.ainvoke` 内，
  contextvar 按 session 隔离，跨 Agent 但**不跨 Run/请求**。→ **保留进程内**。
  若未来出现「跨 Run 读取」需求，再迁 Redis/PG（写入 `docs/design/runtime-state-ownership.md`）。
- **MessageBus**：仅 run-local Agent 间通信 + SLA 告警广播，不用于跨 worker
  streaming / cache invalidation / task dispatch。→ **保留 asyncio**。
  后续 Redis Pub/Sub / Streams 作为可选 bridge，接口预留（见 async-worker 设计）。

## 7. 可能受影响的现有测试

- `tests/unit/test_api_routes.py`、`tests/e2e/**`：调用 `/api/chat`、
  `/api/chat/stream`、WS、multimodal → 会经过新的 `_run_graph` 锁。
  **必须保证 DEV/TEST 下锁为进程内/可关闭，且不依赖真实 Redis。**
- `tests/unit/test_core_modules.py`、`test_app_factory.py`：容器/配置初始化
  → 生产 fail-fast 变更需用 DEV 或显式配置，避免误触发。
- `tests/integration/test_integration.py`：端到端图执行 → 锁路径。
- `tests/unit/test_checkpointer.py`：checkpointer 配置/生命周期 → 指标接线不改变行为。
- 既有 `runtime` 测试（`test_run_executor.py` 等）：复用 `runtime.thread_lock`，
  新增 API 层锁不应改变 worker 锁语义。

## 8. 会因本次修改而过期的文档

- `README.md`：需加 Distributed Runtime Foundation Mermaid。
- `docs/reference/current-state.md`：已含分布式 runtime 段，需补 API 锁/session 强制。
- `docs/design/architecture-design.md`：需补 API 执行边界锁。
- `docs/checklists/production-readiness-checklist.md`：session/lock/多 worker gate。
- `docs/operations/production-operations-guide.md`：session backend / lock 排障。
- `.env.example` / compose 使用说明。
- 新增 `docs/design/runtime-state-ownership.md`、`docs/decisions/009-*.md`、
  `docs/design/async-agent-worker-architecture.md`。

## 9. 已实现 vs 仅设计（事实口径）

**已实现（代码存在，本分支）**：Celery + Redis broker、`runtime/tasks.py`、
`runtime/dispatch.py`、`POST/GET /api/runs`、`GET /api/runs/dead`、AgentRun 真相源、
worker 消费 `run_id`、`acks_late` / `reject_on_worker_lost` / `visibility_timeout`、
retry 基础（RETRYING + 退避）、dead-letter state + `agent_dead_letters` 表、独立
compose `worker` service、Postgres checkpointer、Redis session、Redis per-thread
lock、tool ledger + idempotency helper、Prometheus metrics。

**仅设计（不在本 Foundation 实现）**：

- Worker Pool autoscaling / 多队列编排（基础单 worker service 已实现）。
- DLQ 运维闭环（replay / requeue / 告警）——当前只有 dead-letter state + 查询 API。
- cross-process SSE event bridge（Redis Streams replay）。
- backpressure / queue admission control。
- lease renewal / fencing token（锁 stale-worker 边界）。
- Kubernetes / HPA / Service Mesh / 分布式事务。
- Kafka / RabbitMQ / Temporal / Saga / Outbox。

## 10. 本次结论（Foundation 定义）

本次收口补齐/收紧的**真实缺口**：

1. **API 执行边界 per-thread 分布式锁**（REST/SSE/WS/multimodal 统一），
   拿不到锁返回明确 `THREAD_BUSY`（HTTP 409）。
2. **生产强制 Redis Session**（fail-fast，不再静默回退 memory）。
3. **多 Worker 配置一致性 gate**（`GUNICORN_WORKERS>1` + 生产 →
   要求 postgres checkpoint + redis session + distributed lock；生产要求 celery
   dispatch；lock TTL > task time limit）。
4. **缺失的可运营 metrics** 与统一 trace/run/request/thread 日志字段。
5. **通用 tool 幂等执行 helper**（ledger 已有，补包裹执行 + 测试）。
6. **文档/ADR/ownership/async 设计/README Mermaid/对外评审 evidence + 可复现脚本**。

已具备、无需重做的：Celery 异步链路、Postgres checkpointer、Run 模型/迁移、worker
侧 thread lock、side-effect ledger、fresh-DB migration 修复。
