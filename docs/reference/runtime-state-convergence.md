# AgentRun 状态收敛契约（canonical）

本文件是**状态收敛规则**的唯一真相源：每一种「非终态 + 某个外部信号」的组合，
系统必须收敛到哪个状态、由哪条数据库谓词决定、以及实际检测延迟是多少。

代码位置：`runtime/statuses.py`（合法迁移表）、`runtime/repository.py`（原子谓词）、
`runtime/run_service.py`（服务层编排）、`runtime/retry.py`（周期扫描器）。

**核心原则：所有「能否改状态」的判定都在数据库层，同一条 `UPDATE` 的 `WHERE` 里。**
任何「Python 先 `SELECT` 看到某个值、再据此决定写什么」的写法都是竞态来源，不是
保护。本文件中的每条规则都可以在
`tests/integration/runtime/test_duplicate_claim_lease_fencing_pg.py` 与
`test_pending_enqueue_contract_pg.py` 里找到对应的真实 PostgreSQL 用例。

这条原则**同样适用于 `tool_side_effects` 幂等 ledger**（见 2.7），它曾经是唯一的
例外——`mark_succeeded` / `mark_failed` 过去是「SELECT 判归属 + 改 ORM 对象 +
commit」，而 `ToolSideEffect` 没有 `version_id_col`，真实提交的 SQL 只按主键。

---

## 1. 统一状态模型

下图是**唯一**的状态模型。下文各节只展开其中一条边或一个非终态的收敛规则，不再
各自重画状态机。

```text
PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED        (终态)
               ▲          │      ├──► FAILED           (终态，permanent error)
               │          │      ├──► RETRYING ──┐
               │          │      │               │
               │          │      │        (回到 QUEUED 侧重新领取)
               │          │      ├──► WAITING_APPROVAL ──► RUNNING (非终态，人工审批)
               │          │      └──► DEAD_LETTER      (终态，retry 用尽)
               │          └──► CANCELLED              (终态，协作式)
               └── 人工重放（唯一自动路径之外的出口：DEAD_LETTER -> QUEUED）
```

终态集合：`SUCCEEDED | FAILED | DEAD_LETTER | CANCELLED`。合法迁移的机器可读真相源
是 `runtime/statuses.py::ALLOWED_TRANSITIONS`（本图是它的人类可读投影）。

`WAITING_APPROVAL` 是 human-in-the-loop 专用的**非终态**：它不在
`EXECUTABLE_STATUSES` 里（通用轮询不捞起，否则忙循环），且它回到 `RUNNING` 时
**不递增 `attempt`**（等人不是失败，不该消耗重试预算）。它的收敛策略见 2.5。

## 2. 状态收敛矩阵

`attempt` 是**已开始的执行次数**（在领取时 `attempt + 1`）。`attempt == max_attempts`
是**最后一次正常执行的常态**，不是异常状态——这是本文件最重要的前提，下面多条规则
都建立在它之上。

### 2.1 RUNNING（有人正在执行）

| lease | 预算 | 触发者 | 收敛结果 | 判定谓词（同一条 UPDATE 内） |
|---|---|---|---|---|
| **有效** | 已耗尽 `attempt >= max` | 重复投递 | **保持 RUNNING，不改任何字段** | `takeover_running` 因 `lease_expires_at > now` 全部子句为假而 `rowcount=0`；DLQ 因 `lease_expires_at <= now` 为假而 `rowcount=0` |
| **有效** | 未耗尽 | 重复投递 | **保持 RUNNING，不改任何字段** | 同上 |
| **过期** | 已耗尽 `attempt >= max` | 重复投递 / reconciler | **原子转入 DEAD_LETTER** | `dead_letter_stale_running`：`status=RUNNING AND lease_exp IS NOT NULL AND lease_exp <= now AND attempt >= max_attempts` |
| **过期** | 未耗尽 `attempt < max` | reconciler | **原子接管**（`attempt + 1`、写新 owner 与 lease） | `takeover_running`：`status=RUNNING AND attempt < max_attempts AND (worker_id=新 OR worker_id IS NULL OR lease_exp IS NULL OR lease_exp <= now)` |

要点：

