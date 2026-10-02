# 药妆智多星 — 面试题集·最终版

> **口径纪律（先读这一段）**：
> - 分布式 Agent Runtime 是本项目**当前最强工程亮点**，面试官问到"最难的技术问题"
>   /"架构上最大的改造"时**应该**往这里引。一面/二面已按此重新排序必问优先级。
> - 分布式 runtime 的证据等级是 **Level 2 = CI VERIFIED**（真实 PostgreSQL + Redis
>   + 多进程 Celery + SIGKILL 混沌测试）；**Level 3（真实生产集群 / 多副本长期运行 /
>   真实 ERP 写操作 / K8s autoscaling）是 NOT_VERIFIED**。候选人若把 Level 2 说成
>   "生产集群已验证"，是**减分项**。
> - RAG 当前 649-query 正式指标 **NOT_VERIFIED**（preflight 显示 provider auth
>   blocker）；任何百分比必须绑定 provenance-bearing artifact
>   （详见 `docs/reference/rag-evaluation.md`）。
> - 9 个 Agent 角色、Response Cache 与 Tool Result cache 是独立机制、
>   Qdrant + BM25 lifecycle、retrieval contract、scope-safe offload/recovery。
>   不要用旧的"8 agents""二级缓存"、ChromaDB 口径。
> - 测试数量以 `pytest --collect-only -q` 当前输出为准，不背数字。
>
> 分布式 runtime 的深度问答骨架见
> [`docs/design/interview-deep-dive.md`](design/interview-deep-dive.md) 的 **R1–R11**。
>
> 本题集编号按「轮次-Q序号」局部编号（每轮重新计数），下文的 D/R 前缀题号用于
> 指代分布式 runtime 新增题组。

---

## 一面（技术深度面，45分钟，项目占20分钟）

> ⏱ **用时提醒**：7 个必问 + 追问，平均 3min/题。前 3 题控制节奏，候选人超过 2 分钟没到核心点可温和打断引导。

### LangGraph 与多Agent架构（6分钟）

**Q1 【必问】** 请画一下整个系统的架构图，描述数据从用户输入到最终响应的完整链路。

**Q2 【必问】** LangGraph 中 5 种协作模式，分别在什么场景下使用？切换的判断逻辑是什么？

> **追问：** 模式升级到 ReAct 后，如果质量还是不达标，系统会怎么做？有最大尝试次数吗，还是可以无限升级？

**Q3 （备选）** MessageBus 和 SharedBlackboard 的区别是什么？为什么不统一用一种？

**Q4 （备选）** 9 个 Agent 角色之间的通信是同步还是异步？遇到过死锁或竞态条件吗？

### 意图识别与路由（4分钟）

**Q5 【必问】** LLM 分类器和规则分类器并行执行，两个结果冲突时怎么处理？

> ⚠️ **注意**：如果候选人在 Q5 已经详细讲了路由逻辑，后面的 Q9 可缩减为"刚才你提到规则分类器输出置信度，具体怎么算的？"

**Q6 （必问/备选）** 熔断器"连续5次失败降级"——滑动窗口怎么实现的？

> **追问：** 半开状态（Half-Open）下放过的探测请求，走的是完整 LLM 链路还是简化链路？探测成功后怎么恢复？

**Q7 （备选）** 复杂度评分的具体特征有哪些？阈值怎么确定的？

> 💡 **简历提示**：`router/query_router.py` 中 `_estimate_complexity()` 基于 query 长度、实体数量、意图模糊度等特征评分。

**Q8 （备选）** Response Cache 的语义层（L2/L3）具体怎么做的？动态阈值（短文本/长文本不同）的策略是什么？

> 💡 **简历提示**：`cache/response_cache.py` 当前为三层 Response Cache：L1 Redis MD5 精确 + L2 Qdrant 语义（BAAI/bge-large-zh-v1.5）+ L3 Jaccard 回退（jieba 分词 + 倒排索引 + 动态阈值）。Tool Result exact reuse cache / Store / 压缩是独立机制（ADR-006），不要混为"二级/三级缓存"。

**Q9 【必问】** 路由捷径机制：置信度 ≥0.75 是怎么确定的？规则分类器如何评估置信度？捷径跳过的 LLM 调用量大概占比多少？

