# Failure Modes and Trade-offs — 为什么这样做，以及代价是什么

> **本文件的写法要求**：每一条都必须给出 **trade-off / scope / evidence / future extension**
> 四要素。只写"暂时没时间"是不合格的 —— 那是排期陈述，不是工程判断陈述。
>
> 证据等级见 [production-evidence.md](../evaluation/production-evidence.md)。
> 交叉引用：[source-map.md](source-map.md)。

---

## 0. 本项目犯过的错（先讲这个）

面试里最有说服力的不是"我做对了什么"，而是"我知道自己错在哪、以及改了什么机制"。

### 0.1 flaky test 掩盖了测试前提的缺陷（并差点让我做出错误结论）

**现象**：`test_worker_crash_resumes_from_postgres_checkpoint` 记录为
"7 次 suite 跑挂 1 次"，根因不明。

**我的处理过程，以及其中的两次错误判断**：

| 阶段 | 我看到的数据 | 我当时的结论 | 判断是否正确 |
|---|---|---|---|
| 第一次 | 120 cycles，旧门控放行 4 次 kill，"提交未可见" | "命中了！根因找到了" | ❌ **过早** —— 读到的是 kill 之前，"不可见"不等于"会丢失" |
| 第二次 | 加上 kill 后复读，2 次命中全部恢复 | "假设已被否证，撤销修复" | ❌ **过早** —— 2/2 样本太小 |
| 第三次 | 累计 270 cycles，5 次命中中 1 次提交真丢 | "假设成立，且能端到端复现" | ✅ |

**教训**：

1. **"观察到 A"不等于"A 会导致 B"。** 第一次我把"checkpoint 在 kill 瞬间不可见"
   当成"checkpoint 会丢失"。实际上 LangGraph 的后台提交在"读"与"kill"之间的
   ~8ms 内落地，所以什么都没丢。
2. **少量样本不足以否证假设。** 2/2 恢复不能证明"不会失败"，尤其当基础概率是 ~20%。
   2 次都没出现，只能说"这次没出现"。
3. **两次过早结论里，有一次是往"撤销修复"的方向错的。** 更危险的是第一次 ——
   如果我据此合并了修复，会得到一个"看起来验证过、实际没验证根因"的提交。
4. **有效的做法是把检测变便宜**：不跑完整恢复流程（~40s），只跑
   start → wait → gate → kill（~2.2s），并在命中时才跑恢复尾部。
   这让样本量从 7 次涨到 270 次，让统计结论成为可能。

**改进了什么机制**（不只是修了 bug）：

- 加了失败取证（`tests/integration/runtime/crash_diagnostics.py`），
  失败时留下 broker/worker/checkpoint/AgentRun 的只读快照 ——
  下次这类问题不再是一行 AssertionError；
- 修复本身由 12 个单测钉住判据（包括导致 flake 的那个形状），
  未来不会静默弱化回计数断言。

---

## 1. 为什么不是 exactly-once

**决策**：at-least-once 投递 + 三层业务幂等。

**为什么**：exactly-once 投递需要 broker 与业务之间存在原子提交协议，
而副作用发生在**外部系统**（ERP）。经典反例：

```
向 ERP 提交退款 → ERP 成功 → 进程在 ACK 之前崩溃
```
此时既没有 ACK，也没有业务层的"完成"记录。
broker 重投 → 退第二次。要避免，只有两条路：
(1) 所有参与方加入同一个分布式事务；(2) 外部系统支持按幂等键去重。

**代价**（明确承认）：
- 同一 run 可能被多个 worker 先后执行；
- 实现复杂度显著上升：run 级原子领取、thread 级分布式锁、工具级 ledger 三层；
- 必须处理"重复执行时哪些操作可以重复、哪些不行"的分类。

**替代方案与为什么不选**：