- **重复投递永远不能杀死正在执行的 run。** 前两行是本文件的硬约束：`attempt ==
  max_attempts` 时任何重复投递都只得到 `None`（不执行、不改状态、无 DLQ 行），
  原 owner 的 `mark_succeeded(expected_worker_id=...)` 仍能正常提交。
- 扫描与写入之间可能隔任意长时间，其间原 owner 可能刚刚续上 lease、或被另一个
  worker 接管。因为 lease 谓词在写入的同一条 SQL 里，这两种情况都会让写入落空，
  变成 benign no-op（`rowcount == 0`）。**不存在 TOCTOU 窗口。**
- 并发接管者中恰好一个 `rowcount == 1`：赢家写入即把 lease 变成未来时刻，
  其它竞争者的谓词立刻全部为假。

### 2.2 QUEUED / RETRYING（已入队、未开始执行）

| 预算 | 触发者 | 收敛结果 | 判定谓词 |
|---|---|---|---|
| `attempt < max` | worker 领取 | **原子领取** `RUNNING`（`attempt + 1`） | `claim_executable`：`status IN (QUEUED,RETRYING) AND attempt < max_attempts`，`attempt + 1` 在 SQL 内 |
| `attempt >= max` | worker 领取 / reconciler | **原子转入 DEAD_LETTER** | `dead_letter_exhausted_executable`：`status IN (QUEUED,RETRYING) AND attempt >= max_attempts` |

要点：

- **重复投递不消耗预算。** `attempt + 1` 只发生在赢得 CAS 的那条 `UPDATE` 里；
  落败的领取返回 `None`，`attempt` 不变。这让 at-least-once 投递在预算上是无害的。
- **落败的领取是良性 no-op，不是错误。** 如果竞争者刚把 run 领取成 `RUNNING`，
  本次领取返回 `None`（绝不抛 `RUNNING -> RUNNING` 的 `InvalidRunTransition`——
  那不是非法迁移，只是基于过期快照的比较）。
- 预算耗尽必须**终态**收敛：`list_recoverable_runs` 不比较 `max_attempts`，一个停在
  `QUEUED`/`RETRYING` 且预算耗尽的 run 会被它反复重新投递，形成无界重投循环。

### 2.3 PENDING（已落库、投递结果未知）

| 情况 | 收敛结果 | 判定谓词 |
|---|---|---|
| 落库未超过宽限期（默认 60s） | **不动**，不投递 | `list_stale_pending_runs`：`status=PENDING AND updated_at <= now - grace` |
| 陈旧 PENDING | **条件迁移到 QUEUED（见第 1 节统一模型的第一条边），然后投递** | `promote_pending_to_queued`：`status = PENDING`（单条 UPDATE 的 rowcount 决定谁有权发消息） |
| 陈旧 PENDING 已被取消 / 已 `mark_queued` / 已终态 | **不投递**（谓词不成立） | 同上 |
| 并发 reconciler 同时扫到 | **只有一个迁移成功、只有一个投递** | 同上 |
| 迁移成功但投递失败 | 停在 `QUEUED` + `next_retry_at IS NULL` → QUEUED 孤儿分支兜底 | 见 1.4 |

要点：

- **恢复不是无脑重投。** 直接对 `PENDING` 重投无法区分「publish 之前被打断」（必须
  补投）与「publish 成功但 `mark_queued` 未提交」（补投多余），所以必须先做**条件
  迁移**，让数据库裁决「谁有权发这条消息」，迁移失败即不发。
- 宽限期不可省：没有它就会对「消息正在投递途中」的 run 立刻补投。
- C2（publish 成功、`mark_queued` 未提交）的恢复会产生一次**多余投递**，这是
  at-least-once 入队的固有性质；正确性由 1.2 的领取 CAS 保证（恰好一个执行者）。

### 2.4 QUEUED + `next_retry_at IS NULL`（已入队确认、消息可能已丢）

| 情况 | 收敛结果 | 判定谓词 |
|---|---|---|
| `created_at` 未超过宽限期（默认 60s） | 不动 | `list_recoverable_runs` 的孤儿分支年龄下限 |
| 已超过宽限期 | **重新投递**（不强制改状态） | 同上 |
| 投递失败 | 下一轮扫描再试（不推进 `attempt`） | — |

