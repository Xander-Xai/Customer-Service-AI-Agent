# Customer AI Agent Project Remediation Spec

> **Status:** Proposed remediation specification  
> **Created:** 2026-08-13  
> **Scope:** customer-service-ai-agent  
> **Source:** supplied cross-audit assessment plus current repository path verification  
> **Important:** numbers and findings quoted from the supplied assessment are audit snapshots. They are not completion evidence until the corresponding command, test, or benchmark artifact is rerun from the target checkout.

## 0. 文档目标

本文件是项目整改的唯一执行规格。目标不是继续扩充模拟题，而是完成一次 Project Truth Alignment：

    代码事实 → 整改 → 真实测试 → Benchmark Artifact → Claim Matrix → 简历 → 面试 Q&A

本文件用于：

1. 固化 P0/P1/P2 问题的边界、证据、预期架构和验收标准。
2. 防止修复单个 Bug 时顺便重构无关模块。
3. 区分当前代码事实、历史文档、外部业务数据和未验证假设。
4. 规定什么时候可以更新简历数字和面试答案。
5. 为后续 Codex 执行提供阶段门禁和 Definition of Done。

### 事实优先级

1. 当前目标 checkout 中的代码、配置、测试和可复现命令结果。
2. 带 git SHA、数据集哈希、配置和运行时间的真实 Benchmark Artifact。
3. 活文档和当前设计文档。
4. 历史审计、里程碑和发布说明。
5. 简历草稿、面试答案和模拟题。

### Claim 状态

| 状态 | 含义 |
|---|---|
| PROVEN | 有当前代码、测试或可追溯 Artifact 支持。 |
| PARTIAL | 部分链路存在，但主路径、边界或验收证据不完整。 |
| EXTERNAL | 来自真实业务环境，但不能由仓库证明。 |
| UNVERIFIED | 尚未完成当前 checkout 的可复现验证。 |
| FALSE | 与当前代码或定义直接冲突。 |

## 1. 当前项目事实基线

### 1.1 总体判断

代码事实、历史文档、简历描述和面试答案已经演化成不同版本。整改顺序必须是：先建立唯一事实源，再修复安全与正确性问题，再跑真实测试和 Benchmark，最后更新简历与面试资料。

### 1.2 审计快照中的关键事实

以下数字来自本次提供的审计材料，写入本规格仅作为待复核基线：

| 项目 | 审计快照 | 当前口径 |
|---|---:|---|
| pyproject coverage gate | 80% | 作为强 Gate 保留。 |
| Coverage 实际值 | 77.79% | 重新运行前不得写 Coverage >80%。 |
| pytest passed | 1380 passed | 与 collected 数量分开报告，并重新验证。 |
| tests collected | 1403 | 不是全部通过数，也不是测试函数数。 |
| RAG benchmark queries | 649 | 以当前 tests/eval/rag_benchmark.json 为准。 |
| 历史 Hit Rate@3 | 约 80% | 必须区分 Hit@3 与 Recall@3，并保存 Artifact。 |
| 历史 Top-3 85% | 无当前证据 | 暂停使用。 |

### 1.3 当前 RAG 路径基线

审计材料识别出三条并存路径：

- Graph Prefetch：core/graph_builder.py 在路由阶段预取 product_knowledge；不完整执行 Scene Filter、Query Rewrite、BM25、RRF 和 Reranker；有结果时 Agent 可能直接复用。
- Agent 带 scene 的搜索：Agent 进入带 Qdrant Filter 的 knowledge_base.search；可能绕开 query_multiple，因此不保证进入 BM25 + RRF。
- Hybrid query_multiple：Query Expansion → Vector + BM25 → RRF → Reranker；当前 Agent 并不总是进入这条路径。

因此，简历中的“场景过滤 → Query Rewrite → Vector + BM25 → RRF → Rerank”必须在统一入口和 Trace 验证完成后才能恢复为主链路描述。

### 1.4 当前 Agent、Router 和可靠性基线

- 更准确的角色数量是 7 个业务域 Agent + 1 个 ReAct 推理 Agent + 1 个 Response 后处理 Agent，共 9 个 Agent 角色，不是“9 个垂直 Agent”。
- 业务 Agent 包括 Product、Tech、Billing、Complaint、General、Sales、Aftersales。
- Router 更接近 Rule Router cheap gate：高置信规则 shortcut，规则不足时调用 LLM，冲突时执行覆盖或 fallback；不是无条件 asyncio.gather 并发。
- AftersalesAgent 的正常可达性需要单独验证。建议 Billing 聚焦支付、发票、金额、对账；Aftersales 聚焦订单状态、物流、退换货、售后流程。
- CircuitBreaker 当前语义是连续失败达到阈值后 CLOSED → OPEN，恢复时间后 HALF_OPEN，通过 probe 决定恢复或再次打开；不是滑动窗口失败率熔断。
- Embedding 不可用时存在随机向量 fallback 风险；Qdrant Point ID 使用 Python hash() 的持久化稳定性不足；BM25 为内存索引而 Qdrant 持久化，重启后可能静默退化。
- Benchmark 在服务或 LLM 不可用时存在回退模拟数据的风险；FCR、人效、P99、Token Cost 等数字必须绑定原始 Artifact。

## 2. 整改原则

1. 安全和数据正确性优先于性能。
2. 每个 Issue 只修改其 Required Changes，不顺便重构无关 RAG、Router 或 Agent。
3. 依赖不可用时 fail closed 或显式 degrade，禁止随机或合成数据伪装正常。
4. REST、SSE、WebSocket、多模态统一使用 RequestContext。
5. LLM 只能提出 Tool Call；Schema、AuthZ、风险和资源归属必须在 Tool/Service 层执行。
6. 指标先定义再计算；每个数字必须有输入、公式、脚本和 Artifact。
7. 历史文档是快照，不自动代表当前事实。
8. 未通过 Phase Gate 前，简历和面试答案不得使用对应的生产化表述。

## 3. P0 Critical Issues

### P0-01 CI False Green / CI 假绿

#### Problem

CI 将 pytest/coverage 输出通过 2>&1 | tail -30 处理，管道末端命令可能覆盖 pytest 退出码。即使 coverage 未达到 fail_under = 80，Job 仍可能 success。测试很多不等于 CI 有效。