| 方案 | 不选的原因 |
|---|---|
| Celery result backend 记录结果 | 后端会过期、会丢；"任务跑完"≠"业务成功" |
| 依赖 task_id 做去重 | task_id 由 Celery 分配，重启/重投时不稳定；且不含业务语义 |
| 2PC / XA | 性能与可用性代价高；且 ERP 通常不支持 XA |
| exactly-once 语义框架 | 解决的是进程内消息传递语义，不解决"外部副作用"问题 |

**Evidence**：CI VERIFIED（真实 PG + Redis + 真实 SIGKILL 的重复消费与副作用去重）。

**Future extension**：如果 ERP 侧支持按业务幂等键去重（例如传 `request_id`），
可以把 `operation_key` 下推到 ERP，ledger 退化为第二道防线而不是唯一防线。
这一层需要真实 ERP 接口，当前**NOT_MEASURED**。

---

## 2. 为什么不用分布式事务

**决策**：本地数据库事务保证 ledger 自身的原子性；跨系统靠幂等而非事务。

**为什么**：2PC 的代价在这个场景不划算：

| 代价 | 说明 |
|---|---|
| 可用性 | 协调者故障时参与者全部阻塞 |
| 性能 | 多一次网络往返 + 锁持有时间变长 |
| 参与方要求 | 需要所有资源管理器支持 XA —— 真实 ERP **不**支持 |
| 故障模式复杂 | `HEURISTIC` 状态（事务管理器自己也不知道提交了没有）是最难处理的情况 |

**关键判断**：分布式事务解决的是"跨多个**受控**资源的事务"。
本项目的跨系统边界是 **ERP**，它不受我控制。
面对不受控的外部系统，幂等 + 对账（`reconciliation`）才是可用的方案，
不是放弃正确性，而是选择**可实现**的正确性。

**代价**：需要接受"最终一致"—— 存在一个窗口，ledger 说成功但 ERP 侧结果未知。
这个窗口由 ERP 侧的可查询性来收敛（对账）。

**Evidence**：ledger 自身的事务性由 `tests/integration/runtime/
test_side_effect_claim_concurrency.py`（8 并发只 1 个 `CLAIM_EXECUTE`）
与 `test_tool_idempotency.py` 验证。跨系统对账 **NOT_MEASURED**（无真实 ERP）。

**Future extension**：接入真实 ERP 后实现周期性对账 —— 用
`operation_key` 查询 ERP 侧状态，反查 ledger 标记为 SUCCEEDED 但 ERP 无记录的行。

---

## 3. 为什么 Redis 不是 canonical state

**决策**：业务真相只在 PostgreSQL；Redis 只做协调与观测。

**为什么**：Redis 在本项目的角色：

| 用途 | 为什么它不能当真相源 |
|---|---|
| Celery broker | 消息状态由 broker 记账，但"业务成功"是业务概念 |
| per-thread 锁 | 锁是**临时**协调状态，带 TTL，会过期 |
| run 事件流 | 明确是观测通道，best-effort、可重放、可裁剪 |
| Session 存储 | 可重建（Redis 挂了 session 丢，业务状态不丢） |

**如果把 Redis 当真相源会怎样**：Redis 重启丢数据 → 业务状态丢失；
Redis 主从切换丢已确认的写入 → run 状态回退；`GET /api/runs/{id}` 从 Redis 读
会在缓存未命中时返回"不存在"，而 run 实际在跑。

**具体设计后果**（这是"Redis 不是真相源"的代价，不是收益）：

1. **每一步状态迁移都要写 PostgreSQL**，比写 Redis 慢。
2. **状态查询要 join 多处**（`agent_runs` + checkpoint + 事件流）。
3. **不能把 Redis 事务当作业务事务**。
4. 生产强制 `SESSION_STORAGE_BACKEND=redis`
   （`core/config.py::validate_distributed_runtime_settings`）——
   注意这不是"Redis 当真相源"，而是"多 worker 场景下不能用进程内 session"。

