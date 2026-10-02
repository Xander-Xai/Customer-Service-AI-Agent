# Distributed Runtime Runbook

> 面向运维/值班：分布式 Agent Runtime 的验收命令、排障路径与已知限制。
> 设计说明见 [agent-runtime.md](../design/agent-runtime.md)。

---

## 0. 从零环境准备

```bash
git clone <repository> && cd customer-service-ai-agent

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt  # pytest / ruff / mypy / pytest-timeout
# 可选：pip install -r requirements-optional.txt

cp .env.example .env
```

依赖**完全由仓库声明**：不提交 `.venv`，不依赖系统 Python 的预装包。
`tests/unit/test_dependency_contract.py` 会断言"运行时 import 的第三方模块都在
requirements*.txt 里声明"——这是为了防止再次出现 `prometheus_client` 那样
try/except 静默降级（应用能启动，但 `/api/metrics` 为空、指标全部消失）。

自检（干净 venv 内）：

```bash
python -c "import celery, psycopg, langgraph, prometheus_client; print('deps OK')"
```

## 1. 验收命令

需要**真实** PostgreSQL 与 Redis。环境缺失时目标 FAIL（不静默 skip）。

```bash
make runtime-e2e      # 30 个集成用例（checkpoint / 线程模型 / 队列 / 重试 / DLQ / 幂等 / 事件流）
make runtime-chaos    # worker kill -9 → lease 过期 → checkpoint 续跑 → 副作用恰好一次
make runtime-verify   # 机器可读证据 artifacts/distributed-runtime/<ts>/report.json
make runtime-replay-help
```

覆盖真实 PG/Redis 的数据库：

```bash
docker exec <pg-container> psql -U postgres -c "CREATE DATABASE csai_runtime_test;"
RUNTIME_DB_URL=postgresql://... make runtime-e2e
RUNTIME_REDIS_URL=redis://...  make runtime-chaos
```

`make runtime-verify` 的退出码：`0`=全 PASS，`1`=有 FAIL，`2`=未配置基础设施
（`NOT_RUN`/`PARTIAL`）——**没有**把"没跑"记成"通过"的路径。

---

## 2. Gate ↔ 测试对照

| Gate | 断言 | 测试 |
|---|---|---|
| 1 | checkpoint 跨进程持久化与恢复 | `tests/integration/runtime/test_cross_process_checkpoint.py` |
| 2 | thread/run 分离（共享 thread、run_id 唯一） | `test_run_semantics.py::test_gate2_*` |
| 3 | 同 thread 执行区间不重叠（`started_at`/`finished_at`） | `test_run_semantics.py::test_gate3_*` |
| 4 | 跨 thread 真并发（耗时 ≈ 一次 sleep） | `test_run_semantics.py::test_gate4_*` |
| 5 | lease 非 owner 不可释放 + TTL 过期后可接管 | `test_run_semantics.py::test_gate5_*` |
| 6 | queue/worker 解耦（无 worker 时保持 QUEUED） | `test_queue_worker_decoupling.py` |
| 7 | worker kill -9 后**从 checkpoint 续跑** | `test_worker_checkpoint_recovery.py` |
| 8 | 两次失败后第三次成功（attempt==3） | `test_run_semantics.py::test_gate8_*` |
| 9 | permanent（401/403/参数/业务校验）不重试 | `test_run_semantics.py::test_gate9_*` |
| 10 | DLQ 证据齐全 + CLI 重放 | `test_run_semantics.py::test_gate10_*`、`scripts/replay_dead_run.py` |
| 11 | 重复投递下副作用去重（含 worker 崩溃后重投） | `test_tool_idempotency.py`、`test_tool_idempotency_metric.py` |
| 12 | run 查询字段齐全 + 不泄露 secret | `test_run_semantics.py::test_gate12_*` |
| 13 | 事件流 + SSE `Last-Event-ID` 续读 | `test_run_events.py` |
| 14 | 指标存在 + 无高基数 label | `tests/unit/test_runtime_metrics_contract.py` |
| 0 | SSE 与 checkpointer 共存（回调不入 checkpoint） | `test_sse_checkpoint_serialization.py` |

