# Architecture Walkthrough — 架构逐问

> 面试用。每个回答都落到 `file:line`，并且区分**已验证**与**未验证**。
> 证据等级定义见 [production-evidence.md](../evaluation/production-evidence.md)。
> 交叉引用：[source-map.md](source-map.md)（问题 → 源码 → 测试 → 证据）。

---

## Q1. 这个项目解决什么问题？

化妆品生产/销售企业的多智能体客服系统：接入 ERP（订单/库存/客户）、检索产品与成分
知识库、处理售后与退款，并把一次对话拆给不同职责的 Agent 协作完成。

- 9 个 Agent（售前/订单/物流/售后/账单/产品/客户/投诉/升级），5 种协作模式：
  `collaboration/`
- 四层状态机：缓存 → 路由 → 协作 → 响应后处理：`core/graph_builder.py`
- 在线入口：`POST /api/chat`（快路径，SSE）；`POST /api/runs`（durable 异步 run）

**规模必须按实测说，不要按印象说：**

```bash
pytest --collect-only -q          # 用例数以当前输出为准
python3 scripts/project_facts.py  # 路径数 / 基准查询数等 runtime 事实
```

---

## Q2. 为什么需要 RAG？

客服要回答「透明质酸对敏感肌有什么功效」「这个订单的物流为什么没更新」这类问题。
两类知识来源性质完全不同：

| 来源 | 例子 | 为什么不能只靠 LLM |
|---|---|---|
| 私有事实 | 订单状态、库存、客户资料 | LLM 不知道，且必须实时准确 |
| 领域知识 | 成分功效、政策条款 | LLM 可能"编"出一个听起来合理但错误的功效 |

RAG 的作用是把**可核查的证据**放进 prompt，让回答可以引用来源。
本项目的额外约束是 **fail-closed**：embedding 不可用时**不**退化成假向量检索
（`rag/embedding_status.py`，PR #12），因为假向量检索会返回"看起来相关但完全无关"
的文档，比明确失败更危险。

---

## Q3. 为什么使用 LangGraph？

不是"LangGraph 很好"，而是三个具体需求：

1. **多 Agent 协作需要显式图**：Sequential / Parallel / Consultation / Hierarchical /
   ReAct 五种模式本质是有向图（`collaboration/`）。
2. **需要人工审批挂起与恢复**：HITL 要求图能在副作用前 `interrupt()`、等外部决策
   `Command(resume=...)` 恢复。裸 asyncio 流程要自己实现这套挂起语义。
3. **需要 per-super-step checkpoint**：崩溃恢复要求"已完成节点不重跑"，这需要框架
   提供每步的持久化边界。

同时必须知道 LangGraph 1.2.x 的两个实测语义（写在 `runtime/bootstrap.py` docstring）：

```
ainvoke(None, cfg)   -> 从 checkpoint 的 next 继续，不重跑已完成节点
ainvoke(state, cfg)  -> 从 START 重新执行，用入参覆盖 channel 值
```

崩溃恢复必须用 `ainvoke(None)`。传 state 会退化成"从头重跑"，把已完成的节点重复执行，
其副作用会被重复触发。**并且** `ainvoke(None)` 不能解除 interrupt —— 审批恢复必须显式传
`Command(resume=...)`。这两个语义是实测出来的，不是从文档推的。

**代价**（诚实说明）：LangGraph 的 super-step checkpoint 由 `BackgroundExecutor`
**不阻塞地**提交（`langgraph/pregel/_loop.py`：`# save it, without blocking`）。
这意味着"节点已开始执行"**不蕴含**"上一步 checkpoint 已持久化"。
本项目的 flaky test 根因正是这条语义（见 [runtime-deep-dive.md](runtime-deep-dive.md)）。

---

## Q4. 为什么 `/api/chat` 和 durable async run 并存？

因为它们的**正确性要求**不同，不是因为重复。

| | `POST /api/chat`（快路径） | `POST /api/runs`（durable 异步） |
|---|---|---|
| 执行位置 | 进程内 inline，不经 worker | Celery worker |
| 落 AgentRun 表 | **否** | 是 |
| 崩溃恢复 | 无（进程死了就没了） | 有（checkpoint + 幂等 + 重投） |
| 长任务 | 不适合 | 适合（ERP 全量分页、批量工单） |
| 人工审批边界 | **不在边界内** | 在边界内 |

