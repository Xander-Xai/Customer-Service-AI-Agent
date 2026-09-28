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