混沌证据（`make runtime-chaos` 的 JSON）关键字段：

| 字段 | 含义 |
|---|---|
| `steps[].checkpoint_persisted.checkpoints` | 崩溃前 PG 里已存在的 checkpoint 数 |
| `steps[].side_effect_applied.idem:counter` | 崩前副作用已落地（必须 1） |
| `steps[].recovered.attempt` / `worker_switched` | 重投后 attempt 与接管 worker |
| `steps[].side_effect_deduplicated.idem:counter` | 最终副作用写入次数（必须 **1**，即去重生效） |
| `steps[].side_effect_deduplicated.ledger_hits` | 第二次调用命中 ledger（必须 ≥1） |

---

## 3. 排障

### 3.1 Run 一直 `QUEUED`

```sql
SELECT status, attempt, next_retry_at, worker_id, error_code
FROM agent_runs WHERE id = '<run_id>';
```

- `next_retry_at` 非空 → 因 thread lease 竞争或退避被推迟，等 broker countdown。
- `error_code = 'DISPATCH_FAILED'` → 入队失败（broker 不可达）。
- 无 worker 在消费：`celery -A runtime.celery_app:celery_app inspect active`。
  生产要求 `AGENT_RUN_DISPATCH=celery`；`inline` 只用于开发（生产 fail-fast）。

### 3.2 Run 卡在 `RUNNING`

worker 崩溃但消息还没回到队列，或 worker 仍在跑。

```sql
SELECT id, worker_id, task_id, lease_expires_at, heartbeat_at
FROM agent_runs WHERE status = 'RUNNING';
```

`heartbeat_at` 停止推进且 `lease_expires_at` 已过期 → 该 worker 已死。等
`AGENT_RUN_VISIBILITY_TIMEOUT` 后 broker 重投，新 worker 会接管 lease 并递增
`attempt`。

**排障时不要手工把状态改成 `SUCCEEDED`**：真相源由条件更新保护，手改会破坏 lease
语义。

### 3.3 `DEAD_LETTER` 堆积

```sql
SELECT run_id, thread_id, attempt_count, error_type, error_code, entered_at
FROM agent_dead_letters ORDER BY entered_at DESC LIMIT 50;
```

按 `error_type` 区分：
- `timeout` / `transient` → 基础设施抖动，考虑提高 `AGENT_RUN_MAX_ATTEMPTS`；
- `permanent` → 代码/契约问题，重放前先修。

重放：

```bash
python scripts/replay_dead_run.py <run_id>          # 交互确认
python scripts/replay_dead_run.py <run_id> --yes    # 非交互（CI / 自动化）
```

重放**复用原 `run_id`**（只产生新的队列投递），原始 DLQ 历史不被改写。不要通过
"新建一个 run"来重放——那会绕过工具幂等键，导致已成功的退款/改单再执行一次。

> 目前**没有** dead-letter 告警，只有 `agent_run_dead_letter_total` 计数；需要告警时
> 自行加基于该指标的规则。

### 3.4 线程一直 `THREAD_BUSY`（HTTP 409）

同一 `thread_id` 已有 Run 在执行。快路径获取锁有界重试
（`AGENT_RUN_THREAD_LOCK_ACQUIRE_TIMEOUT_SECONDS`，默认 5s）。

```bash
redis-cli --scan --pattern 'agent:thread-lock:*'
redis-cli get 'agent:thread-lock:<thread_id>'   # value = owner token
```

若长时间卡住：确认锁 TTL 大于任务时限（启动 gate 已强制
`AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT + 30`）。执行期间锁
由 `_heartbeat_loop` 续租；若看到 `agent_thread_lease_renewed_total{outcome="lost"}`
增长，说明 worker 失去租约（可能已被接管）。

