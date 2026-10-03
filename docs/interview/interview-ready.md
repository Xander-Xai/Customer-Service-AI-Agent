# 面试 Evidence Pack（面试可带走的唯一一份）

> Lifecycle: 🟢 CURRENT
>
> **对齐范围**：本文所有事实对齐 `main` @ `c4fac3934de0856949efd15481301237cf820f46`。
> 复现方式：`git rev-parse main`。**不在 `main` 上的能力不作为本文的宣称**（见 §6.4）。
>
> **本文在面试材料家族中的位置**（唯一入口，避免四份文档互相竞争）：
>
> | 需要什么 | 去哪里 |
> |---|---|
> | **一页带走（介绍 + 架构 + 3 个问题 + demo + 证据映射 + 边界 + 追问）** | **本文** |
> | 60 秒 / 2 分钟 / 3 分钟三档口播脚本 | [../design/interview-intro.md](../design/interview-intro.md) |
> | 面试问题 → 源码 → 测试 → 证据（问题溯源） | [source-map.md](source-map.md) |
> | 失败模式与取舍（为什么不做 X） | [failure-and-tradeoffs.md](failure-and-tradeoffs.md) |
> | 分布式 runtime 逐概念深挖 | [runtime-deep-dive.md](runtime-deep-dive.md) |
> | HITL 深挖 | [hitl-deep-dive.md](hitl-deep-dive.md) |
> | RAG 深挖（**本文不含任何 RAG 指标数字**） | [rag-deep-dive.md](rag-deep-dive.md) |
> | 证据等级阶梯的 canonical 定义 | [../evaluation/production-evidence.md](../evaluation/production-evidence.md) |
> | 当前 runtime 事实 + 验证命令 | [../reference/current-state.md](../reference/current-state.md) |
>
> **口径纪律**（先读这一段，比本文任何内容都重要）：
>
> - 本文出现的每个数字后面都跟了**取得它的命令或 artifact 路径**。没有 artifact
>   支撑的百分比，本文不写。
> - **RAG 649-query 正式指标是 `NOT_VERIFIED`**。禁止报任何 Hit@K / MRR / NDCG /
>   Recall 百分比（连"大约 80%"这种量级也不行）。
> - **分布式 Agent Runtime 的证据等级是 Level 2 = CI VERIFIED**：真实 PostgreSQL +
>   Redis + 真实多进程 Celery 的验收套件 + SIGKILL 混沌测试。**不是**"生产集群已验证"。
>   真实生产集群 / 多副本长期运行 / 真实用户流量 / 真实 ERP 写操作属于 Level 3，
>   `NOT_VERIFIED`。
> - **测试数量不写入本文**。用 `pytest --collect-only -q` 的当前输出，不要背历史数字。
> - 缓存命中率、FCR、人效、P99、成本、provider 计费：`NOT_MEASURED` / `NOT_VERIFIED`。
> - 应用语义 tracing **不等于**有 trace 后端；真实 trace 持久化/查询后端
>   `NOT_VERIFIED`（见 §6.3）。

---

## 1. 90 秒项目介绍

> **0–15 秒｜业务定位**
>
> 这是一个面向化妆品生产/销售企业的多智能体客服系统。目标是让多个 AI Agent 协同
> 处理从"你好"到"我要退款并投诉物流"的各种问题。基于 LangGraph，9 个运行时 Agent
> 角色、5 种协作模式，带 RAG 混合检索和 Function Calling 工具调用。
>
> **15–35 秒｜架构一句话**
>
> 业务链路是四层状态机：缓存检查 → 双层路由（LLM 分类器和规则分类器并行）→
> 五种协作模式 → 响应后处理。工程上分**两条路径**：低延迟问答走同步快路径，长任务
> 走异步 Run，交给独立的 Celery worker。
>
> **35–70 秒｜最硬的工程点：分布式 Agent Runtime**
>
> 这套系统原来是单进程的：LangGraph checkpoint 在内存里，多开一个 worker 状态就
> 分片，重启全丢。我把它改造成实时/异步双路径，并且明确了三条边界：
>
> 1. **业务状态的唯一真相源是数据库的 `agent_runs` 表**，不是队列的 result backend
>    ——队列只是调度层，消息会丢也会重投。
> 2. **投递语义是 at-least-once，不是 exactly-once**，而且我把它明说了。幂等分三层：
>    run 级（终态重复投递 no-op + `idempotency_key` 唯一约束）、thread 级（Redis 锁）、
>    工具级（副作用 ledger，唯一键 `run_id:tool_call_id`）。
> 3. **崩溃后从 checkpoint 的下一个节点续跑**，不是从头重跑。这是我修过的真 bug：
>    LangGraph 的 `ainvoke(state, cfg)` 会从 START 重新执行并覆盖 channel 值，
>    只有 `ainvoke(None, cfg)` 才续跑。
>
> 这三件套（PostgreSQL checkpoint + Redis session + Redis per-thread 锁）在生产
> 启动时强制校验，缺一个直接拒绝启动——不是 warning。
>
> **70–90 秒｜主动划边界（这段最加分）**
>
> 证据等级我分成三层，这些能力是 **CI VERIFIED**：真实 PostgreSQL、Redis、多进程
> Celery 的端到端验收，加一个把 worker 整个进程组 SIGKILL 掉的混沌测试，验证
> 续跑和副作用不重复。**但真实生产集群、多副本长期稳定性、真实 ERP 写操作我都没
> 验证过**，所以我不会说"生产已验证"。RAG 那块我有一套可复现的评测流水线，但正式
> 指标当前是 `NOT_VERIFIED`，所以我只讲方法论，不报数字。

**为什么这样切 90 秒**：业务定位（15s）→ 一句话架构（20s）→ 押在唯一真正硬核的
工程点上（35s）→ **自己主动**划证据边界（20s）。主动说边界比被追问才承认可信得多；
如果时间只够 30 秒，就讲第一段 + 第三段的第一句。

---

## 2. Canonical architecture（一张图）

> 这张图是**本文唯一的架构图**，把"四层业务状态机"和"两条执行路径 + 真相源"画在
> 同一张图里——因为它们是同一个请求的两个视角，不是两个系统。
>
> 溯源：四层状态机 `core/graph_builder.py::build_graph`；快路径执行边界
> `api/app.py::_run_graph`；异步路径 `api/routes/runs.py` + `runtime/`；状态机
> canonical 源 `runtime/statuses.py::ALLOWED_TRANSITIONS`。

```text
 客户端 / Widget
        │
        ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │ Nginx（TLS 终止）→ Gunicorn 多 worker（FastAPI）                     │
 └───┬──────────────────────────────────────────────────────┬─────────┘
     │ 快路径：低延迟，永远 inline，不经队列                  │ 异步路径：长任务
     │  POST /api/chat          REST，inline 跑完整张图      │  POST /api/runs
     │  POST /api/chat/stream   SSE 真流式                  │   ↓ 写库(QUEUED) → 入队
     │  WS /api/ws · /api/chat/multimodal                    │   ↓ 立即返回 202
     │                                                      │   ↓ GET /api/runs/{id} 轮询
     │                                                      │   ↓ GET /api/runs/{id}/events (SSE)
     └───────────────────────────┬──────────────────────────┘
                                 │ 两条路径都经 api/app.py::_run_graph
                                 │ 拿同一把 per-thread 分布式锁
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │ 四层状态机（core/graph_builder.py::build_graph）                      │
 │  L0 缓存  L1 Redis 精确 → L2 Qdrant 语义 → L3 Jaccard 兜底           │
 │  L1 路由  LLM 分类器 ∥ 规则分类器（asyncio.gather）+ 复杂度评分       │
 │            → 高置信规则捷径 / 快路径 / 复杂链路                       │
 │  L2 协作  Sequential ∥ Parallel ∥ Consultation ∥ Hierarchical ∥ ReAct│
 │  L3 后处理 质量评估 → 模式升级重试 → 写缓存 → SLA 监控 → 事件广播      │
 └────────────────────────────────────────────────────────────────────┘
                                 │
        ┌────────────────────────┴─────────────────────────┐
        │ Celery worker（独立进程/容器，acks_late +          │
        │ reject_on_worker_lost + Redis visibility_timeout） │
        │  runtime/executor.py::execute_run                 │
        │   ├ Redis per-thread 锁（owner token + TTL +       │
        │   │   Lua 原子 compare-and-delete 释放）           │
        │   ├ PostgreSQL checkpoint（官方 AsyncPostgresSaver） │
        │   │   有未完成 checkpoint → ainvoke(None) 续跑      │
        │   ├ tool_side_effects ledger（operation_key 认领）  │
        │   ├ human_approval_gate（HIGH 风险 → interrupt）     │
        │   └ Redis Stream 事件 → API 转 SSE（Last-Event-ID）  │
        │ 状态提交一律走 owner CAS：run_id + status + worker_id│
        │ + lease 未过期，在同一条 UPDATE 里判定               │
        └────────────────────────┬─────────────────────────┘
                                 ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │ 真相源：PostgreSQL（不是 Celery result backend，task_ignore_result）│
 │                                                                     │
 │  agent_runs（业务状态唯一真相源）                                     │
 │    PENDING ─► QUEUED ─► RUNNING ─┬─► SUCCEEDED   (终态)             │
 │      │                 │         ├─► FAILED      (终态)             │
 │      │                 │         ├─► RETRYING ─► RUNNING (退避重试)  │
 │      │                 │         ├─► WAITING_APPROVAL ─► RUNNING      │
 │      │                 │         │      人工审批决策后恢复            │
 │      │                 │         │      不递增 attempt（等人不是失败）│
 │      │                 │         └─► DEAD_LETTER (终态，retry 用尽)   │
 │      └──► CANCELLED (终态)   任意未终态 ─► CANCELLED（逃生口）       │
 │                                                                     │
 │  human_approvals      durable 审批记录（run_id + action +            │
 │                       proposal_fingerprint 唯一约束）               │
 │  tool_side_effects    副作用幂等 ledger（tool_name + operation_key）  │
 │  agent_dead_letters   不可变 DLQ 历史 + 人工重放（复用原 run_id）     │
 │  LangGraph checkpoints 官方 saver 自管，不与业务 SQLAlchemy Base 耦合 │
 │                                                                     │
 │  另两个独立存储：Redis session（多副本共享）/ Response Cache           │
 └────────────────────────────────────────────────────────────────────┘
```

