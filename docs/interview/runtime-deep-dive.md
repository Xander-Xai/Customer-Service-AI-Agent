# Runtime Deep Dive — 分布式 Agent Runtime

> 每个概念按 **是什么 / 为什么需要 / 仓库怎么实现 / 对应文件 / 失败场景 / 设计 trade-off**
> 六段展开。证据等级见 [production-evidence.md](../evaluation/production-evidence.md)。
> 交叉引用：[source-map.md](source-map.md)。

---

## 0. 全景

```
POST /api/runs
   │
   ├─ runtime/run_service.py::create_run   → agent_runs 表 (QUEUED)   ← 真相源
   │
   └─ runtime/dispatch.py::dispatch_run     → Celery (Redis broker)
                                              │
                          ┌───────────────────┴───────────────────┐
                          │  worker (Celery prefork)              │
                          │  runtime/tasks.py::execute_agent_run │
                          │        │                             │
                          │        ├─ runtime/repository.py::mark_running
                          │        │     └─ 原子领取 + lease；别人持有则跳过
                          │        │
                          │        ├─ runtime/thread_lock.py::acquire
                          │        │     └─ agent:thread-lock:{thread_id}
                          │        │        owner token + TTL + Lua CAS
                          │        │
                          │        ├─ runtime/bootstrap.py::invoke_graph_with_resume
                          │        │     └─ ainvoke(None) → 从 checkpoint 续跑
                          │        │           │
                          │        │           └─ LangGraph super-step
                          │        │                └─ core/checkpointer.py
                          │        │                     └─ AsyncPostgresSaver
                          │        │                          └─ checkpoints 表
                          │        │
                          │        ├─ tools/tool_registry.py
                          │        │     └─ runtime/side_effects.py::claim  ← 幂等
                          │        │
                          │        └─ runtime/run_service.py::mark_*  → agent_runs
                          │
                          └─ 崩溃 → 未 ACK → visibility_timeout → 重投 → 新 worker 接管

观测旁路（不是真相源）：runtime/events.py → Redis Stream agent:run:{id}:events
                                              → GET /api/runs/{id}/events → SSE
```

---

## 1. Celery task lifecycle

**是什么**：一次 `execute_agent_run` 任务从 broker 被取出、执行、ACK 的全过程。

**为什么需要**：把"谁执行这次 run"从 HTTP 进程里解耦出来。快路径不该在用户请求线程里
跑 ERP 全量分页。

**怎么实现**（`runtime/celery_app.py`）：

| 配置 | 值 | 为什么 |
|---|---|---|
| `task_acks_late` | `True` | 任务**完成后**才 ACK。崩溃则未 ACK → 重投 |
| `task_reject_on_worker_lost` | `True` | worker 进程丢失时拒绝消息而非 ACK |
| `task_acks_on_failure_or_timeout` | **`False`** | 关键：Celery 默认 `True`（失败即 ACK）。保持默认会让"让异常逃逸触发 redelivery"失效，run 停在 RETRYING 等一条永不到的消息 |
| `worker_prefetch_multiplier` | `1` | 崩溃时最多丢一条任务，不会丢一整个 prefetch 窗口 |
| `broker_transport_options` | `{"visibility_timeout": AGENT_RUN_VISIBILITY_TIMEOUT}` | 未 ACK 消息重新可见时间 |
| `task_ignore_result` | `True` | **result backend 不是真相源** |
| `task_default_queue` | `AGENT_RUN_QUEUE` | 单队列 |

**失败场景**：若 `task_acks_on_failure_or_timeout=True`，worker 抛异常 → Celery ACK 并把
任务标记 FAILURE → run 永远停在 RETRYING/QUEUED。无人重试，无 DLQ，无告警。

**trade-off**：`acks_late` + 短 `visibility_timeout` 意味着**可能重复投递**。这是刻意的
（见 §3、§8）。生产默认值 `AGENT_RUN_VISIBILITY_TIMEOUT=3600` 必须大于任务时间上限，
否则运行中的任务会被重复投递 —— 这条由 `core/config.py::validate_distributed_runtime_settings`
在生产启动时 **fail-fast** 校验。

---

## 2. ACK 与 visibility timeout