这是 `POST /api/runs` 现行单阶段路径（先建 `QUEUED` 再 publish）的恢复机制，也是
1.3 最后一行「投递失败后的最终收敛」所依赖的分支。两条路径合起来保证：**不存在
永久搁浅的 `PENDING` / `QUEUED`。**

### 2.5 WAITING_APPROVAL（人工审批）

**保持既有保守策略，本轮不变更。** 不自动重发：审批已被 claim、但 worker 随后崩溃的
窗口里，审批载荷可能已被消费而副作用尚未提交，自动重发无法证明「不会重复执行
副作用」。保留人工 Runbook + 审批 TTL 到期按拒绝处理。合法迁移集合以
`runtime/statuses.py::ALLOWED_TRANSITIONS` 为准（`→ RUNNING` 不递增 `attempt`）。

### 2.6 CANCELLED / 终态

终态不可再迁出（唯一例外是人工重放的 `DEAD_LETTER -> QUEUED`，见
`RunService.requeue_dead_letter`；**没有任何自动路径**会做这件事）。

取消用**单条** `UPDATE ... WHERE status NOT IN (终态)` 判定：先读状态再按该状态做
CAS 会与「promote / 领取」竞争出假失败（读到 `PENDING`、写入时已变 `QUEUED`），
而取消对每个非终态都合法，谓词直接写成「不是终态」即可，不需要预先知道当前状态。

### 2.7 副作用 ledger（`tool_side_effects`）：PENDING → SUCCEEDED | FAILED

同一笔写操作可以被两个执行者**同时**持有过认领（租约过期后被接管），因此这里的
核心不变量是：**只有当前且未过期的认领者能提交终态，且终态不可被迟到写入改写。**

| 情况 | 收敛结果 | 判定谓词（同一条 UPDATE 内，`_commit_terminal_state`） |
|---|---|---|
| 当前 owner、租约未过期 | **原子提交** SUCCEEDED / FAILED | `tool_name=? AND operation_key=? AND claim_owner=? AND status='PENDING' AND claim_expires_at > now [AND request_fingerprint=?]` |
| 旧 owner（已被接管，接管者租约**未过期**） | **拒写**（`rowcount=0`），接管者的 PENDING 不受影响 | `claim_owner` 不匹配 |
| 旧 owner 试图把接管者的 SUCCEEDED 改成 FAILED | **拒写** | `claim_owner` 不匹配（**且** `status` 谓词独立兜底） |
| 已 SUCCEEDED / FAILED 的行收到迟到回调 | **拒写**，终态不可改写 | `status='PENDING'` 不成立 |
| 租约已过期但**尚未被接管** | **拒写**，不能靠一次迟到提交复活执行权 | `claim_expires_at > now` 不成立 |
| `owner` 缺失 | **fail-closed 抛 `PermanentError`**，不做无归属写入 | 调用契约违反，不是 SQL 谓词 |

要点：

- **租约未过期谓词挡不住旧 owner。** 接管者的租约是**未来时刻**，所以
  `claim_expires_at > now` 对旧 owner 依然为真；真正拦住它的只有 `claim_owner`。
  这正是「读到的 owner」与「写入的行」之间必须同一条 SQL 的原因。
- **`claim_owner` 就是 epoch。** 每次接管都原子改写它，因此不需要额外的 version 列：
  `updated_at` 不能当 version（`renew_claim` 会更新它而不换 owner，正常续租会误判
  冲突）。`request_fingerprint` 作为可选的纵深防御随 `execute_idempotent_operation`
  始终传入（`claim()` 接管时会改写它，它始终代表当前 owner 的参数集）。
- **提交失败不得上报成功。** 外部写已经落地、ledger 却写不进结果时，
  `commit_result_or_raise` 抛 `SideEffectOwnershipLost`（transient），
  最终落 DLQ 人工核对，绝不返回「成功」。
- **缺失 owner 不等于「管理员模式」。** 过去 `owner` 是可选参数、传 `None` 会跳过
  归属谓词；现在 fail-closed。

真实 PostgreSQL + 多连接 + 受控并发屏障的行为级用例（8 个场景全部覆盖）：
`tests/integration/runtime/test_side_effect_commit_fencing_pg.py`。

---

## 3. DLQ 转移的原子性与剩余限制

### 2.1 原子性保证