**看图时必须一起说清的 5 件事**（面试官最容易在这里误判）：

1. **四个存储不是同一个概念，禁止互相替代叙述**：LangGraph checkpoint（图状态快照，
   键是 `thread_id`）/ Session Memory（会话窗口+摘要）/ Response Cache（答案复用）/
   Tool Result Store（工具结果复用）。它们各自的失效语义完全不同。
2. **checkpoint ≠ 业务状态**。checkpoint 说"图执行到哪了"，`agent_runs` 说"这次业务
   运行处于什么状态"。前者能续跑执行，后者能对外承诺状态与做幂等判定。
3. **队列不是真相源**。result backend 明确 `task_ignore_result=True`。
4. **`WAITING_APPROVAL` 是非终态，且不在 `EXECUTABLE_STATUSES`（`{QUEUED, RETRYING}`）里**
   ——通用轮询不会把等人审批的 run 反复捞起（否则变忙循环）。
5. **快路径不在 durable HITL 治理边界内**（`/api/chat` 没有 run 上下文）。见 §3.3。

---

## 3. 三个最值得讲的 engineering problems

选这三个的标准：① 有真实的 bug / 真实的事故，不是"设计得很好"；② 能落到
`file:line`；③ 有可复现的验证命令；④ 能讲清我放弃了什么。

### 3.0 总览

| # | Problem | 一句话 | 证据等级 |
|---|---|---|---|
| P1 | "崩溃后续跑"其实是从头重跑，而且**门禁在撒谎** | LangGraph 调用语义用错 + 测试门禁概率性放行 | CI VERIFIED |
| P2 | at-least-once 下的三层幂等，以及一个**真会重复退款的 bug** | 重放换了 `run_id` → 幂等键失效 | CI VERIFIED |
| P3 | 把"人工审批"当成一个**分布式状态**，而不是一个 UI 弹窗 | 审批 ≠ 幂等；等人不该消耗 retry 预算 | CI VERIFIED（治理机制）/ NOT_VERIFIED（真实 ERP） |

---

### 3.1 P1 — 崩溃恢复：从头重跑，以及一个概率性放行的门禁

**Problem**

worker 被 SIGKILL 后任务重投，我需要它**从崩溃点续跑**，而不是把整张图重跑一遍。
理由很直接：节点有副作用（写 ERP、调支付、发通知），重跑 = 重复副作用 + 重复扣款。

**Naïve solution**

一开始的写法是 `await graph.ainvoke(state, config)`，同时用一个"杀 worker 然后看
结果对不对"的混沌脚本当门禁。两个都是错的。

**Final design**

1. **区分 LangGraph 的两种调用语义**（`runtime/bootstrap.py::invoke_graph_with_resume`
   的 docstring 里写死了实测结论）：
   - `ainvoke(state, cfg)` → **从 START 重新执行**，并用入参覆盖 channel 值；
   - `ainvoke(None, cfg)` → 从 checkpoint 的 `next` 继续，不重跑已完成节点。

   所以恢复路径必须传 `None`。判定逻辑是"存在未完成 checkpoint（`next` 非空）则续跑，
   否则正常执行"，API 快路径（`api/app.py::_pending_steps`）用同一语义。
2. **崩溃后先接管 lease，再续跑**：worker lease 过期后新 worker 才能
   `mark_running` 接管（`runtime/repository.py::takeover_running`，原子谓词，两个
   竞争者只有一个能接管），`attempt + 1`。
3. **旧 worker 不许再提交状态**：所有 worker 发起的迁移走
   `repository.transition_owned()`，`run_id + status + worker_id + lease 未过期`
   在**同一条 UPDATE** 里判定；`heartbeat()` 是原子 owner CAS。失去所有权抛
   `RunOwnershipLost`，executor 读到即退出：不记失败、不重试、不进 DLQ、不消耗 attempt。
4. **执行期间续租**：`_heartbeat_loop` 同时续 DB ownership lease 与 Redis 锁 TTL，
   把"pause 超 TTL"的窗口压到很小。

**Failure mode（这个坑最值得讲）**

门禁本身在撒谎。混沌测试的判定条件原本是"worker 进程组被 SIGKILL 了"，但 LangGraph
在后台 executor 上提交 super-step checkpoint，而门控是**先读 checkpoint、再发 kill**。
如果提交恰好落在这两次读之间（约 8ms 的测量缝隙），进程就"体面地"退出了，测试仍然
算通过——**门禁放行了它本该抓住的情况**。

于是我做了概率量化而不是继续加 sleep。方法论的关键是：**在 kill 前后各读一次
checkpoint 状态**。只看 kill 前会误判；加上 kill 后的读数才能区分
`commit_landed_during_kill_window`（没丢，测试仍会过）和 `commit_absent_after_kill`
（真丢了，测试必挂）。

两轮定向复现的结论**已提交为 artifact**（`schema: crash-kill-gate-probe/v1`）：

| Artifact | cycles | 门控命中 | 提交落在 kill 缝隙内（未丢） | 提交在 kill 后仍缺失（真丢） | verdict |
|---|---|---|---|---|---|
| `artifacts/flake-investigation/killgate-20261002T191255Z/summary.json` | 120 | 4 | 该轮 summary 未拆分此字段 | 该轮 summary 未拆分此字段 | `RACE_DEMONSTRATED` |
| `artifacts/flake-investigation/killgate-20261002T194012Z/summary.json` | 150 | 3 | 2 | **1** | `RACE_REPRODUCED_END_TO_END` |

第二轮的 `end_to_end_reproductions: 1`、`repro_cycles: [146]`，对应的端到端复现记录在
`killgate-20261002T194012Z/cycle-146.json`（`commit_absent_after_kill: true`、
`test_would_pass: false`）。两轮合计 **270 cycles**，采样从"7 次完整 real-infra suite 里
失败 1 次"这个无法解释的观察，扩到了有 per-cycle 明细的概率量化。

修复方向是**校准判据**（断言接管 worker 与崩溃 worker 不同 + 断言副作用计数器仍为 1
且工具真实调用次数 ≥ 2），而不是靠 sleep 赌概率。判据本身被钉成
`tests/unit/test_crash_recovery_kill_gate.py` 的单测，所以它以后不会退回去。

这里有个关键的反直觉点值得讲：**如果副作用计数是 1，可能是"去重生效了"，也可能
是"它根本没重试过"**。所以判据必须同时断言"计数器 = 1"**和**"真实调用次数 ≥ 2"。

**Tradeoff（我放弃了什么）**

- 没有实现 **fencing token**。owner CAS 解决的是"stale worker 不能提交 AgentRun
  状态"，**不是**完整的 stale-worker fencing：pause 超过 TTL 的旧 worker 不会被
  强制 abort，其协程可能继续跑完；它的**外部节点副作用**也不受 owner CAS 保护，
  仍依赖 side-effect ledger 的幂等。严格解法是单调递增 fencing token 或数据库
  版本号校验，让旧持有者的写入无条件被拒——我知道缺口在哪，没做。
- 恢复粒度是 **node / checkpoint 边界**，不是任意 Python 指令级无损恢复。失败节点
  可能重新执行，所以节点副作用必须幂等——这是我把幂等做成三层的原因之一。
- 只有一个 Redis，不做 Redlock 集群。

**Evidence**