**是什么**：Redis broker 的 kombu 实现里，消息被取出时写入全局 hash `unacked`，
并加进 zset `unacked_index`。若在 `visibility_timeout` 内没有 ACK，broker 视为投递失败，
重新放回队列。

**为什么需要**：worker 崩溃时没有任何代码能运行，也就没法 ACK。visibility timeout 是
"重新投递"的唯一触发器。

**怎么实现**：见 §1。`runtime/dispatch.py::dispatch_run` 用 `apply_async(countdown=...)`
做退避投递。

**失败场景（真实踩过）**：`unacked` / `unacked_index` 是 **broker 全局的，不是 per-queue**。
仓库里有 4 处测试会 `DELETE` 这两个 key（`test_worker_checkpoint_recovery.py`、
`test_tool_idempotency.py`、`test_queue_worker_decoupling.py`、`scripts/test_worker_crash_recovery.py`）。
**并发运行这些用例会互相摧毁在途 delivery**，产生与代码无关的失败。
因此这些用例不可并发执行，串行化由 pytest 默认行为保证（无 xdist）。

**trade-off**：visibility_timeout 调小 → 崩溃恢复更快，但重复投递窗口变大 → 幂等层压力更大；
调大 → 恢复慢，超过任务时限会造成运行中任务被重投。默认值必须匹配任务时限，这是**配置契约**
而不是性能调优：生产校验强制 `AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT + 30`。

---

## 3. at-least-once 与为什么不是 exactly-once

**是什么**：at-least-once = 消息**至少**被投递一次，因此**可能**被处理多次。
exactly-once 在跨系统边界上做不到。

**为什么需要**：崩溃恢复的前提就是"未完成的工作会被重新尝试"。如果 broker 只投递一次，
worker 崩溃后这次工作就永久丢失。

**仓库怎么实现**：见 §1 的 `acks_late` + `reject_on_worker_lost` + visibility_timeout。

**为什么做不到 exactly-once**：exactly-once 投递需要 broker 与业务之间有原子提交协议，
而副作用发生在**外部系统**（ERP）。经典例子：向 ERP 提交退款成功后进程崩溃，此时既没有
ACK 也没有"完成"记录，重投就会退两次。要真正 exactly-once，唯一途径是让所有参与方加入
同一个分布式事务——代价是性能和可用性（见 [failure-and-tradeoffs.md](failure-and-tradeoffs.md) §2）。

**trade-off**：接受重复投递，把幂等责任显式下推到业务层。**这是把不可能的事换成可测的事**。

**明确不宣称**：`Exactly-once` / `零重复` 这类表述在本仓库任何材料中都不允许出现。

---

## 4. Retry（应用层重试）

**是什么**：与 broker redelivery **不同的**机制。

| | broker redelivery | 应用层 retry |
|---|---|---|
| 触发 | worker 崩溃 / 未 ACK | 业务异常被分类为 transient |
| 作用域 | 消息级 | run 级 |
| 记账 | broker 的 `unacked` | `agent_runs.attempt` / `next_retry_at` |
| 上限 | visibility_timeout 反复重投 | `AGENT_RUN_MAX_ATTEMPTS` |

**为什么需要两者都要**：redelivery 只能覆盖"进程死了"，覆盖不了"进程活着但业务失败"
（ERP 超时、限流、依赖抖动）。

**怎么实现**：`runtime/errors.py::classify_exception` 把异常分成 transient / permanent。
`runtime/executor.py::_handle_failure`：

- transient → `RETRYING` + `next_retry_at`（退避）→ 到期后重新投递 → attempt+1
- permanent → 直接 `FAILED`（**不重试**）
- attempt 用尽 → 写 `agent_dead_letters` + `DEAD_LETTER`

退避算法在 `runtime/retry.py`。

**失败场景**：把 permanent 错误当 transient → 无意义的重试 + DLQ 里全是无解任务，
真正需要人工处理的信号被淹没。反之把 transient 当 permanent → 可恢复的抖动被当成
永久失败。

**trade-off**：分类是**启发式**的（按异常类型 + 状态码），无法覆盖所有情况。
因此 DLQ + 人工重放是必需的第二道，而不是"应该不会走到"。

---

## 5. DLQ（dead letter）与人工重放