**Evidence**：CI VERIFIED（配置契约测试
`tests/unit/test_execution_mode_contract.py`；run 事件"不是真相源"由
`tests/integration/runtime/test_event_delivery_semantics.py` 显式断言）。

**Future extension**：需要读性能时加 Redis 缓存，但**必须**承认缓存可能过期，
API 语义上以 PostgreSQL 为准；关键写后立即失效缓存，不做最终一致缓存。

---

## 4. 为什么 checkpoint 不是唯一业务状态源

**决策**：checkpoint（LangGraph 拥有）与 AgentRun（业务拥有）分离。

**为什么**：两者所有权与生命周期不同：

| | checkpoint | AgentRun |
|---|---|---|
| 所有者 | LangGraph 框架 | 业务 |
| 组织方式 | 按 `thread_id` | 按 `run_id` |
| 记录内容 | channel 值 + pending task | 状态机 + attempt + worker_id + result |
| 能否回答"失败要不要重试" | 否 | 能 |
| 能否回答"图跑到哪一步" | 能 | 否 |

强行合并的两条路都有问题：

- **把业务状态塞进 checkpoint**：被框架 schema 绑死；框架升级改 schema 就丢业务数据。
- **把 checkpoint 塞进 AgentRun**：无法支持一个 thread 多次 run，也无法支持
  HITL 挂起（挂起状态是图的状态，不是 run 的状态）。

**代价（真实的复杂度）**：崩溃恢复必须**两者一起**用 ——
checkpoint 说"从哪继续"，AgentRun 说"算第几次、要不要重试、用尽怎么办"。
这意味着崩溃恢复逻辑要同时理解两套状态，测试也必须同时断言两者
（`test_worker_checkpoint_recovery.py` 同时断言 checkpoint 续跑**和** attempt 切换**和**
worker 切换）。

**这条代价本轮真的付了**：那个 flaky test 的根因就是"把 checkpoint 存在当成了
checkpoint 已提交"—— 混淆了两者。见 §0.1。

**Evidence**：CI VERIFIED（`test_cross_process_checkpoint.py` 验证 checkpoint 跨进程存活；
`test_worker_checkpoint_recovery.py` 验证两者配合）。

---

## 5. 为什么没有直接上 Kubernetes

**决策**：不进入主线。提供 Docker Compose 6 个变体
（base / prod / override / canary / scale / monitoring）。

**为什么**：K8s 解决的是**编排与自愈**问题。本项目的瓶颈不在那里：

| K8s 能力 | 本项目是否需要 | 理由 |
|---|---|---|
| Pod 自愈 | 部分 | Celery worker 崩溃重启有用，但 Redis 已经用 `reject_on_worker_lost` 重投 |
| HPA 自动扩缩 | **不需要** | 瓶颈是 ERP 延迟和 LLM 配额，不是本地 CPU；盲目 HPA 会加剧 ERP 压力 |
| 滚动更新 / 蓝绿 | 有价值 | 但当前流量规模用 Compose + `make scale` 足够 |
| 服务发现 / ConfigMap | 不需要 | 6 个进程，配置走环境变量 |
| 网络策略 / mTLS | **不需要** | 同机部署 |

**关键判断**：K8s 会引入一个新的失败面（调度、etcd、Ingress、证书轮转），
而它缓解的问题本项目并不突出。**在解决不了业务瓶颈的地方加编排层，
是把运维复杂度换成了业务复杂度。**

**代价（诚实）**：Compose 方案的边界很清楚 ——
单机、没有自动故障转移、没有弹性伸缩、没有多可用区。
如果真的上生产，这个方案不够。**所以当前生产状态是 NOT_VERIFIED，
而不是"已做好生产准备"。**

**Evidence**：`NOT_VERIFIED`。Compose 变体可构建、有 canary/scale 脚本，
但没有生产集群证据（Issue #7）。

