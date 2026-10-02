# HITL Deep Dive — 人工审批治理边界

> 交叉引用：[architecture-walkthrough.md](architecture-walkthrough.md) Q12–Q14、
> [runtime-deep-dive.md](runtime-deep-dive.md) §10、[source-map.md](source-map.md)。
> 证据等级见 [production-evidence.md](../evaluation/production-evidence.md)。

---

## 0. 一句话

> **approval 回答"能不能做"，side-effect ledger 回答"是不是已经做过了"。**
> 两道防线解决不同问题，缺一就有具体事故。

---

## 1. 风险分级（HIGH / MEDIUM / LOW）

**是什么**：给每个工具调用分一个风险等级，决定是否需要人工审批。

**为什么需要**：不能对所有写操作都要求审批 —— 那会让审批队列淹没人，
最终所有人都会无脑点"批准"，审批就退化成橡皮图章。分级让**有限的审批注意力**
花在真正不可逆的操作上。

**分级定义**（`core/hitl/risk.py`）：

| 等级 | 语义 | 例子 |
|---|---|---|
| `LOW` | 只读查询 | 订单查询、产品信息 |
| `MEDIUM` | 低风险写 | 创建售后工单 —— **记录但不强制人工审批** |
| `HIGH` | 退款 / 改单 / 高额赔付 / 投诉升级 / ERP 写 | **必须人工审批** |

**分类优先级**（`core/hitl/risk.py::classify_risk`，代码里的顺序就是刻意的）：

```
工具显式声明 risk_level  >  工具名称 allowlist  >  金额阈值  >  默认 LOW
```

为什么这个顺序：

1. **显式声明优先**：工具作者最清楚它做了什么。这是唯一不会误判的来源。
2. **名称 allowlist 次之**：历史工具没有声明时的兜底。
3. **金额阈值再次**：`refund_order(arguments={"amount": 50000})` 即使工具声明为 LOW，
   金额也应触发 HIGH —— **风险来自后果的规模，不只来自操作类型**。
4. **默认 LOW**：让新工具不会因为忘记配置而被卡住。

**关键判断 —— `should_propose_approval` 只认 HIGH**
（`core/hitl/gate.py`）。注释里写明了一个真实的坑：

```python
# RiskLevel("HIGH") 会抛 ValueError 并落到 False —— 那是**放行**
```

枚举值大小写不一致（`RiskLevel.HIGH` vs 字符串 `"HIGH"`）会导致一个**看起来在
拦截高风险操作**的函数静默变成放行。这是 fail-open 方向的 bug，比 fail-closed 危险得多。

**失败场景**：新工具作者忘记声明 `risk_level`，且名字不在任何 allowlist 里，
金额也不高 → 默认 LOW → 高风险操作免审批。
缓解：金额阈值 + HITL 与 side-effect ledger 叠加（见 §8）。

**trade-off**：宁可多拦（误拦代价 = 一次审批）也不要漏拦（漏拦代价 = 真实扣款）。
所以默认值是 LOW 但金额阈值兜底 —— 这一点在设计上有张力，值得在面试中主动说明。

---

## 2. Gate before side effect（副作用之前拦截）

**是什么**：审批发生在工具**真正执行**之前，而不是执行之后补一个确认。

**为什么必须在之前**：退款一旦执行，钱已经出去了，事后"审批"只能决定要不要冲正。
冲正不是退款 —— 它是另一笔交易，产生新的账务、新的客服工单、新的用户困惑。
**事后审批不是审批，是补救。**

**怎么实现**（`tools/tool_registry.py::_idempotent_operation`）：

```
execute_raw(name, arguments)
  ├─ 查 tools registry（无此工具则直接返回错误，不进任何审批路径）
  ├─ 计算 operation_key（副作用工具）
  ├─ side_effects.claim(operation_key)   ← 幂等层
  │    └─ 已 SUCCEEDED → 直接复用，**不执行**
  ├─ 若需要审批（HIGH）→ 图 interrupt() 挂起
  │    runtime/executor.py::_park_for_approval
  │    → AgentRun → WAITING_APPROVAL
  │    → checkpoint 落库（记住图挂在哪个副作用前）
  └─ 否则执行 handler
```