**是什么**：`agent_dead_letters` 表记录重试耗尽的 run。

**为什么需要**：自动重试耗尽后必须有终点，否则 run 永久占用资源且无人知晓。
DLQ 是"需要人看一眼"的队列。

**怎么实现**：`scripts/replay_dead_run.py`。

**关键设计 —— 复用原 run_id**：重放**必须**用原 `run_id`，不能新建。原因：
工具幂等键是 `operation_key = run_id:tool_call_id`
（`runtime/side_effects.py::default_operation_key`）。换 run_id 等于换幂等键，
ledger 查不到历史记录，于是**同一个退款会被当成新操作执行第二次**。
这是一条容易被忽略但后果严重的约束。

**失败场景**：用新 run_id 重放 → 幂等层被绕过 → 重复副作用。

**trade-off**：复用 run_id 意味着重放会撞上"终态 run 重复投递是 no-op"的保护。
实现上必须显式把状态推回可执行状态，且要保留 attempt 语义 —— 这比新建 run 复杂，
但复杂总比重复扣款好。

---

## 6. Reconciliation（对账）

**是什么**：扫描数据库与 broker 状态，找出"数据库说该跑、但没有对应活跃任务"的 run，
并重新调度。

**为什么需要**：三种丢失路径 re-delivery 救不回来：
1. 投递消息时进程崩溃（在 `mark_queued` 与 `apply_async` 之间）；
2. `RetryPublicationError` 之后依赖逃逸重投，但 broker 记账已被清空；
3. 人工从 DLQ 重放时投递失败。

这些情况数据库里 run 停在 `QUEUED`/`RETRYING`，但没有消息在飞。**没有任何进程会再碰它**
——它被遗忘了。对账就是定期问"有没有被遗忘的 run"。

**怎么实现**：`core/checkpoint_retention.py` 与 run 侧的 reconciliation 逻辑，
扫描 `QUEUED` / `next_retry_at` 已到期的 run 并重新调度。退避窗口必须被尊重
（`tests/integration/runtime/test_retention_and_reconciler_contract.py` 验证 reconciler
会跳过未到 `next_retry_at` 的 run）。

**trade-off**：对账必须**幂等**且**保守**——不能重复调度正在执行、lease 有效的 run，
否则对账本身会成为重复执行的来源。它本质上是"用数据库状态反推队列状态"，
是弱一致到强一致的兜底。

---

## 7. Distributed lock（per-thread 锁）

**是什么**：`agent:thread-lock:{thread_id}`，owner token + TTL + **Lua 原子 compare-and-delete**。

**为什么需要**：`mark_running` 保证"一个 run 同时只被一个 worker 执行"，
但**同一 thread 的不同 run** 仍可能并发（比如两个 API 请求同时派发两个 run 到同一
thread）。LangGraph checkpoint 按 thread 组织，并发写同一 thread 的 checkpoint 会
互相覆盖。

**怎么实现**（`runtime/thread_lock.py`）：
- `acquire(thread_id, owner, ttl)`：SET NX + TTL，`owner` 是 token；
- `release(thread_id, owner)`：Lua 脚本原子比较 owner 后删除 —— **只有 owner 能解锁**。

**为什么必须用 Lua compare-and-delete**：如果"读取 owner → 判断 → 删除"分两步，
worker A 释放锁之后、删除之前，worker B 可能刚好拿到锁，此时 A 的 DEL 会删掉 B 的锁。
Lua 让比较与删除在 Redis 内原子执行。

**为什么需要 TTL**：worker 崩溃时不会执行 release，锁会永久占用。TTL 让锁自动释放。
代价是 TTL 必须大于任务最长执行时间（生产校验强制
`AGENT_RUN_THREAD_LOCK_TTL_SECONDS > AGENT_RUN_TASK_TIME_LIMIT + 30`），
否则任务还在跑锁就过期了。

**失败场景**：锁 TTL 小于任务时长 → 锁在任务中途过期 → 第二个 worker 拿到锁 →
两个 worker 并发跑同一 thread → checkpoint 互相覆盖 → 状态错乱。
这正是上面那条配置校验存在的原因。

**trade-off**：worker 崩溃到锁过期之间，同 thread 的新 run 会被延迟调度（退避重试），
这是**正确性换可用性**：宁可暂时阻塞，不要并发写坏 checkpoint。