```bash
# 混沌验收（真实 PG + Redis + 多进程 Celery，SIGKILL 整个 worker 进程组）
make runtime-chaos
# → artifacts/runtime/chaos-<ts>.json，result=PASS，8 个 step

# 崩溃恢复 / 副作用幂等 / checkpoint 跨进程的定向验收
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 \
  python3 -m pytest tests/integration/runtime/test_worker_checkpoint_recovery.py \
                 tests/integration/runtime/test_cross_process_checkpoint.py \
                 tests/integration/runtime/test_tool_idempotency.py -q

# 概率性判据的单测（把"门禁会不会撒谎"钉成断言）
python3 -m pytest tests/unit/test_crash_recovery_kill_gate.py -q

# owner CAS 的纯配置/契约断言 + 真实 PG 下的并发验证
python3 -m pytest tests/unit/test_agent_run_runtime.py -q
TEST_DISTRIBUTED_DB_URL=... TEST_REDIS_URL=... \
  python3 -m pytest tests/integration/runtime/test_worker_ownership_cas.py -q
```

混沌脚本 `scripts/test_worker_crash_recovery.py` 的 step 序列本身就是证据链：
`create_run → enqueue → side_effect_applied → checkpoint_persisted →
sigkill_worker_group → restart_worker → recovered → side_effect_deduplicated`，
最后一步的 payload 里 `side_effect` 计数为 1、工具真实调用次数为 2、ledger 命中 1 次。

**规模感（要背的话只背这一句）**：证据来自 **270 个定向 cycle 的概率量化**（两轮
summary.json 都在 `artifacts/flake-investigation/` 里），而不是 7 次 suite 的偶然失败。
**样本量本身是被当作证据的一部分来设计的**，不是顺手多跑了几次。

---

### 3.2 P2 — at-least-once 下的三层幂等，与一个真会重复退款的 bug

**Problem**

我要的是"崩溃恢复 + 重试之后，同一笔退款只发生一次"。但我先要诚实地承认：
**分布式系统里端到端 exactly-once 代价极高，而且做不到。** 所以我不追 exactly-once，
我声明 at-least-once，然后把幂等做实。

**Naïve solution**

天真做法是"靠队列保证不重复投递"。它错在：Redis/Celery 的 at-least-once 语义
（`acks_late` + `reject_on_worker_lost` + `visibility_timeout`）**明确允许**同一条
任务被投递多次；进程在"已 ACK 之前"死掉、visibility timeout 到期、reconciler 兜底
扫描——都会产生重复投递。按 task_id 去重也不可靠，因为重试会换 delivery tag。

**Final design — 三层，各挡一类重复**

| 层 | 挡什么 | 键 | 实现 |
|---|---|---|---|
| run 级 | 同一次业务请求被重复投递/重复创建 | `idempotency_key`（scoped: user + endpoint + key）唯一约束 | `runtime/run_service.py::create_run` |
| thread 级 | 同一会话的并发执行互相踩 | `agent:thread-lock:{thread_id}` | `runtime/thread_lock.py`，API 侧 `core/concurrency/distributed_lock.py` |
| 工具级 | 同一笔副作用被执行两次 | `(tool_name, operation_key)`，其中 `operation_key = run_id:tool_call_id` | `runtime/side_effects.py::SideEffectStore.claim` |

关键细节：

- **认领是原子的**。`claim()` 让 8 个并发线程抢同一个 key 时，只有 1 个拿到
  `CLAIM_EXECUTE`，其余拿到 `CLAIM_IN_PROGRESS`（抛 `TransientError` → 退避重投，
  而不是重复执行副作用）。
- **执行期续租**。执行期间续租 DB ownership lease 与 Redis 锁 TTL，把"pause 超 TTL"
  的窗口压到很小。
- **写工具自动走 ledger**。`ToolRegistry.register(side_effect=True)` 的写工具在 Run
  执行上下文内自动被 ledger 包裹，`agents/base_agent.py` 把 LLM 的 `tool_call_id`
  透传到 registry——**幂等不是每个 Agent 作者要记得调用的东西，而是注册时声明的**。

**Failure mode（这是本项目最值钱的 bug）**

DLQ 人工重放。一开始 `scripts/replay_dead_run.py` 重放时**新建了一个 run**。
但工具幂等键是 `run_id:tool_call_id`——换 `run_id` 就等于**绕过幂等**，把一笔已经
成功退款的请求又执行了一遍。

修法不是"重放时小心点"，而是把不变量写进接口：**重放必须复用原 `run_id`**，只产生
新的队列投递。`RunService.requeue_dead_letter` + CLI 强制这一点，原始 DLQ 历史不被
改写，并有 `agent_run_dead_letter_replay_total` 计数。

这个 bug 的教育意义我一般这样讲：**幂等键的作用域选错了，比没有幂等更危险**——因为
它给了你"我已经有幂等了"的错觉。

**Tradeoff（我放弃了什么）**

- ledger 只保证**同一个 Agent 不重复发起同一副作用**。如果下游 ERP 需要端到端幂等，
  得下游 API 接受 idempotency key——**这不是我这边能单方面保证的**，我会主动说这句。
- run 级幂等键依赖调用方提供 `Idempotency-Key`（或 body 字段），超长直接 400。
- 认领租约未过期时抛 `TransientError` 退避重投，所以极端情况下会有"延迟"而不是
  "丢失"——我选延迟，因为退款场景里重复比延迟危险得多。

**Evidence**

```bash
# 副作用幂等（含 8 并发只 1 个 CLAIM_EXECUTE）
python3 -m pytest tests/integration/runtime/test_side_effect_claim_concurrency.py -q
python3 -m pytest tests/integration/runtime/test_tool_idempotency.py -q

# DLQ + 重放必须复用原 run_id
python3 -m pytest tests/integration/runtime/test_run_semantics.py -q
make runtime-replay-help        # scripts/replay_dead_run.py --help

# machine-readable 幂等证据（side_effect_calls=1）
make runtime-verify
# → artifacts/distributed-runtime/<ts>/report.json
```

`make runtime-verify` 产出的 artifact 里，`tool_idempotency` 这一项的 payload 就是
`{"status": "PASS", "side_effect_calls": 1}`——这是可被第三方独立复核的机器证据，
不是文字描述。

---

### 3.3 P3 — HITL：把"人工审批"当成一个分布式状态

**Problem**

HIGH 风险工具副作用（退款 / 改单 / 高额赔付 / 投诉升级）不能由 LLM 自己拍板。
但真正的难点不是"弹个框"，而是：**审批跨越进程重启**（审批人可能第二天才处理）、
**审批本身必须可审计**、**审批记录和幂等 ledger 是两条独立的防线**。

**Naïve solution**

天真做法是在工具执行前同步问一次"是否批准"。它错在：同步等待会把 worker 线程挂住
几小时；进程一重启审批上下文就没了；而且"批准"和"不重复执行"被混为一谈——
批准过的操作仍然可能被重试执行两次。

**Final design**

1. **拦在执行之前**。Agent 工具循环只把 HIGH 风险调用**摘进** `state["pending_actions"]`
   （**不执行**），由图节点 `human_approval_gate` 逐个 `interrupt()`。
2. **durable 审批**。`human_approvals` 表，唯一约束 `(run_id, action, proposal_fingerprint)`；
   `PENDING → APPROVED | REJECTED | EXPIRED`，均为终态。
3. **风险分级**（`core/hitl/risk.py`）：`LOW / MEDIUM / HIGH`，优先级为
   「工具显式声明 > 工具名白名单 > 金额阈值 > 默认 LOW」；**只有 HIGH 需要人工审批**。
   **判定失败时 fail-closed（挂起而非放行）**。
4. **新增非终态 `WAITING_APPROVAL`**，并且：
   - **不进** `EXECUTABLE_STATUSES`（`{QUEUED, RETRYING}`）——通用队列轮询不会把
     无人处理的审批变忙循环；
   - `WAITING_APPROVAL → RUNNING` 由 `RunService.mark_resumed_running()` 执行且
     **不递增 attempt**——**等人不是失败，不该消耗 `AGENT_RUN_MAX_ATTEMPTS`**；
   - 刻意**没有** `WAITING_APPROVAL → QUEUED` 这条边（改回 QUEUED 会走
     `mark_running`，而它每次领取都 `attempt + 1`）。
5. **职责分离在 service 层强制**（`reviewer_id != user_id`），API 层不是唯一防线。
6. **TTL 到期落 `EXPIRED`，按拒绝处理，绝不默认放行**。可在决策 / 读取 / 恢复三处
   收敛，图不会死等。
7. **已批准的执行仍走 ledger**，`operation_key = run_id:approval:{approval_id}`
   （由 `approval_id` 派生，在任意次重试中恒定）→ 恰好一次。复用
   `runtime/side_effects.py` 的原子 `claim`，不重复实现。
8. **脱敏留痕**：`core/hitl/sanitize.py` 黑名单 + 定长截断。

**验证过的 LangGraph 语义**（langgraph 1.2.12 / Python 3.10，真实 PG checkpoint 上实测）：
`interrupt()` 不抛异常而是注入 `__interrupt__`；`ainvoke(None)` **解除不了** interrupt
（所以崩溃恢复与审批恢复必须分两条路径）；`Command(resume=...)` 需要 checkpointer。

**Failure mode**

两个真实翻车点：

- **把审批和幂等当成一件事**。结果：审批只解决了"不该做的被做了"，没解决"做了一次
  被重做"。反过来只做幂等，LLM 可以自己批准自己的高风险调用。**两条防线缺一不可**，
  而且它们的作用域不同，我会在面试里明确区分。