#### Evidence

- .github/workflows/ci.yml
- pyproject.toml
- 当前 CI 命令中的 2>&1 | tail -30
- 审计快照：77.79% coverage、1380 passed、1403 collected

#### Expected Architecture

测试退出码必须真实传递到 CI。日志截断只能是显示层行为；Coverage、passed、collected、skipped、failed 必须分别输出。

#### Required Changes

1. 为 CI shell step 启用 set -o pipefail，或移除会吞退出码的管道。
2. 分离 test execution 与 log summary。
3. 保持 fail_under = 80 作为 blocking gate。
4. 明确 real_llm、stress、integration service lane 是 blocking 还是 informational。
5. 输出 machine-readable test/coverage summary。
6. 禁止 pytest ... || true、pip-audit ... || true 制造全绿。

#### Required Tests

- test_ci_fails_when_coverage_below_threshold
- intentionally failing test 的 CI smoke test
- CI YAML/config lint
- 本地复现完整 CI 命令

#### Acceptance Criteria

- 任一测试失败时 CI Job 必须失败。
- Coverage 低于 80% 时 CI Job 必须失败。
- tail、日志折叠或摘要不能改变 pytest 退出码。
- 报告区分 passed、collected、skipped、failed 和 coverage。
- Coverage Claim 只有在 Gate 真实通过后才能标为 PROVEN。

#### Out of Scope

本 Issue 不补齐业务测试覆盖率；不重构业务代码；不把所有 informational 检查强行改为 blocking。

### P0-02 Cache Cross-User Leakage / Cache 跨用户污染

#### Problem

L1 主要基于 normalized query，L2 主要过滤 expires_at/user_role，L3 主要保存 tokens/response/timestamp，缺少 user/tenant scope。订单、退款、投诉等个性化回答可能被其他用户命中。

#### Evidence

- cache/response_cache.py
- core/graph_builder.py
- agents/response_agent.py
- L1 MD5(normalized_query)
- L2 metadata filter
- L3 tokens/response/timestamp 数据结构

#### Expected Architecture

所有响应先经过统一 CachePolicy：

    Response → CachePolicy
                 ├── cacheable
                 ├── scope
                 ├── ttl
                 ├── sensitivity
                 └── version

公共 FAQ、成分功效和公开政策可以共享；订单状态、退款、投诉、消费信息默认不可进入共享缓存。L1/L2/L3 必须遵守同一 scope/version 语义。

#### Required Changes

1. 新增 CachePolicy：cacheable、scope、ttl、sensitivity、version。
2. 禁止 order_status、refund、complaint、用户消费信息进入共享缓存。
3. 支持 user-scoped 或 tenant-scoped key/filter。
4. L1/L2/L3 使用同一 policy，不允许某一层绕过 scope。
5. 增加 prompt、knowledge、product 和 cache metadata version。
6. 指标和日志不得泄露响应正文或敏感身份。

#### Required Tests

- test_cross_user_cache_isolation
- test_personalized_response_not_shared
- test_public_faq_can_be_shared
- test_cache_ttl_policy
- test_cache_scope_consistency
- L1/L2/L3 parity tests

#### Acceptance Criteria

- User A 的订单回答绝不可能由 User B 命中。
- FAQ 仍可共享缓存。
- 三层缓存遵守相同 scope、TTL、version。
- 现有 Cache API 不被无计划破坏。
- 缓存架构和 Claim Matrix 更新。

#### Out of Scope

本 Issue 不重构整个 RAG；不修改 RAG Benchmark；不以提高命中率为目标改算法。

### P0-03 ERP IDOR / Authorization

#### Problem

BillingAgent 从用户文本解析 Cxxx 或 ORDxxxx 后直接查询订单；无 ID 时甚至可能查询订单列表并取前 3 条。没有在 Tool/Service 层可靠检查订单是否属于认证用户，存在 IDOR 和数据泄露风险。

#### Evidence

- agents/billing_agent.py
- ERP adapter/query implementation
- API identity extraction path
- Cxxx/ORDxxxx 用户输入解析
- 无 ID 时的 order-list fallback

#### Expected Architecture

    JWT/API identity
            ↓
         user_id
            ↓
       customer_id
            ↓
     query order ORD123
            ↓
    ownership check
       ├─ YES → return
       └─ NO  → Forbidden

LLM 只能提出 order ID，不能决定资源归属。

#### Required Changes

1. 从可信认证上下文取得 user_id。
2. 建立可信 user_id → customer_id 映射。
3. 在 Service/Tool 层执行 ownership check。
4. 未提供 ID 时禁止返回任意用户订单列表，要求澄清或返回受控摘要。
5. 统一 unauthorized/not-found/forbidden，避免枚举订单。
6. 保留输入清理，但不把字符串过滤当作授权。

#### Required Tests

- test_order_idor_denied
- test_order_owner_can_query_order
- test_missing_order_id_does_not_list_other_orders
- test_customer_id_from_prompt_cannot_override_identity
- test_tool_authz_runs_before_erp_call
- unauthorized/not-found non-enumeration tests

#### Acceptance Criteria

- 非所有者请求始终被拒绝或安全地返回 not-found。
- Prompt 不能覆盖认证身份。
- 无 ID 不会返回任意订单列表。
- 授权失败发生在 ERP 数据返回前并产生结构化安全事件。
- REST、SSE、WebSocket、多模态的授权一致。

#### Out of Scope

本 Issue 不重写整个 ERP；不改变订单业务规则；不以 Prompt 防护替代最终授权。

### P0-04 SSE Identity Context Loss / SSE 身份上下文丢失

#### Problem

REST /api/chat 会传 user_id，SSE 创建 Graph Task 的路径可能未一致传递 user_id。_run_graph 只有收到参数才写入 state[user_id]，导致工具权限、缓存、Quota 和审计行为因入口不同而不一致。

#### Evidence

- api/routes/chat.py
- api/app.py
- core/graph_builder.py
- REST run_graph(..., user_id=...) 调用
- SSE _build_sse_stream_context/_sse_stream_generator
- state[user_id] 写入条件