`agent_runs` 的状态迁移与 `agent_dead_letters` 的记录写入在**同一个事务**里完成
（`AgentRunRepository._dead_letter_conditionally`）。

这一点是必需的，不是优化。分两次提交会留下一个**不可恢复**的窗口：状态已是终态
（不再被任何扫描器捞起）而 DLQ 行缺失，于是这个 run 既不会重投、也没有记录供人工
`scripts/replay_dead_run.py` 重放——它变成静默消失。行为级证据见
`test_dead_letter_transition_and_dlq_row_are_atomic`（注入 DLQ 写入故障，断言状态
迁移被**回滚**）。

`agent_dead_letters` 行**不可变**：重复补写返回既有行，保留首次进入 DLQ 时的
`attempt_count` / `error_code` / `entered_at`（重放后再次进入 DLQ 不覆盖历史证据）。

### 2.2 剩余限制（诚实边界）

- **不是 broker-native DLX。** 没有 RabbitMQ/Redis 死信交换机，也没有自动重投队列；
  闭环是「数据库 AgentRun 状态机 + 周期扫描器 + 人工重放脚本」。
- **不覆盖 `WAITING_APPROVAL`。** 见 2.5。
- **人工重放是唯一出口。** `DEAD_LETTER` 没有自动出口；`requeue_dead_letter` 复用原
  `run_id`。
- **`operation_key` 的稳定性来自键的构造，不是来自 `run_id` 恒定。**
  - **HITL**：`operation_key = run_id:approval:{approval_id}`（`core/hitl/gate.py`）。
    审批 id 恒定，因此任意次重放中键恒定。**保持不变。**
  - **非 HITL**：**重放稳定键** `build_replay_stable_operation_key`
    （`run_id + node_id + agent + tool_name + ordinal` 的 SHA-256，版本前缀 `v1`）。
    `run_id` 只是其中一个分量；它恒定**不足以**让键恒定。
  - **历史缺陷（已修复，不再是当前设计）**：旧文档与旧实现推荐
    `run_id:tool_call_id` 作为幂等键。其中 `tool_call_id` **由 LLM 生成**，
    节点在 checkpoint 提交前崩溃并被重放时 LLM 会被重新调用、可能产生不同的 id，
    于是同一笔逻辑写操作拿到新键、唯一约束看不到重复、**副作用执行两次**（真实复现
    见 `tests/integration/runtime/test_side_effect_replay_identity_pg.py`）。
    `build_tool_idempotency_key` 现在**只**用于 `tool_call_id` 本身稳定的场景。
- **DLQ 落终态后不再被扫描。** 这是有意的（终态收敛才能终止无界重投），代价是
  恢复完全依赖人工——所以 DLQ 写入与状态迁移必须同事务，否则会静默丢失。
- **外部系统端到端 exactly-once：`NOT PROVIDED`。** ledger 只保证本仓不重复发起，
  下游需接受同一把幂等键。

---

## 4. 实际恢复延迟与配置关系

**`AGENT_RUN_RECONCILE_INTERVAL_SECONDS`（默认 30s）是扫描间隔，不是恢复时间。**
真实延迟 = 触发条件（过期 / 宽限期）到被扫到之间的等待 + 执行时间。

| 故障形态 | 触发条件 | 实际检测延迟（下界） | 依据的配置 |
|---|---|---|---|
| `QUEUED`/`RETRYING` 退避到点但消息丢失 | `next_retry_at <= now` | 0 ～ 一个扫描间隔 | `AGENT_RUN_RECONCILE_INTERVAL_SECONDS`（30s） |
| `QUEUED` 单阶段路径 publish 前/后崩溃 | `created_at <= now - 60s` | **60s 宽限期** + 0 ～ 30s 扫描间隔 → **60 ～ 90s** | `orphan_grace_seconds=60` + `RECONCILE_INTERVAL` |
| `PENDING` 两阶段入队被打断 | `updated_at <= now - 60s` | **60 ～ 90s** | `pending_grace_seconds`（默认 60）+ `RECONCILE_INTERVAL` |
| `RUNNING` worker 崩溃（lease 过期） | `lease_expires_at <= now` | **lease TTL**（`AGENT_RUN_LEASE_SECONDS`，默认 180s）+ 0 ～ 30s | `AGENT_RUN_LEASE_SECONDS` + `RECONCILE_INTERVAL` |
| `RUNNING` 被误判（心跳抖动） | 同上 | 同上 | 同上 |