**关键判断**：拦截点在 `handler` 调用之前，而 `handler` 是唯一的副作用发生地。
因此"未审批就执行"在结构上不可能。

**`core/hitl/gate.py::collect_pending_actions(state)`** 从 LangGraph state 里
收集待审批动作，`should_propose_approval(...)` 判定是否需要审批。
`has_pending_interrupt(result)` 判断图是否真的挂起了 —— 后者用于区分
"图挂起等待审批"与"图正常结束但返回了奇怪的 dict"。

**失败场景**：agent 在一次执行中触发**多个**高风险副作用。设计要求**全部**收集后
一次审批（`collect_pending_actions` 返回 list 而非单个），
避免"批一个放一个"导致的审批语义模糊。

**trade-off**：一次审批批多个动作，人工审核者的负担更重、单个动作的上下文更弱。
替代方案是逐个审批，但会让一次对话需要 N 次往返，且部分执行部分拒绝后状态很难解释。

---

## 3. WAITING_APPROVAL（run state）

**是什么**：run 的一种**非终态**状态，表示"图挂在 interrupt 上等人工决策"。

**为什么是 run state 而不是 API 状态**：见
[architecture-walkthrough.md](architecture-walkthrough.md) Q12。核心是
**时间尺度不匹配**：审批可能等几小时，无法用挂起的请求表达。

**状态机位置**（`runtime/statuses.py`）：

```
RUNNING ──► WAITING_APPROVAL ──► RUNNING        (审批放行/编辑后恢复)
                  │
                  ├──► DEAD_LETTER               (逃生口)
                  └──► CANCELLED                 (逃生口)
```

**两个刻意分开的集合**：

```python
EXECUTABLE_STATUSES          = {QUEUED, RETRYING}            # worker 可领取
APPROVAL_RESUMABLE_STATUSES  = {WAITING_APPROVAL}            # 只能由审批 API 恢复
```

**为什么 `WAITING_APPROVAL` 不在 `EXECUTABLE_STATUSES`**：如果在里面，
通用队列轮询会把"没人处理的审批"当成待办反复捞起 → 忙循环 → CPU 烧掉且无意义。
只有审批 API **显式投递**才能恢复它。

**trace 边界**：这一段与审批后的恢复段**不是同一个 span**。run 可能在
`WAITING_APPROVAL` 停留到 TTL（默认 `HITL_APPROVAL_TTL_SECONDS=3600`），
审批决策在另一个 HTTP 请求里到达，OpenTelemetry context 不跨进程/跨长时间保持。
承诺的是"用 `run_id` / `approval_id` 关联两段 trace"，**不是**"一个 span 跨越任意长暂停"。
（见 `core/telemetry.py` 模块 docstring）

---

## 4. 审批不消耗 retry attempt

**是什么**：`WAITING_APPROVAL → RUNNING` **不递增** `attempt`。

**为什么**：因为 `AGENT_RUN_MAX_ATTEMPTS=3` 是**失败预算**。
等待人不是失败。

如果审批消耗 attempt：一个审批人周一发起 3 笔审批，周三回来时这三个 run
早已 `DEAD_LETTER`。而且审批与重试的**触发方根本不同**：
重试由系统失败驱动（可以自动发生），审批由人驱动（必须等）。

**怎么实现**：单独一个方法而不是复用 `mark_running()`：

```python
# runtime/run_service.py
mark_resumed_running(run_id, worker_id=..., task_id=..., lease_seconds=...)
```

`runtime/executor.py` 里两条路径显式分开：
`resuming_approval` → `mark_resumed_running()`；否则 → `mark_running()`。

**验证**：`tests/unit/test_hitl_run_status.py`。

