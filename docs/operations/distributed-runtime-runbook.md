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
make runtime-e2e      # tests/integration/runtime（checkpoint / 线程模型 / 队列 / 重试 / DLQ / 幂等 / 事件流）
make runtime-chaos    # worker kill -9 → lease 过期 → checkpoint 续跑 → 本 Agent 侧副作用去重（counter==1；非端到端 exactly-once）
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

**告警**：`AgentRunDeadLetterDetected`（`increase(agent_run_dead_letter_total[5m]) > 0`，
`severity: critical`，见 `monitoring/alert_rules.yml`）。用 `increase()` 而非
`agent_run_dead_letter_total > 0`，避免历史一次失败后永久报警。

**这个指标是 worker 容器写的，Prometheus 抓的是 app 容器。** 收到告警前先确认
指标真的可见 —— 否则你会盯着一个恒为 0 的计数器：

```bash
# 1) 应用确实暴露了指标（需要监控 token：X-Admin-Token 或 Bearer）
curl -s "$BASE_URL/metrics" -H "Authorization: Bearer $MONITORING_ADMIN_TOKEN" \
  | grep -E '^agent_run_dead_letter_total'

# 2) 多进程聚合是否开启 —— 0 表示 worker 的计数在抓取侧不可见，DLQ 告警不会触发
curl -s "$BASE_URL/metrics" -H "Authorization: Bearer $MONITORING_ADMIN_TOKEN" \
  | grep '^prometheus_multiprocess_enabled'
#   期望 1。0 = 两个容器没共享同一个 PROMETHEUS_MULTIPROC_DIR 目录
#   （compose 里应为同一个 prom-metrics 卷），或服务栈启动时没跑 metrics-init。

# 3) Prometheus 侧确实有这条时间序列（不是"抓到了 0"）
curl -s -G "$PROM_URL/api/v1/query" --data-urlencode 'query=agent_run_dead_letter_total'
```

> 若 `prometheus_exposition_metric_families` 明显偏小，说明大量指标模块根本没被
> import —— 暴露面是按「真的注册并输出」的 family 计数的，不是按代码里声明的数量。

1. **查看待处置队列**：

   ```bash
   curl -s "$BASE_URL/api/runs/dead" -H "Authorization: Bearer $TOKEN"
   ```

   或直接查表：

   ```sql
   SELECT run_id, thread_id, attempt_count, error_type, error_code, entered_at
   FROM agent_dead_letters ORDER BY entered_at DESC LIMIT 50;
   ```

2. **按 `error_type` / `error_code` / `attempt_count` / `entered_at` 分类**：
   - `timeout` / `transient` → 基础设施抖动，可重放；若反复出现，考虑提高
     `AGENT_RUN_MAX_ATTEMPTS`；
   - `permanent` → 代码/契约问题，**先修再重放**；
   - downstream 不可用（ERP / provider / Redis）→ 等下游恢复；
   - config defect / code defect → 修配置或代码。

3. **确认 side-effect / 幂等状态**：

   ```sql
   SELECT run_id, tool_name, operation_key, status, created_at, finished_at
   FROM tool_side_effects WHERE run_id = '<run_id>';
   ```

4. **重放**（仅在确认可重放后）：

   ```bash
   python scripts/replay_dead_run.py <run_id>          # 交互确认
   python scripts/replay_dead_run.py <run_id> --yes    # 非交互（CI / 自动化）
   ```

   重放**复用原 `run_id`**（只产生新的队列投递），原始 DLQ 历史不被改写。不要通过
   "新建一个 run"来重放——`operation_key = run_id:tool_call_id`（审批执行时为
   `run_id:approval:{approval_id}`）在任意次重试中恒定，正是它保证已成功的
   退款/改单**恰好一次**；换 `run_id` 会绕过这个幂等键，导致副作用重做。

**何时不要直接重放**：

- `permanent` 业务拒绝（如订单状态不允许）——重放只会再次失败；
- 外部副作用的真实状态未知（无法确认第一次是否已生效）；
- 配置仍然损坏（如缺少凭据 / 依赖不可达）；
- 下游仍然不可用；
- 无法建立幂等保证（`operation_key` 缺失或 ledger 状态不可信）。

在这些情况下，先修复根因或人工核对副作用，再决定是否重放。

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

### 3.7 Run 卡在 `WAITING_APPROVAL`

图挂在 human-in-the-loop 的 `interrupt()` 上等人工决策。**这不是故障，也不是失败**：
该状态是非终态，且刻意**不进**通用队列轮询（`EXECUTABLE_STATUSES = {QUEUED,
RETRYING}`），所以没有 worker 会反复捞起它。等待审批**不消耗** `attempt`。

```sql
-- 1) 该 run 在等谁的审批
SELECT approval_id, run_id, action, risk_level, status, requested_at, expires_at
FROM human_approvals WHERE run_id = '<run_id>' ORDER BY requested_at DESC;

-- 2) run 侧确认确实是非终态、且没有被错误地标成功
SELECT id, status, attempt, worker_id, lease_expires_at
FROM agent_runs WHERE id = '<run_id>';
```

处置顺序：