> **追问分支（面试官根据回答选择其一）：**
> - 若候选人说置信度是规则分类器输出的概率值 → 这个概率值怎么校准的？做过置信度修正吗？
> - 若候选人说这是人工设定的阈值 → 怎么验证 0.75 是最优的？试过 0.7 或 0.8 吗？

**Q10 （备选）** asyncio.gather 处理 LLM 和规则分类并行时，其中一个超时或抛出异常怎么兜底？另一个结果还能用吗？

### 状态管理（4分钟）

**Q11 【必问】** 滑动窗口裁剪和 LLM 摘要压缩，什么情况下触发摘要压缩？压缩后追问"我刚才说的那个产品"还能定位吗？

> **追问（二选一）：**
> - 若候选人说窗口裁剪为主 → 裁剪掉的历史消息里如果有重要信息（如订单号），后续还能引用吗？
> - 若候选人说摘要压缩为主 → 压缩的 LLM 调用本身也消耗 Token，这笔开销算过吗？和直接保留原文比哪个更划算？

**Q12 【必问】** 对话漂移检测的 4 种类型，能分别举一个实际的例子吗？自动修复具体做了什么？

**Q13 【必问】** 响应质量评估的维度有哪些？评估结果怎么驱动协作模式升级？能讲一个从 Sequential 升级到 ReAct 的具体链路吗？

> **追问：** 用户发了一条消息，系统开始走 Sequential→Parallel 升级流程。升级还没跑完时，用户又发了第二条消息。此时你会让第二条消息等待第一条跑完，还是立即并行处理？如果并行，共享状态怎么隔离？

**Q14 （备选）** ContextVar 按 Session 隔离黑板数据——为什么选择 ContextVar 而不是传参？多用户并发场景下出过数据污染问题吗？

### 分布式 Agent Runtime（4分钟）— 当前最强工程亮点

> 这组是本轮**优先准备**部分。问法要开放，让候选人自己展开；不要像考背诵题那样逐条追问。

**D1 【必问】** 你的系统怎么支持多进程/多副本部署？多个 Gunicorn worker 之间怎么共享状态？

> ⚠️ **合格线**：候选人应能说出**三件套**——checkpoint 用 PostgreSQL（不是内存）、
> session 用 Redis（不是进程内）、同一会话加 Redis 分布式锁。并能指出这三者缺一个
> 在多副本下会怎样错。只说"加 Redis 就行"是**不合格**。
>
> **追问：** 这三个是"配置"还是"代码"保证的？有人配错了会怎样？
> → 加分回答：生产启动时 fail-fast 校验，缺一个直接拒绝启动；`GUNICORN_WORKERS>1`
> 时额外要求 checkpoint=postgres + session=redis + redis 锁。

**D2 【必问】** 异步的 Run 是怎么保证不重复执行的？

> ⚠️ **筛人点**：候选人主动区分 **at-least-once vs exactly-once** 是关键。
> 说"exactly-once"或含糊带过是明显减分。
>
> **加分回答**：分三层讲幂等（run 级终态 no-op / thread 级锁 / 工具级 ledger），
> 并**主动说出 ledger 的边界**（只保证同一 Agent 不重复发起副作用，
> 下游 ERP 端到端幂等要靠对方接受 idempotency key）。

**D3 【必问】** worker 执行到一半挂了，会发生什么？

> ⚠️ **筛人点**：候选人是否知道"续跑"和"从头重跑"的区别。
>
> **加分回答**：主动提这个真 bug——LangGraph `ainvoke(state, cfg)` 会从 START 重跑并
> 覆盖 channel 值，只有 `ainvoke(None, cfg)` 才从 checkpoint 的 `next` 续跑。
> 以及用 `killpg(SIGKILL)` 杀整个进程组做混沌测试（只杀父进程测不到东西），
> 并且断言"第一个节点没重跑"而不只是"最终成功"。

**D4 （备选）** 分布式锁怎么实现的？为什么不能简单地 `SETNX` 然后 `DEL`？

> ⚠️ **筛人点**：候选人能否自己讲出"旧 owner 误删新锁"这个竞态。
>
> **加分回答**：owner token + TTL + Lua 原子 compare-and-delete；
> 主动声明这是**单 Redis** 互斥，没实现 Redlock 集群；
> 提到 API 执行边界和 worker 共用同一 key namespace（用契约测试锁死）。

### 知识检索（3分钟）

**Q15 【必问】** 查询改写是怎么做的？改写后怎么保证语义不漂移？