**保留的逃生口**：`WAITING_APPROVAL → DEAD_LETTER` / `→ CANCELLED` 仍然允许。
理由：审批可能被永久搁置（审批人离职、审批记录丢失）。没有逃生口，
run 会永久停在 `WAITING_APPROVAL`，既不成功也不失败 —— 这是最坏的状态
（它在状态查询里看起来"还在处理中"）。

**trade-off**：不消耗 attempt 意味着一个反复"审批 → 拒绝 → 重新发起 → 再审批"的 run
可以无限循环。缓解：拒绝是终态（不会自动重新排队），重新发起需要新的人工动作。

---

## 5. TTL fail closed

**是什么**：审批有 TTL；到期按**拒绝**处理。

**为什么 fail closed**：TTL 到期时，安全的选择和可用性选择是相反的：

| 选择 | 后果 |
|---|---|
| **默认放行** | 无人审批的高风险操作在 TTL 后自动执行 —— 审批形同虚设，且这正是审批系统最该防的事故 |
| **默认拒绝** | 需要人重新发起，代价是用户 inconvenience |

本项目选择拒绝（`core/hitl/approval_service.py::is_expired`，
状态 `EXPIRED`）。理由：**审批的目的是授权，不是延迟**。一个没人做的决定就是没有决定。

**怎么实现**：`expires_at = requested_at + TTL`（`HITL_APPROVAL_TTL_SECONDS`，默认 3600s）。
`expires_at is None` 表示未设 TTL，永不过期 —— 这允许显式配置"某些审批不过期"，
但默认必须有 TTL。

**失败场景**：TTL 判定与实际执行之间有时间差。审批在 TTL 最后一秒被放行，
worker 领取时才发现已过期 → run 仍需重新审批。实现上审批决策的原子消费
（见 §7）处理了这个竞争。

**trade-off**：默认 3600s 是业务假设（"人会在一小时内处理"），不是技术推导。
不同业务需要不同 TTL，且**过期后需要能重新发起**，否则用户被卡死。
本项目通过保留逃生口 + 允许新审批来覆盖。

---

## 6. approve / edit / reject

**是什么**：三种决策，不只是同意/拒绝。

| 决策 | 语义 | 语义安全问题 |
|---|---|---|
| `approve` | 按提案执行 | 提案被篡改怎么办？→ 提案存 DB，审批绑定 proposal 快照 |
| `edit` | 人改了参数再执行 | 改后的参数**仍需过幂等层**，且要记录原值 |
| `reject` | 不执行 | run 走向何处？→ 终态 |

**为什么需要 `edit`**：现实中审批人经常不是简单同意/拒绝，而是"改成 X 再退"。
没有 edit 只能二选一，导致审批人为了改一个金额而拒绝，然后客服手工处理 ——
反而绕过了系统内的所有记账。

**edit 的关键设计**：编辑后的参数**仍然走 `side_effects` ledger**。
审批解决"能不能做"，编辑解决"做什么"，**幂等层解决"是不是已经做过"** ——
三件事互相独立。改了参数意味着 `operation_key` 可能变化，
但 `run_id:tool_call_id` 保持不变，所以幂等保护仍然成立（除非工具自己按参数哈希做 key）。

**失败场景**：审批人把 `amount` 从 100 改成 10000。如果幂等 key 是
`hash(tool_name + arguments)`（`runtime/side_effects.py::default_operation_key` 的兜底路径），
key 会变，ledger 查不到旧记录 —— 这是**正确**的（参数确实不同，是一笔新操作）。
但如果显式传了固定 `operation_key`，就会错误复用旧记录。这两个语义必须区分清楚。

**trade-off**：`edit` 增加了实现复杂度（需要存 proposal 快照、需要 diff、需要记录审计），
换来的是审批流程贴合真实业务。

---

## 7. Decision idempotency（决策幂等）

**是什么**：同一个审批的决策是**幂等**的 —— 重复提交同一个决策只生效一次。