1. **先查审批人是谁**。`human_approvals.reviewer_id IS NULL` 表示还没人批；
   `status='EXPIRED'` 表示 TTL 到期按拒绝收敛（`HITL_APPROVAL_TTL_SECONDS`，默认
   3600s）——**过期不会被当作批准**，需要重新发起。
2. **审批 API 决策**（只有 admin / supervisor，且 `reviewer_id != user_id`）：

   ```bash
   curl -X POST "http://localhost:8000/api/approvals/<approval_id>/decision" \
     -H "Authorization: Bearer <JWT>" \
     -H "Content-Type: application/json" \
     -d '{"decision":"approve","reason":"值班确认"}'
   ```

   `decision` ∈ `approve` / `edit`（须带 `edited_args`）/ `reject`。重复提交是幂等
   的 no-op（返回 `newly_decided=false`），不会重复执行副作用。
3. **确认恢复投递**。响应里的 `resume_dispatched=false` 表示决策已记录但 dispatch
   失败；此时 run 会停在 `WAITING_APPROVAL`，需运维重投
   （`python3 scripts/replay_dead_run.py` 只处理 DLQ，审批场景按 §3.7.1 手工
   重投队列消息，**不要**改数据库状态）。
4. **兜底逃逸口**：`WAITING_APPROVAL → DEAD_LETTER` / `CANCELLED` 合法。审批被
   永久搁置（例如人离职）时用它收口，而不是让 run 无限期悬着。

> **边界**：`/api/chat` 实时快路径**不在**该治理边界内（无 run 上下文、
> 无 durable checkpoint）。快路径上的高风险工具不会产生审批记录——不要按
> "有审批保护" 假设部署。真实 ERP 写操作仍是 `NOT_VERIFIED`（验证走确定性
> staging 工具）。

### 3.7.1 审批已决策但 run 没恢复

```sql
SELECT run_id, action, status, reviewed_at, resumed_at
FROM human_approvals WHERE approval_id = '<approval_id>';
```

- `status='APPROVED'` 且 `resumed_at IS NULL` → 决策已记录、无 worker 消费。
  重投该 `run_id` 到队列即可；`consume_resume` 以 `WHERE resumed_at IS NULL`
  原子认领，重复投递安全。
- `resumed_at` 非空但 run 仍在 `WAITING_APPROVAL` → 恢复执行本身失败，查
  `agent_runs.error_code` 与 DLQ。
- `status='REJECTED'` / `'EXPIRED'` → 图已按拒绝恢复，**不会**产生副作用
  （`operation_key = run_id:approval:{approval_id}` 从未写入 ledger）。

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
| `prometheus_multiprocess_enabled` | **≠ 1 → 告警全部失明**：worker 写的 `agent_*` 指标在抓取侧恒为 0 |
| `prometheus_exposition_metric_families` | 骤降 → 指标模块未 import / REGISTRY 被换掉 |
| `tool_execution_timeout_total` | 突增 → 上游（ERP/外部 API）变慢，检查 `TOOL_EXECUTION_TIMEOUT_SECONDS` 是否偏紧 |

**label 纪律**：任何 `agent_*` 指标都不得带 `run_id` / `thread_id` / `user_id` /
`query`（高基数会打爆 Prometheus）。有测试断言这一点。

### 4.1 告警链自检（改了监控配置之后）

```bash
make metrics-exposure-check    # 契约：告警/Grafana 引用的每个指标名都真的可达
make alert-rules-test          # 官方 promtool：规则语法 + 真会 firing + 不 latching
make metrics-exposure-verify   # 真实 Prometheus 端到端（DLQ 告警实际进入 firing）
```

三条命令覆盖的是**不同**的失效面，缺一不可：

| 命令 | 抓什么 |
|---|---|
| `metrics-exposure-check` | 「注册了却没暴露」「引用了不存在的指标」「scrape job 没带凭据」 |
| `alert-rules-test` | 表达式语义：真的有 dead-letter 时会不会 firing；历史值会不会 latching |
| `metrics-exposure-verify` | 真实 Prometheus + 真实抓取 + **另一个进程**写入 → 跨进程聚合是否生效 |

`metrics-exposure-verify` 的证据落在
`artifacts/observability/metrics-exposure-<ts>/report.json`。它**不**验证
Alertmanager 的通知投递 —— 那是另一段链路，见 §5。

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
8. **审批通知不推送**：审批请求只在 `GET /api/approvals?status=PENDING` 里可见，
   没有主动 webhook / IM 通知；长时间无人处理会静默等到 TTL 过期。需要值班盯
   `agent_approval_requested_total` 与 `agent_approval_expired_total`。
9. **审批 SLA 未测量**：`agent_approval_wait_seconds` 的分布没有生产数据，
   `NOT_MEASURED`。
10. **DLQ 告警的通知投递未端到端验证**：指标可达与告警表达式在真实 Prometheus 上
    已验证（`make metrics-exposure-verify`，告警实际进入 `firing`），但
    **Alertmanager 的 webhook / SMTP 实际送达、整套 compose 栈上的端到端、
    Grafana 面板真实渲染均 NOT_VERIFIED**。值班仍需主动查 §3.3 的队列。
11. **生产集群未验证**：以上全部为本地 + CI 证据，`PRODUCTION NOT_VERIFIED`。
