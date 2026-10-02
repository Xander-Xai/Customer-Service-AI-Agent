# 项目介绍脚本（面试用：60 秒 / 2 分钟 / 3 分钟）

> Lifecycle: 🟢 CURRENT（随代码同步）。
>
> **口径纪律**：
> - FCR、人效、P99、成本、provider latency 没有当前生产证据 → 说 `NOT_MEASURED` / `NOT_VERIFIED`。
> - RAG 当前 649-query 正式指标是 `NOT_VERIFIED` → **不要报任何 Hit/MRR 百分比**。
> - 分布式 Agent Runtime 的证据等级是 **Level 2 = CI VERIFIED**
>   （真实 PostgreSQL + Redis + 多进程 Celery 的验收与 chaos 测试），
>   **不是**"生产集群已验证"。真实生产集群 / 多副本长期运行 / 真实 ERP 写操作
>   属于 Level 3，未验证。**不要把 Level 2 说成生产验证。**
> - 测试数量以 `pytest --collect-only -q` 当前输出为准，不报历史数字。

三档时长共用同一套事实，差别只在**取舍**，不是不同版本的事实。

---

## 60 秒版（最短、最难反驳）

> 我做的是一个面向化妆品企业的**多智能体客服系统**，基于 LangGraph，有 9 个 Agent 角色、5 种协作模式、RAG 混合检索和 Function Calling。
>
> 我在里面投入最多的是**分布式 Agent Runtime**这一层。原来这套系统只能在单进程里跑——checkpoint 在内存里、session 在进程里、worker 没有；我把它改成**实时/异步双路径**：低延迟问答走原来的快路径，长任务走异步 Run，交给独立的 Celery worker 执行，状态以数据库里的 `AgentRun` 记录为唯一真相源。
>
> 为了让多副本真的正确，我做了三件事：checkpoint 换成官方 PostgreSQL saver，session 换成 Redis，同一个会话加 Redis 分布式锁串行化；这三件套在生产启动时会强制校验，缺一个就拒绝启动。投递语义我明确是 **at-least-once 加幂等**，不是 exactly-once——幂等分三层：run 级终态去重、thread 级锁、工具级副作用 ledger。
>
> 这些有真实 PostgreSQL + Redis + 多进程 worker 的验收测试和一个 SIGKILL 混沌测试，证据等级是 CI 验证通过；但**真实生产集群我没验证过**，这一点我不会含糊。

**为什么 60 秒版这样说**：先给业务定位，再直接押在最硬的工程亮点上（分布式 runtime），最后**主动**划出证据边界——主动说边界比被追问才承认可信得多。

---

## 2 分钟版