**为什么需要**：网络重试、用户双击、审批人刷新页面，都可能重复提交决策。
如果重复处理：

- `approve` 两次 → 恢复被执行两次 → 靠 side-effect ledger 兜住（但浪费一次 run 执行）；
- 决策与恢复之间崩溃 → 需要知道"决策到底有没有生效"。

**怎么实现**：
`core/hitl/approval_service.py::decide(...)` 消费决策时用条件更新，
并与 `runtime/executor.py::_build_resume_command(run_id)` 配合 ——
**先原子消费已决策的审批**，再决定是否真的恢复。

`runtime/executor.py` 里的顺序很关键：

```python
resume_command = None
if resuming_approval:
    resume_command = _build_resume_command(run_id)   # 原子消费

if resuming_approval and resume_command is not None:
    running = svc.mark_resumed_running(...)          # 恢复
elif resuming_approval:
    # 消费不到（仍 PENDING 未过期 / 已被他人消费）→ 不动 run 状态
    return RunStatus.WAITING_APPROVAL.value
```

**注释里说明了为什么必须先消费**：
消费不到就**不动 run 状态** —— 否则会出现"run 已 RUNNING 但没有 resume 值，
图永远不恢复"的僵死状态。

**失败场景**（真实踩过，代码里有注释）：
如果注入的 `runtime.run()` 不支持 `resume_command`，`resume_command` 会被静默忽略
→ 图永远不恢复 → run 卡在 `WAITING_APPROVAL`。
处理方式是**响亮失败**：`runtime/executor.py::_runtime_accepts_resume` 检查签名，
不支持就抛 `TypeError`（走 permanent → DLQ 可观测），而不是静默吞掉。
**理由：静默吞掉会制造一个永远不会自己恢复的 run。**

**resume 的语义正确性**（`runtime/bootstrap.py`）：
审批恢复**不能**用 `ainvoke(None, cfg)` —— 实测 langgraph 1.2.12 上对处于 interrupt
的图调用它只会原样再次返回 `__interrupt__`，节点不推进。必须显式传
`Command(resume=<decision>)`。这是实测而非文档推断的。

---

## 8. 两道防线：approval ≠ idempotency

**这是本文件最重要的一节。**

| | approval ledger（`core/hitl/approval_service.py`） | side-effect ledger（`runtime/side_effects.py`） |
|---|---|---|
| 记录什么 | **决策**：谁、何时、批不批、改成什么 | **执行**：这个操作生效过没有 |
| 问题域 | 授权 authorization | 幂等 idempotency |
| 回答 | "这笔退款**该不该**执行" | "这笔退款**已经**执行过了吗" |
| 失效后果 | 未授权的高风险操作被执行 | 已执行的操作被执行第二次 |
| 生命周期 | 审批记录（审计用） | 幂等记录（正确性用） |
| 清理 | 审计保留期 | 可清理（旧操作不会重来） |

**为什么两个都要 —— 具体事故**：

**只有 approval，没有 side-effect ledger**：
```
审批通过 → 进程在写 ERP 之前崩溃
        → broker 重投
        → 新 worker 看到 approval = "已批准"，直接放行
        → 退款执行第二次
```
审批是**决策**的记录，不是**执行**的记录。审批通过只说明"有人同意过"，
不说明"是否已经执行了"。崩溃点落在两者之间，幂等层是唯一能救的防线。

**只有 side-effect ledger，没有 approval**：
```
ledger 完美阻止了重复执行
但"第一次"执行仍然在没有任何人授权的情况下发生
```
高风险操作仍然会在无人审批时生效。ledger 不能替代"该不该做"的判断 ——
它回答的是"是不是已经做过了"，对"该不该做"完全沉默。

**顺序也不能反**：**先判授权（approval），再判幂等（ledger）**。
如果先查 ledger，会出现"上次执行过 → 直接复用结果"，
从而**跳过了本次的授权判断** —— 一个未授权的请求会因为"上次有人做过"而通过。
这是真实存在的设计陷阱。