- **等人消耗 retry 预算**。人等三小时被算成三次失败，最后 run 进 DLQ——形式上"优雅
  失败"，实际上是 bug。修法不是调大 `max_attempts`，而是把
  `WAITING_APPROVAL → RUNNING` 从 `mark_running` 里拆出来。

**Tradeoff（我放弃了什么）**

- **没有审批的主动通知链路**（无 Webhook / 邮件推送），只能轮询待审批队列；长时间
  无人处理会静默过期（按拒绝处理，不会误放行，但会浪费一次业务机会）。
- **审批 SLA / 人工效率没有任何生产数据** → `NOT_MEASURED`，我不会报"平均审批时长"。
- **快路径不在边界内**：`/api/chat` 实时快路径没有 run 上下文，因此**明确不在**这个
  治理边界内，我不会宣称它受 durable HITL 保护。这条边界由机器 guard
  `scripts/audit_doc_consistency.py::check_hitl_fastpath_not_claimed` 强制。
- **`HITL_ENABLED` 默认 `false`**，所以"默认可用"是错的说法。

**Evidence**

```bash
# durable HITL 端到端（真实 PG checkpoint + interrupt/resume）
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 \
  python3 -m pytest tests/integration/runtime/test_hitl_approval_flow.py \
                 tests/integration/runtime/test_hitl_langgraph_interrupt.py -q

# 契约层：状态机完整性 / 默认值 / 快路径不被误宣称
python3 scripts/audit_doc_consistency.py
```

**边界（必须主动说）**：**真实 ERP 退款 / 改单是 `NOT_VERIFIED`**——没有企业 staging
环境，任何 artifact 都支撑不了这个宣称。我的副作用验证走
`tools/hitl_staging_tools.py` 确定性 staging 工具，它验证的是**治理机制**（会不会被
重复执行、会不会绕过审批、TTL 会不会误放行），**不是 ERP 集成的正确性**。

---

## 4. 5 分钟 demo 路径

> 设计原则：**只演示能被当场验证的东西**。每一步都给出命令 + 预期观察 + 我会当场
> 说的话。如果某一步依赖失效的外部凭据，我会在开始前就说，而不是演完再解释。

### 4.0 开场前 30 秒：说清环境

> "先说环境，免得你误判：LLM provider 凭据当前失效，所以我**不会** demo 回答质量。
> 我 demo 的是**架构行为**——路由决策、两条路径、状态机、幂等、崩溃恢复。这些不依赖
> LLM 质量，而且这正是我今天要讲的部分。"

（如果面试环境有可用凭据，去掉这段，直接 demo 答案质量。）

### 4.1 第 1 步（0:00–0:40）事实与自检 —— 建立"这个仓库的数字是可追溯的"

```bash
python3 scripts/project_facts.py     # 版本 / 模型 / HTTP 路径数 / Agent 角色数 / RAG 基准数 / 评测状态
python3 scripts/generate_openapi.py --check   # OpenAPI 快照与 app.openapi() 一致
python3 scripts/audit_doc_consistency.py      # 文档一致性 + 生产级措辞 + 状态机完整性等机器 guard
```

**预期观察**：`runtime_version 6.3`、`openapi_path_count 62`、`agent_role_count 9`、
`rag_benchmark_query_count 649`、`rag_formal_metrics_status NOT_VERIFIED`；后两条命令
以 `OK` 退出。

**我会说的话**："注意最后那个字段——RAG 正式指标是 `NOT_VERIFIED`。这不是我忘了跑，
是**没有 artifact 就不许报数字**这条规则被机器强制执行了。"

### 4.2 第 2 步（0:40–2:00）快路径：路由决策 + 四层状态机

```bash
make env-dev
python3 -m uvicorn api.app_factory:app --host 127.0.0.1 --port 8000 --log-level info
# 另开一个终端
curl -s localhost:8000/api/health | python3 -m json.tool        # 看 checkpoint backend / execution mode

# 不要自带 session_id——让服务端签发，省掉会话令牌这一层
curl -s -X POST localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"我的订单什么时候发货？"}' | python3 -m json.tool
```

**预期观察**：返回 `agent` / `mode` / `elapsed` / `cached` / `agents_used` /
`session_id` / `session_token`；`/api/health` 里能看到 `langgraph_checkpoint.backend`
和 `agent_execution`。

**服务端日志（这才是重点，我会把日志窗口转给面试官看）**：

```text
[Cache] MISS: 我的订单什么时候发货？...
route: type=order_status agent=billing_agent complexity=30 confidence=0.95 rule_override=False
[Router] type=order_status agent=billing_agent complexity=30 fast_path=True
[ModeSelect] -> sequential
[SLA] mode=sequential agent=billing_agent cached=False resolution=resolved eval=50.5
```

**我会说的话**："这一屏同时展示了 Layer 0 缓存未命中、Layer 1 双层路由的规则分类器
以 0.95 置信命中、复杂度 30 分把它判进快路径、Layer 2 选了 Sequential、Layer 3 的
质量评估和 SLA 打点。**四层全在这一次请求里走完了**，而且完全没有经过队列。"

**如果 LLM 凭据失效**（当前状态）：响应会是降级文案，日志里出现
`Sequential ... 超时 (15.0s)，返回降级回复`。我会顺势讲**降级设计**："熔断器跳闸后
降级到规则引擎，这就是 SLA 超时降级路径——真实系统里 provider 会挂，挂了要优雅而不是
500。"

### 4.3 第 3 步（2:00–3:20）异步路径：Run 状态机 + run 级幂等（**核心**）

```bash
# 创建 run → 立即返回 202
RUN=$(curl -s -X POST localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-idem-001' \
  -d '{"query":"我要退款"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["run_id"])')

curl -s -X POST localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-idem-001' \
  -d '{"query":"我要退款"}'     # ← 同一个 key 再发一次

sleep 5; curl -s localhost:8000/api/runs/$RUN | python3 -m json.tool
sleep 20; curl -s localhost:8000/api/runs/$RUN | python3 -m json.tool
```

**预期观察**：

1. 两次创建返回**同一个 `run_id`**，都是 HTTP 202；
2. 第一次轮询：`status: RUNNING`、`attempt: 1`、`max_attempts: 3`、
   `trace_id` 已生成；
3. 第二次轮询：`status: SUCCEEDED`、`finished_at` 非空、`attempt` 仍是 1。

**我会说的话**："第二次请求**没有创建新 run**——这就是 run 级幂等，键是
`(user, endpoint, Idempotency-Key)` 的唯一约束。注意 `attempt: 1`：这次执行只被尝试了
一次就成功了，重试预算完整保留。而状态是从数据库读出来的，不是从队列读出来的——
Celery 的 result backend 被显式关掉了。"

**顺手补一刀（30 秒，很值）**：

```bash
curl -s localhost:8000/api/runs/dead        # → 401，DLQ 查询需要监控鉴权
```

"DLQ 列表需要鉴权，因为它会暴露 run 与错误码——这跟 `/api/chat` 的会话边界是不同的
一条授权线。"

### 4.4 第 4 步（3:20–5:00）崩溃恢复与副作用只发生一次（**最有说服力**）

需要本地 PostgreSQL + Redis（`make runtime-e2e` 的默认地址即可）。

```bash
make runtime-chaos     # 约 15 秒
```

**预期观察**：脚本输出 8 个 step 的结构化证据，`[PASS] runtime-chaos`，artifact 落在
`artifacts/runtime/chaos-<ts>.json`。关键在最后一步的 payload：

```json
{ "step": 8, "action": "side_effect_deduplicated",
  "idem:counter": 1, "tool_invocations": 2, "ledger_hits": 1 }
```

**我会说的话**（这段是整场 demo 的落点）：

> "worker 的**整个进程组**被 SIGKILL 了。任务重投、新 worker 接管了过期 lease、
> **从 checkpoint 的下一个节点续跑**。然后看这三个数：`tool_invocations: 2`——工具
> **真的**被调了两次；`idem:counter: 1`——但副作用**只发生了一次**；`ledger_hits: 1`——
> 第二次被 ledger 挡下来了。
>
> 我特意要求断言 `tool_invocations >= 2`，因为如果只断言计数器是 1，这个测试可能是
> **因为它根本没重试才通过的**——那是我在这个项目里踩过的最贵的坑。"

如果时间还够（+20 秒，可选）：

```bash
TEST_DISTRIBUTED_DB_URL=postgresql://postgres:postgres@localhost:5432/csai_runtime_test \
TEST_REDIS_URL=redis://localhost:6379 \
  python3 -m pytest tests/integration/runtime/test_worker_checkpoint_recovery.py \
                 tests/integration/runtime/test_tool_idempotency.py \
                 tests/integration/runtime/test_hitl_langgraph_interrupt.py -q
```

### 4.5 Demo 的已知坑（**提前知道，别当场翻车**）

这几条是我实测踩出来的，写在这里是为了让 demo 可复现：