> **开场（约 20 秒）**
>
> 这是一个化妆品企业的 AI 客服系统，目标是让多个 AI Agent 协同处理从简单问候到复杂投诉的问题。基于 LangGraph，9 个 Agent 角色、5 种协作模式（Sequential / Parallel / Consultation / Hierarchical / ReAct），带 RAG 混合检索和 Function Calling。
>
> **核心架构（约 30 秒）**
>
> 业务链路是一个**四层状态机**：Layer 0 缓存（Redis 精确 → Qdrant 语义 → Jaccard 兜底）、Layer 1 双层路由（LLM 分类器和规则分类器并行跑，加复杂度评分决定走快路径还是复杂链路）、Layer 2 五种协作模式、Layer 3 响应后处理。
>
> **工程亮点一：分布式 Agent Runtime（约 45 秒）**
>
> 这是我最主要的工作。原来这套系统是单进程的：图 checkpoint 放在内存里，多开一个 worker 状态就分片了，重启全丢。
>
> 我做了**实时/异步混合架构**：快路径 `/api/chat` 保持同步低延迟，不动；长任务走异步路径——创建一条 Run 记录、立刻入队返回，真正的执行在独立的 Celery worker 里，客户端轮询状态。
>
> 关键设计有几点：
> - **状态真相源是数据库的 `agent_runs` 表**，不是 Celery 的 result backend——因为队列只是调度，消息会丢也会重投。
> - **四个 ID 概念严格区分**：`thread_id`（会话级）、`run_id`（单轮执行）、`task_id`（队列投递）、`AgentRun`（业务记录）。我踩过一个坑：一开始 DLQ 重放新建 run，结果工具幂等键是 `run_id:tool_call_id`，换 run_id 就等于绕过幂等，把已经成功的退款又执行了一遍。所以重放必须复用原 `run_id`。
> - **跨进程互斥**：同一会话串行、不同会话并发，用 Redis 锁（owner token + TTL + Lua 原子释放，避免旧 owner 误删新锁）。API 执行边界和 worker 共用同一个 key namespace。
> - **崩溃恢复**：worker 挂了任务会重投，新 worker 接管租约后**从 checkpoint 的下一个节点续跑**，不是从头重跑。这个是我修过的 bug——一开始 `ainvoke(state, cfg)` 会从 START 重新执行并覆盖状态值。
> - **副作用幂等**：写操作工具走一个 ledger，唯一键是 `run_id:tool_call_id`，保证重试和崩溃恢复不会重复扣款/重复改单。
>
> **工程亮点二：其他（约 20 秒）**
>
> ReAct 推理引擎、四类会话漂移检测、LLM 熔断器降级到规则引擎、Tool Result 的确定性压缩与 exact-reuse cache。
>
> **证据边界（约 15 秒）**
>
> 分布式 runtime 这块我用真实 PostgreSQL、Redis 和多进程 worker 做了验收测试，还写了一个把 worker 整个进程组 SIGKILL 掉的混沌测试，验证续跑和副作用不重复——这些是 CI 验证通过。但**真实生产集群、多副本长期运行、真实 ERP 写操作我都没验证过**，所以我不会说"生产已验证"。RAG 那块我有一套可复现的评测流水线，但当前 provider 凭据失效，正式指标是 NOT_VERIFIED，我只讲方法论不报数字。

---

## 3 分钟版（完整版）

