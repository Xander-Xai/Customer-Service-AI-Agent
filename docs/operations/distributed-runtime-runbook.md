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
make runtime-delivery # Celery 投递语义真实验收（ACK / reject / requeue / 重新投递）
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
| `code_provenance.commit_sha` / `dirty` | **被测代码**是谁；`dirty=true` 时该 SHA 不代表被测代码 |
| `code_provenance.publication_status` | `REPRODUCIBLE` 才可作公开证据；`DIRTY_WORKTREE…` / `PROVENANCE_UNAVAILABLE` 只说明"跑过" |

---

## 3. 排障

### 3.1 Run 一直 `QUEUED`

```sql
SELECT status, attempt, next_retry_at, worker_id, error_code
FROM agent_runs WHERE id = '<run_id>';
```

- `next_retry_at` 非空 → 因 thread lease 竞争或退避被推迟，**等周期 reconciler**
  （不是等 broker——见下）。
- `error_code = 'DISPATCH_FAILED'` → 入队失败（broker 不可达）。
- 无 worker 在消费：`celery -A runtime.celery_app:celery_app inspect active`。
  生产要求 `AGENT_RUN_DISPATCH=celery`；`inline` 只用于开发（生产 fail-fast）。

> **broker 不会为普通失败重新投递。** 实测（`make runtime-delivery`，Celery 5.6.3
> + 真实 Redis broker）：普通 Exception、`RetryPublicationError`、软/硬时间上限
> 全部落在 `Request.on_failure` 的 `reject(requeue=False)`——**消息被丢弃**。
> 只有 `WorkerLostError`（worker 进程被杀）才重新投递。因此 run 停在
> `QUEUED`/`RETRYING` 时，队列里**可能一条消息都没有**，恢复靠 beat 周期扫描。
>
> 先确认 beat 活着（它是恢复闭环的驱动器）：
>
> ```bash
> docker compose ps beat            # 应当 Up (healthy)
> docker compose logs beat --tail 30 | grep 'reconcile-stuck-runs'
> ```
>
> `beat` 的 liveness 判据是 schedule 文件是否还在被写：文件存在且 mtime 在
> `max(120s, 4x AGENT_RUN_RECONCILE_INTERVAL_SECONDS)` 之内。beat 挂了 = 滞留 run
> 没有任何自动恢复手段，只能手工 `python3 scripts/reconcile_stuck_runs.py --apply`。

### 3.2 Run 卡在 `RUNNING`

worker 崩溃但消息还没回到队列，或 worker 仍在跑。

```sql
SELECT id, worker_id, task_id, lease_expires_at, heartbeat_at
FROM agent_runs WHERE status = 'RUNNING';
```

`heartbeat_at` 停止推进且 `lease_expires_at` 已过期 → 该 worker 已死。恢复有两条路：

- 进程被 SIGKILL（`WorkerLostError`）→ broker **立即**重新投递同一条消息
  （task_id 不变），新 worker 通过 `takeover_running` 原子接管并 `attempt+1`；
- 进程没死但不再推进（例如被 SIGSTOP、网络分区）→ broker 不会主动做任何事，
  周期 reconciler 扫到 lease 已过期的 `RUNNING` 后重新投递。

两条路都会消耗 attempt；预算耗尽时 `mark_running` **拒绝领取并落 DLQ**，不会
在预算外执行。

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
   "新建一个 run"来重放——`operation_key` 以 `run_id` 为分量（非 HITL 为内容寻址
   重放稳定键 `build_replay_stable_operation_key`；审批执行时为 `run_id:approval:{approval_id}`），
   因此在任意次重试中恒定，它让**本仓 ledger 不会
   再次发起**已成功的退款/改单；换 `run_id` 会绕过这个幂等键，导致副作用重做。

   > 历史缺陷（已修复，不再是当前设计）：旧文档写「`operation_key = run_id:tool_call_id`
   > 在任意次重试中恒定」。这**不成立**——`tool_call_id` 由 LLM 生成，节点重放时
   > 可能变化，导致同一笔逻辑写操作换键并被执行两次（真实复现见
   > `tests/integration/runtime/test_side_effect_replay_identity_pg.py`）。
   注意这不是外部系统的 exactly-once 保证：下游是否真的没扣第二次，取决于下游
   是否接受同一把幂等键（本仓**不提供、不宣称**端到端 exactly-once）。

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
   永久搁置（例如审批人长期不可达）时用它收口，而不是让 run 无限期悬着。

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
- **`resumed_at` 非空、`status='APPROVED'`、run 停在 `WAITING_APPROVAL`，且无
  error / 无 DLQ** → worker 曾在**认领决策之后、run 收敛之前**崩溃（Issue #14）。
  **现已自动收敛**（不再需要人工介入）：reconciler 经
  `RunService.list_resumable_waiting_approval_runs`（有终态审批且无未消费
  `PENDING`）重新投递；`_build_resume_command` 经 `ApprovalService.peek_resume`
  **只读重建**已消费的决策并继续执行。重复恢复由 `mark_resumed_running` 的 CAS 与
  ledger（`operation_key = run_id:approval:{approval_id}`）去重，**本仓不重复发起**该
  已批准写操作。**边界**：这是本仓的发起次数保证，外部系统端到端 exactly-once
  **NOT PROVIDED**（需下游接受同一把幂等键）。
  若 30s 内未收敛，按 §3.8 查 reconciler 是否停滞。
  - 修复前该状态是硬滞留（`consume_resume` 一次性认领会永远返回 `None`），需人工
    重投 / 重建审批；现保留该手工路径仅作为 reconciler 不可用时的兜底：
    §3.7 第 4 步经 `WAITING_APPROVAL → CANCELLED` 收口，或重建一条审批（新
    `approval_id` → 新 `operation_key`）。
  - 回归用例：`tests/integration/runtime/test_hitl_approval_crash_window.py`、
    `test_hitl_resume_fault_injection.py::TestCrashDuringResume`。