### 3.5 怀疑副作用被执行了两次

```sql
SELECT run_id, tool_name, operation_key, status, created_at, finished_at
FROM tool_side_effects WHERE run_id = '<run_id>';
```

- 同一 `(tool_name, operation_key)` 应只有**一行**，`status=SUCCEEDED`；
- 指标 `agent_tool_idempotency_hit_total` 增长说明去重路径在工作；
- 若写工具没有 ledger 记录，说明它注册时漏了 `side_effect=True`。

**注意**：`PENDING` 行表示"已认领未完成"。认领后崩溃的行在租约过期后会被接管重放
——如果外部系统在崩溃瞬间其实已经成功，这里会产生重复写。根治需要下游 API 接受
idempotency key。

### 3.6 事件流没有内容

```bash
redis-cli XLEN 'agent:run:<run_id>:events'
```

- 空 → worker 未发布（该 run 走的不是 Run 路径，或 Redis 不可用；事件发布失败会降级
  为 no-op，不影响 Run 本身）。
- 有数据但 SSE 收不到 → 检查 `Last-Event-ID` 是否超出 `AGENT_RUN_EVENT_MAXLEN`
  裁剪窗口（默认 500）。超出部分**无法**恢复。注意该上限是**近似**的
  （`XADD MAXLEN ~`），实际可能保留多于该条数；这是设计边界，不是 bug。
- 事件流是 best-effort 可续读，**非** exactly-once：用较旧 `Last-Event-ID` 重连会
  **重复**收到已处理事件；`replay=false` 与 idle 超时造成**缺口**；`MAXLEN` 是
  **近似**裁剪（不是硬上界），被裁历史**不可恢复**。权威状态始终用
  `GET /api/runs/{run_id}`。

---

## 4. 值班指标速查

| 指标 | 告警建议 |
|---|---|
| `agent_run_total{status="DEAD_LETTER"}` 增速 | > 0 持续 10min → 排查上游 |
| `agent_thread_lease_contention_total` 增速 | 突增 → 有热点会话或锁 TTL 偏小 |
| `agent_thread_lease_renewed_total{outcome="lost"}` | > 0 → worker 频繁失去租约，查 lease/TTL 配置 |
| `agent_worker_heartbeat{outcome="lost"}` | > 0 → ownership 丢失，检查 DB 延迟 |
| `agent_run_queue_wait_seconds` p99 | 持续升高 → worker 不足 |
| `agent_checkpoint_recovery_total{recovered}` | 突增 → worker 反复崩溃 |
| `agent_tool_idempotency_hit_total` | 突增 → 重复投递变多，查 broker/visibility timeout |
| `checkpoint_errors_total` | > 0 → checkpoint 后端故障（生产会 fail-fast 拒绝启动） |

**label 纪律**：任何 `agent_*` 指标都不得带 `run_id` / `thread_id` / `user_id` /
`query`（高基数会打爆 Prometheus）。有测试断言这一点。

---

## 5. 已知限制（值班须知）

1. 无 fencing token：pause 超过 lease TTL 的旧 worker 不会被强制中止。
2. 单 Redis 互斥，不是 Redlock；Redis 故障转移期间可能双持有。
3. 认领后崩溃的副作用会被重放（见 §3.5）。
4. 取消是协作式的，不打断已在执行的图。
5. 事件流仅 best-effort 可续读（非 exactly-once）：可重复、可有缺口、裁剪后不可恢复。
6. 无 backpressure：永久锁死的 thread 会持续按
   `AGENT_RUN_THREAD_LOCK_RETRY_DELAY_SECONDS` 重投。
7. Worker Pool 无自动扩缩。
8. **生产集群未验证**：以上全部为本地 + CI 证据，`PRODUCTION NOT_VERIFIED`。