#### Expected Architecture

    REST / SSE / WebSocket / Multimodal
                    ↓
              RequestContext
                    ↓
                LangGraph

RequestContext 至少包含 session_id、user_id、tenant_id、user_role、scopes、trace_id。

#### Required Changes

1. 定义不可伪造的 RequestContext。
2. 在请求边界一次构建 context，并传入 Graph、Tool、Cache、Quota、日志。
3. SSE task 显式携带 context，不依赖隐式全局状态。
4. WebSocket 和多模态复用同一 identity path。
5. 缺失、冲突或伪造 user_id 时 fail closed。
6. Trace 记录 entrypoint 和 identity scope，不记录敏感正文。

#### Required Tests

- test_sse_rest_identity_parity
- test_websocket_rest_identity_parity
- test_multimodal_identity_parity
- test_sse_user_id_reaches_graph_state
- test_missing_identity_fails_closed
- quota/cache/tool authorization parity tests

#### Acceptance Criteria

- REST 与 SSE 对同一用户拥有一致授权、Quota、缓存 scope。
- SSE 不会落入公共或匿名 scope。
- 客户端传入的 user ID 不能覆盖认证上下文。
- Trace 可证明 context 到达 Graph 和 Tool。

#### Out of Scope

本 Issue 不改变 SSE 数据格式；不重做前端 UI；不把 trace propagation 与业务身份校验混为一谈。

### P0-05 Random Embedding Fallback / 随机 Embedding 降级

#### Problem

Embedding function 不可用时可能生成随机向量继续检索，使依赖故障伪装成检索质量下降，破坏 RAG 与语义缓存的正确性。

#### Evidence

- rag/qdrant_knowledge_base.py
- embedding initialization/fallback path
- 审计快照中的 random.random fallback
- RAG 与 cache retrieval callers

#### Expected Architecture

    Embedding unavailable
            ↓
      disable vector
            ↓
       BM25 fallback
            ↓
    retrieval_degraded=true
            ↓
       metrics + alert

若 BM25 也不可用，返回明确 degraded/error result，由上层选择安全回答或人工升级。

#### Required Changes

1. 删除随机向量 fallback。
2. 将 embedding unavailable 转为 typed failure/degraded state。
3. BM25 ready 时禁用 vector channel 并执行 lexical fallback。
4. 写入 retrieval_degraded、degraded_reason、provider/model metadata。
5. RAG、semantic cache、benchmark 统一降级语义。
6. 增加故障指标和告警，区分依赖失败与低召回。

#### Required Tests

- test_embedding_failure_degrades_to_bm25
- test_embedding_failure_never_generates_random_vector
- test_retrieval_degraded_metadata
- test_vector_and_cache_fail_closed_when_no_fallback
- test_embedding_failure_emits_metric_and_alert

#### Acceptance Criteria

- 不发出随机向量查询。
- BM25 可用时显式 degraded 并完成受控检索。
- 无检索通道时不声称 RAG 正常。
- Benchmark 不把 degraded run 当正常 vector run。

#### Out of Scope

本 Issue 不更换 embedding 模型；不统一 RAG 主链路；不调整离线评测集。

## 4. P1 Architecture Issues

### P1-01 RAG Pipeline Unification / RAG Pipeline 统一

#### Problem

query、search、query_multiple 和 Prefetch 各自拥有不同语义。Agent 带 scene 时可能绕过 BM25 + RRF，Prefetch 又可能提前复用不完整上下文。

#### Evidence