> ### 第一段：开场（30 秒）
>
> 面试官您好，我介绍一下我最近做的项目——**多智能体客服系统**。面向化妆品生产企业，目标是让多个 AI Agent 协同处理从简单问候到复杂投诉的各种客户问题。基于 **LangGraph**，9 个 Agent 角色、5 种协作模式，具备 RAG 知识检索、Function Calling 工具调用、ReAct 推理链。
>
> ### 第二段：核心业务架构（50 秒）
>
> 业务链路是一个**四层状态机**：
>
> **第一层缓存**。Response Cache 三层：L1 Redis 精确匹配、L2 Qdrant 语义相似、L3 Jaccard 兜底。这里要说明一点：Tool Result 的 exact-reuse cache、Tool Result Store 和 Session Memory 是**三个独立机制**，不是缓存的一部分——很多人会把它们混为一谈。
>
> **第二层双层路由**。LLM 分类器和规则分类器 `asyncio.gather` 并行跑，规则高置信时走捷径跳过 LLM。同时算复杂度评分——"你好"直接走快速通道，"过敏要退款还要查物流"走复杂链路。
>
> **第三层五种协作模式**，这是业务侧最核心的设计：简单问题 Sequential；跨领域 Parallel 并发；需要专业补充 Consultation；投诉升级 Hierarchical；复杂推理 ReAct（Thought → Action → Observation 循环，带最大迭代次数防死循环）。
>
> **第四层响应后处理**：质量评估、模式升级重试、写缓存、SLA 监控。
>
> ### 第三段：技术亮点（90 秒）
>
> **第一个、也是最主要的：分布式 Agent Runtime。**
>
> 原来这套系统只能在单进程里跑：LangGraph checkpoint 在内存里，多个 Gunicorn worker 之间状态分片，重启全丢。我把它改造成**实时/异步混合架构**：
>
> - 实时快路径 `POST /api/chat` 和 SSE 流式保持同步不动——客服问答要低延迟。
> - 长任务走异步路径：创建 Run 记录、立刻入队返回，Celery worker 在独立进程执行，客户端轮询状态或订阅事件流。
>
> 我做的几个关键决策：
>
> 1. **状态真相源放在数据库**，不是队列 result backend。队列只是调度层，消息会丢会重投；业务状态必须在 `agent_runs` 表，用状态机 + 原子条件更新防并发竞态。
> 2. **四个 ID 概念严格区分**。这里有个我踩过的坑：DLQ 重放如果新建 run，工具幂等键 `run_id:tool_call_id` 就变了，等于绕过幂等把已经成功的退款再执行一遍。所以重放必须复用原 `run_id`。
> 3. **跨进程互斥**。同一会话串行、不同会话并发，用 Redis 锁，释放时用 Lua 做 owner 比对再删——不能简单地 `SETNX` 然后 `DEL`，那会误删别的 worker 刚拿到的锁。
> 4. **崩溃后从 checkpoint 续跑，不是从头重跑**。这是一个真实的 bug：LangGraph 的 `ainvoke(state, cfg)` 会从 START 重新执行并覆盖 channel 值，只有传 `None` 才从 checkpoint 的 `next` 续跑。
> 5. **副作用幂等 ledger**。写操作工具按 `(工具名, run_id:tool_call_id)` 唯一约束，重试和崩溃恢复都不会重复执行。
>
> **第二个：Tool Result Context Engineering**。工具返回值经常巨大，直接进 context 会挤爆预算。我做了确定性压缩、Top-K 预算、offload 到外部存储再按需恢复、以及 scope-safe 的精确复用缓存。
>
> **第三个：降级设计**。LLM 熔断器连续失败后跳闸，降到本地规则分类器；checkpoint 在生产初始化失败是 fail-closed，绝不静默回退到内存。
>
> **第四个：部署架构**。Nginx TLS 终止 + Gunicorn 多 Worker + 独立的 Celery worker service + PostgreSQL + Redis + Prometheus/Grafana 监控栈，Docker Compose 一键部署。
>
> ### 第四段：反思与改进（30 秒）
>
> 有做得不够的地方，我说清楚：
>
> 1. **ERP 是 Mock 的**——适配器工厂和接口抽象都做好了，真实对接要企业配合。
> 2. **分布式 runtime 我只做到 CI 验证**。测试用的是真实 PostgreSQL、Redis 和多进程 worker，也写了 SIGKILL 整个 worker 进程组的混沌测试，但**真实生产集群、多副本长期稳定性、真实 ERP 写操作都没验证过**。当前 Redis 锁在 owner 长时间卡顿超过 TTL 的情况下没有 fencing token，这是一个我明确知道但还没解决的缺口。
> 3. **RAG 评测**。我搭了完整的可复现评测流水线——4 种检索配置做 ablation、多 K 值指标、失败分类、带 provenance 的 artifact 和 preflight 门禁。但当前 provider 凭据失效，正式 649-query 指标是 NOT_VERIFIED，所以**我不会报 Hit/MRR 数字**，只能讲评测方法论。
> 4. **前端 CSP**：`script-src` 已经用 nonce 去掉了 `unsafe-inline`，但 `style-src` 还留着 `unsafe-inline`，因为主题切换和动态样式需要，还没收敛到 CSS 变量。
> 5. **安全升级**：密码哈希从 PBKDF2 升到 Argon2id，主要收益是 memory-hard 属性。具体提速倍数我没做基准测试，所以不报数字。
>
> 这段反思对我自己最大的价值是：**清楚区分"代码里存在"、"测试验证过"、"生产验证过"这三件事**。分布式 runtime 我能诚实地说"CI 验证通过、生产未验证"，而不是含糊地讲成"已经生产可用"。

---

## 准备面试追问

深度问答见 [interview-deep-dive.md](interview-deep-dive.md)；
题库与优先级见 [../interview-questions-final.md](../interview-questions-final.md)。

### Q1：为什么用 LangGraph 而不是 LangChain Agent 或自己写状态机？
> LangChain Agent 是黑盒，控制流不透明。LangGraph 的 `StateGraph` 提供声明式节点和条件边，我能精确控制什么时候切哪种协作模式，调试时状态流转也清晰——checkpoint 状态可以直接 dump 出来。多 Agent 系统里这种可控性是刚需。