选型判据：**如果这次执行会产生不可逆的副作用，或者需要跨重启存活，就走 `/api/runs`**。
快路径牺牲了持久性换取延迟，它的可接受性建立在"失败等于用户重试，且没有不可逆副作用"
这个前提上——而这个前提对退款、改单不成立。

这是一个**明确的取舍**，不是妥协：见 [failure-and-tradeoffs.md](failure-and-tradeoffs.md) §快路径的代价。

---

## Q5. PostgreSQL、Redis、Celery 各负责什么？

| 组件 | 职责 | **不**负责 |
|---|---|---|
| PostgreSQL | 业务状态真相源（`agent_runs`）；审批记录（`human_approvals`）；副作用 ledger（`tool_side_effects`）；DLQ（`agent_dead_letters`）；LangGraph checkpoint（`checkpoints` / `checkpoint_writes` / `checkpoint_blobs`） | 不做锁服务 |
| Redis | Celery broker；per-thread 分布式锁（`agent:thread-lock:*`，owner token + TTL + Lua CAS）；run 事件流（`agent:run:{id}:events`，**观测通道**）；Session 存储 | **不是 canonical state** |
| Celery | 投递与调度；worker 进程生命周期；ACK 时机 | 不是真相源（`task_ignore_result=True`，见 `runtime/celery_app.py`） |

一句话：**业务真相在 PostgreSQL，Redis 只做协调，Celery 只做搬运。**

---

## Q6. 谁是 durable truth source？

**`agent_runs` 表，唯一真相源**（`db/models.py::AgentRun`）。

理由不是"数据库可靠"，而是它同时满足三件事：
1. 状态迁移是**数据库层的原子条件更新**（`runtime/run_service.py`），防并发竞态；
2. 终态可查——API 直接读它回答"这个 run 怎么样了"；
3. 跨进程、跨副本、跨重启一致。

Celery result backend **刻意不启用**（`task_ignore_result=True`）。原因：
result backend 会过期、会丢、且它的成功/失败语义与业务成功/失败不是一回事。
把"任务跑完了"当成"业务成功了"是常见事故源。

---

## Q7. checkpoint 和 AgentRun 有什么区别？

这是最容易混淆的一对，必须能分清：

| | LangGraph checkpoint | AgentRun |
|---|---|---|
| 记什么 | 图的 channel 值 + pending task | 业务状态机（QUEUED/RUNNING/…）+ attempt + worker_id + result |
| 谁写 | LangGraph 的 `AsyncPostgresSaver` | `runtime/run_service.py` |
| 粒度 | 每个 super-step | 每次执行尝试 |
| 能回答 | "图跑到哪一步了" | "这个 run 现在什么状态、谁在执行、第几次尝试" |
| **不能**回答 | "有没有人取消过""失败了会不会重试" | "图的 channel 值是什么" |

**为什么不能合并成一个**：两者的生命周期和所有权不同。checkpoint 由框架拥有、按 thread
组织；AgentRun 由业务拥有、按 run 组织。一个 thread 可以有多次 run；一次 run 可以跨
多个 checkpoint。合并会导致要么被框架 schema 绑死，要么业务状态被框架生命周期绑架。

崩溃恢复需要**两者一起**：checkpoint 告诉你"从哪继续"，AgentRun 告诉你"这次执行算第几次、
失败了要不要重试、重试用尽怎么办"。

---

## Q8. 为什么选择 at-least-once？

因为 exactly-once 在跨系统边界上**做不到**，假装做到比诚实承认更危险。

Celery + Redis 提供的原语是：`task_acks_late=True` + `task_reject_on_worker_lost=True` +
`visibility_timeout` 重投（`runtime/celery_app.py`）。这意味着**同一个 run 可能被多个
worker 先后执行**。这套配置是 Celery 官方推荐的 crash-safe 组合。

另外 `task_acks_on_failure_or_timeout=False` 是本项目刻意设的：Celery 默认值是 True
（失败即 ACK），那会让"让异常逃逸以触发 redelivery"的策略完全失效——run 会停在
RETRYING 等一条**永远不会到的消息**。