**Q16 （备选）** Qdrant 4 个领域集合的划分依据？跨集合检索怎么做？（v6.0 从 ChromaDB 迁移至 Qdrant，迁移动机和收益是什么？）

**Q17 （备选）** RRF 为什么选 k=60？调过吗？不同取值对结果有什么影响？

### 基础功底验证（3分钟）

**Q18 （备选）** asyncio.gather 的异常处理机制？如果其中一个协程抛异常，其他的会怎样？

**Q19 （备选）** 为什么选 SSE 做流式输出而不是 WebSocket？踩过什么坑？

> 必问 **10 题**（含 D1/D2/D3），备选 **8 题**（含 D4）

---

## 二面（系统设计与工程能力面，50分钟，项目占20分钟）

### 架构设计决策（5分钟）

**Q1 【必问】** 为什么选择 LangGraph 而不是 AutoGen / CrewAI / 自研状态机？做过技术选型对比吗？

> **条件追问（仅当候选人主动提到其他框架时使用）：** 如果今天重新选型，会考虑 Semantic Kernel 或 Rig 吗？这两个和 LangGraph 的范式差异是什么？

**Q2 【必问】** 如果让你重新设计这个系统，架构上会做哪些改变？

**Q3 （备选）** 9 个 Agent 角色的粒度怎么确定的？有没有考虑过合并或拆分？

### RAG 质量与效果（4分钟）

**Q4 【必问】** RAG 检索质量怎么评估？分析过 bad case 吗？举一个检索失败的例子，后来怎么改进的？

> 💡 **加分口径**：评测链是 `scripts/evaluate_rag.py`（4-config ablation：
> vector_only / bm25_only / hybrid_no_rerank / hybrid_rerank）+ multi-K 指标
> + 三套 population 分母；bad case 走 failure taxonomy
> （TIMEOUT / PROVIDER_ERROR / GOLD_NOT_INDEXED / MISS_ALL / LOW_RANK）逐条记账。
> 当前 649 正式指标 NOT_VERIFIED（provider 401 blocker）；历史 30-query 数字
> 只能以"历史口径"叙述。

**Q5 【必问】** 查询改写 + 双路重排 + RRF，这套链路的端到端效果怎么验证？有没有对比过"去掉某个环节"的效果差异？

> 💡 **追问弹药（ablation 设计）**：
> - 为什么不能只报一个 Recall？→ Hit@3/Recall@8/Precision@3/NDCG/MRR 各回答不同问题（用户是否在前几条看到 vs 全量覆盖 vs 展示质量 vs 排序）
> - hybrid_no_rerank vs hybrid_rerank = 重排净贡献（逐 query improved/degraded 计数，不许只报提升）
> - all_queries / retrieval_eligible / full_gold_covered 为什么都要？→ 系统级端到端 vs retriever 能力 vs 算法纯净口径；引用必须注明 population
> - GOLD_NOT_INDEXED 怎么记账？→ 主口径计 0 + 逐查询标记，不静默丢弃
> - provider 401 为什么 fail closed？凭据失效时"继续跑"会产出无意义指标并可能被静默降级掩盖
> - reranker silent fallback 为什么危险？preflight 探针识别 silent_fallback = 不产出假阳性；两实验结果完全一致本身就是重排未生效的证据
> - 数字的 provenance：artifact 带 git SHA + benchmark sha256，基准变了历史数不能直接比

**Q6 （备选）** A/B 测试 prompt 变体分配怎么做的？SHA-256 确定性分流怎么保证同一个用户永远看到同一个变体？变体效果怎么对比？

### Token 成本管控（3分钟）

**Q7 【必问】** Token Quota 双后端（Redis 生产 + 内存开发自动降级）——Redis 挂了时内存降级后配额统计如何处理？恢复后数据怎么同步？

**Q8 【必问】** 路由捷径 + Token Quota + 全局限流，这三者如何配合系统性控制成本？能估算一下实际节省的 Token 量吗？

### 性能与可观测性（4分钟）

**Q9 【必问】** SLA 基线（Sequential 15s / Parallel 20s / ReAct 30s 配置超时）——哪个环节决定了达标差异？瓶颈在哪？做过哪些优化？

> ⚠️ **口径**：SLA 相关只谈**配置目标**（`SLA_*` 配置）与监控告警机制；
> 端到端真实延迟分布是 `NOT_MEASURED`，不要把配置目标说成"实测 P95"。