| 现象 | 原因 | 处理 |
|---|---|---|
| `POST /api/runs` 返回 500（`table agent_runs has no column named error_type`） | 本地 `data/csai.db` 是旧 schema 且没有 alembic 标记 | 删掉 `data/csai.db` 重启；dev 环境下 `init_db()` 会回退到 `Base.metadata.create_all` 补齐 |
| `make db-upgrade` 在 SQLite 上停在 003 | 003 用了 SQLite 不支持的 `ALTER TABLE ... ALTER COLUMN` | **空白库迁移门禁是 PostgreSQL-only**（`scripts/verify_fresh_db_migration.sh`）。SQLite 只作开发便利存储 |
| `POST /api/chat` 返回 `{"error":"会话令牌无效"}` | 你自带了 `session_id`，服务端要求配套 `session_token` | **省略 `session_id`** 让服务端签发，或带上 token |
| `POST /api/chat` 耗时约 20 秒且返回降级文案 | provider 凭据失效 + Sequential SLA 15 秒 | 当成降级路径讲解；不要解释成"卡住了" |
| `GET /api/runs/dead` 返回 401 | 需要监控鉴权 | 正常；说明授权边界（见 §4.3） |

---

## 5. CI / tests / evidence artifact mapping

### 5.1 CI lane → 命令 → 证据

| CI job（`.github/workflows/ci.yml`） | 跑什么 | 门禁 | 证据 |
|---|---|---|---|
| `test`（matrix Python 3.10 / 3.11 / 3.12） | `tests/unit/` + `tests/integration/` + `tests/e2e/` + `tests/stress/`；coverage 显式列 15 个业务源码目录 | **coverage `fail_under = 80`**（`pyproject.toml`）；`real_llm` lane 仅信息性（`-m "not real_llm"`），`stress` 是阻塞的 | 上传 `coverage.xml` / `pytest-report.xml` / `pytest-counts.json` / `htmlcov/`（保留 14 天）；`scripts/ci_summary.py` 写 job summary 并带 git SHA provenance |
| `dev-compat`（Python 3.10） | 校验 `pytest-asyncio` 下限、`--collect-only`、unit、跨 pytest 8 范围 | 契约断言（声明的下限必须真的能 collect） | — |
| `runtime-e2e`（postgres:15 + redis:7 service container） | `pytest tests/integration/runtime -q`；再跑 `scripts/test_worker_crash_recovery.py` 并断言 `result == "PASS"` | 硬 FAIL（`TEST_DISTRIBUTED_DB_URL` / `TEST_REDIS_URL` 由 job env 注入） | 上传 `artifacts/runtime/`（`runtime-chaos-evidence`） |
| `security` | mypy **strict**（8 个具名模块：blocking，实测 `Success: no issues found in 7 source files`）、mypy **gradual**（既有模块，⚠️ 见下方"已发现的 CI 缺陷"）、`pip-audit`（`continue-on-error`，信息性）、bandit、`scripts/check_secrets.py` + 两个 `grep` 兜底 | strict / bandit / secrets 阻塞；gradual / pip-audit 仅 ⚠️ 不阻塞 | — |
| `build-and-push` → `deploy` | Docker Buildx → GHCR → 生产部署 | 仅 `workflow_dispatch` | — |
| `.github/workflows/secret-history-scan.yml` | 历史提交密钥扫描（定时 + 手动） | — | — |

覆盖率工具链是**显式 pin 住**的（pytest / pytest-asyncio / pytest-cov / coverage 四个
都 pin），并且有一个契约测试强制这行 pin 不许漂移——理由很实在：不 pin 的话同一个
仓库在不同工具链下会给出不同的覆盖率数字，**那个数字就没有证据价值**。

**⚠️ 我自己发现的一个 CI 缺陷（主动交代，不等被问）**：`security` job 里那条
gradual mypy 命令引用了仓库根目录的 `session_manager.py` 和 `drift_detector.py`，
但这两个文件现在在 `core/session/` 下面。所以那条 step 一启动就报
`Cannot read file` 并中止——**它从来没有真正 type-check 过任何东西**。而因为它是
`continue-on-error: true`，CI 里表现为一个 ⚠️ 步骤而不是红灯。

```bash
# 复现：这条命令在当前 main 上直接中止
python -m mypy agents/ cache/ collaboration/ core/ rag/ router/ tools/ api/ \
  auth/ db/ erp/ knowledge/ session_manager.py token_counter.py drift_detector.py \
  --ignore-missing-imports
# → mypy: error: Cannot read file 'session_manager.py': No such file or directory
```

所以关于类型检查我只能诚实地说：**strict lane（8 个具名模块）是阻塞且干净的**；
gradual lane 目前**没有在跑**，修法是把两个路径改成 `core/session/` 下的实际位置
（这是 CI 配置一行修复，不在本次文档整理范围内）。**我不会说"mypy 基本全绿"。**

### 5.2 本地复现命令（全部是 canonical 入口）

```bash
make test                # 全量（-x）
make test-fast           # 跳过 slow
make test-cov            # 带覆盖率
make lint                # ruff

make runtime-e2e         # 真实 PG + Redis 的 runtime 验收（未注入 URL 时是硬 FAIL，不静默 skip）
make runtime-chaos       # SIGKILL 混沌验收 → artifacts/runtime/chaos-<ts>.json
make runtime-verify      # 机读证据 → artifacts/distributed-runtime/<ts>/report.json
make runtime-replay-help # DLQ 人工重放用法

make rag-eval-import          # 导入评测语料（幂等 + gold 覆盖率审计 + manifest）
make rag-eval-649-preflight   # preflight gate（provider auth / Qdrant / BM25）
make rag-eval-649-smoke       # 冒烟（前 16 条，**不是正式证据**）
make rag-eval-649             # 正式 649 全量 4-config ablation → evidence artifact
make eval-rag                 # rag-eval-649 的兼容 alias

make otel-collector-smoke     # 真跑一次 OTLP Collector：启动→发 span→校验→出证据→关闭
make facts                    # 当前 runtime 事实 JSON
make audit-docs               # 文档一致性审计
make openapi-check            # docs/openapi.json 与 app.openapi() 一致
pytest --collect-only -q      # 当前测试数量（唯一权威来源，本文不写死）
npm test                      # 前端测试（web/）
```

### 5.3 artifact → schema → 能证明什么

| Artifact | Schema / 版本 | Provenance 字段 | 能证明 | **不能**证明 |
|---|---|---|---|---|
| `artifacts/distributed-runtime/<ts>/report.json` | `distributed-runtime-evidence/v2` | `tested_code_sha` + `generated_at` + `overall_status` + `checks` | 4 个维度在**真实 PG + Redis** 上 PASS：`checkpoint_cross_process`、`same_thread_serialization`、`different_thread_parallelism`、`tool_idempotency`（payload 里 `side_effect_calls: 1`） | 它**不覆盖**另外两个必需维度：`worker_crash_recovery`（来自 `make runtime-chaos`）与 `retry_recovery`（来自 `tests/integration/runtime/`）。也别把 `overall_status: PASS` 说成生产验证 |
| `artifacts/runtime/chaos-<ts>.json` | ad hoc | step 日志 + `result`，**无 git SHA / 无 schema version** | 8 步崩溃恢复链路，最后一步断言副作用只发生一次 | 这是仓库里**provenance 最弱**的 artifact——按"步骤追踪"读，不要当正式证据引用 |
| `artifacts/runtime/chaos-ci.json`（CI 里生成） | ad hoc | 同上 | CI job 会 `assert report["result"] == "PASS"` | 同上 |
| `artifacts/evaluation/rag-649/preflight-<ts>/report.json` | `rag-eval-evidence/v2` | `git_sha` + benchmark `sha256` + `declared/executed_queries` | preflight gate 的阻塞原因与因果结构 | **preflight 不是正式指标**。`declared_queries: 649` / `executed_queries: 649` 是**探针计数**，不是"649 条正式评测跑完了" |
| `artifacts/evaluation/rag-649/import_manifest_*.json` | — | 数据集 sha256 + gold 覆盖率审计 | 语料导入是幂等且可审计的 | — |
| `artifacts/observability/otel-collector-<ts>/report.json` | `otel-collector-evidence/v1` | `tested_code_sha` + `generated_at` + `status` | 5 个语义 span **真的到达了真实 OTLP Collector**（`otel/opentelemetry-collector:0.162.0`，OTLP gRPC） | **不等于有 trace 后端**：被验证的 Collector 只有 `debug` exporter——不存储、无 retention、无查询 UI、无 dashboard |
| `artifacts/flake-investigation/killgate-<ts>/cycle-*.json` | — | 每 cycle 明细 | 定向复现"提交落在 kill 窗口内"的概率量化 | — |
| `make runtime-e2e` | 无提交 artifact | — | 验收套件在真实基础设施上通过（CI job 是它的证据） | 没有可长期引用的机读 artifact |

**读 artifact 的一条硬规则**：`tested_code_sha` 不是当前 `HEAD` 的祖先，就说明它描述的是
更老的代码——重跑再引用。`artifact_commit_sha` 为 `null` 是**设计如此**（生成时无法预知
自己会被提交到哪个 commit），所以 provenance 只认 `tested_code_sha`。