**代价**：重复消费和重复副作用必须由业务层处理。这就是下面两问。

---

## Q9. 怎么处理重复消费？

三层，前两层管"不要重复执行"，第三层管"重复了也别出错"：

1. **run 级 — 原子领取**：worker 领取任务时用带条件的原子更新
   （`runtime/repository.py::mark_running`）。别人持有有效 lease 时返回 `None`，
   当前 worker 直接跳过。这利用的是"领取"和"标记 RUNNING"必须是同一个原子操作。
2. **thread 级 — 分布式锁**：`runtime/thread_lock.py`，Redis key
   `agent:thread-lock:{thread_id}`，owner token + TTL + **Lua 原子 compare-and-delete**
   （只有 owner 能解锁，避免 worker A 误删 worker B 后来拿到的锁）。
   同 thread 串行、跨 thread 并发（`tests/integration/runtime/test_run_semantics.py` 验证）。
3. **失败重投的幂等**：终态 run 被重复投递是 no-op；`idempotency_key` 唯一约束。

---

## Q10. 怎么处理重复副作用？

这是本项目最核心的设计。**审批通过 ≠ 可以重复执行。**

三层防线，缺一不可：

| 层 | 机制 | 位置 | 防的是什么 |
|---|---|---|---|
| run 级 | 原子领取 + 终态 no-op | `runtime/repository.py` | 同一 run 被两个 worker 同时执行 |
| 线程级 | Redis per-thread 锁 | `runtime/thread_lock.py` | 同一 thread 的并发写 |
| **工具级** | `tool_side_effects` ledger | `runtime/side_effects.py::claim` | **同一笔退款被执行两次** |

ledger 的关键语义（`runtime/side_effects.py`）：

- `operation_key` 默认 `run_id:tool_call_id`（无显式 key 时对 `tool_name + arguments` 取哈希）；
- `claim()` 是原子的：创建 PENDING 行 + 判定可执行 + 拿到 `CLAIM_EXECUTE` 在一次操作里；
- 已有 `SUCCEEDED` 的 `(tool_name, operation_key)` **直接返回命中，不再执行**；
- 已被别人持有且未过期 → `CLAIM_IN_PROGRESS`；
- lease 过期允许接管（worker 崩溃后不会永久卡死）。

**验证强度**（这是可以放心讲的部分）：

- `test_side_effect_claim_concurrency.py`：8 个线程并发 claim 同一 key → 恰好 1 个
  `CLAIM_EXECUTE`，其余 7 个 `CLAIM_IN_PROGRESS`；
- `test_tool_idempotency.py`：真实 worker 被 SIGKILL 后重投，副作用**仅执行一次**；
- `test_tool_idempotency_metric.py`：ledger 命中时 Prometheus 计数器递增；参数冲突时
  抛 `PermanentError`（而不是静默复用）。

**诚实边界**：ledger 保证的是"**至多一次生效**"（at-most-once effect），不是 exactly-once
执行。如果一个工具的 handler 本身有非幂等副作用而 ledger 记不住它（比如外部系统的
写入不在 key 的覆盖范围内），仍需要该工具自己提供幂等键。

---

## Q11. worker 崩溃后如何恢复？

链路（每一环都有测试）：

```
worker 执行中被 SIGKILL
  ↓ 进程组被杀，含 prefork 子进程
未 ACK 消息在 visibility_timeout 后重新可见
  ↓ broker redelivery
新 worker 调 mark_running：原 lease 已过期 → attempt+1 接管
  ↓ runtime/repository.py::mark_running
invoke_graph_with_resume：aget_state 发现 pending steps
  ↓ runtime/bootstrap.py
ainvoke(None, cfg) → 从 checkpoint 的 next 继续，已完成节点不重跑
  ↓
mark_succeeded，最终 attempt>=2、worker_id 已切换
```

四层保险：

1. **ACK 时机**：`acks_late` + `reject_on_worker_lost` 保证未完成任务会被重投。
2. **lease + heartbeat**：崩溃后 lease 过期（默认 180s）才允许接管，避免活着的 worker
   被误判为死亡。`AGENT_RUN_HEARTBEAT_SECONDS` 续租。