> **追问：** 你能拆一下端到端链路里每个环节的耗时分布吗？比如 LLM 调用、检索、后处理各占多少？如果不知道具体数值，可以估算一下比例——哪一段是你觉得最需要优化的？

**Q10 （备选）** 系统上线后有没有监控体系？关键指标有哪些？怎么发现和定位问题的？

> 💡 **简历提示**：`core/monitoring.py` 有 Prometheus 指标（cache_hit_rate、llm_call_duration、collaboration_mode_counter 等），支持 Grafana 大盘。

### 分布式运行时与可靠性边界（8分钟）— 本面核心

> 这组是**二面的主战场**。这里最容易区分"做过工程"和"看过工程博客"。
> 详细追问弹药见 `docs/design/interview-deep-dive.md` 的 R1–R11。

**R1 【必问·第一题就问】** 你说这个分布式 Runtime 已经"验证通过"了——具体验证到哪一步？边界在哪？

> ⚠️ **这是本套题最重要的一题。** 观察候选人是否**主动**划出证据边界。
>
> **合格线（必须答出）**：Level 1 代码存在 / Level 2 真实 PostgreSQL + Redis +
> 多进程 Celery 的自动化验收 + SIGKILL 混沌测试，**CI VERIFIED** /
> Level 3 真实生产集群、多副本长期运行、真实 ERP 写操作**未验证**。
>
> **减分项**：把 CI 验证说成"生产集群已验证"；或含糊说"基本都验证过了"。
> **加分项**：能说出证据 artifact 带 `tested_code_sha` + `generated_at`，
> 且"没跑"和"跑过但失败"是两回事（fail-closed，不是同一件事）。

**R2 【必问】** `thread_id`、`run_id`、`task_id` 分别是什么？为什么必须分开？

> ⚠️ **筛人点**：候选人是否意识到同一个 run 重投会有**多个** task_id，
> 所以业务幂等不能锚在 task_id 上。
>
> **加分回答**：主动讲 DLQ 重放的坑——换新 run_id 会让工具幂等键
> `run_id:tool_call_id` 失效，把已成功的退款再执行一遍，所以必须复用原 run_id。

**R3 【必问】** checkpoint、session memory、response cache、tool result store，这四个有什么区别？

> ⚠️ **筛人点**：这四个如果被混着讲，基本可以判定没真正理解状态架构。
>
> **合格线**：checkpoint = 执行到哪一步（按 thread_id，支撑崩溃恢复）；
> session memory = 聊了什么（支撑上下文连贯）；response cache = 这个问题的答案
> （支撑重复问题短路）；tool store = 工具返回的大块数据（支撑 context 预算）。
> 并能说出它们**不能互相替代**。

**R4 【必问】** 锁的 TTL / lease / fencing token 到底解决什么？你有没有做 fencing？

> ⚠️ **这是深度筛子题。** 诚实回答"没做 fencing，我知道这是缺口"**优于**
> 含糊地说"有 TTL 所以很安全"。
>
> **加分回答**：说清已做的缓解（执行期间 lease 续租 + 续租失败有计数器 +
> 启动强制 TTL > 任务时限 + 余量）；说清残余风险（GC/宿主机卡顿 pause 超过 TTL 时
> 旧 worker 与新 owner 可能同时写，终态会被条件更新挡下但**节点副作用**仍需幂等）；
> 并知道严格解法是 fencing token 或 DB 版本号。

**R5 【必问】** Redis 挂了 / PostgreSQL 挂了分别会怎样？

> ⚠️ **筛人点**：能否区分"关键的正确性依赖必须 fail-closed"和
> "观测性的东西可以降级"。
>
> **合格线**：锁不可达 → 不执行、延迟重投（fail-safe，不 fail-open）；
> checkpoint 生产初始化失败 → **绝不静默回退内存**；
> session 不可用 → 生产启动就 fail-fast；
> 事件流不可达 → no-op 但不影响业务状态。

**R6 【必问】** retry 是怎么设计的？退避策略是什么？用尽之后呢？重试的投递本身失败了怎么办？

> ⚠️ **加分点**：候选人若主动讲"先写库再投递"这个顺序选择，并说明它的方向性
> （投递失败留下可观测可重放的 `RETRYING`，而不是静默丢失），是很好的工程信号。
>
> **加分回答**：区分 transient/permanent，指数退避 + jitter + 硬上限，不无限重试；
> 用尽 → `DEAD_LETTER` + 不可变 DLQ 历史 + **人工**重放（不是 broker-native DLX）。