关键点：

- **lease 过期才是 `RUNNING` 的故障判定信号，不是心跳失败。** 心跳持续续租的 run
  永远不会被回收；心跳只是让 lease 不过期。
- **`orphan_grace_seconds=60` 与扫描间隔 30s 的关系**：60s 宽限期意味着刚入队的
  `QUEUED`/`PENDING` 在前 60s 内不会被补投（避免对投递途中的消息重复投递）；之后
  最坏再等一个扫描间隔。所以「60s 宽限期 + 30s 扫描」给出的实际检测延迟是
  **60 ～ 90s**，把它写成「30 秒恢复」是错的。
- **`RUNNING` 恢复的延迟由 lease 主导**（默认 180s），与扫描间隔无关。把
  `AGENT_RUN_LEASE_SECONDS` 调小会缩短崩溃恢复延迟，但更容易因 GC / 事件循环饿死
  造成**误判过期**——而误判的直接后果是新 worker 接管并从 checkpoint 续跑
  （工具幂等 ledger 兜底副作用），不是数据损坏。

### 4.1 超时交叉配置（可执行不变量表）

下表是 `core/config.py::validate_timeout_invariants` 的**可执行镜像**
（`tests/unit/test_timeout_invariants.py` 同时钉住两者）。该校验在**所有环境**
强制、且**早于 `DEV_MODE` 早退**：这些关系错了会在运行时产生不确定的重复执行或锁
丢失，与「是不是生产」无关。

**致命约束**（不满足 -> 拒绝启动）

| 约束 | 出厂默认 | 违反后果 |
|---|---|---|
| `AGENT_RUN_HEARTBEAT_SECONDS < AGENT_RUN_LEASE_SECONDS` | 30s < 180s | 续租永远追不上过期，lease 恒定失效，任何执行都被判为失去所有权 |
| `AGENT_RUN_LEASE_SECONDS >= 2 * AGENT_RUN_HEARTBEAT_SECONDS` | 180s >= 60s | 只有一次续租机会，无法证明长执行不会因单次抖动丢租约 |
| `TOOL_EXECUTION_TIMEOUT_SECONDS < AGENT_RUN_TASK_TIME_LIMIT` | 30s < 180s | 工具超时的局部降级来不及发生，退化为整体失败 |
| `AGENT_RUN_TASK_TIME_LIMIT < AGENT_RUN_VISIBILITY_TIMEOUT` | 180s < 3600s | broker 未 ACK 可见性时间覆盖不住执行时长：任务还在跑、消息已重投 -> 同一 run 并发执行 |
| `claim TTL >= TOOL_EXECUTION_TIMEOUT_SECONDS + margin(30s)` | 60s >= 60s | 执行期间认领过期 -> 第二个执行者拿到执行权并重复发起同一笔写 |
| `AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT + margin(30s)` | 300s > 210s | 任务仍在执行时锁已过期，同 thread 的另一个 worker 可进入同一 state lineage |

`LOCK_TTL` 一行在 `AGENT_RUN_THREAD_LOCK_ENABLED=false` 时不适用；任何一行为 `<= 0`
视为**显式关闭**，该维度不参与校验（关掉旋钮不该报一堆无关错误）。

**刻意不是致命约束**（只影响恢复延迟 / 运维偏好，只警告）

| 项 | 理由 |
|---|---|
| `LEASE > TASK_TIME_LIMIT` | 有正常续租时这不是正确性问题。一刀切要求会让「lease 60s + task 180s + heartbeat 20s」这种正常配置被判为非法。可证明条件是 `LEASE >= 2 * HEARTBEAT`。只有**心跳停用**时它才升级为建议（`timeout_invariant_recommendations`） |
| `RECONCILE_INTERVAL` 与 `LEASE` 的相对大小 | 只改变滞留 run 的检测延迟（见 4.2），不影响正确性 |
| `RECONCILE_INTERVAL >= 1s` | 防忙循环，由调度侧下限保证，不属于重复执行风险 |

上一版文档曾把「lease/heartbeat/visibility 没有硬校验」列为已知缺口（P1）；该缺口
已由 `validate_timeout_invariants` 关闭。