- `status='REJECTED'` / `'EXPIRED'` → 图已按拒绝恢复，**不会**产生副作用
  （`operation_key = run_id:approval:{approval_id}` 从未写入 ledger）。

---

### 3.8 恢复闭环停滞（`AgentReconcileStalled`）

**先分清两个信号**：beat 进程存活 ≠ reconciler 扫描成功。

beat 的容器健康检查只看 schedule 文件的 mtime，进程活着它就一直更新。因此
「beat 活着但每次扫描都失败」（数据库不可达、扫描抛错）时健康检查是**绿色**的，
而恢复闭环已经断了——滞留的 `RUNNING`/`QUEUED`/`PENDING` run 不会被重新投递，也不
会落 DLQ，会静默堆积。

```bash
# 1) 最近的扫描成功了吗？
curl -s "$METRICS_URL" | grep -E 'agent_reconcile_(last_success|scan_total)'

# 2) 扫描是否在失败？（beat 仍调度时）
curl -s "$METRICS_URL" | grep 'agent_reconcile_scan_total{outcome="error"}'

# 3) beat 存活与 schedule 文件是否推进
docker compose -f deploy/compose/docker-compose.yml ps beat
stat -c '%y %n' /tmp/celerybeat-schedule      # 容器内
```

判定与处置：

| 观察 | 结论 | 处置 |
|---|---|---|
| `last_success` 陈旧 + `outcome="error"` 增长 | beat 在调度，扫描在失败 | 查数据库可达性（`pg_isready` / 连接池）；修好后下一轮扫描自动恢复 |
| `last_success` 陈旧 + `outcome="error"` 不增长 | beat 没在调度 | `docker compose restart beat`；检查 `--schedule` 路径可写 |
| `last_success` 从未出现 | beat 刚部署或从未成功扫描过一次 | 确认 beat 配置与数据库；这一段窗口此前无监控，现由 `absent()` 分支覆盖 |
| `prometheus_multiprocess_enabled != 1` | **告警全部失明**（含本条） | 先修抓取侧，见 §4 表 |

手工触发一次扫描验证闭环是否恢复：

```bash
python3 scripts/reconcile_stuck_runs.py --dry-run
```

恢复后确认滞留 run 被收敛：`agent_run_stale_reclaimed_total` /
`agent_run_stale_dead_letter_total` 应出现增长。

**未覆盖的失效面**：本条告警验证的是「表达式在 promtool 下真的会 firing」。真实
Alertmanager 的**通知送达**是另一段链路（见 §5），本仓库不声称已端到端验证。

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
| `agent_reconcile_last_success_timestamp_seconds` | `time()` 减去它 > 360s → 恢复闭环停滞（见 §3.8），**与 beat 存活正交** |
| `agent_reconcile_scan_total{outcome="error"}` | 突增 → 扫描在失败但 beat 仍调度，最常见是数据库不可达 |

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

0. **broker 不为普通失败重新投递**（实测，`make runtime-delivery`）：普通异常 /
   `RetryPublicationError` / 软硬时间上限 = `reject(requeue=False)` = 消息丢弃。
   能重投的只有 `WorkerLostError`，且**立即**重投、无退避。因此恢复完全依赖
   数据库 AgentRun + beat 周期 reconciler；`beat` 停摆 = 没有自动恢复。
   另：裸 `Reject(requeue=True)` 同样立即重投（实测 5 秒 80 次），不能当策略。
1. **无 fencing token**，只有协作式取消：失去 lease 的 worker 会被**取消图执行**（`_FenceGuard`）并在写外部副作用前复核 ownership（`ToolRegistry` 的 execution fence），但取消之前已发出的外部调用无法回收；卡在同步 C 调用里的执行也不可取消，由 `AGENT_RUN_TASK_TIME_LIMIT` 兜底。lease/heartbeat/visibility/lock/claim 的交叉关系已在启动时强制（`validate_timeout_invariants`），不是靠文档约定。
2. 单 Redis 互斥，不是 Redlock；Redis 故障转移期间可能双持有。
3. 认领后崩溃的副作用会被重放（见 §3.5）。认领租约在执行期间会续租（`_ClaimRenewer`），且 claim TTL 必须覆盖工具执行上界 + 余量（启动时强制），因此「工具仍在执行、认领先过期」不再是可达状态；旧执行者写不进结果时会抛「结果未知」而不是静默返回成功。
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