**R7 【必问】** 工具副作用的幂等 key 是怎么设计的？key 冲突了怎么办？

> ⚠️ **筛人点**：候选人是否想过"用参数哈希做 key"会误杀合法的重复操作。
>
> **加分回答**：`operation_key = run_id:tool_call_id`；认领租约未过期时第二个执行者
> **不得**重复触发副作用，而是抛 transient 退避重投（宁可退避也不重复扣款）；
> 同 key 不同参数 → `PermanentError`。

**R8 【必问】** 为什么快路径 `/api/chat` 不走 worker？

> ⚠️ **筛人点**：这是**主动的架构取舍**还是**没做完**？
>
> **合格线**：快路径要低延迟，队列+轮询引入额外延迟且 SSE 连接生命周期和 worker
> 任务生命周期对不上；两条路径共享同一 checkpoint 后端所以状态连贯。
> **加分项**：提到用契约测试直接读源码断言快路径**不含**任何 `apply_async`/Celery 调用，
> 防止后人改坏。

**R9 （备选）** 你说引入了 Celery，为什么不用 Temporal / Kafka / Sidekiq？

> ⚠️ **注意**：这是选型开放题，重点听**取舍是否诚实**，不要求背答案。
> 合理回答包括：当前规模下 Redis broker 够用、Temporal 运维成本不划算、
> Kafka 是为吞吐而非调度语义。**若候选人说"Kafka 未来会上"却不给触发条件，
> 属于空泛承诺。**

**R10 （备选）** 你的 Run 事件流是怎么跨进程传到浏览器的？它的投递语义是什么？

> ⚠️ **筛人题**：Redis Streams 很容易被说成"at-most-once"——**这是错的**。
> 正确答案：best-effort resumable；单连接内 cursor 单调前进所以不重复，
> 但用**较旧的** `Last-Event-ID` 重连会**重放已处理事件**（重复）；
> `replay=false` 与 idle 超时造成缺口；`MAXLEN ~` 是**近似**裁剪，
> 被裁掉的历史不可恢复，且近似裁剪**不是硬上界**。事件流**不是业务真相源**。

### 高可用与容错（3分钟）

**Q11 【必问】** 如果 LLM 调用超时或返回异常，整个链路怎么兜底？降级策略是什么？

> ⚠️ **与一面 Q6 的区分**：一面问的是熔断器机制本身（滑动窗口如何计数、如何恢复）。本题侧重全局降级链路——路由捷径→熔断器→规则引擎是如何层层配合的。注意引导候选人不要重复熔断器细节。

**Q12 （备选）** 多个 Agent 同时访问 SharedBlackboard，有没有并发写入冲突？ContextVar 引入后解决了什么具体问题？

### 追问（3分钟）

> ⏱ **时间紧张时 Q13/Q14 可合并为**："如果并发从 200 到 20000，你觉得架构上最大瓶颈在哪？结合你遇到的某个具体困难来说。"

**Q13 【必问】** 项目中你遇到过最难的技术问题是什么？怎么解决的？

> ⚠️ **期望答案方向**：候选人**应该**引到分布式 runtime 的续跑 bug
> （`ainvoke(state, cfg)` 从 START 重跑并覆盖 channel 值，只有传 `None` 才从
> checkpoint 的 `next` 续跑），或 SSE + checkpoint 的序列化冲突
> （`stream_callback` 作为 channel 被 checkpointer 序列化，抛
> `TypeError: Type is not msgpack serializable` —— 内存 saver 和官方 PostgreSQL saver
> **都**失败，生产默认 PostgreSQL 时 `/api/chat/stream` 必然报错）。
>
> 这两题都优于"调 prompt""改阈值"这类答案。若候选人答不出真实踩过的坑，
> 继续追问"那你最近一次推翻自己设计是什么时候？"

**Q14 【必问】** 如果并发量从 200/天增长到 20000/天，架构上需要做哪些改造？