### 5.4 测试套件分层（数字以命令输出为准，本文不写死）

| 层 | 位置 | 外部依赖 | CI 是否阻塞 |
|---|---|---|---|
| 纯契约 / 配置单测 | `tests/unit/`（含 `test_distributed_runtime.py`、`test_runtime_architecture_contract.py`、`test_execution_mode_contract.py`、`test_runtime_metrics_contract.py`、`test_crash_recovery_kill_gate.py`、`test_agent_run_runtime.py`） | 无 | 是 |
| 文档一致性 | `tests/unit/test_doc_consistency.py` | 无 | 是 |
| 真实基础设施集成 | `tests/integration/runtime/`（15 个测试文件：checkpoint 跨进程、run 语义与 retry/DLQ、worker 崩溃恢复、worker ownership CAS、tool 幂等、tool 幂等指标、claim 并发、SSE checkpoint 序列化、run 事件流、事件投递语义、queue-worker 解耦、HITL 审批流、HITL LangGraph interrupt、retention/reconciler、checkpoint 与 session 删除） | 真实 PG + Redis；未注入 URL 时 skip，经 `make runtime-e2e` 时是硬 FAIL | 是（`runtime-e2e` job） |
| 业务集成 / E2E（mock LLM） | `tests/integration/`、`tests/e2e/` | 无（MockLLM） | 是 |
| 真实 LLM E2E | `tests/e2e/test_e2e_real_llm.py` | `OPENAI_API_KEY` | 否（`real_llm` 仅信息性） |
| 压力 | `tests/stress/`（`@pytest.mark.stress`） | — | 是 |
| RAG 基准资产 | `tests/eval/rag_benchmark.json`（metadata 与 `len(queries)` 由 `check_benchmark_metadata` 强制一致） | — | — |
| 前端 | `web/src/__tests__/`（Vitest） | — | `npm test` |

---

## 6. 项目明确未完成的边界

> 这一节的原则：**主动列出缺口，比被追问出来强得多**。每条都写"缺什么 + 为什么缺 +
> 我怎么验证我确实知道它缺"。

### 6.1 分布式 Agent Runtime（Level 3，全部 `NOT_VERIFIED`）

| 未验证项 | 说明 |
|---|---|
| 真实生产集群 | 从未在生产环境部署运行过。全部证据来自本地/CI 的真实 PG + Redis |
| 多副本长期稳定性 | 分钟级并发验证 ≠ 天级。长时间内存/连接池/锁竞争没有数据 |
| 真实用户流量 / 真实 QPS / P50/P95/P99 | `NOT_MEASURED`。CI 里测到的延迟不代表生产 |
| 大规模 queue backlog | 没有积压恢复测试，没有 backpressure / admission control |
| 真实 ERP 写操作 | 没有企业 staging，ERP 是 Mock 适配器（工厂和接口抽象就绪） |
| K8s autoscaling / HPA | 未实现。当前部署形态是 Docker Compose，`deploy/compose/` 下 7 个编排文件（base / prod / override / canary / scale / monitoring / 可观测性栈变体） |
| multi-region | 未实现 |
| worker pool 弹性 / worker pool autoscaling | 未实现 |
| **fencing token** | 已实现 owner CAS（stale worker 不能提交 AgentRun 状态），**没有** fencing token，也没有强制中止：pause 超 TTL 的旧 worker 不会被 abort，其外部节点副作用仍依赖 ledger 幂等 |
| Redlock 集群 | 只有单 Redis per-thread 锁 |
| DLQ 主动告警 / 运维闭环的通知侧 | `GET /api/runs/dead` + 重放 CLI 已有；**主动通知（Webhook/邮件）没有** |
| Run 事件流的强语义 | best-effort resumable，**不是** exactly-once：较旧 `Last-Event-ID` 重连会重放已处理事件；`replay=false` 与 idle 超时会造成缺口；`MAXLEN` 近似裁剪后的历史不可恢复，且近似裁剪不是硬上界 |

### 6.2 RAG

- **649-query 正式指标（Hit@K / Recall@K / Precision@K / NDCG@K / MRR@K）：`NOT_VERIFIED`。**
  状态由 `scripts/rag_evidence_status.py` 从 artifact 动态推导，不是手写声明。
- 语义缓存命中率、缓存各层访问延迟：`NOT_MEASURED`。设计目标区域的重复占比估计
  **不是**实测命中率；而且收益语义是"跳过 Router/Agent/LLM 链路"，
  **不等于**任何具体的延迟数字——延迟收益本身也是 `NOT_MEASURED`。
- 真实 embedding / reranker provider 的稳定调用：**取决于凭据**。已提交的 preflight
  artifact 记录过 `EMBEDDING_PROVIDER_AUTH` 阻塞（HTTP 401）；它是**根因**，
  `VECTOR_INDEX_EMPTY` 是 downstream 症状（`caused_by` 指回根因，因为 BM25 索引由
  Qdrant 重建，连 `bm25_only` 也被它阻塞）。`RERANKER_PROVIDER_AUTH` 是
  `blocking: false`，**只**阻塞 `hybrid_rerank`——不能据此说其它实验也被 reranker 阻塞。
- 只有一个检索语料（化妆品域），没有跨域 / 多语言 / 长文档压力的评测。

### 6.3 Observability

- 应用语义 tracing 本身：`IMPLEMENTED` / `LOCALLY VERIFIED`（span 语义
  `csai.agent.execute`、`.execute.resume`、`csai.rag.retrieve` + `rag.stage.*`、
  `csai.llm.chat_completion`、`csai.tool.execute`；属性白名单 + 敏感词过滤，OTel 失败
  降级为 no-op）。
- 真实 OTLP Collector 传输链路：`LOCALLY VERIFIED`（`make otel-collector-smoke`）。
- **持久化 / 可查询 trace 后端（Jaeger / Langfuse / Tempo 等）：`NOT_VERIFIED`。**
  「Collector 收到了 trace」**不等于**「有 trace 后端」。
- **生产 trace 传播 / 真实流量：`NOT_VERIFIED`。** 不得写成 "production-ready tracing"。
- Collector 那一腿**不覆盖**真实 Agent → RAG → LLM → tool 全链路（smoke 直接发 span，
  避免为此拉起真实依赖）；call-site 接线由单测单独覆盖。
- 审批前后两段**不承诺**是同一个 span（用 `run_id` / `approval_id` 关联）。

### 6.4 明确不在 `main` 上的东西（本文不宣称）

```bash
git ls-tree main --name-only tools/ | grep -c mcp     # → 0
```

- **MCP 外部工具接入**只存在于工作分支（`feat/mcp-tool-adapter-land`），**未进入
  `main`**。因此 §7.8 里关于 read-only-first 的答案是**该分支的设计立场**，
  证据等级是 Level 1 `IMPLEMENTED`（纯函数契约经单测运行验证），
  **跨进程 / 传输 / 策略 / 时序的端到端取证是 `NOT_VERIFIED`**。
  在它合并并跑出真实结果前，我不会把"MCP 端到端可用"当作已实现能力讲。
- 同理，`main` 上不存在 `MCP_ENABLED` 这个配置面——它是分支引入的。

### 6.5 HITL

见 §3.3 的 Tradeoff：无主动通知链路、审批 SLA `NOT_MEASURED`、`HITL_ENABLED` 默认
`false`、快路径不在边界内、真实 ERP 写操作 `NOT_VERIFIED`。

### 6.6 工程债（明确知道、明确没做）

- **类型检查**：strict lane（8 个具名模块）是阻塞且干净的；**gradual lane 目前没在跑**
  ——CI 那条命令引用了已迁移的根目录路径，一启动就 `Cannot read file` 中止，又因为
  `continue-on-error: true` 不阻塞。详见 §5.1 的复现命令。
  **我不宣称 "mypy 全绿"，也不报任何 mypy error 数量**（那条命令跑不出有效数字）。
- **Ruff 全仓格式化**会带来约 100 个文件的纯格式改动，我选择不做（噪音 > 收益）。
- 前端 CSP：`script-src` 已用 nonce 去掉 `unsafe-inline`，但 `style-src` 仍留着
  `unsafe-inline`（主题切换 / 动态样式需要），还没收敛到 CSS 变量。
- 密码哈希已从 PBKDF2 升到 Argon2id（主要收益是 memory-hard 属性），但**提速倍数
  没有做基准测试**，所以不报数字。

---

## 7. 面试追问

### 7.1 为什么用 LangGraph，而不是 LangChain Agent 或自己写状态机？

三个理由，按重要性：

1. **控制流可声明、可检查**。`StateGraph` 的节点和条件边是数据结构，不是运行时黑盒。
   我需要精确控制"什么时候切哪种协作模式"——多 Agent 系统里这是刚需，不是加分项。
2. **checkpoint 是框架能力，不是我自己造的**。官方的 checkpointer 抽象给了我
   "图状态快照 + 从 `next` 续跑"这件事，而且换后端（内存 / PostgreSQL）只改一个配置。
   自己写状态机就得自己实现这一整套，而且大概率实现错——**P1 那个 bug 就是框架语义
   用反了**，不是自己写能避免的。