3. **retry + 退避**：transient 错误走 `RETRYING` → 退避 → `RUNNING`，**attempt 递增**；
   permanent 错误直接 `FAILED` 不重试；attempt 用尽写 `agent_dead_letters` + `DEAD_LETTER`。
4. **DLQ 人工重放**：`scripts/replay_dead_run.py`，**复用原 run_id**——这点很重要，
   因为换 run_id 会绕过工具幂等键（`run_id:tool_call_id`），等于重开一次退款。

**已验证到什么程度**：真实 PostgreSQL + 真实 Redis + 真实 SIGKILL 的端到端恢复，
见 `tests/integration/runtime/test_worker_checkpoint_recovery.py` 与
`make runtime-chaos`。**未验证**：真实生产集群、多副本长期运行下的恢复行为。

---

## Q12. HITL 为什么属于 run state，而不是普通 API 状态？

因为审批等待的**时间尺度与请求尺度不匹配**。

一个审批可能等 3 小时。如果它是"某个 HTTP 请求挂在那里等"，那么：
- 需要一个活着的进程持有它 —— 进程重启就没了；
- 需要一个活着的连接 —— 网关超时、客户端断开都没了；
- 无法回答"现在还有哪些审批在等我"—— 没有可查询的状态。

把它做成 run state（`WAITING_APPROVAL`）之后：
- 状态在 PostgreSQL，跨进程、跨重启；
- 有查询接口（`GET /api/runs/{id}`）；
- 有 TTL（过期按拒绝，**绝不默认放行**，`core/hitl/approval_service.py`）；
- 有 DLQ / cancel 逃生口，不会把 run 永久卡死。

**关键取舍**：`WAITING_APPROVAL` **不**在 `EXECUTABLE_STATUSES`
（`runtime/statuses.py`）。只有审批 API 显式投递才会恢复它，通用队列轮询不会把
"没人处理的审批"当成待办反复捞起变成忙循环。

---

## Q13. WAITING_APPROVAL 为什么不消耗 retry attempt？

因为**等待人不是失败**。`AGENT_RUN_MAX_ATTEMPTS=3` 是失败预算。

如果审批等待消耗 attempt：一个人下班前发起 3 笔审批，第二天审批人回来时，这三个 run
早就 `DEAD_LETTER` 了。而且审批和重试的**触发方不同**：重试由系统失败驱动（可以自动），
审批由人驱动（必须等）。

实现：`WAITING_APPROVAL → RUNNING` 走 `RunService.mark_resumed_running()`，
与普通 `mark_running()` 分开，**不递增 attempt**
（`tests/unit/test_hitl_run_status.py` 验证）。

同时保留 `WAITING_APPROVAL → DEAD_LETTER` / `→ CANCELLED` 逃生口，
避免审批被永久搁置时 run 无处可去。

---

## Q14. 为什么 approval 和 side-effect ledger 是两个不同防线？

因为它们防的是**不同的东西**，缺一就会出现具体的事故。

| | approval ledger | side-effect ledger |
|---|---|---|
| 记录什么 | **决策**：谁、何时、批不批、改成什么 | **执行**：这个操作生效过没有 |
| 问题域 | 授权（authorization） | 幂等（idempotency） |
| 回答的问题 | "这笔退款**该不该**执行" | "这笔退款**已经**执行过了吗" |
| 失效后果 | 未授权的高风险操作被执行 | 已执行的操作被执行第二次 |

具体事故场景，说明为什么两个都要：

- **只有 approval，没有 side-effect ledger**：审批通过 → 进程在写库前崩溃 → 重投 →
  approval 显示"已批准"所以直接放行 → **退款两次**。审批是**决策**的记录，不是执行的记录。
- **只有 side-effect ledger，没有 approval**：ledger 完美阻止重复执行，但**第一次**执行
  没人授权。高风险操作仍然会在无人审批时生效。ledger 不能替代"该不该做"的判断。

顺序也不能反：**先判授权（approval），再判幂等（ledger）**。
`tools/tool_registry.py::_idempotent_operation` 是入口；审批在工具**执行前**拦截，
ledger 在执行时兜底。