> 💡 **诚实回答的样子**：区分"已经有机制但没压测"和"还没做"。
> 已有：Celery worker 横向扩展、`AGENT_RUN_QUEUE` 多队列基础、Redis 锁与
> checkpoint 已经跨进程正确。**未做**：Kubernetes / HPA、队列背压
> （入队限流 / 最大 in-flight / 拒绝策略）、worker `SIGTERM` 优雅停机、
> fencing token。以及**生产级吞吐数字当前 NOT_MEASURED**。
>
> ⚠️ **减分项**：给出具体的 QPS / P99 数字而无 artifact 支撑。

> **Q11** 编号延续原高可用段（LLM 异常兜底），原 Q13/Q14 见"追问"段。
>
> 必问 **10 题**（其中分布式运行时 R1–R8 为 8 题，全部必问），备选 **7 题**（含 R9/R10）

---

## 三面（综合能力与潜力面，40分钟，项目占10分钟）

### 技术视野（4分钟）

**Q1 【必问】** 你关注 MCP、Context Engineering 这些前沿方向——这些技术会怎么影响 Agent 系统的演进？你的系统如果引入 MCP 会有什么变化？

> **追问：** 如果引入 MCP，你系统里的 9 个 Agent 角色中哪些可以 MCP 化？哪些不行？为什么？判断标准是什么？

**Q2 （备选）** 多 Agent 系统目前有几个主流范式（状态机、Actor 模型、黑板模式），你怎么看各自的优劣？

### 业务理解与 Owner 意识（3分钟）

**Q3 【必问】** 你通过什么方式衡量这个系统的效果？如果让你设计指标体系，你会关注哪些指标？

> **追问：** 如果产品经理说"用户投诉回答质量下降了"，你怎么区分是模型能力不够、检索没召回还是 prompt 写坏了？你的排查工具体系是什么样的？

**Q4 （备选·当 Q3 追问时间不够时替代使用）** 如果产品经理说"用户投诉回答质量下降了"，你的排查思路是什么？

> ⚠️ **注意**：Q4 与 Q3 的追问高度重复，面试时不要两个都问。

### 团队协作与自我认知（3分钟）

**Q5 （备选）** 这个项目你和谁协作？你负责哪些模块？有没有和其他同学产生过分歧？怎么解决的？

**Q6 【必问】** 这个项目你最自豪的一个技术决策是什么？为什么？

> 必问 **3 题**，备选 **3 题**

---

## 附录：候选准备建议（三档策略）

### 第一档：必准备（优先做分布式运行时）

> **优先级已重排**：分布式 Agent Runtime 是当前最强的工程亮点，面试官问
> "最难的技术问题" / "架构上最大的改造" / "怎么支持多副本" 时**应该**往这里引。
> 下面标 ⭐ 的是新增/提升的运行时题。

| 题号 | 准备清单 |
|------|---------|
| ⭐ 二面 R1 | **证据边界**：Level 1/2/3 怎么划，"CI 验证"≠"生产验证" |
| ⭐ 二面 R2 | 四个 ID/概念区分 + DLQ 重放为什么必须复用原 run_id |
| ⭐ 二面 R4 | TTL / lease / fencing 的边界；**主动承认没做 fencing** |
| ⭐ 二面 R5 | Redis / PG 分别挂掉的行为；fail-closed vs 降级的区分 |
| ⭐ 二面 R7 | 工具幂等 key 设计；认领租约；冲突怎么处理 |
| ⭐ 二面 R8 | 为什么快路径不走 worker（是取舍不是没做完） |
| ⭐ 二面 Q13 | 最难的技术问题 → 续跑 bug 或 SSE checkpoint 序列化 bug |
| ⭐ 二面 Q14 | 扩容改造：区分"已有机制"与"未做"，不报无据数字 |
| ⭐ 一面 D1 | 三件套：PG checkpoint + Redis session + Redis 锁 |
| ⭐ 一面 D2 | at-least-once + 三层幂等 + ledger 边界 |
| ⭐ 一面 D3 | worker 崩溃三层恢复；续跑 vs 从头重跑 |
| 一面 Q1 | 在白板上画过一遍架构图（四层状态机 + 数据流箭头 + 分布式 runtime 分叉） |
| 一面 Q2 | 5 种协作模式的场景 + 切换判断条件 + 终止条件 |
| 一面 Q5 | LLM vs 规则冲突的 3 种处理策略 |
| 一面 Q9 | 置信度来源 + 0.75 怎么定的 + 估算捷径占比 |
| 一面 Q13 | 5 维质量维度 + Sequential→ReAct 链路 + 并发消息处理 |
| 一面 Q15 | 查询改写的 prompt 设计 + 防漂移机制 |
| 二面 Q4 | 一个真实的 bad case + 改进前后对比（用 taxonomy 归类） |
| 二面 Q5 | 4-config ablation 问题清单（见 Q5 追问弹药）+ provenance 语义 |
| 二面 Q7 | Redis 降级的完整流程：detect→fallback→sync |
| 二面 Q9 | 各环节耗时估算（明确标注为"我的估算/假设"，端到端分布当前 NOT_MEASURED） |
| 二面 Q11 | 全局降级链路：路由捷径→熔断器→规则引擎的层层兜底 |