---

## 8. Idempotency key

**是什么**：`operation_key = run_id:tool_call_id`（无显式 key 时对
`tool_name + arguments` 取哈希，`runtime/side_effects.py::default_operation_key`）。

**为什么需要**：at-least-once 意味着同一个工具调用可能被执行多次。
没有幂等键，退款会退两次。

**怎么实现**：`runtime/side_effects.py::claim()` 是原子的 ——
创建 PENDING 行、判定是否可执行、返回 `CLAIM_EXECUTE`，全部在一次数据库操作里完成。
`tool_side_effects` 表对 `(tool_name, operation_key)` 建唯一索引。

**claim 的四种结果**：

| 返回 | 含义 | 调用方动作 |
|---|---|---|
| `CLAIM_EXECUTE` | 你是第一个，可以执行 | 执行，然后 `mark_succeeded` / `mark_failed` |
| 已 `SUCCEEDED` | 之前执行过 | **直接复用结果，不执行** |
| `CLAIM_IN_PROGRESS` | 别人持有且未过期 | 抛 `TransientError`，让出后重试 |
| lease 已过期 | 之前执行者崩溃 | 允许接管（attempt+1） |

**失败场景**：参数冲突（同一个 `operation_key` 但 `arguments` 不同）→ 抛
`PermanentError`。这是刻意的：它意味着上游出了 bug（同一个 tool_call_id 配了不同参数），
静默复用旧结果会掩盖问题。

**验证**：`test_side_effect_claim_concurrency.py`（8 并发只 1 执行）、
`test_tool_idempotency.py`（真实 worker SIGKILL 后副作用仅一次）。

**trade-off**：ledger 是**数据库写入**，每次副作用工具调用多一次往返，且需要清理策略
（旧记录）。换来的是跨进程、跨副本、重启后仍然有效的幂等性。

---

## 9. Checkpoint（LangGraph 持久化）

**是什么**：每个 super-step 结束后，图的状态（channel values + pending tasks）
写入 PostgreSQL。

**为什么需要**：崩溃恢复要求"已完成的节点不重跑"。没有 checkpoint，重启后只能从头执行，
所有已完成的下游副作用被重复触发。

**怎么实现**：`core/checkpointer.py::build_postgres_checkpointer`。
`LANGGRAPH_CHECKPOINT_BACKEND=postgres` → 官方 `AsyncPostgresSaver`。
生产初始化失败 **fail closed**，不静默回退 `MemorySaver`
（MemorySaver 只存在于当前进程，gunicorn 多 worker 之间不共享，重启即丢 —— 那等于没有）。

表：`checkpoints`（checkpoint 行）、`checkpoint_writes`（pending task 写入）、
`checkpoint_blobs`（大 value）。

**`checkpoint_writes` 为什么关键**：LangGraph 的 `aget_state` 通过**重放 pending writes**
计算 `next`。本项目的图中 `checkpoint->'next'` **恒为 null** —— pending task 记录在
`checkpoint_writes` 里。所以判别"是否有未完成工作"必须看 `checkpoint_writes`，
而不是 `next`。这一点在 flaky 根因分析里是关键。

**代价（重要）**：super-step checkpoint 由 `BackgroundExecutor` **不阻塞地**提交
（`langgraph/pregel/_loop.py`：`# save it, without blocking`）。
因此**节点已开始执行不蕴含上一步 checkpoint 已持久化**。
SIGKILL 落在窗口内 → 在途提交随进程一起丢失 → 恢复方只能从更早的 checkpoint 重跑。
这是框架的语义，不是本项目的 bug，但它要求**依赖 checkpoint 的测试必须显式等待提交落地**。
本项目的 flaky test 正是因此失败。

**trade-off**：每步一次数据库写入（延迟 + 存储）。换来崩溃恢复与 HITL 挂起/恢复。
`AGENT_RUN_TASK_TIME_LIMIT` 与 checkpoint 写入耗时必须匹配，否则会被
`task_soft_time_limit` 打断。

---

## 10. AgentRun（业务状态真相源）

**是什么**：`agent_runs` 表，状态机定义在 `runtime/statuses.py`。