**Future extension**：当出现以下信号时再上 K8s ——
多可用区容灾、需要在流量高峰弹性扩容以避免打垮 ERP、
或者需要 10+ 副本且人工发布开始成为瓶颈。届时优先考虑托管 K8s
（把控制面运维外包）。

---

## 6. 为什么没有 multi-tenant

**决策**：单租户。没有租户隔离、没有租户级配额、没有租户级加密密钥。

**为什么**：真正的 multi-tenant 不是加一个 `tenant_id` 字段。需要同时具备：

1. 所有查询强制带 tenant scope（漏一处就是数据泄露）；
2. 缓存 / 向量检索按租户隔离（L1/L2/L3 cache、Qdrant collection 或 payload filter）；
3. 令牌与配额按租户计量；
4. 加密密钥按租户派生；
5. 运维/审计日志按租户分权。

这是一整套系统性改造，且**改变数据模型**。本项目的定位是"化妆品行业客服系统"，
租户隔离对目标岗位（AI 大模型应用开发）不是核心考察点。

**代价**：这个系统不能直接对外多租户售卖。要支持，需要一次数据模型迁移 +
全链路 scope 改造。

**Evidence**：`NOT_IMPLEMENTED`（有意不做，不是遗漏）。

**Future extension**：如果要 SaaS 化，第一步应该是**数据库层 RLS**
（PostgreSQL Row Level Security），因为它在存储层强制，
比在应用层靠人记得加 `WHERE tenant_id` 可靠得多。

---

## 7. 为什么 MCP 目前不进主线

**决策**：MCP（Model Context Protocol）工具适配不进主线。
旧实验快照保留在 tag `archive/integration-distributed-runtime-a9dc939`。

**为什么**：三个判断：

1. **解决的问题不是当前瓶颈。** 当前痛点是 durability 与幂等（已解决），
   不是"如何接入更多工具"。已有 Function Calling 工具注册表
   （`tools/tool_registry.py`）能覆盖需求。
2. **MCP 会引入新的信任边界。** 外部 MCP server 提供的工具是**不可信输入**：
   返回值可能含恶意内容、可能试图注入 prompt、可能调用不期望的工具。
   这需要一整套 server 信任与沙箱机制，而本项目的 HITL 边界是按
   "自建工具"设计的。
3. **旧快照的 runtime / HITL 实现已经过时。** 那是 PR #27 之前的状态，
   与当前 `main` 的 HITL 治理边界不一致，整体搬运会带回已知的 correctness 缺陷。

**代价**：没有 MCP 就不能复用社区 MCP server 的工具生态。

**Evidence**：`NOT_IMPLEMENTED`（有意推迟）。旧代码仅作为参考存档。

**Future extension**：如果要做，从当前 `main` 重新起一条分支实现"工具适配层"，
只借鉴适配思路，**不复用**旧 runtime / HITL 实现。
且 MCP 工具必须默认视为高风险（走 §8 的两道防线）。

---

## 8. 为什么 Production NOT_VERIFIED

**决策**：明确标注生产未验证，而不是含糊过去。

**为什么必须明确**："代码跑在本地"和"代码在生产环境可靠"是两个不同命题。
后者需要：真实流量、真实依赖行为、真实故障注入、长期运行观测。
本项目**都没有**，因为没有生产环境。

**当前 NOT_VERIFIED / NOT_MEASURED 的完整清单**：

| 项 | 状态 | 缺什么 |
|---|---|---|
| 生产环境整体行为 | NOT_VERIFIED | 生产集群 |
| 真实 ERP 写操作 | NOT_VERIFIED | 真实 ERP 接口（当前 Mock；副作用验证走确定性 staging 工具） |
| 649-query RAG 正式指标 | NOT_VERIFIED | provider 认证（HTTP 401，本轮已复测确认） |
| 生产延迟 P50/P95/P99 | NOT_MEASURED | 生产负载 artifact |
| 缓存命中率与收益 | NOT_MEASURED | 真实部署观测 |
| 多副本长期稳定性 | NOT_VERIFIED | 长跑压测 |
| Queue backlog 行为 | NOT_VERIFIED | 背压压测 |
| K8s 弹性伸缩 | NOT_VERIFIED | 未进入主线 |

