# Tool Result Context Engineering V2 面试材料

## 30 秒版

这个项目的 ReAct 工具结果会进入 ToolMessage，并在后续轮次重复携带。我
在工具边界做了可回滚的确定性压缩：结构化字段裁剪、去重、Top-K、预算和
历史 compaction；结果太大时可选地放进带 TTL、作用域隔离的 Store，只把
opaque reference 和 bounded preview 给模型。恢复由应用层完成，Redis 故障
回退到本地压缩。所有 benchmark 都是本地估算，不冒充生产 token 或 API
latency 数据。

## 60–90 秒版

我先审计了真实链路：`execute_raw → ToolResultOptimizer → ToolMessage →
old-result compaction → next LLM call`。V1 保留 function-calling 配对关系，
不删除 ToolMessage，只替换旧内容；V2 增加了 Store protocol、内存实现和
复用现有 Redis URL 的实现。大结果超过阈值后保存原始 payload，ToolMessage
只保留状态、opaque reference、摘要、条数和可恢复标记。恢复接口在 agent
运行时执行，严格比较 user/session scope，过期、未知、损坏或 Redis 不可用
都安全降级。JSON、search、HTML 使用不同 compressor；semantic summary 是
带 timeout 的可选 fallback，默认关闭。ERP pagination 与 Agent Top-K 是
数据源层和上下文层两个不同问题。测试和 benchmark 只报告 estimated tokens
与 local latency，外部 API latency 是 `NOT_MEASURED`。

## 3 分钟深挖：15 个问题

1. **为什么膨胀？** 每轮 observation 进入历史，后续输入重复携带，增长是多轮累积而不是单条结果大小。
2. **为什么不是 `result[:2000]`？** 字符切片会破坏 JSON、截断 ID，也无法区分 continuation field 与 debug payload。
3. **Top-K 和 pagination？** Pagination 在数据源减少一次返回；Top-K 在 context 边界减少进入模型的记录。
4. **为什么不删除旧 ToolMessage？** 删除会破坏消息历史语义和 tool-call 配对；这里替换 content，保留位置。
5. **为什么保留 tool_call_id？** Function Calling 协议需要 AI tool call 与 ToolMessage 一一对应。
6. **Reference-based context 是什么？** 模型看到 bounded preview 和 opaque reference，应用层按受控接口恢复原始结果。
7. **为什么 Redis offload？** 让大 payload 离开 active context，并支持多实例共享；它是可选依赖且有 TTL。
8. **Redis 挂了？** 当前结果不伪造 reference，回退到 deterministic compact result；恢复失败返回 unavailable。
9. **如何防跨用户？** record 保存 scope，恢复必须 exact match；reference 本身不是授权凭证。
10. **为何 summary 默认关闭？** 额外 LLM call 会增加 token、latency、cost 和 hallucination risk。
11. **和 RAG compression 区别？** Tool Result compression 服务于 action loop 和可恢复协议；RAG compression 服务于证据相关性。
12. **和 Prompt Engineering 区别？** Prompt 改指令；Context Engineering 决定信息何时进入、保留、压缩或恢复。
13. **benchmark 测了什么？** 本地结构化/search/HTML/offload/recovery 的字符、token estimate、处理耗时和 golden outcome；没有测真实 provider API latency。
14. **最大 trade-off？** 节省 context 的同时会丢失细节，故保留 continuation fields、preview、TTL recovery，并让 feature flag 可回滚。
15. **为何是 runtime engineering？** 它涉及协议合法性、状态生命周期、权限、故障降级、观测和成本边界，不只是改 prompt。

## 证据边界

可以说“本地 benchmark 的 estimated token reduction”和“本地 compressor
latency”；不能说“生产 token cost reduction”“真实 API latency reduction”
或“真实 ERP 已支持分页”，除非另有独立、可复现的生产证据。

## 与 RAG 评测链的关系（2026-09-30，PR #19）

Context Engineering（本文档）与 RAG retrieval evaluation（
`scripts/evaluate_rag.py`，canonical：docs/reference/rag-evaluation.md）是两个
相邻但独立的问题：

- RAG 评测链回答"检索质量"：4-config ablation（vector_only / bm25_only /
  hybrid_no_rerank / hybrid_rerank）、multi-K（Hit@K / Recall@K /
  Precision@K / NDCG@K / MRR@K）、实测 stage latency、三套 population 分母
  （all_queries 主口径）、failure taxonomy、带 git SHA + benchmark sha256 的
  provenance artifact
- 面试时同样适用本文档的证据纪律：**当前 649-query 正式指标 NOT_VERIFIED**
  （provider 401 blocker 已留档），不得报"当前 Hit/MRR"百分比；可以讲
  ablation 设计、指标分母定义与失败记账
- 共同纪律：本地/评测证据 ≠ 生产结果；external API latency 一律 NOT_MEASURED

## Tool Result Cache Reuse：60–90 秒答案

在 Context Engineering V2 之后，我又把“减少上下文”和“减少工具执行”拆成
两个问题。Tool Result Compression 负责控制进入 LLM 的 Observation 大小，
而 exact Cache Reuse 负责避免相同的只读工具重复访问 ERP。实现上 cache
发生在真实 `execute_raw()` 前，以 tool name、canonical arguments 和
authenticated scope 构造 key，只对显式 read-only policy 开启，并按工具配置
TTL；订单、库存这类强时效数据 TTL 很短，写操作完全 bypass。Cache 命中后
返回的仍是 raw structured result，继续进入 ToolResultOptimizer 做压缩或
offload，所以 cache、compression 和 external store 三层职责独立。缓存故障
采用 fail-soft，退回真实工具调用，不把 Redis 变成 Agent 单点故障。当前是
exact cache，不是 semantic tool cache，也没有声称生产效果。

## Cache 面试问题

1. **Cache 和 Compression 有什么区别？** Cache 减少真实工具执行；Compression 减少进入 LLM 的 token。
2. **为什么 cache 在 compression 之前？** 缓存 raw structured result，命中后仍能按当前轮次预算重新压缩。
3. **为什么 Store 不等于 Cache？** Store 为已产生的大结果提供 reference recovery；Cache 为避免再次执行。
4. **为什么库存 TTL 很短？** 库存是强时效外部状态，正确性优先于 hit rate。
5. **为什么写操作不能缓存？** 缓存“发送成功”可能掩盖第二次实际未发送，破坏 side-effect correctness。
6. **为什么不做 semantic tool cache？** 结构化参数的细微差异可能改变执行语义，embedding 相似不等于可复用。
7. **为什么 key 要有权限 scope？** 相同参数不代表相同授权边界，scope 防止跨用户读取。
8. **Redis 挂了怎么办？** cache get/set 都 fail-soft，直接执行真实工具。
9. **为什么错误结果不缓存？** 瞬时故障会被 TTL 放大成持续故障。
10. **Cache Hit 后为什么仍需要 optimizer？** 命中解决执行次数，不解决当前 LLM context budget。