**为什么需要**：checkpoint 只知道"图跑到哪"，不知道"这次执行算第几次、失败了要不要重试、
重试用尽怎么办"。这些是业务语义，必须有自己的真相源。

**状态机**（`runtime/statuses.py`）：

```
PENDING ──► QUEUED ──► RUNNING ──┬──► SUCCEEDED      (终态)
                 │              ├──► FAILED          (终态，permanent，不重试)
                 │              ├──► RETRYING ──► RUNNING  (transient，退避后重试，attempt+1)
                 │              ├──► WAITING_APPROVAL ──► RUNNING  (人工审批，不递增 attempt)
                 │              └──► DEAD_LETTER    (终态，retry 用尽)
                 └──► CANCELLED                    (终态)
```

关键集合（**刻意分开**）：

- `TERMINAL_STATUSES`：`SUCCEEDED` / `FAILED` / `DEAD_LETTER` / `CANCELLED`
- `EXECUTABLE_STATUSES`：`QUEUED` / `RETRYING` —— 可被 worker 领取
- `APPROVAL_RESUMABLE_STATUSES`：`WAITING_APPROVAL` —— **只能**由审批 API 显式恢复

把 `WAITING_APPROVAL` 排除在 `EXECUTABLE_STATUSES` 之外，是为了防止通用队列轮询把
"没人处理的审批"当成待办反复捞起，形成忙循环。

**迁移由数据库层强制**：`runtime/run_service.py` 用原子条件更新（`WHERE status = :expected`），
防并发竞态。不允许 `SUCCEEDED → RUNNING` 这类回退。

**trade-off**：状态机在应用层定义、在数据库层强制，两者可能漂移。
`tests/unit/test_distributed_runtime.py` 与 `test_retention_and_reconciler_contract.py`
做契约守卫。

---

## 11. Run events（观测通道）

**是什么**：worker 写 Redis Stream `agent:run:{run_id}:events`，API 经
`GET /api/runs/{run_id}/events` 转 SSE，支持 `Last-Event-ID` 续读。

**为什么需要**：让客户端能看到"执行到哪一步了"，而不是只能轮询最终状态。

**怎么实现**：`runtime/events.py`。

**明确不宣称**：这是**观测通道，不是真相源**。
`tests/integration/runtime/test_event_delivery_semantics.py` 明确测试并断言
"event stream is not source of truth"。它可能丢（best-effort）、可能重复重放
（游标语义是 at-least-once）。业务状态永远查 `agent_runs`。

**trade-off**：Redis Stream 的 trim 是近似裁剪，没有硬上界
（测试里也验证了这一点）。所以长 run 的早期事件可能被裁掉 ——
这正是它只能是观测通道的原因之一。

---

## 12. Side-effect ledger

见 [architecture-walkthrough.md](architecture-walkthrough.md) Q10 的详细展开。
这里是补充说明：

**是什么**：`tool_side_effects` 表，`(tool_name, operation_key)` 唯一。

**为什么需要**：见 Q10。它是"副作用只发生一次"的最后一道防线。

**怎么实现**：`runtime/side_effects.py::SideEffectStore.claim()`（原子 claim），
`tools/tool_registry.py::_idempotent_operation`（工具侧入口）。
只有**声明了 `side_effect=True`** 的工具才走 ledger —— 只读工具不需要，
因为重复执行无害。

**失败场景**：handler 抛异常 → 必须 `mark_failed` 释放 claim，否则后续重投看到
`CLAIM_IN_PROGRESS` 会一直退避。这也是为什么"副作用工具的失败必须冒泡"——
吞掉异常会把"写操作失败"伪装成成功 run，让上层 retry/DLQ 完全失效。

**trade-off**：只有 `side_effect=True` 的工具受保护。**误标是真实风险**：
一个实际有副作用但标成 `side_effect=False` 的工具会被重复执行。
缓解手段是 `core/hitl/risk.py` 的风险分级与审批（见
[hitl-deep-dive.md](hitl-deep-dive.md)），它对高风险工具强制人工审批，
即使幂等层漏了，多一层人工闸门。

---

## 13. Crash recovery（崩溃恢复）