- core/graph_builder.py
- rag/qdrant_knowledge_base.py
- agents/* 中的 _retrieve_knowledge
- query、search、query_multiple 实现
- tests/unit/test_qdrant_knowledge_base.py

#### Expected Architecture

    RetrievalRequest
          ↓
    KnowledgeBase.retrieve
          ↓
    Rewrite → Scene/Metadata Filter
          ↓
    Vector + BM25 → RRF → Reranker
          ↓
        Evidence

Agent 只能调用 retrieve。Prefetch 预取 query/rewrite/embedding/context，不提前决定最终 evidence。

#### Required Changes

1. 定义 RetrievalRequest 和 RetrievalTrace。
2. 新增统一 KnowledgeBase.retrieve。
3. 将 rewrite、filter、vector、BM25、RRF、rerank 收敛到该入口。
4. 迁移 Agent 调用；兼容 shim 禁止新增调用。
5. Prefetch 改为预取路由/改写/embedding。
6. 防止重复 rerank，记录每阶段候选数与耗时。

#### Required Tests

- test_all_agent_retrieval_uses_unified_entrypoint
- test_scene_filter_and_hybrid_channels_are_composed
- test_prefetch_does_not_bypass_retrieval_policy
- test_retrieval_trace_contains_all_stages
- test_no_double_reranking

#### Acceptance Criteria

- 业务 Agent 只使用一个 retrieval API。
- 每个查询有 rewrite、scene、vector、BM25、RRF、rerank Trace。
- 主链路与简历描述一致，或 Claim Matrix 标 partial。
- Prefetch 不绕过权限、scene、版本过滤。

#### Out of Scope

不更换 Qdrant/BM25；不修改缓存隔离；不重新定义 Agent 边界。

### P1-02 BM25 Restart Rebuild / BM25 重启重建

#### Problem

BM25 是内存索引，Qdrant 是持久化索引。重启后 BM25 为空，_bm25_search 可能直接跳过，Hybrid RRF 静默退化为纯向量检索。

#### Evidence

- rag/qdrant_knowledge_base.py
- _bm25_search
- BM25 initialization
- Qdrant persistence path

#### Expected Architecture

    startup → Qdrant scroll → rebuild BM25
            → index_version/document_count/last_build_at
            → ready

BM25 未 ready 时必须显式 degraded。

#### Required Changes

1. 启动时从 Qdrant scroll/recover 文档并构建 BM25。
2. 增加 readiness 状态和超时。
3. 记录 index_version、document_count、last_build_at、source snapshot。
4. upsert/delete 后维护增量更新或可追踪重建。
5. 未 ready 时输出 degraded metadata 和指标。

#### Required Tests

- test_bm25_rebuilds_from_qdrant_after_restart
- test_bm25_readiness_blocks_false_hybrid_claim
- test_bm25_document_count_matches_source
- test_bm25_index_version_changes_on_rebuild
- persisted-Qdrant restart integration test

#### Acceptance Criteria

- 重启后 BM25 从持久化文档恢复。
- 只有 vector 和 BM25 均 ready 才标记 hybrid 正常。
- 重建失败可观测且不会静默跳过。
- 文档更新后的索引版本可追踪。

#### Out of Scope

不迁移 sparse-vector vendor；不重做 RRF 权重；不改变 Benchmark 指标定义。

### P1-03 Stable Qdrant IDs / 稳定 Qdrant Point ID

#### Problem

Python 字符串 hash 受 hash randomization 影响，不适合作为跨进程、跨重启稳定的持久 Point ID。

#### Evidence

- rag/qdrant_knowledge_base.py
- hash(id_) & 0x7FFFFFFFFFFFFFFF
- upsert/delete 和 expected document ID 逻辑

#### Expected Architecture

逻辑文档 ID 与存储 Point ID 必须稳定、可复现、可迁移。候选为 uuid.uuid5(namespace, doc_id) 或 SHA-256 截断为 uint64，最终只选一种并固化。

#### Required Changes

1. 选择并记录稳定 ID 算法。
2. 封装 document_id_to_point_id，禁止业务代码直接使用 hash。
3. 提供旧数据迁移或重建策略。
4. 更新 delete、upsert、Benchmark expected IDs 和 golden files。
5. 增加 collision、跨进程和重复导入保护。

#### Required Tests

- test_point_id_stable_across_processes
- test_same_document_id_is_idempotent
- test_document_delete_uses_stable_id
- test_point_id_collision_policy
- benchmark/golden ID consistency test

#### Acceptance Criteria

- 同一文档在不同进程和重启后得到相同 Point ID。
- 重复导入不会生成重复文档。
- 删除和 Benchmark expected IDs 一致。
- 迁移前后有可回滚或可重建方案。

#### Out of Scope

不更换 Qdrant；不修改文档切分；不同时重做 BM25 算法。

### P1-04 Agent Reachability and Billing/Aftersales Boundary / Agent 可达性与边界

#### Problem

AftersalesAgent 可能已初始化但不可由 Router 正常到达；Billing 与 Aftersales 同时处理订单、物流、退款、退换货和发票，职责和权限重叠。

#### Evidence

- agents/billing_agent.py
- agents/aftersales_agent.py
- Router intent-to-agent mapping
- Agent registry
- routing tests

#### Expected Architecture

    Billing    → 支付 / 发票 / 金额 / 对账
    Aftersales → 订单状态 / 物流 / 退换货 / 售后流程

建议 billing → BillingAgent，order_status/return_policy → AftersalesAgent。

#### Required Changes

1. 固化 intent、scene、Agent 映射表。
2. 修复 Aftersales 正常可达路径。
3. 重新划分 Billing/Aftersales Tool 和数据权限。
4. 增加未知、冲突和 fallback 路径。
5. Trace 记录 selected_agent、reason、confidence。
6. 更新 Agent 数量和职责文档。

#### Required Tests

- test_aftersales_route_is_reachable
- test_billing_handles_payment_and_invoice
- test_order_status_routes_to_aftersales
- test_return_policy_routes_to_aftersales
- test_unknown_intent_has_safe_fallback
- route confusion matrix fixture test

#### Acceptance Criteria

- Aftersales 至少有一个正常业务路径被测试覆盖。
- Billing/Aftersales 职责、权限、Tool 可解释。
- Router Trace 证明关键意图实际去向。
- 对外统一使用“7 个业务域 + ReAct + Response”。

#### Out of Scope

不新增业务场景；不重构整个 LangGraph；不把 Router 改成无条件规则/LLM 并发。

### P1-05 Tool Runtime Safety / Tool Runtime 安全

#### Problem

当前 ToolRegistry 有注册、描述、JSON Schema、Function Calling 输出和 dispatch，但不能据此声称已完整实现 runtime validation、per-tool timeout/retry/breaker、authorization、risk、idempotency、confirmation 和统一 ToolResult。

#### Evidence

- ToolRegistry
- tool definitions and JSON Schema
- handler dispatch
- agents/billing_agent.py
- current tool tests

#### Expected Architecture

    LLM tool_call → Registry → Schema → AuthZ → Risk/Confirmation
                   → Timeout/Retry Budget → CircuitBreaker
                   → Execute → Structured ToolResult

ToolResult 至少包含 status、data、error_code、retryable。

#### Required Changes

1. dispatch 前执行服务端 Schema validation。
2. 每个 Tool 声明 auth scope、risk、timeout、retryability、idempotency。
3. Tool 层执行 AuthZ，不依赖模型或 Prompt。
4. 高风险动作增加 confirmation policy。
5. timeout、breaker、unauthorized、validation 返回统一 ToolResult。
6. 记录 tool name、policy decision、latency、error code，不记录敏感参数。

#### Required Tests

- test_tool_runtime_schema_validation
- test_tool_authorization_before_handler
- test_tool_timeout_returns_structured_result
- test_tool_retry_budget_is_bounded
- test_tool_breaker_isolated_per_tool
- test_high_risk_tool_requires_confirmation
- test_idempotent_tool_does_not_duplicate_side_effect

#### Acceptance Criteria

- 非法参数在 handler 前拒绝。
- 未授权调用不能产生业务副作用。
- timeout/retry/breaker/result 可观测可测试。
- 面试答案不把设计目标写成已实现能力。

#### Out of Scope

不接入新的 LLM provider；不改变 ERP 业务；不替代 P0-03 ownership check。

### P1-06 Global Deadline, Recursion Limit and Retry Budget / 全局 Deadline

#### Problem

RESPONSE_TIME_TARGET_MAX 更像执行后记录超时，不等于包住整个 Graph 的 asyncio.wait_for。Router、Agent、Tool 各自 retry 或 mode upgrade 可能造成 Retry Amplification。未实现前不能把 recursion_limit=20 或 visited_nodes >3 写成当前事实。

#### Evidence

- core/graph_builder.py
- collaboration timeout configuration
- LLM/Tool retry paths
- current Graph recursion configuration
- docs/interview-questions-final.md 中旧 termination 描述

#### Expected Architecture

    Request Deadline
       ├─ Router budget
       ├─ RAG budget
       ├─ Agent budget
       ├─ Tool budget
       └─ Postprocess budget

Graph 显式 recursion limit；所有重试受 request-level budget 约束。

#### Required Changes

1. 在 Graph run 外层实现真实 request deadline。
2. 将剩余 deadline 传给 Router、RAG、Agent、Tool、postprocess。
3. 显式配置 LangGraph recursion limit，并处理 GraphRecursionError。
4. 定义全局、组件级和 mode-upgrade retry budget。
5. 到达上限时执行 fallback/escalation。
6. 更新面试文档，只描述已实现数值。

#### Required Tests

- test_graph_request_deadline_cancels_slow_path
- test_graph_recursion_limit_falls_back
- test_retry_amplification_is_bounded
- test_mode_upgrade_consumes_budget
- test_remaining_deadline_reaches_children

#### Acceptance Criteria

- 单次请求最坏时间和最大调用次数有上限。
- recursion 超限不返回伪成功。
- fallback、人工升级和 trace 原因明确。
- P99 与 retry/deadline 数据可由 Artifact 解释。

#### Out of Scope

不把 CircuitBreaker 改成滑动窗口；不重做全部协作模式；不默认牺牲回答质量。

### P1-07 Router and CircuitBreaker Semantics / Router 与熔断语义

#### Problem

历史答案声称“规则与 LLM 并发”和“窗口失败率 >50% 熔断”，而当前代码更接近 Rule cheap gate + LLM fallback，以及连续失败后 OPEN。代码、文档和面试口径不一致。

#### Evidence

- Router implementation and confidence threshold
- core/monitoring.py CircuitBreaker
- router/circuit breaker tests
- docs/interview-questions-final.md

#### Expected Architecture

Router：Rule shortcut → low confidence 时 LLM fallback → conflict handling。  
CircuitBreaker：连续失败 N 次 → OPEN → recovery time → HALF_OPEN probe → success CLOSED / failure OPEN。

#### Required Changes

1. 固化 Router shortcut、LLM fallback、conflict policy。
2. 固化 CircuitBreaker consecutive failure、recovery time、probe 语义。
3. 统一 metrics、日志、活文档和面试术语。
4. 若未来改 rolling-window，另开 ADR/Issue。

#### Required Tests

- test_high_confidence_rule_skips_llm
- test_low_confidence_rule_uses_llm
- test_router_conflict_policy
- test_circuit_breaker_consecutive_failures_open
- test_circuit_breaker_half_open_probe
- test_circuit_breaker_success_closes

#### Acceptance Criteria

- 代码、metrics、活文档和面试材料使用同一定义。
- 不把执行后超时写成全局 timeout。
- 不把连续失败熔断写成失败率窗口熔断。

#### Out of Scope

不增加 ML 分类器；不迁移 CircuitBreaker；不重新定义 SLA。

## 5. P2 Evaluation & Evidence

### P2-01 FCR Definition / FCR 定义

#### Problem

first_resolution_rate 更接近单轮 session 占比，未检查 resolved、升级或同一 issue 在窗口内重新联系。single-turn 不能自动等于 FCR，因此 FCR 78% 无当前代码证据。

#### Evidence

- 当前 FCR calculation implementation
- first_resolution_rate
- total_single_turn_resolved
- session/conversation/reopen data model
- metric tests and reports

#### Expected Architecture

    issue_id
      ↓
    first contact
      ↓
    resolution_status == resolved
      ↓
    not escalated
      ↓
    N-hour window has no same-issue reopen/recontact

    FCR = 首次接触解决且窗口内未再次联系的问题数 / 总问题数

N 小时必须是显式业务参数。

#### Required Changes

1. 定义 issue identity 和 reopen/recontact window。
2. 分离 single-turn、resolved、not-escalated、no-recontact。
3. 重写 FCR 计算和输入契约。
4. 缺失 resolution 或 issue ID 时返回 unknown，不默认 resolved。
5. 报告输出分子、分母、窗口、过滤规则、时间范围。

#### Required Tests

- test_fcr_requires_resolved_status
- test_fcr_excludes_escalated_issue
- test_fcr_reopen_window
- test_same_issue_recontact_is_not_fcr
- test_missing_resolution_is_unknown

#### Acceptance Criteria

- FCR 定义、代码、测试和报告一致。
- 单轮 session 不再自动等于 FCR。
- 每个数字可追溯到 issue 数据和 Artifact。
- 数据不足时 Claim 为 UNVERIFIED 或 EXTERNAL。

#### Out of Scope

不虚构客服数据；不把演示数据写成生产 FCR；不修改缓存或路由。

### P2-02 Labor Efficiency Metric / 人效指标

#### Problem

labor_savings_estimate = ai_handled_rate 不能推出人工人效提升 2 倍或 3 倍。节省比例、AHT 下降比例和人效倍数是不同指标。

#### Evidence

- labor_savings_estimate
- monitoring/benchmark metric implementation
- 缺少 before/after AHT 或 throughput Artifact
- resume/release 中的 2x/3x claims

#### Expected Architecture

    Before AHT = assistant 前平均处理时长
    After AHT  = assistant 后平均处理时长
    time reduction = (Before - After) / Before
    efficiency multiple = Before / After

同时明确人工接管率、AI handled rate、每人每小时处理量和样本窗口。

#### Required Changes

1. 删除用 ai_handled_rate 代替人效的计算。
2. 定义 AHT、throughput、assisted rate、AI handled rate、efficiency multiple。
3. 保存 before/after、样本窗口、分组、统计方法。
4. 合成数据标记 synthetic/unverified。
5. 更新简历和面试口径。

#### Required Tests

- test_efficiency_multiple_uses_before_after_aht
- test_time_reduction_is_not_efficiency_multiple
- test_missing_baseline_returns_unverified
- test_metric_artifact_contains_sample_window

#### Acceptance Criteria

- 2x/3x 仅在真实 before/after AHT/throughput Artifact 存在时可标 PROVEN/EXTERNAL。
- 指标名称和公式不会混淆。
- 报告可复算分子、分母和样本范围。

#### Out of Scope

不通过合成数据制造人效；不改变客服排班或运营流程。

### P2-03 Benchmark Provenance and Real/Simulated Isolation / Benchmark 证据链

#### Problem

延迟、成本、A/B、Prefetch 等脚本在服务或 LLM 不可用时可能回退模拟值；成本脚本可能使用默认场景数据。由此产生的 P99、Token Cost、调用下降不能作为真实事实。

#### Evidence

- scripts/benchmark_latency.py
- scripts/benchmark_ab_test.py
- scripts/benchmark_cost.py
- scripts/benchmark_prefetch.py
- scripts/_benchmark_utils.py
- simulated fallback branches

#### Expected Architecture

    --mode real
      any request/dependency failure → benchmark FAILED

    --mode simulated
      evidence_level = synthetic
      never enters Resume Metrics

每个 Artifact 至少含 git_sha、dataset_sha256、run_at、mode、provider、model、embedding_model、reranker、query_count、environment、config、metrics。

#### Required Changes

1. 所有 Benchmark 支持 --mode real 与 --mode simulated。
2. Real mode 的请求失败、服务不可达、依赖异常必须使 Benchmark 失败。
3. Simulated mode 写入 evidence_level=synthetic 和模拟原因。
4. 禁止单条真实请求异常自动替换模拟值。
5. 保存 git SHA、数据集 SHA-256、provider/model、embedding/reranker、环境和配置。
6. 统一产出 reports/latest/manifest.json 和各 Benchmark Artifact。
7. Resume Claim 读取 evidence level，不人工复制未验证数字。

#### Required Tests

- test_real_benchmark_fails_on_server_error
- test_real_benchmark_never_falls_back_to_simulation
- test_simulated_benchmark_marks_synthetic_evidence
- test_benchmark_manifest_contains_provenance
- test_resume_metrics_reject_synthetic_artifact

#### Acceptance Criteria

- P99、LLM call reduction、Token Cost、cache A/B 都能追溯到 real Artifact，或明确标 synthetic/unverified。
- Real mode 在服务器不存在时返回非零退出码。
- Artifact 能解释运行条件和数字来源。
- 不再把 P99 <2s、-40%、-25% 等模拟结果当生产事实。

#### Out of Scope

不优化 Benchmark 指标；不保证真实服务达到目标值；不更换压测工具。

### P2-04 RAG Metric Semantics / RAG 指标语义

#### Problem

当前材料混用 Top-3 召回率、Hit Rate@3 和 Recall@3。多相关文档场景下，Hit@3 命中一个即可为 1，但 Recall@3 仍可能小于 1。

#### Evidence

- scripts/evaluate_rag.py
- tests/eval/rag_benchmark.json
- docs/reference/rag-evaluation.md
- historical resume descriptions

#### Expected Architecture

报告分别输出 Hit@K、Recall@K、MRR、NDCG，并写明 gold relevance、K、aggregation 和 query filtering。

#### Required Changes

1. 评测代码和报告使用完整指标名称。
2. 校验 649 query 的 dataset metadata、类别分布和 expected docs。
3. 用示例测试证明 Hit@3 与 Recall@3 差异。
4. 简历不再使用模糊 Top-3 召回率。

#### Required Tests

- test_hit_at_k_and_recall_at_k_are_distinct
- test_rag_metric_definition_matches_report
- test_expected_doc_ids_are_present
- test_rag_report_contains_dataset_hash

#### Acceptance Criteria

- 报告明确写 Hit Rate@3 或 Recall@3。
- 历史约 80% 只能按真实 Artifact 对应指标引用。
- 85% 重新跑出并具 provenance 前保持 UNVERIFIED。

#### Out of Scope

不提升召回率；不调整 RRF 权重；不扩充数据集规模。

### P2-05 Dataset and Business Evidence Provenance / 数据集来源

#### Problem

仓库可证明存在 5000+ synthetic knowledge base documents 的脚本和真实文档导入器，但不能仅凭 synthetic dataset 证明真实公司沉淀 500+ 文档。FCR、人效等业务数字也缺少当前原始 Artifact。

#### Evidence

- scripts/generate_knowledge_base.py
- scripts/import_real_docs.py
- data/knowledge_base/
- tests/eval/rag_benchmark.json
- docs/reports/resume-description.md
- reports and data manifests

#### Expected Architecture

    source → de-identification → cleaning → chunking
           → metadata → version → ingestion → evaluation

synthetic、fixture、脱敏业务数据和 external-only data 必须分开标记。

#### Required Changes

1. 为数据集建立 manifest、source type、privacy/license、version、SHA-256。
2. 分离 synthetic、repository fixture、de-identified business、external-only。
3. 真实业务数据保留脱敏、清洗、切分、入库、评测记录。
4. Benchmark Artifact 引用 dataset manifest。
5. 无仓库证据的业务规模 Claim 标 EXTERNAL 或 UNVERIFIED。

#### Required Tests

- test_dataset_manifest_is_complete
- test_synthetic_and_business_sources_are_separated
- test_benchmark_references_dataset_hash
- test_missing_source_provenance_blocks_proven_claim

#### Acceptance Criteria

- 可以准确表述“支持真实文档导入，并提供 5000+ 条合成领域知识数据用于测试/演示”。
- 500+ 真实业务文档只有在有外部证据且标 EXTERNAL 时保留。
- 数据来源、版本和隐私处理可审计。

#### Out of Scope

不要求提交公司私有数据；不伪造无法公开的业务 Artifact。

### P2-06 Claim Evidence Matrix / Claim 证据矩阵

#### Problem

简历、README、发布说明和面试答案存在冲突的数字和实现描述：9 个垂直 Agent、Top-3 85%、FCR 78%、人效 2/3 倍、P99 <2s、Coverage >80% 和完整 RAG 链路。没有矩阵时，旧 Claim 会继续传播。

#### Evidence

- docs/reports/resume-description.md
- docs/interview-questions-final.md
- README.md
- docs/reference/rag-evaluation.md
- docs/reports/releases/changelog.md
- historical audit/milestone documents

#### Expected Architecture

    claim → status → code path → test → benchmark artifact
          → definition → bad case → owner/date

#### Required Changes

1. 新增 docs/audit/CLAIM_EVIDENCE_MATRIX.md，或建立等价活文档。
2. 每个数字和架构描述标 PROVEN/PARTIAL/EXTERNAL/UNVERIFIED/FALSE。
3. 记录代码路径、测试命令、Artifact 路径和最后验证时间。
4. 简历和面试更新前先更新矩阵。
5. 旧文档保留为 archive snapshot，不继续命名为 current final。

#### Required Tests

- claim matrix link/path validation
- artifact existence validation
- stale claim detection for coverage/benchmark/agent count
- documentation consistency check

#### Acceptance Criteria

- 每个 Claim 可定位到证据，或明确说明不能证明。
- PROVEN 不依赖历史文档或“合理假设”。
- 面试 Q&A 只解释已证明实现，未来设计标 proposed。

#### Out of Scope

不编造业务数据；不在没有证据时润色成更强营销表述。

## 6. Documentation Alignment

### 6.1 文档分层

建议建立：

    docs/audit/
    ├── CODEX_PROJECT_REMEDIATION_SPEC.md
    ├── CURRENT_ARCHITECTURE.md
    ├── CLAIM_EVIDENCE_MATRIX.md
    ├── STATE_SCHEMA.md
    ├── AGENT_REACHABILITY.md
    ├── RAG_PIPELINE_TRACE.md
    └── METRIC_DEFINITIONS.md

### 6.2 活文档与历史快照

- README.md、docs/README.md、docs/reference/*、生产清单和当前架构文档属于活文档。
- docs/reports/milestone/*、旧 release notes、旧 audit report 属于历史快照，保留但不作为当前事实源。
- docs/interview-questions-final.md 含旧版本内容，建议归档，并建立 docs/interview/00_PROJECT_TRUTH.md、01_ARCHITECTURE.md、02_RAG.md、03_AGENT_ROUTING.md、04_TOOL_CALLING.md、05_CACHE_RELIABILITY.md、06_EVALUATION_METRICS.md、07_FAILURE_CASES.md、08_INTERVIEW_QA.md。
- 活文档更新必须以当前代码和 Artifact 为准；历史文档应通过新的对齐记录纠正，不直接改写历史快照。

### 6.3 文档更新规则

1. 先更新代码、测试、Artifact，再更新 Claim Matrix。
2. Claim Matrix 变绿后才更新简历和面试答案。
3. “已实现”必须指向当前代码；“计划”必须写 Proposed。
4. 不使用“合理假设，所以可写进简历”的状态。

## 7. Resume Claim Matrix

这是整改完成前的保守口径，最终状态必须由当前验证结果更新。

| Resume claim | Current status | Safe interim wording | Evidence before PROVEN |
|---|---|---|---|
| 9 个垂直 Agent | PARTIAL | 7 个业务域 + ReAct + Response，共 9 个角色 | registry、reachability tests、活文档 |
| 500+ 真实业务文档 | EXTERNAL/UNVERIFIED | 支持真实文档导入，并提供 5000+ 合成数据用于测试/演示 | 脱敏 manifest 或外部证据 |
| Top-3 约 85% | UNVERIFIED | 暂不写 | Real RAG Artifact，明确 Hit/Recall |
| Hit Rate@3 约 80% | PARTIAL | 仅引用历史报告时注明历史快照 | 当前 dataset hash、代码、Artifact |
| FCR 78%/80% | UNVERIFIED/EXTERNAL | 暂不写 | issue-level FCR、resolution、reopen |
| 人效 2/3 倍 | UNVERIFIED/EXTERNAL | 暂不写 | before/after AHT 或 throughput |
| 流式首字 P99 <2s | UNVERIFIED | 暂不写 | Real latency Artifact |
| LLM 调用降低 40/65% | UNVERIFIED | 暂不写 | Real A/B baseline Artifact |
| Token 成本下降 25/35% | UNVERIFIED | 暂不写 | Real cost Artifact |
| 1400+ 测试 | UNVERIFIED | 重跑后填当前 collect 数量 | collect/test-function 统计 |
| Coverage >80% | FALSE against audit snapshot | 暂不写；快照为 77.79% | CI Gate 真实通过 |
| 完整多阶段 RAG | PARTIAL | 统一 retrieve 后再写完整链路 | unified retrieval tests + trace |

### Resume Freeze

Phase 1 至 Phase 4 完成并更新 Claim Matrix 前，冻结简历数字。冻结不是删除历史材料，而是阻止继续优化未经证据支持的措辞。

## 8. Interview Q&A Alignment

### 8.1 六张母卡

1. 系统链路：Request → Cache → Router → Retrieval → Collaboration → Agent/Tool → Quality Gate → Response → Metrics。
2. Router：Rule shortcut → LLM fallback → conflict handling → route accuracy/confusion matrix。
3. RAG：rewrite/filter/vector/BM25/RRF/rerank → Hit/Recall/MRR/NDCG → bad case/fallback。
4. Agent：knowledge/prompt/tool/permission boundary → 5 modes → step/deadline/upgrade budget → degrade/human。
5. Tool + Cache + Reliability：schema → auth → risk → timeout → breaker → result；cacheability → scope → TTL → version；deadline → retry → circuit → fallback。
6. Evaluation：routing/retrieval/tool/answer、FCR/escalation/CSAT、latency/errors/availability、calls/tokens/cost。

### 8.2 Q1–Q7 aligned wording

| Question | Current aligned answer |
|---|---|
| 为什么 LangGraph | 有缓存跳转、Router、协作模式、质量升级和 fallback，需要可观察、可测试的 StateGraph。 |
| 状态机怎么设计 | Cache → Router → collaboration → mode → ResponseAgent → quality → optional upgrade → End；字段以当前 AgentState 为准。 |
| 为什么 9 个 Agent | 7 个业务域 + ReAct + Response，不称为 9 个垂直业务 Agent。 |
| 路由、死循环、超时 | Rule shortcut → LLM fallback；recursion limit；global deadline；mode/tool retry budget；最终 fallback/human。只描述已实现数值。 |
| Function Calling | 修复前只说已有 Registry/schema/dispatch；修复后再说 Schema → AuthZ → Risk → Timeout → Breaker → Idempotency → Structured Result。 |
| Agent 评估 | Routing、Retrieval、Tool、Business 四层分开，不混成一个 Agent accuracy。 |
| 缓存体系 | 先 CachePolicy，再 L1 exact、L2 semantic、L3 lexical；订单/退款/用户信息默认禁止共享缓存。 |

### 8.3 必须新增的追问

1. Prefetch 如何避免错误 scene 知识被复用？
2. BM25 重启如何恢复？
3. 缓存如何防止用户间污染？
4. Prompt、知识库、product version 如何使旧缓存失效？
5. Tool Calling 如何防 IDOR？
6. LLM/Tool/Agent retry 如何防 Retry Amplification？
7. 如何证明 Benchmark 是 REAL 而不是 Simulation？
8. Hit@3 和 Recall@3 的区别？
9. FCR 如何定义 issue identity 和 reopen window？
10. 为什么 Aftersales 与 Billing 要拆？
11. 水平扩容后 L3 内存缓存如何一致？
12. 为什么选择 RRF，做过哪些 ablation？
13. Embedding 不可用时为什么不能生成随机向量？
14. 每个简历指标对应哪个 commit、dataset、config？

## 9. Phase Gates

### Phase 0 — Truth Baseline

不改业务代码，先产出 CURRENT_ARCHITECTURE.md、CLAIM_EVIDENCE_MATRIX.md、STATE_SCHEMA.md、AGENT_REACHABILITY.md、RAG_PIPELINE_TRACE.md、METRIC_DEFINITIONS.md。

**Gate：** 所有外部 Claim 指向代码、测试或 Artifact；无法证明的标 PARTIAL/EXTERNAL/UNVERIFIED/FALSE。

### Phase 1 — Security & Correctness

范围：P0-01 至 P0-05。

Required tests：

    test_ci_fails_when_coverage_below_threshold
    test_cross_user_cache_isolation
    test_order_idor_denied
    test_sse_rest_identity_parity
    test_embedding_failure_degrades_to_bm25

**Gate：** 安全/正确性测试通过；CI 能真实阻断；不存在随机向量和跨用户个性化共享缓存。

### Phase 2 — RAG Refactor

范围：P1-01、P1-02、P1-03。

**Gate：** 所有 Agent 走 retrieve；BM25 可重启恢复；Point ID 稳定；查询可输出完整 Retrieval Trace。

### Phase 3 — Agent & Tool Runtime

范围：P1-04 至 P1-07。

**Gate：** Aftersales 可达；边界清晰；Tool AuthZ/schema/timeout/breaker/result 完整；recursion limit、global deadline、retry budget 可测试。

### Phase 4 — Evaluation & Benchmark

范围：P2-01 至 P2-05。

Required outputs：

    reports/latest/
    ├── manifest.json
    ├── rag_eval.json
    ├── latency.json
    ├── routing_eval.json
    ├── cache_ab.json
    ├── cost.json
    └── test_summary.json

**Gate：** FCR、人效、RAG 指标定义明确；Real/Simulated 严格隔离；每个数字有 provenance；合成数据不进入 Resume Metrics。

### Phase 5 — CI & Documentation

范围：P2-06 及活文档、简历、面试材料。

**Gate：** Coverage gate 真实有效；blocking/informational 语义明确；Claim Matrix 更新后才解除 Resume Freeze。

### 推荐执行顺序

1. 修 CI 假绿，让红灯真实地红起来。
2. 修缓存跨用户、ERP 越权和 SSE 身份上下文。
3. 统一 RAG retrieve，消灭 Prefetch/Scene/Hybrid 绕开路径。
4. 移除随机 Embedding fallback，重建 BM25，使用稳定 Qdrant ID。
5. 重做 Benchmark provenance 和 FCR，再决定简历最终数字。

## 10. Final Definition of Done

### Code and Architecture

- REST、SSE、WebSocket、多模态统一使用 RequestContext。
- 个性化响应默认不进入共享缓存，L1/L2/L3 scope 一致。
- ERP ownership/AuthZ 在 Tool/Service 层强制执行。
- Embedding 失败不生成随机向量，降级可观测。
- RAG 统一使用 retrieve，Prefetch 不绕过 scene、权限和版本。
- BM25 可从持久化数据重建，Qdrant ID 跨进程稳定。
- Agent 可达性、Billing/Aftersales 边界和 Router 语义有测试证据。
- Tool Runtime 具备 Schema、AuthZ、Risk、Timeout、Retry、Breaker、Structured Result。
- Graph 有 recursion limit，请求有 global deadline，重试有预算。

### Tests and CI

- CI 测试失败和 coverage 低于阈值都会阻断 Job。
- P0/P1 required tests 全部通过。
- 测试结果区分 collected、passed、skipped、failed、coverage。
- blocking 与 informational 检查清晰可见。
- 当前测试数量和 coverage 重新运行后再写入文档。

### Evaluation and Evidence

- FCR 按 issue、resolution、escalation、reopen window 计算。
- 人效按 before/after AHT 或 throughput 计算。
- Hit@K、Recall@K、MRR、NDCG 名称和公式准确。
- Benchmark Real/Simulated 隔离，Real failure 不自动模拟。
- Artifact 含 git SHA、dataset SHA-256、运行时间、provider/model、配置、环境、evidence level。
- reports/latest/manifest.json 可关联所有指标报告。

### Documentation and Claims

- 活文档与当前代码一致，历史文档保留为 snapshot。
- 每条 PROVEN Claim 都能定位到代码、测试、Artifact。
- 外部业务数字标 EXTERNAL，未验证数字标 UNVERIFIED，错误表述标 FALSE。
- Resume Freeze 解除前，不更新 FCR、人效、P99、成本、调用下降和完整 RAG 数字。
- 面试 Q&A 只解释已证明实现；未来设计标 Proposed。

最终事实闭环：

    Code + Tests + Benchmarks → Project Truth → Claim Matrix
                                      ├── current docs
                                      ├── Resume
                                      └── Interview Q&A

代码是实现事实，测试证明行为，Benchmark 证明数字，Claim Matrix 决定简历能写什么，Interview Q&A 只解释已经被证明的事实。