**一句话**：`approval` 回答"能不能做"，`ledger` 回答"是不是已经做过了"。
两个问题都要问，顺序是先授权后幂等。

---

## 9. RBAC 与职责分离

**职责分离（separation of duties）**：`reviewer_id != user_id`（发起人）。

**为什么**：能自己发起退款的人也能自己批准退款，审批就完全无效 ——
它只是多了一次点击。

**怎么实现**：`core/hitl/approval_service.py::_guard_reviewer`，在**服务层**强制
而不是只在 API 层。代码注释说明了理由：

> 真正的边界由 API 的 RBAC（customer 无审批权）承担

即：服务层强制"不能自我批准"，API 层 RBAC 强制"谁能当审批人"。
两层是**不同**的约束，缺一层都不行：
- 只有 API 层 → 其他内部调用路径可以绕过；
- 只有服务层 → 无法表达"customer 根本不能审批"。

**RBAC 在哪一层**：`api/routes/approvals.py`，4 级
（customer / agent / supervisor / admin）。审批权属于 supervisor 及以上。

**失败场景**：一个 customer 直接调用审批 API → 需要 RBAC 拦截。
一个 supervisor 审批自己发起的退款 → 需要 `_guard_reviewer` 拦截。

**trade-off**：职责分离在单人开发/演示环境下"很麻烦"（自己发起的单自己批不了）。
这是刻意的摩擦 —— 它存在的全部意义就是让单人无法独自完成一笔高风险操作。

---

## 10. `/api/chat` 快路径**不在** durable HITL 边界内

**这是一个必须主动说清楚的边界**。

| | `POST /api/chat` | `POST /api/runs` |
|---|---|---|
| 落 AgentRun 表 | 否 | 是 |
| HITL gate | **不生效** | 生效 |
| WAITING_APPROVAL | 无法表达 | 支持 |
| 审批 API 恢复 | 不适用 | 支持 |
| 崩溃恢复 | 无 | 有 |

**为什么**：快路径 inline 执行、不落库，所以它没有可供审批挂起的状态载体。
一次审批可能等几小时，无法挂在一个 HTTP 请求上。

**这意味着什么（风险要说清）**：如果快路径能触发高风险工具，它就**绕过了审批**。
所以安全边界依赖于一条不变式：

> **高风险副作用工具只能由 durable run 路径调用。**

快路径适合只读查询、建议、闲聊；一旦涉及退款/改单，必须走 `/api/runs`。

**当前状态**：这个边界由设计约定保证，但**没有运行时强制** ——
快路径调用高风险工具不会被自动拒绝（而是走 HITL gate，但因为没有 run 状态承载，
无法挂起）。这是一个**真实的设计缺口**，面试时应当主动承认而不是回避。

**正确的修法**（本轮未做）：在 `tools/tool_registry.py::_idempotent_operation` 里
检查"当前是否在 durable run 上下文中"（`runtime.context.get_current_run_id()`），
不在则拒绝高风险副作用工具。这样边界由代码强制，而不是靠调用方自律。

---

## 11. 恢复的"恰好一次"语义（诚实版）

**能承诺什么**：

> 审批放行后，副作用**至多生效一次**（at-most-once effect），
> 由 `side_effects` ledger 保证。

**不能承诺什么**：

> **不是** exactly-once **执行**。恢复路径会被重投，`runtime.run()` 可能执行多次；
> 是 ledger 让其中只有一次真正生效。

**为什么不承诺 exactly-once 执行**：审批恢复通过 `Command(resume=...)` 触发，
它是一次正常的图执行。图执行过程中崩溃 → 消息重投 → 图可能从头再走一遍。
ledger 保证副作用不重复，但 LLM 调用、RAG 检索这些**只读但昂贵**的副作用会重复发生。

**这意味着成本可能翻倍**（恢复时重跑 RAG + LLM）。这是当前设计的已知代价。

