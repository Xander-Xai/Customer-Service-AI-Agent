# 分布式 Runtime 运维 Runbook

> 适用：Celery worker + Redis（broker / thread-lock / event stream）+ PostgreSQL
> （AgentRun 真相源 + LangGraph Checkpoint）+ Qdrant。
> 相关设计：[distributed-agent-runtime.md](../design/distributed-agent-runtime.md)。
> 状态：本文是**排查手册**，其中的命令需在目标环境实际执行确认；本地未执行项
> 不构成生产验证。

## 0. 关键事实（先记住）

- **真相源是 PostgreSQL `agent_runs`**，不是 Celery result，也不是 Redis Stream。
- Redis 承担 3 件事：Celery broker、thread lock、run event stream（临时）。
- Redis Stream 只是 ephemeral streaming channel；过期后最终状态仍从 `GET /api/runs/{id}` 读取。
- 投递语义是 **at-least-once + application-level idempotency**（非端到端 exactly-once）。

## 1. Worker stuck（任务不推进）

**症状**：`agent_worker_active` 长期不降；`agent_run_total{status="RUNNING"}` 增长；
`GET /api/runs/{id}` 一直 RUNNING；队列积压。

**排查**：
1. 看 worker 进程与日志：`docker compose logs -f worker`（或 `celery -A runtime.celery_app:celery_app inspect active`）。
2. 判断是 lease 有效还是过期（stuck vs 崩溃遗留）：
   ```sql
   SELECT id, status, worker_id, lease_expires_at, heartbeat_at, attempt, updated_at
   FROM agent_runs WHERE status = 'RUNNING' ORDER BY updated_at ASC LIMIT 20;
   ```
   - `lease_expires_at > now()` 且 `heartbeat_at` 在更新 → worker 仍在跑（可能 LLM 慢）。
   - `lease_expires_at < now()` → worker 崩溃遗留；重新投递后新 worker 会接管。
3. 是否被 thread lock 卡住：
   ```bash
   redis-cli --scan --pattern 'agent:thread-lock:*'
   redis-cli PTTL 'agent:thread-lock:<thread_id>'
   redis-cli GET  'agent:thread-lock:<thread_id>'
   ```
4. 是否卡在 Celery broker：`celery -A runtime.celery_app:celery_app inspect ping`。

**处理**：
- LLM/工具慢：确认 `AGENT_RUN_TASK_SOFT_TIME_LIMIT`/`_TIME_LIMIT` 是否合理。
- 崩溃遗留：等待 lease 过期后重投；或手动把 RUNNING 置回 QUEUED（谨慎，需确认无活跃 worker）：
  ```sql
  UPDATE agent_runs SET status='QUEUED', worker_id=NULL, lease_expires_at=NULL
  WHERE id='<run_id>' AND status='RUNNING' AND lease_expires_at < now();
  ```
  然后重新投递该 `run_id`。
- 指标：`agent_worker_active`、`agent_run_duration_seconds`、`thread_lock_wait_seconds`。

## 2. Redis unavailable

**影响**：
- Celery broker 不可用 → `POST /api/runs` 入队失败：`QUEUED -> DEAD(DISPATCH_FAILED)`，API 返回 `503`。
- thread lock 后端不可用 → run 延迟重调度（`run_queued`，不消耗 attempt），不会误并发。
- event stream 不可用 → 发布 best-effort 失败（`run_event_publish_error_total`），SSE 退化为 DB 终态轮询。

**排查**：`redis-cli ping`；`docker compose ps redis`；检查 `REDIS_URL` / `CELERY_BROKER_URL`。

**处理**：恢复 Redis；DEAD 的 DISPATCH_FAILED run 需人工重投（见 §4）。生产禁止
`RUN_EVENT_STREAM_BACKEND=memory` 与 `AGENT_RUN_THREAD_LOCK_BACKEND=memory`（仅单进程）。

## 3. PostgreSQL unavailable

**影响**：`agent_runs` 是真相源，写入失败 → API `POST/GET` 报错；checkpoint 后端
（`LANGGRAPH_CHECKPOINT_BACKEND=postgres`）初始化失败会 **fail closed**，API/worker
拒绝启动（生产不静默回退 MemorySaver）。

