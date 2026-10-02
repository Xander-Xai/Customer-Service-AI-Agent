# Runtime State Ownership

> Status: CURRENT（随 `core/concurrency/`、`runtime/`、`core/checkpointer.py` 实现同步）。
> 目的：明确每一种运行时状态的 **owner / scope / storage / 是否跨 Worker / 是否 durable**，
> 避免把进程内状态误当成分布式状态。

## 判定原则

1. 跨 Worker / 跨副本 / 跨请求必须一致读取 → 必须外部化（PostgreSQL / Redis）。
2. 仅单次 `graph.ainvoke` 生命周期内使用 → 可保留进程内。
3. 可重建的瞬时派生数据 → 进程内可接受。
4. 进程内状态在 gunicorn 多 worker 下是**每 worker 一份**，不得作为正确性依赖。

## 状态归属表（当前代码事实）

| State | Scope | Storage | Cross Worker | Durable | Owner 代码 |
|---|---|---|---|---|---|
| LangGraph checkpoint | thread | PostgreSQL（生产 `AsyncPostgresSaver`；dev/test `MemorySaver`） | Yes | Yes | `core/checkpointer.py` |
| Session history | thread/user | Redis（生产强制）；dev/test 可 memory | Yes（redis） | TTL | `core/session/session_manager.py` |
| Agent Run metadata | run | PostgreSQL `agent_runs` | Yes | Yes | `runtime/repository.py` |
| Tool idempotency ledger | operation | PostgreSQL `tool_side_effects` | Yes | Yes | `runtime/side_effects.py` |
| Dead letter evidence | run | PostgreSQL `agent_dead_letters` | Yes | Yes | `runtime/repository.py` |
| Thread execution lock | thread | Redis `agent:thread-lock:{thread_id}`（dev memory） | Yes（redis） | TTL | `core/concurrency/distributed_lock.py` + `runtime/thread_lock.py` |
| Response cache L1 | query/user | Redis | Yes | TTL | `cache/response_cache.py` |
| Response cache L2 | query/user | Qdrant | Yes | TTL | `cache/response_cache.py` |
| Response cache L3 | query/user | 进程内 Jaccard 回退 | No | No | `cache/` |
| SharedBlackboard | run/session | 进程内 dict + contextvar | No | No | `core/shared_blackboard.py` |
| MessageBus event | run-local | 进程内 asyncio.Queue | No | No | `core/message_bus.py` |
| SSE event queue | request/run | 进程内 asyncio.Queue | No | No | `api/routes/chat.py` |
| Circuit breaker | process | 进程内 | No | No | `core/monitoring.py` |
| Metrics counters | process | 进程内 + Redis 快照 | 部分 | 部分 | `core/monitoring.py` |

## 进程内状态的合法性论证

- **SharedBlackboard**：所有 key 的读写在单次 `graph.ainvoke` 内完成，contextvar
  按 session 隔离；不跨 Run/请求。→ 保留进程内（`docs/design/architecture-design.md`
  与 `core/shared_blackboard.py` 语义一致）。若未来出现跨 Run 读取，需迁移 Redis/PG。
- **MessageBus**：仅 run-local Agent 间通信与 SLA 告警广播；**不是**消息队列，
  不用于跨 worker streaming / cache invalidation / task dispatch。→ 保留 asyncio。
- **SSE event queue**：单请求内流式推送。多副本下客户端需 sticky 或后续
  Redis Streams bridge（见 `docs/design/async-agent-worker-architecture.md`）。
- **Circuit breaker / metrics counters**：每 worker 独立可接受；汇总以 Prometheus
  抓取为准。

## 多 Worker 正确性依赖（生产 gate）

生产 + `GUNICORN_WORKERS>1` 时，以下三项必须成立，否则启动 fail-fast
（`core/config.py::validate_distributed_runtime_settings`）：

1. `LANGGRAPH_CHECKPOINT_BACKEND=postgres`（跨 worker 共享图状态）；
2. `SESSION_STORAGE_BACKEND=redis`（跨 worker 共享会话）；
3. `AGENT_RUN_THREAD_LOCK_ENABLED=true` + `AGENT_RUN_THREAD_LOCK_BACKEND=redis`
   （同一 thread 跨进程串行）。

## 反模式（禁止）

- 用 sticky session 替代 durable state。
- 用全局 `asyncio.Lock` 声称分布式互斥（跨进程无效）。
- 生产 PostgreSQL 不可用时静默 fallback 到 `MemorySaver`。
- 把所有进程内状态无差别 Redis 化（先判断 scope）。
- 把 asyncio MessageBus 当消息队列。