一句话：**approval 回答"能不能做"，ledger 回答"是不是已经做过了"。**

---

## Q15. 项目哪些是 VERIFIED，哪些不是？

### 已验证（按证据强度降序）

| 能力 | 证据等级 | 证据 |
|---|---|---|
| durable run 全生命周期（状态机、lease、重试、DLQ、取消、幂等键） | **CI VERIFIED** | `make runtime-e2e`（真实 PG + Redis） |
| checkpoint 跨进程存活 + 崩溃续跑 | **CI VERIFIED** | `test_worker_checkpoint_recovery.py`、`make runtime-chaos` |
| 崩溃恢复 flaky 根因 | **CI VERIFIED** | 270-cycle 探针 + 端到端复现（见下） |
| 副作用仅一次（真实 worker kill） | **CI VERIFIED** | `test_tool_idempotency.py` |
| HITL 审批全流程 + interrupt 恢复 + TTL + 职责分离 | **CI VERIFIED** | `test_hitl_approval_flow.py`、`test_hitl_langgraph_interrupt.py` |
| tracing 白名单 / 降级 / 异常不被吞 | **CI VERIFIED** | `tests/unit/test_telemetry.py`（23 例） |
| 文档一致性守卫 | **CI VERIFIED** | `make audit-docs`、`test_doc_consistency.py` |

### 明确**未**验证（不得含糊表述）

| 项 | 状态 | 阻塞原因 |
|---|---|---|
| 生产环境验证 | **NOT_VERIFIED** | 无生产集群证据（Issue #7） |
| 真实 ERP 写操作 | **NOT_MEASURED** | 以 Mock 为主 |
| 649-query RAG 指标 | **NOT_MEASURED** | provider 认证 HTTP 401（见下） |
| 性能数字（P50/P95/P99） | **NOT_MEASURED** | 无生产负载 artifact |
| 缓存命中率与收益 | **NOT_MEASURED** | 仅本地/受控环境 |
| 多副本长期稳定性 / queue backlog | **NOT_VERIFIED** | 未压测 |
| K8s 生产就绪 | **未进入主线** | 有意不做（见 trade-offs） |

### 本轮的真实阻塞

RAG 正式 649-query 评测被 **provider 认证**阻塞：直接探针返回
HTTP 401 `{"code":30014,"message":"Token is invalid."}`（SiliconFlow）。
因此 **Recall@K / MRR / NDCG / reranker uplift 全部是 `NOT_MEASURED`**。
本轮**没有**更换 provider——换了就与历史实验不可比，引用它等于伪造可比性。
详见 Issue #7 与 `artifacts/evaluation/rag-649/provider-auth-blocked-*.json`。

---

## 附：tracing 的 span 拓扑（新增观测能力）

```
HTTP request                    core/tracing.py（FastAPI 自动 instrument）
  └─ csai.agent.execute         runtime/executor.py，每次执行尝试一个
       ├─ csai.rag.retrieve     rag/qdrant_knowledge_base.py
       │    └─ event: rag.stage.{REWRITE,FILTER,VECTOR,BM25,FUSION_RRF,RERANK,FINAL}
       ├─ csai.llm.chat_completion   llm/client.py（含重试，按逻辑调用计）
       └─ csai.tool.execute      tools/tool_registry.py

csai.agent.execute.resume       审批后恢复段，通过 run_id / approval_id 关联
```

**边界声明（不要过度承诺）**：一个 run 可能在 `WAITING_APPROVAL` 停留到 TTL，
审批决策在另一个请求里到达，OpenTelemetry context 不跨进程/跨长时间保持。
所以承诺的是"**用 run_id 能把两段 trace 串起来**"，**不是**"一个 span 跨越任意长暂停"。

**隐私是边界不是偏好**：属性白名单（`core/telemetry.py::ALLOWED_ATTRIBUTES`）+
敏感词过滤。raw prompt、用户原文、召回文档、工具参数、PII、凭据一律不进 trace。
这一点是**实测**的，不只是构造上保证：一个用参数
`{order_id: SECRET-123, amount: 9999}` 构造的 tool span 只记录 name/side_effect/risk_level。