> 术语口径：`NOT_VERIFIED` = 该命题当前**没有**支持它的 artifact；
> `NOT_MEASURED` = 该量纲的**数值**未采集到。两者的完整 canonical 清单见
> [docs/evaluation/production-evidence.md](../evaluation/production-evidence.md)，
> 不要在本文档另立一套口径。

**替代的是什么**：**CI VERIFIED + 可复现的 artifact**。
每个声称"已验证"的能力都有对应命令和产出文件。这是本项目在没有生产环境时
能提供的最强证据形式。

**Evidence**：见 `docs/evaluation/production-evidence.md` 与 Issue #7（保持 OPEN）。

**Future extension**：按优先级：真实 ERP 打通（解除 RAG 401 后重跑评测）→
受控 staging 流量 → 延迟/缓存 artifact → 长时间稳定性观测。

---

## 9. 为什么 Ruff / mypy 仍然有历史债

**决策**：不因为本轮任务顺手清全仓。**只保证 changed files clean。**

**为什么**：这是一个**范围与风险**的判断，不是懒。

1. **全仓 lint/mypy 清理会产生巨大的 diff**，与本轮目标（可信度、可解释性）
   无关。100 个文件的格式改动会淹没真正的变更，review 成本极高，
   也让"这次改了什么"变得不可读。
2. **mypy 全仓清理可能需要改类型标注甚至改逻辑**。历史错误里有一部分
   反映真实的设计问题（比如 `ERPProtocol | None` 的 union-attr 意味着
   某处可能在 ERP 未初始化时调用）。把它们改成"类型通过"而不是"行为正确"，
   是**用类型系统掩盖 bug**。
3. **历史债本身不阻塞本轮功能**。本轮改动的文件是 clean 的。

**当前状态（必须诚实报告，不粉饰）**：

- **repo-wide Ruff: NOT clean**（历史 findings，未清理）；
- **repo-wide mypy: NOT clean**（存在历史类型错误）；
- **changed files: ruff clean + mypy clean**。

> **本文档刻意不写错误条数。** 全仓 `ruff` / `mypy` 的 finding 数会随每一次
> 提交漂移，且没有任何自动生成机制把它同步回文档；写死一个数字只会变成
> 下一次 review 要纠正的假事实。要现场取数就跑命令，结论以输出为准：
>
> ```bash
> make lint                                    # repo-wide ruff
> mypy . --ignore-missing-imports              # repo-wide mypy
> mypy <changed-paths> --ignore-missing-imports  # 本次改动范围
> ```
>
> 唯一的**稳定**事实是上面三行定性结论（clean / not clean），不是任何计数。

**本轮的实际影响**：新增的 `Any` 标注（`llm/client.py` 的
`CustomResponse.tool_calls: list[Any] | None` 与 `payload: dict[str, Any]`）顺手消掉了
几个**既有** mypy 错误。这是副作用，不是目标 —— 目标是"不新增错误"。

**Future extension**：单独开一个 PR 分批清理，按模块分组，每个 PR 都保持测试绿。
优先修 `union-attr` 类（它们可能是真 bug），优先修 `return-value` 类。

---

## 10. 关键设计 trade-off 速查