**失败场景**：恢复时图从头执行，LLM 被重新调用，可能产生不同的 tool_call_id
（如果 LLM 非确定），那么 `operation_key = run_id:tool_call_id` 就变了，
ledger 查不到旧记录 → 副作用重复执行。

**为什么当前风险可控**：图在副作用**之前**就挂起了。恢复时前面的节点（LLM 决策、
RAG 检索）通常不重跑 —— LangGraph 从 checkpoint 的 `next` 继续。
但这依赖"挂起点之前的所有节点都无副作用"这个不变式，它不是被强制的。

---

## 12. HITL 的证据边界

### 已验证（CI VERIFIED，真实 PostgreSQL）

| 能力 | 测试 |
|---|---|
| 审批全流程（propose → approve/reject/edit → resume → 终态） | `tests/integration/runtime/test_hitl_approval_flow.py` |
| LangGraph interrupt / resume 语义 | `tests/integration/runtime/test_hitl_langgraph_interrupt.py` |
| 风险分级规则 | `tests/unit/test_hitl_risk.py`、`test_hitl_gate.py` |
| 审批状态机（不消耗 attempt、状态分离） | `tests/unit/test_hitl_run_status.py` |
| API 层（RBAC、请求校验） | `tests/unit/test_hitl_api.py` |
| 决策幂等 / 提案清洗 | `tests/unit/test_hitl_approval.py`、`test_hitl_sanitize.py` |

### 未验证（不得声称）

| 项 | 状态 |
|---|---|
| 生产环境审批流程 | **NOT_VERIFIED** |
| 真实 ERP 退款（审批放行后真的退钱） | **NOT_MEASURED**（Mock ERP） |
| 真实 ERP 写操作的幂等性 | **NOT_MEASURED** |
| 审批吞吐 / 审批人响应时延 | **NOT_MEASURED** |
| 审批量级下的 Redis/PG 行为 | **NOT_VERIFIED** |

---

## 13. 已知工程缺口（面试时主动说）

| 缺口 | 影响 | 修法方向 |
|---|---|---|
| 快路径不强制 durable HITL 边界（§10） | 高风险工具可能被快路径调用而无审批载体 | 在工具层检查 `get_current_run_id()`，不在则拒绝 |
| 审批等待期间的恢复可能重复 RAG/LLM 调用（§11） | 成本翻倍 | 恢复时复用已召回证据（Tool Result Store 已有基础） |
| reranker / embedding 静默降级不 fail-loud | 检索精度悄悄下降 | 降级时打 error 并在 `.meta` 标记 |
| 审批与 side-effect ledger 清理策略未统一 | 长 run 后表增长 | 统一的保留期与清理任务 |

---

## 14. 面试问答要点

**Q：人工审批听起来很重，为什么不直接让 LLM 自己判断要不要审批？**

A：三个理由。(1) 风险分级是**确定性规则**（金额阈值、工具名），
LLM 判断不可复现、不可审计，而且能被 prompt 注入影响 —— 让被审批的操作自己决定要不要被审批
是结构性错误。(2) LLM 的判断**没有 accountability**：出事之后不能说"是模型说的"。
(3) 成本与延迟：每个工具调用都过一遍 LLM 判断不可接受。
确定性规则便宜、可审计、可解释。

**Q：那审批系统会不会变成瓶颈/橡皮图章？**

A：会，所以有分级：只有 HIGH 强制审批，MEDIUM 只记录。而且 MEDIUM 记录本身
就有价值 —— 它是事后审计和阈值调优的数据来源：如果你发现某类 MEDIUM 操作
反复出问题，就该把它升级为 HIGH。分级阈值应该基于这些记录调整，
而不是拍脑袋定。

**Q：审批人离职了，pending 的审批怎么办？**

A：TTL 到期按拒绝（fail closed），run 保留逃生口到 `DEAD_LETTER` / `CANCELLED`，
不会永久僵死。用户/客服可以重新发起，走一个新的审批。
这比"默认放行"安全得多 —— 默认放行意味着无人审批的高风险操作自动执行。