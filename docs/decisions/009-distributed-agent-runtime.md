# ADR-009: Distributed Agent Runtime Foundation（PostgreSQL + Redis + asyncio 分工）

**日期**：2026-10-02
**状态**：已采纳
**决策者**：项目负责人

## 背景

系统以 FastAPI + LangGraph 运行，生产用 gunicorn 多 worker、Docker Compose
多实例。此前若干运行时状态是进程内的：LangGraph 曾默认 `MemorySaver`、
Session 生产模板默认 memory、MessageBus/SharedBlackboard 是进程内对象、
同一 thread 的并发请求没有跨进程互斥。这导致多 worker 下 checkpoint 不共享、
会话分片、同 thread 并发写状态、worker 崩溃无法可靠恢复、重试可能重复副作用。

## 决策

采用「按状态生命周期分工」的 Distributed Runtime Foundation：

- **PostgreSQL** 承担 durable state：LangGraph checkpoint（`AsyncPostgresSaver`）、
  Agent Run metadata（`agent_runs`）、tool 幂等 ledger（`tool_side_effects`）、
  dead letter（`agent_dead_letters`）。
- **Redis** 承担 ephemeral coordination：per-thread 分布式锁
  （`agent:thread-lock:{thread_id}`，owner + TTL + Lua compare-and-delete）、
  Session、Response cache L1。
- **asyncio / 进程内** 承担 run-local 并发与通信：MessageBus、SharedBlackboard、
  SSE event queue、circuit breaker。
- 生产 + 多 worker 强制一致性 gate（postgres checkpoint + redis session +
  distributed lock），否则启动 fail-fast。

## 理由

- PostgreSQL 有事务/唯一约束/持久化，适合 canonical 状态与幂等最后防线；
  Redis 有低延迟 TTL/原子操作，适合锁与会话；asyncio 零开销，适合 run-local。
- 替代方案：
  - 只用 sticky session：不能替代 durable state（实例重启/扩缩容即失效）。
  - 全量 Redis 化 Blackboard/MessageBus：无必要，且引入序列化与一致性成本。
  - 全局 `asyncio.Lock`：跨进程无效，是伪分布式。
  - 引入 Kafka/Temporal：当前规模不需要，增加运维复杂度。

## 影响

- 正面：多 worker/多副本下 checkpoint/session 共享、同 thread 串行、崩溃可恢复、
  副作用可去重；`THREAD_BUSY` 提供明确并发契约。
- 负面：新增 PostgreSQL/Redis 依赖与迁移；生产配置更严格（fail-fast 可能阻断
  配置错误的启动）；异步 Run 路径引入 Celery（本 PR 保留为既有能力，不扩展）。

## 失败语义

- **Worker 崩溃**：未 ACK 任务经 Redis `visibility_timeout` 重投递，lease 过期后
  被新 worker 接管；失败节点可能重执行 → 节点/副作用需幂等。
- **PostgreSQL 不可用**：生产 checkpoint 初始化 fail-fast，不回退 MemorySaver；
  Run/幂等写入失败则请求失败（不静默降级）。
- **Redis 不可用**：生产 Session 初始化 fail-fast；锁后端不可用 fail closed
  （`THREAD_LOCK_UNAVAILABLE`，不静默并发）。
- **锁超时**：返回 `THREAD_BUSY`（HTTP 409），不悄悄并发执行。
- **重复 tool call**：ledger `(tool_name, operation_key)` 唯一约束 + claim 原子语义，
  第二次返回已保存结果。

## 未来工作

Redis/Celery durable task queue 深化、独立 Agent Worker Pool、retry/DLQ 强化、
cross-process SSE event bridge（Redis Streams）、Kubernetes/HPA。
见 [docs/design/async-agent-worker-architecture.md](../design/async-agent-worker-architecture.md)。

## 来源文档

- [docs/design/distributed-agent-runtime.md](../design/distributed-agent-runtime.md)
- [docs/design/runtime-state-ownership.md](../design/runtime-state-ownership.md)
- `DISTRIBUTED_RUNTIME_AUDIT.md`