**排查**：
```bash
docker compose ps postgres
psql "$DATABASE_URL" -c "SELECT 1"
psql "$DATABASE_URL" -c "SELECT count(*) FROM agent_runs WHERE status='RUNNING';"
```

**处理**：恢复 PG；确认 Alembic 已到 head（`alembic current` / `alembic upgrade head`）；
重启 API/worker。检查 `/api/health` 的 `database.connected` 与 `langgraph_checkpoint.status`。

## 4. DLQ growth（DEAD 增长）

**症状**：`agent_run_dead_total` 增长；`GET /api/runs/dead` 出现条目。

**排查**：`GET /api/runs/dead?limit=100`（需 admin/supervisor）看 `error_type` / `error_code` / `error_message`（已脱敏）。
- `permanent`：参数/权限/业务拒绝 → 修业务输入，不 retry。
- `transient` 且 attempts 用尽 → 下游持续故障；修下游后需人工重投。
- `DISPATCH_FAILED` → broker 故障。

**处理**：修复根因后，把 DEAD run 重新入队（人工/脚本，需评估幂等）：将状态置回
QUEUED 并投递 `run_id`；side-effect 工具由 `tool_side_effects` 保证不重复执行。

## 5. Lock leaked（thread lock 泄漏）

**症状**：`thread_lock_contention_total` 持续增长；某 thread 的 run 一直 QUEUED。

**排查**：
```bash
redis-cli GET  'agent:thread-lock:<thread_id>'
redis-cli PTTL 'agent:thread-lock:<thread_id>'
```
- `PTTL` 为 `-1`（无 TTL）不应出现；应为正数（`AGENT_RUN_THREAD_LOCK_TTL_SECONDS`）。
- owner 是已崩溃的 worker_id → 等待 TTL 自动过期即可恢复（`lock_expiration_recovery`）。

**处理**：一般**等待 TTL**；确需手动清理时，只有在确认无活跃 owner 后才删除：
```bash
redis-cli DEL 'agent:thread-lock:<thread_id>'
```
禁止用简单 `SETNX ... DEL` 语义的脚本乱删（可能误删其他 worker 新锁）。

## 6. SSE unavailable

**症状**：`/api/runs/{id}/stream` 无事件 / 立即结束；`sse_connections` 异常。

**排查**：
1. Stream 是否过期/被裁剪：
   ```bash
   redis-cli XLEN  'agent:run-events:<run_id>'
   redis-cli TTL   'agent:run-events:<run_id>'
   redis-cli XRANGE 'agent:run-events:<run_id>' - + COUNT 5
   ```
2. worker 是否在发布：`run_event_publish_total` / `run_event_publish_error_total`。
3. 客户端是否用 `Last-Event-ID` 续读。

**处理**：Stream 过期是设计内行为；最终状态从 `GET /api/runs/{id}` 读取（真相源）。
若 Redis 不可用，SSE 会回退 DB 终态；恢复 Redis 后事件流恢复。检查
`RUN_EVENT_STREAM_MAXLEN` / `RUN_EVENT_STREAM_TTL_SECONDS` 是否过小。

## 7. 相关指标速查

| 指标 | 含义 |
|------|------|
| `agent_run_total{status}` | run 结果分布 |
| `agent_run_retry_total` / `agent_run_dead_total` | 重试 / DEAD |
| `agent_run_duration_seconds` / `agent_run_queue_wait_seconds` | 执行时长 / 排队 |
| `agent_worker_active` / `agent_worker_task_total` | worker 活跃 / 启动任务数 |
| `thread_lock_wait_seconds` / `thread_lock_contention_total` | 锁等待 / 竞争 |
| `checkpoint_operation_seconds` | checkpoint 操作延迟 |
| `run_event_publish_total` / `_error_total` / `run_event_lag_seconds` | 事件发布 / 延迟 |
| `sse_connections` / `sse_reconnect_total` | SSE 连接 / 重连 |
| `idempotency_hit_total` / `tool_idempotency_hit_total` | 幂等命中 |

## 8. 关联排查（trace）

HTTP 入口生成 `trace_id`（`X-Trace-Id`），写入 AgentRun.trace_id；worker 执行前
`set_trace_id(run.trace_id)`，日志与 LangGraph 执行共享同一 trace_id；SSE 事件含
`run_id` / `thread_id`。**不要把完整用户 query 放进 trace/metric label**（高基数 + 隐私）。