3. **可观测性**。状态可以直接 dump，节点边界就是天然的 span 边界和幂等边界。

**我会主动补的代价**：LangGraph 的 interrupt / resume 语义是**版本相关**的实测行为，
不是稳定契约。我把实测结论写进了 `runtime/bootstrap.py` 的 docstring 并钉了回归测试
（`tests/integration/runtime/test_hitl_langgraph_interrupt.py`），因为我知道它会变。
**框架给的是能力，不是保证。**

### 7.2 为什么 checkpoint 用 PostgreSQL？

因为它要跨**进程**和跨**重启**存活，而且要多副本共享。

- **不用内存**：多开一个 Gunicorn worker 状态就分片，重启全丢——这是我接手时的原始状态。
- **不用 Redis**：checkpoint 的写入模式和读放大（每次节点转移都要读写整个 channel
  快照）不适合当作唯一的执行真相源；而且 Redis 已经是锁 / session / broker / 事件流
  的依赖，再叠一个就变成单点。
- **用官方 `AsyncPostgresSaver`** 而不是自己实现 saver：checkpoint 表由官方 saver 自管，
  不与业务 SQLAlchemy Base 耦合——这样 LangGraph 升级不会和我的业务迁移打架。

**关键的失败语义**：生产环境 checkpoint 初始化失败是 **fail closed**，**绝不静默回退
`MemorySaver`**。理由：静默回退意味着你以为在跑可恢复的执行，实际跑的是"重启即丢"，
而且**你不会收到任何信号**。静默降级比启动失败危险得多。

**唯一真相源仍然是 `agent_runs`**：checkpoint 说"图执行到哪了"，`agent_runs` 说"这次
业务运行处于什么状态、能对外承诺什么、能不能重投"。checkpoint 能让执行续跑，但它不能
替代业务状态机——这是我 P1 里花最多时间才想清楚的一件事。

### 7.3 为什么锁用 Redis？

因为要挡的是**跨进程 / 跨副本的并发写同一个会话**，而进程内的 `asyncio.Lock` 在多副本
下毫无作用。

三个设计点，每个都对应一个真实缺陷：

1. **owner token**：锁的值是持有者身份，不是 `1`。释放时必须比对 owner。
2. **TTL**：owner 崩了锁不会永久泄漏。
3. **Lua 原子 compare-and-delete**：释放必须是"比对 + 删除"一个原子操作。
   `SETNX` 然后 `DEL` 会误删别的 worker 刚拿到的锁——这是分布式锁最经典的 bug。

**执行期续租**：`_heartbeat_loop` 在任务执行期间持续续 Redis 锁 TTL 和 DB ownership
lease，把"owner 因为 GC / 宿主机卡顿 pause 超过 TTL"的窗口压到很小。

**我明确知道的缺口**：**没有 fencing token**。owner token + TTL 能防"旧 owner 误删新锁"，
但防不住"旧 owner 恢复后继续跑、和新 owner 同时写同一个 thread"。严格解法是单调递增的
fencing token 或数据库版本号校验，让旧持有者的写入**无条件被拒**。我做了 owner CAS 把
AgentRun 状态那一半堵住了（`run_id + status + worker_id + lease` 同一条 UPDATE 判定），
但**外部副作用那一半还依赖 ledger 幂等**。只上单 Redis，不做 Redlock 集群。

### 7.4 为什么是 Celery？

因为我要的是 **at-least-once + 长任务 + 崩溃重投 + 水平扩容**这四件事同时成立，
而我需要它们在我熟悉的运维模型里。

具体配置理由：

- `task_acks_late` + `task_reject_on_worker_lost`：worker 死在没 ACK 的任务会被重投，
  而不是丢。
- Redis `visibility_timeout`：未 ACK 的任务重新可见。生产启动时强制校验它大于任务
  time limit，否则 fail-fast。
- `task_ignore_result=True`：**主动关掉** result backend 作为状态来源——它不是真相源。
  留着一个"看起来能用但会丢"的状态源，比一开始就明确它不可信更危险。
- 应用级 dead-letter 表（`agent_dead_letters`），**不是** broker-native DLX。理由：
  我要不可变历史（run_id / attempt_count / error_type / error_code / entered_at）
  加一个可查询接口，broker 的 DLX 给不了这些。

**代价**：Celery 的 at-least-once 是**特性不是缺陷**，但它意味着我必须自己解决幂等——
这就是 P2 那三层设计的直接原因。如果重写，我不确定能在可接受的时间内做到同等运维成熟度。

**快路径为什么不用 Celery**：客服问答要低延迟。走队列 = 多一次序列化 + 一次网络跳转 +
一次可能的调度延迟。所以我**保留**了 inline 快路径，只把长任务异步化。这是有意的
双路径，不是架构不统一。

### 7.5 为什么要做幂等？（既然我可以声称 exactly-once）

**因为 exactly-once 在分布式系统里做不到，而声称做到了比不做幂等更危险。**

我的选择是：**明确声明 at-least-once，然后把幂等做实**。三层各挡一类重复：run 级
（重复创建）、thread 级（并发写同一会话）、工具级（重复副作用）。

关键是我能讲清**幂等的边界**：工具级 ledger 只保证**同一个 Agent 不重复发起同一副作用**。
如果下游 ERP 需要端到端幂等，得**下游 API 接受 idempotency key**——这不是我这边能单方面
保证的。这种"我知道我的保证到哪为止"本身就是幂等设计的一部分。

**一个反直觉的教训**（我一般会主动讲）：幂等键的**作用域选错了，比没有幂等更危险**。
DLQ 重放时新建 run → `run_id` 变了 → `operation_key = run_id:tool_call_id` 变了 →
幂等静默失效 → 已成功的退款被执行第二次。修法是把"重放必须复用原 `run_id`"变成接口
不变量，而不是靠人记得。

### 7.6 为什么做 HITL？（这不是"加个审批弹窗"）

因为**高风险副作用的授权决策必须跨进程重启存活、可审计、且不能被 LLM 自己绕过**。

三个把它变成分布式系统问题的理由：

1. **审批跨越进程生命周期**。审批人可能第二天才处理，worker 早被回收了。所以审批记录
   必须 durable（`human_approvals` 表），图必须在 checkpoint 处挂起，run 必须有一个
   能表达"在等人"的状态——这就是 `WAITING_APPROVAL` 存在的理由。
2. **等待不该被计为失败**。如果"等人"消耗 `AGENT_RUN_MAX_ATTEMPTS`，人等三小时会被算成
   三次失败，最后进 DLQ。所以 `WAITING_APPROVAL → RUNNING` 走
   `mark_resumed_running()`，**不递增 attempt**；而且 `WAITING_APPROVAL` **不进**
   `EXECUTABLE_STATUSES`，否则通用轮询会把它反复捞起变忙循环。
3. **审批 ≠ 幂等，两条防线缺一不可**。审批防"不该做的被做了"；ledger 防"做了一次被
   重做"。只做审批，重试仍会重复扣款；只做幂等，LLM 可以自己批准自己的高风险调用。

**边界我会主动说**：快路径 `/api/chat` 没有 run 上下文，**明确不在**这个治理边界内；
`HITL_ENABLED` 默认 `false`；无主动通知链路；**真实 ERP 写操作 `NOT_VERIFIED`**（我验证
的是治理机制，用确定性 staging 工具，不是 ERP 集成正确性）。

### 7.7 失败时为什么选 fail closed？

一句话：**静默降级比启动失败危险得多**，因为静默降级让你在错误的世界里继续运行而不
知道。

我在四个地方刻意 fail closed：

| 位置 | fail closed 的行为 | 静默回退的灾难 |
|---|---|---|
| 生产 checkpoint 初始化 | 启动失败 | 以为在跑可恢复执行，实际"重启即丢"，且无任何信号 |
| 生产 Redis session | 启动失败 | 多副本会话分片，用户看到别人对话 |
| `HITL_APPROVAL_TTL_SECONDS` 到期 | 落 `EXPIRED`，**按拒绝处理** | 无人处理的高风险退款被**自动放行** |
| 风险分级判定失败 | 收敛到 `HIGH`（挂起） | 高风险工具被当低风险直接执行 |

第四行是最典型的：**判定逻辑出 bug 时，危险方向永远是"多问一次"，不是"少问一次"**。

`make runtime-verify` 也是同一个哲学：exit `0` 只在所有检查 PASS 时，`1` 在任一 FAIL，
`2` 在 `NOT_RUN` / `PARTIAL`。**"没跑"被刻意不算成功**——把没跑当成成功是证据污染。

### 7.8 为什么 MCP 走 read-only-first？

> **前置声明**：这一节描述的是**工作分支**（未进入 `main`）的设计立场，证据等级
> Level 1 `IMPLEMENTED`（纯函数契约经单测运行验证），**端到端 `NOT_VERIFIED`**。
> 见 §6.4。

核心理由：**MCP 不构成新的安全边界，它只扩大了不可信输入的来源。**