| 决策 | 得到 | 付出 | 何时应该重新评估 |
|---|---|---|---|
| at-least-once | 崩溃恢复能力 | 三层幂等复杂度 | ERP 支持幂等键时 |
| 无分布式事务 | 可实现、可运维 | 最终一致窗口 | 多方都可控时 |
| PG 是唯一真相源 | 一致性、可审计 | 每次迁移都写库 | 读性能成瓶颈时 |
| checkpoint / AgentRun 分离 | 各自演进自由 | 恢复逻辑要懂两套状态 | 永不（边界是本质的） |
| 快路径 + 异步 run 并存 | 延迟与可靠性分层 | 两套语义、边界需强制 | 高风险工具需收敛到一路 |
| HITL 只拦 HIGH | 审批注意力可支配 | 漏分级 = 漏拦 | MEDIUM 记录显示应升级时 |
| approval + ledger 两道 | 授权与幂等都覆盖 | 顺序敏感、易出错 | 永不 |
| WAITING_APPROVAL 不消耗 attempt | 人不会被重试预算逼走 | 可能无限循环 | 需要人为次数上限时 |
| 单租户 | 范围聚焦、深度做好 | 不能 SaaS | 商业形态变化时 |
| Compose 而非 K8s | 少一个失败面 | 无自愈/弹性 | 出现 §5 的信号时 |
| 记录 NOT_VERIFIED | 不制造虚假证据 | 简历/面试看起来"不够" | 有真实 artifact 时逐项升级 |

---

## 11. 被明确拒绝的"看起来更专业"的方案

记录下来，因为面试官常会问"为什么不上 X"，而**能拒绝并说出理由**
比"全都做了"更有说服力。

| 方案 | 拒绝理由 |
|---|---|
| 分布式事务（2PC/XA） | ERP 不支持；协调者故障阻塞；HEURISTIC 状态难处理（§2） |
| Kubernetes | 解决的不是当前瓶颈；引入新失败面（§5） |
| 多租户 / SaaS | 需要数据模型迁移 + 全链路 scope；非当前能力重点（§6） |
| SAML / OIDC / SCIM / 完整 IAM | 目标形态是内部 + API Key + JWT 双模式；完整 IAM 是独立课题 |
| 完整 SIEM | 需要真实运维环境与合规要求；当前无可审计对象 |
| Multi-region | 延迟与成本收益为负；单 region + 恢复机制已足够 |
| MCP 进主线 | 引入不可信工具边界；非当前瓶颈（§7） |
| 更大规模的 Prompt Platform | prompt 是代码不是数据；当前无版本管理需求，trace 里带 `prompt_name`/`prompt_version` 即可 |
| 更多 Agent 角色 | 9 个已覆盖客服域；再加是数量而非能力 |
| 第二个向量数据库 | Qdrant 已在用；迁移成本不带来检索质量提升 |

---

## 12. 面试时如何回答"这个项目最大的不足"

不要防守，要给出**已定位 + 有证据 + 有计划**的三段式：

> 最大的不足是**没有任何生产环境证据**。所有验证都是 CI 级的真实基础设施，
> 但真实流量、真实 ERP 写操作、生产延迟这些都没有 artifact。
>
> 具体到 RAG：正式 649-query 评测链路是通的，但 provider 认证返回 401，
> 所以 Recall@K 这些指标是 NOT_VERIFIED，我没有引用过任何数字，
> 也没有换个模型假装可比 —— 换了就与历史实验不可比。
>
> 第二个是**快路径的 HITL 边界只靠约定没有强制**：如果快路径触发了高风险工具，
> 理论上没有 run 状态承载审批。代码上就是这个缺口 ——
> `core/hitl/gate.py::should_propose_approval` 在 `get_current_run_id()` 为空时
> 返回 `False`（快路径无 durable checkpoint，拦了也无法挂起/恢复），于是
> `tools/tool_registry.py` 直接执行 handler：既没有审批记录，也没有 ledger key。
> 正确的修法是在工具执行入口检查 durable run 上下文，不在就拒绝高风险副作用。
> 这个缺口我定位清楚了，但**当前 `main` 尚未修**——说"已修"就是伪造证据。

这个回答同时展示了：知道边界、区分证据等级、不粉饰、并且知道怎么修。