完整链路见 [architecture-walkthrough.md](architecture-walkthrough.md) Q11。
这里记录**本轮定位的那个 flaky test**，因为它是"如何正确写崩溃恢复测试"的实例。

### 13.1 现象

`test_worker_crash_resumes_from_postgres_checkpoint` 记录为
**"7 次完整 real-infra suite 里失败 1 次"**。历史无法解释根因。

### 13.2 根因（已证明，非假设）

测试用 SIGKILL 模拟 worker 崩溃，SIGKILL 的时机由这个门控：

```python
ckpts_before_crash = _wait_for(lambda: checkpoint_rows() >= 1, 20)   # 旧门控
```

`count(*) >= 1` 被 **input checkpoint** 满足 —— 那是 LangGraph 在**任何节点运行之前**
写的第一行。于是这个门控远弱于测试断言真正依赖的不变式：
"first 的 super-step 结果已提交"。

叠加 §9 的语义：super-step checkpoint 由后台 executor 不阻塞提交，
所以存在一个真实窗口 —— `second` 已在执行（`ckpt:node_second` 已自增），
而 `first` 的提交还在途中。SIGKILL 落在窗口内 → **在途提交随进程丢失** →
恢复方只能从 input checkpoint 重跑 `first` → `first_after == 1` 与
`recovered >= 1` 断言失败。

**运行时是对的，测试的前提太弱。**

### 13.3 证据

`scripts/probe_crash_kill_gate.py` 重放测试自己的 start/wait/gate/kill 序列，
并在命中时跑完整的 visibility-timeout + 重投 + 恢复流程。

关键方法论：**在 kill 前后各读一次 checkpoint 状态**。
只看 kill 前会误判 —— LangGraph 的提交在"读"与"kill"之间的 ~8ms 内落地，
看起来像"命中"，实际没丢提交。加上 kill 后的读数，就能区分
`commit_landed_during_kill_window`（没丢，测试仍会过）和
`commit_absent_after_kill`（丢了，测试必挂）。

跨 270 cycles：

| 结果 | 计数 |
|---|---|
| 旧门控放行的 kill | 5 |
| 其中提交在 kill 窗口内落地（未丢） | 4 |
| **其中提交在 kill 后仍然缺失（真丢）** | **1** |

端到端复现（`killgate-20261002T194012Z/cycle-146.json`）：

```
pre_durable_has_first_result: false    post_durable_has_first_result: false
commit_absent_after_kill:     true
trace:       ['first:1', 'second:1', 'first:2', 'second:2']
first_after: 2    recovered: 0    resumed: false    test_would_pass: false
```

这正是记录中的失败形态。

**为什么它此前看起来"无法复现"**：需要 (a) 0.3s 轮询恰好在 `second` 自增前命中
（窗口 ~3–22ms vs 轮询 300ms，命中率约 2–3%），**并且** (b) 提交恰好还没落地
（命中后约 1/5 概率）。两个条件叠乘，所以单次运行几乎必然看不到。

### 13.4 修复

最小改动，且**不加 sleep、不放宽 timeout、不改 `visibility_timeout`、不放宽任何断言**：

```python
durable = _wait_for(
    lambda: first_superstep_durable(committed_checkpoint_channels()), 20
)
assert durable, f"...rows={committed_checkpoint_channels()}"
```

等**真实不变式**（最新已提交 checkpoint 的 `channel_values` 含 `branch:to:second`），
而不是"表非空"。

为什么 `branch:to:second` 是正确判据：

- 它只在 `first` 的 super-step 完成后才出现在已提交 checkpoint 里；
- `branch:to:first` **不能**用 —— 它由 START→first 边写入，在 `first` 返回**之前**；
- `checkpoint->'next'` **不能用** —— 本图 pending task 记在 `checkpoint_writes`，`next` 恒 null。

回归测试 `tests/unit/test_crash_recovery_kill_gate.py`（12 例）钉住这个判据，
包括导致 flake 的那个形状（只有 input checkpoint）。

### 13.5 顺带修掉的两个诊断缺陷

1. `_wait_for` 超时返回最后一次求值；predicate 返回 `False` 时
   `assert final is not None` 会**通过**，随后 `final[0]` 抛
   `TypeError: 'bool' object is not subscriptable`。
   "run 没进终态"这个可诊断的失败被变成了看不懂的崩溃。
   改为返回 `None`，断言信息恢复可读。