1. **外部 MCP server 是不可信输入**。它可能返回恶意 schema、超大响应、伪装成只读的
   写操作。所以 MCP 工具**不另立一套风险词汇表**——沿用 `core.hitl.risk.RiskLevel`，
   这样 HITL 治理不需要为 MCP 再写一遍。
2. **只注册显式声明 `risk_level: "low"` 的 server 的工具**。缺失 / 非法一律**只向上**
   收敛到 `HIGH`，**绝不 fail-open**。`medium` / `high` 一律不注册。
   这里的关键判断是：面对"我不知道这个工具安不安全"，唯一安全的答案是"当作不安全"。
3. **不采信 server 自述的 `annotations`** 来决定风险等级。一个声称自己是只读的工具，
   恰恰是最需要被当作不可信的时刻。
4. **双 allowlist**：`MCP_SERVERS` 是 JSON 数组 allowlist（空 = 不允许任何 server），
   `allowed_tools` 为空同样等于不允许任何工具。默认拒绝。
5. **叠加而非替换**：MCP 工具叠加进**同一个** `ToolRegistry`，同名时**跳过**、绝不覆盖
   native 工具。已有的 ERP / RAG / 内建工具行为不变。

**我明确知道的缺口**：写操作 MCP 工具**未接入**（它缺幂等 ledger + 人工审批这两道防线，
和 §6.1 的结论是同一条纪律）；RBAC 是**请求级**而非 per-tool，所以我不会说"MCP 工具
经过了 RBAC"；响应侧结果大小当前**不设上限**（只限请求 payload 字节）。

### 7.9 怎么评估 RAG？——**当前我不会报任何指标**

分三层讲，顺序很重要：**机制 → 评测设计 → 证据边界**。

**机制（链路）**：query rewrite / filter → 向量检索 + BM25 → retrieval contract
（`rag/retrieval_contract.py`，7 个阶段）→ RRF 融合（k=60）→ reranker 重排 → context。
Embedding 走 HTTP API 在应用侧计算，Qdrant 只做存储检索。

**评测设计**：
- 语料：`tests/eval/rag_benchmark.json`，649 条 query，`metadata.total_queries` 与
  `len(queries)` 由机器 guard 强制一致。
- **4 个检索配置做 ablation**：`vector_only` / `bm25_only` / `hybrid_no_rerank` /
  `hybrid_rerank`。
- **multi-K 指标**：Hit@K / Recall@K / Precision@K / NDCG@K / MRR@K，K ∈ {1,3,5,8}。
- **三套 population，运行时动态计算，禁止硬编码分母**：`all_queries`（主口径，
  end-to-end）/ `retrieval_eligible` / `full_gold_covered`。分母口径不同能差出一整档
  数字——**只报一个数字而不说分母是不诚实的**。
- **失败按分类记账**（failure taxonomy），这样"指标低"能落到"是 gold 没进索引，还是
  检索全错，还是 provider 挂了"。
- **artifact 带 git SHA + 数据集 sha256 + 显式状态**（`rag-eval-evidence/v2`）。

**证据边界（我会主动说）**：**当前正式指标是 `NOT_VERIFIED`。** preflight gate 现在能
挡住 provider 认证失败、Qdrant 索引为空、BM25 未建这些问题——**能挡住失败，但过闸之后
还要跑满 649 条才会产生正式指标**。

我还踩过一个语义坑，值得讲：一次 preflight artifact 里同时有
`EMBEDDING_PROVIDER_AUTH`（根因，401）和 `VECTOR_INDEX_EMPTY`（索引 0 points）。
一开始我准备把两者并列为"两个 blocker"，后来加了机器 guard
（`scripts/audit_doc_consistency.py::check_reranker_blocker_semantics`）强制区分
**根因 / downstream 症状 / 非阻塞**——因为把症状当根因，会导致去修错的东西。
`caused_by` 字段就是为此存在的。同理 `RERANKER_PROVIDER_AUTH` 是 `blocking: false`，
只阻塞 `hybrid_rerank`，不能拿它去解释其它配置的失败。

### 7.10 怎么 trace 一个 Agent？

**先说结论：我能 trace 到"哪一步、哪个 stage、多慢"，但我还没有能查询的 trace 后端。**

三层分开说：

1. **应用语义 tracing**（`core/telemetry.py` + `core/tracing.py`）：span 语义是
   `csai.agent.execute` / `csai.agent.execute.resume`（worker 侧）、
   `csai.rag.retrieve` + `rag.stage.*` 事件（rewrite / filter / vector / bm25 /
   fusion_rrf / rerank / final）、`csai.llm.chat_completion`、`csai.tool.execute`。
   属性走**白名单** + 敏感词过滤：raw prompt、用户原文、召回文档、工具参数、PII、凭据
   一律不进 trace。**任何 OTel 失败都降级为 no-op 且不改变业务语义**——可观测性不能
   成为业务可用性的风险来源。
2. **传输链路**：`make otel-collector-smoke` 会真拉起
   `otel/opentelemetry-collector:0.162.0`（OTLP gRPC），发 span、flush、校验到达，出
   `otel-collector-evidence/v1` artifact。这一腿是 `LOCALLY VERIFIED`。
3. **持久化 / 可查询后端**：`NOT_VERIFIED`。被验证的 Collector 只有 `debug` exporter
   ——不存储、无 retention、无查询 UI、无 dashboard。**「Collector 收到了 trace」不等于
   「有 trace 后端」**，这两句话我从不混用。

另外两个我会主动说的边界：Collector 那一腿**不覆盖**真实 Agent → RAG → LLM → tool
全链路（smoke 直接发 span，避免为此拉起真实依赖；call-site 接线由单测单独覆盖）；
**审批前后两段不承诺是同一个 span**（用 `run_id` / `approval_id` 关联）——因为中间隔着
人的时间，把它算成一个 span 会得到一个没有意义的 duration。

除了 trace，RAG 链路还有一条独立的 retrieval trace：各 stage 的 latency 是**实测**的，
不是从总延迟推算的。

### 7.11 CI VERIFIED 和"生产验证过"差在哪？

差三样东西，而且都是**质的差别**，不是程度差别。

| | CI VERIFIED（本项目现状） | 生产验证过（**未达到**） |
|---|---|---|
| **环境** | CI 的 postgres:15 + redis:7 service container；本地手工起的 PG + Redis | 真实生产集群：真实数据量、真实网络、真实配置漂移 |
| **时长** | 分钟级 | 天 / 周级。**多副本长期稳定性只有长时间才暴露**（内存增长、连接池、锁竞争、慢查询） |
| **流量** | 合成请求 + 确定性 staging 工具 | 真实用户流量、真实长尾、真实 ERP 写操作 |

所以我能说、也只应该说：

> "分布式 Runtime 的核心不变量——checkpoint 跨进程恢复、同会话串行、跨会话并发、
> 崩溃后从 checkpoint 续跑、副作用只发生一次、retry 与 DLQ 闭环——在**真实 PostgreSQL
> + Redis + 真实多进程 Celery** 上验证通过，证据是可复现命令 + 带 git SHA 的机读
> artifact。**真实生产集群、多副本长期运行、真实 ERP 写操作我没有验证过。**"

**为什么 Level 2 不等于 Level 3**（这是我认为最值得讲的一点）：CI 验证的是
**机制在受控环境下的正确性**；生产验证要回答的是**机制在真实世界的退化条件下是否还
成立**。这两件事需要完全不同的证据类型。前者可以自动化、可以每次提交跑；后者需要时间、
需要真实负载、需要真实的下游系统——**它本质上不能被 CI 替代**。

**同一套纪律我用在了 RAG 上**：649-query 正式指标是 `NOT_VERIFIED`，因为没有带
provenance 的 artifact。分布式 runtime 有 artifact 所以是 CI VERIFIED；RAG 的正式评测
没跑出 artifact 所以是 NOT_VERIFIED。**等级不是按"这个功能重不重要"给的，是按"有什么
证据"给的。**

---

## 8. 自检清单（面试前 5 分钟跑一遍）

```bash
python3 scripts/project_facts.py          # 事实与数字
python3 scripts/audit_doc_consistency.py  # 文档纪律（含生产级措辞 / 状态机完整性）
python3 scripts/generate_openapi.py --check
pytest --collect-only -q | tail -2        # 测试数量（不要背历史数字）
```

然后口头过一遍这 6 条——**这 6 条答不上来，就不该在面试里提对应能力**：

1. 你的证据等级是什么？（→ CI VERIFIED，不是生产验证）
2. 哪个数字你有 artifact？（→ 只有 `make runtime-verify` / `runtime-chaos` /
   `rag-eval-*` preflight / `otel-collector-smoke` 产出的那几个）
3. 你的幂等保证到哪为止？（→ 只到 Agent 侧；端到端要下游接受 idempotency key）
4. 你没做什么？（→ §6，照着念，别用"基本完成"含糊过去）
5. RAG 指标多少？（→ `NOT_VERIFIED`，只讲方法论）
6. 快路径受 HITL 保护吗？（→ **不受**，它没有 run 上下文）