### Q2：缓存命中率真的有 70% 吗？
> 70% 是设计目标区域内的估计，不是实测的生产指标。上线后通过 `/api/cache/stats` 和 Prometheus 观测真实值；拿到生产观测之前只能叫估算。而且要区分收益语义：缓存命中跳过的是 Router/Agent/LLM 链路，不是"零延迟"。

### Q3：为什么不用 OpenAI 官方 SDK？
> 三个原因：按 base_url 做连接池隔离（不同模型不同地址）；要和自研熔断器集成；Function Calling 降级——有些模型不支持 tools，400/422 时回退纯文本模式。官方 SDK 不支持这些定制。

### Q4：并发安全怎么保证？多进程呢？
> 分两层。**单进程内**用 `asyncio.Lock` 保护共享状态——熔断器状态转换、MetricsCollector 计数器、SessionManager 写入。**多进程/多副本**靠 Redis：同一会话的分布式锁 + PostgreSQL checkpoint + Redis session，这三件套生产启动时强制校验，缺一个直接 fail-fast。

### Q5：为什么不是 exactly-once？
> 分布式系统里端到端 exactly-once 代价极高，而且做不到。我选择明确声明 at-least-once，然后把幂等做实：run 级终态重复投递 no-op、thread 级 Redis 锁、工具级副作用 ledger 三层。更关键的是我会讲清楚 ledger 的边界——它只保证**同一个 Agent 不重复发起同一副作用**，如果下游 ERP 需要端到端幂等，得下游 API 接受 idempotency key，这不是我这边能单方面保证的。

### Q6：worker 崩溃了会发生什么？
> 三层保护。消息层面 `acks_late` + `reject_on_worker_lost` + Redis visibility timeout，未 ACK 的任务会重新可见。执行层面数据库里的 worker lease 过期后新 worker 可以接管，attempt 加一。状态层面从 PostgreSQL checkpoint 的下一个节点续跑，不是从头重跑。副作用层面靠 ledger 保证不重复。

### Q7：DLQ 里的失败任务怎么办？
> 我做的是 application-level dead-letter，不是 broker-native DLX：独立的 `agent_dead_letters` 表存不可变历史（run_id、attempt_count、error_type、error_code、进入时间），配一个查询接口和一个 CLI 重放工具。重放是人工介入的，而且**必须复用原 run_id**，否则工具幂等键就失效了。

### Q8：RAG 效果怎么样？——怎么回答才可信？
> 我不会当场报百分比。分三层讲：**机制**——改写 → 向量+BGE → BM25 → RRF 融合 → reranker 重排；**评测设计**——4 种检索配置做 ablation，多 K 值指标，三个不同的分母口径，失败按分类记账，artifact 带 git SHA 和数据集 sha256；**证据边界**——当前正式指标因为 provider 凭据失效还是 NOT_VERIFIED，所以只讲方法论。这套"没有 provenance 就不报数字"的做法本身就是工程成熟度的体现。

### Q9：这个项目最值得改进的是什么？
> 加 fencing token。目前 Redis 锁靠 owner token 和 TTL，owner-safe 释放能防"旧 owner 误删新锁"，但如果某个 worker 因为 GC 或宿主机卡顿 pause 超过 TTL，它恢复后可能和新的 owner 同时改同一个 thread。我已经做了执行期间的 lease 续租来把窗口压到很小，但严格的解法是引入 fencing token 或者数据库版本号校验，让旧持有者的写入无条件被拒。

### Q10：前端是怎么实现的？
> 原生 JavaScript ES Module，没用框架。理由是可嵌入（widget 页能直接嵌到任意网站）和体积小。实现了聊天界面、主题切换、无障碍支持、SSE 真流式、WebSocket 实时通信和管理后台。

---

## 相关文档

- 分布式 runtime 完整设计：[agent-runtime.md](agent-runtime.md)
- 架构边界与可靠性语义：[distributed-agent-runtime.md](distributed-agent-runtime.md)
- 状态归属（checkpoint / session / cache / tool store 的区别）：[runtime-state-ownership.md](runtime-state-ownership.md)
- 证据边界（能宣称什么、不能宣称什么）：[../reference/distributed-runtime-interview-evidence.md](../reference/distributed-runtime-interview-evidence.md)
- 当前事实入口：[../reference/current-state.md](../reference/current-state.md)