> ⚠️ **红线**：以上任何一题都**不能**把 Level 2 说成"生产集群已验证"，
> 也**不能**给无 artifact 支撑的 RAG 百分比或生产 QPS/P99。

### 第二档：熟悉即可（6 个）

| 题号 | 准备框架 |
|------|---------|
| ⭐ 一面 D4 | 分布式锁：owner token + Lua 原子释放；讲出"旧 owner 误删新锁"竞态；声明非 Redlock |
| ⭐ 二面 R6 | retry 分类 + 退避 + DLQ；能讲"先写库再投递"的顺序选择 |
| ⭐ 二面 R10 | 事件流投递语义（**不是** at-most-once，重连会重放） |
| ⭐ 二面 R9 | 为什么不选 Temporal / Kafka（看取舍是否诚实、有无触发条件） |
| 一面 Q3 | MessageBus = 事件驱动 pub/sub；SharedBlackboard = 共享状态读写 |
| 一面 Q14 | ContextVar 解决多 session 数据污染 + 对比传参的优劣 |
| 二面 Q6 | A/B 分流流程：user_id → SHA-256 hash → 模 N 分流 |
| 三面 Q1 | MCP 对系统的影响 + 哪些 Agent 可以/不可以 MCP 化 |
| 三面 Q3 | 指标体系 + 排查链路 |

### 第三档：了解即可（5 个）

| 题号 | 策略 |
|------|------|
| 一面 Q10/18 | asyncio.gather 基础——面试前过一遍文档 |
| 一面 Q19 | SSE vs WebSocket——准备 2 个关键区别 |
| 一面 Q16/17 | 集合划分 / k 值——真实项目经验回答 |
| 三面 Q6 | 准备一个真实自豪的技术决策和 why |

---

## 附：分布式 runtime 速查（答题时可直接引用的代码位置）

| 主题 | 位置 |
|---|---|
| AgentRun 状态机 | `runtime/statuses.py` |
| AgentRun 真相源 / 租约 / DLQ | `runtime/run_service.py` |
| worker 执行与失败分类 | `runtime/executor.py`（含 `_heartbeat_loop`） |
| checkpoint 续跑判定 | `runtime/bootstrap.py::invoke_graph_with_resume` |
| Celery 配置（acks_late / visibility_timeout） | `runtime/celery_app.py` |
| 投递（payload 仅 run_id） | `runtime/dispatch.py` |
| per-thread 分布式锁 | `runtime/thread_lock.py`、`core/concurrency/distributed_lock.py` |
| 工具副作用 ledger | `runtime/side_effects.py`、`tools/tool_registry.py` |
| Redis Stream 事件 | `runtime/events.py` |
| 生产 fail-fast 校验 | `core/config.py::validate_distributed_runtime_settings` |
| API 表面 | `api/routes/runs.py` |
| 设计 / 边界 / runbook | `docs/design/agent-runtime.md`、`docs/design/distributed-agent-runtime.md`、`docs/operations/distributed-runtime-runbook.md` |
| 证据边界 | `docs/reference/distributed-runtime-interview-evidence.md`、`docs/reference/current-state.md` |
| 真实基础设施验收 | `tests/integration/runtime/`、`make runtime-e2e`、`make runtime-chaos` |

---

*版本：v2.0 · 2026-10-02 Repository Truth Convergence 重排：新增分布式 Agent Runtime 题组
（一面 D1–D4 / 二面 R1–R10），必问优先级从"业务链路优先"改为"分布式运行时优先"，
证据边界（Level 1/2/3）写入题集抬头作为红线。RAG 口径沿用 PR #19 evidence pipeline
（当前正式指标 NOT_VERIFIED）。简历文本以 `docs/reports/resume-description.md` 为冻结证据快照，
不在本文档改写。*