### 5.2 Beat 存活 ≠ Reconciler 成功执行

这是两个**正交**的信号，必须分开看：

| 信号 | 覆盖什么 | 覆盖不到什么 |
|---|---|---|
| beat 容器健康检查（`/tmp/celerybeat-schedule` mtime < `max(120, 4×interval)`） | beat 进程活着、schedule 文件在被更新 | 扫描任务本身是否成功执行 |
| `agent_reconcile_scan_total{outcome}` | 每次扫描的执行结果（`success` / `error`） | 扫描跑了但一条 run 都没收敛 |
| `agent_reconcile_last_success_timestamp_seconds` | 最近一次**成功**扫描的 Unix 时间戳 | — |
| `agent_run_stale_reclaimed_total` / `agent_run_stale_dead_letter_total` | 实际发生了收敛动作 | 无可收敛对象时的沉默 |

只监控 beat 存活是不够的：beat 活着但每次扫描都抛错（例如数据库不可达）时，
schedule 文件照样更新、健康检查照样是绿的，而恢复闭环已经断了。
`agent_reconcile_last_success_timestamp_seconds` 才是「恢复闭环还在工作」的直接证据。

**告警已接入**（`monitoring/alert_rules.yml`，并由 `monitoring/alert_rules_test.yml` +
`promtool test rules` 钉住行为）：

| 告警 | 表达式要点 | 覆盖的失效形态 |
|---|---|---|
| `AgentReconcileStalled`（critical，`for: 5m`） | `time() - agent_reconcile_last_success_timestamp_seconds > 360`，或 `absent(...)` 且实例存活 | 「beat 根本没在调度」与「调度了但扫描一直失败」**同时**覆盖（用时间戳而非失败计数，失败计数在未调度时恒为 0） |
| `AgentReconcileScanFailing`（warning，`for: 5m`） | `increase(agent_reconcile_scan_total{outcome="error"}[10m]) > 0` | beat 仍在调度但扫描本身持续失败（最常见：数据库不可达） |

两者互补：**停滞看时间，失败看原因分类**；`increase(...) > 0` 保证只在窗口内有失败时
报警，不会因历史一次失败永久 latching。阈值 360s ≈ 扫描间隔（30s）的 12 倍。

> 历史缺陷（已修复）：本节曾写「这两个指标已实现并暴露，但**尚未接入告警规则**」。
> 该描述在告警接入后即失效，现已按 `monitoring/alert_rules.yml` 的现行规则更新。

### 5.3 Queue backlog 与批量扫描饥饿

扫描器按 `created_at ASC` 取前 `limit` 条（默认 100）。因此：

- **队头饥饿**：若最老的一批 run 持续投递失败（例如 broker 局部故障），它们会一直
  占据扫描窗口，靠后的新 run 得不到处理。
- 缓解：单条投递失败不会中断本轮其余 run（`continue`），下一轮仍从队头开始，所以
  **只要投递恢复，全部 backlog 会被逐轮清空**——饥饿是暂时的，不是永久丢失。
- 重复投递的数量随 backlog 上升，但**不消耗预算**（见 2.2），因此不影响正确性，
  只影响无效工作量。
- 未实现：按失败次数退避、或对 `next_retry_at` 重置以把失败项移出队头。

---

## 6. 相关文档

- 状态机与整体设计：[distributed-agent-runtime.md](../../docs/design/distributed-agent-runtime.md)
- 状态归属：[runtime-state-ownership.md](../../docs/design/runtime-state-ownership.md)
- 运维手册：[../operations/distributed-runtime-runbook.md](../../docs/operations/distributed-runtime-runbook.md)
- 人工审批与副作用幂等：[human-in-the-loop.md](../../docs/design/human-in-the-loop.md)
- 投递语义实测：[../operations/e2e-verification-guide.md](../../docs/operations/e2e-verification-guide.md)

**证据分级**：本文件描述的收敛规则全部有**真实 PostgreSQL** 行为级用例覆盖
（`tests/integration/runtime/test_duplicate_claim_lease_fencing_pg.py`、
`test_pending_enqueue_contract_pg.py`）。但真实生产集群 / 多副本长期运行 / 真实用户
流量仍未验证，属 `NOT_VERIFIED`；本文件不构成生产验证结论。