2. `test_tool_idempotency.py` 同一模式同样处理。

### 13.6 验证强度

- 12 例单测钉住判据（含 input-checkpoint-only 形状）；
- 50 次重复运行在干净树上 0 失败（`scripts/repeat_crash_recovery_test.py`，
  记录 `clean_tree_throughout` 与每次的 git SHA）；
- 完整 runtime 套件、`make runtime-chaos`、`make runtime-verify` 全绿。

### 13.7 教训（值得单独讲）

1. **"不稳定"的测试先怀疑测试前提，再怀疑被测代码。** 这里的运行时行为从头到尾是对的。
2. **"无法复现"不等于"没问题"，也不等于"猜对了"。** 本轮在 270 cycles 前的中间状态
   （2 次命中都恢复）差点得出"假设已被否证"的结论；再跑 150 cycles 才出现真丢提交的那一次。
   中途因为证据不足而**没有合并修复**，这个克制是对的。
3. **计数类断言（`count(*) >= 1`、`len(x) > 0`）几乎总是比看起来弱。**
   它只证明"有"，不证明"是对的那个"。判据必须直接表达不变式。
4. **诊断能力要先于修复。** 本轮先加了失败取证（`crash_diagnostics.py` +
   conftest hook），才有了能定位问题的证据；否则根因只能靠猜。

---

## 14. 可观测性：失败取证

**是什么**：`tests/integration/runtime/crash_diagnostics.py` —— 只读收集器，
在失败时把状态写成 JSON artifact。

**为什么需要**：本轮的历史记录是一行 `AssertionError`，无法定位。
取证必须能回答：

| 问题 | 字段 |
|---|---|
| 任务真的收到了吗 | `broker.queue_length`、`broker.unacked_size` |
| ACK 了吗 | `broker.unacked_task_ids` |
| worker 什么时候死的 | `workers[].kill_sent_at` / `exit_code` / `killed_by_signal` |
| checkpoint 提交了吗 | `checkpoints.durable_channels` |
| AgentRun 到 RUNNING 了吗 | `agent_run.status` / `attempt` / `worker_id` |
| 消息何时重新可见 | `env.AGENT_RUN_VISIBILITY_TIMEOUT` + timeline |
| 新 worker 有没有再消费 | `agent_run.worker_id` vs `workers[].pid` |
| 为什么没 resume | `checkpoint_writes`（pending task）+ `agent_run.result` |

**怎么实现（两个时刻）**：

1. **failure 时**（`conftest.py::pytest_runtest_makereport` hookwrapper，
   仅 `rep.failed`）：teardown 还没跑，测试自己的行和 key 还在。
2. **teardown 后**（autouse fixture）：暴露**泄漏** —— 活着的 worker 进程、
   没清掉的 broker key。

第二个时刻是关键：记录的现象是"**7 次 suite 跑挂 1 次**"，属于 suite 级。
单测试范围的快照无法区分"这次自己竞态"和"之前某个测试污染了共享状态"。
`test_worker_checkpoint_recovery` / `test_tool_idempotency` / `test_queue_worker_decoupling`
三者都会 `DELETE` broker **全局**的 `unacked` / `unacked_index`（不是 per-queue），
且都杀 worker —— 泄漏的 worker 会继续消费并改写共享 key。

**为什么取两次而不是一次**：teardown 会删掉测试自己的行和 key，
所以"失败瞬间"和"泄漏"是两个不同的问题，必须分开取证。

**发现的缺陷（已修）**：两个快照在同一秒内写出时文件名相同（只由 nodeid + 时间戳构成），
第二个会**静默覆盖**第一个，丢掉 failure 时刻的证据。修法是把 `phase` 放进文件名。

**为什么严格只读**：取证绝不能改变它要测量的行为。所以没有任何写、没有 publish、
没有发信号。取证开销只在失败时发生 —— 每次运行都取证既产生噪声，
其 I/O 也会扰动正在被测量时序的套件。

**验证**：`artifacts/runtime-diagnostics-selftest/` —— 故意注入一次失败，
在干净树上产出两个快照，manifest 标注这是刻意触发、不是真实事故。