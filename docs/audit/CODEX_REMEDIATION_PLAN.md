# Customer AI Agent Remediation Plan

> 规划范围：仅基于当前 main 分支代码、测试、配置和 CI 做 Remediation Planning。  
> 本文不实施任何 Issue，不修改业务源码、测试源码或 CI 配置。  
> 事实优先级：当前代码与可复现验证结果 > 当前配置与测试 > 历史审计文档、README、简历和面试材料。

## 0. 规划边界与验证方法

### 0.1 当前基线

- 分支：main
- HEAD：e4c137e
- 仓库状态：存在用户已有修改 secrets/keys.json；本次规划未触碰。
- 新增规划文件：docs/audit/CODEX_REMEDIATION_PLAN.md
- 审计规格：docs/audit/CODEX_PROJECT_REMEDIATION_SPEC.md

本次只验证和规划，不执行修复、不重构 RAG、不调整路由、不修改认证和缓存行为，也不更新简历或对外材料。

### 0.2 已执行的验证

- 阅读整改规格及其 P0/P1/P2 的全部 18 个 Issue。
- 静态检查 core、agents、router、rag、cache、tools、api、tests、.github/workflows。
- 选定回归测试：
  - Qdrant、Router、Tool Registry、WebSocket：76 passed，4 warnings。
  - KPI/CircuitBreaker 相关测试及场景测试：9 passed，180 deselected。
  - 选定测试文件 collect-only：216 tests collected。
- 验证 tests/eval/rag_benchmark.json：
  - total_queries = 649
  - 文件记录数 = 649
  - SHA256 = 0185fcfabbb7437c87dc0ff417af883c468761cc2931f75e29b68aab833e693c
- 未执行完整 CI、完整覆盖率门禁或真实外部依赖基准。因此整改规格中“77.79% coverage、1380 passed、1403 total”等历史快照在本轮标记为 UNVERIFIED，不作为当前事实。

### 0.3 判定枚举

- CONFIRMED：当前代码已直接证明问题或治理缺口存在。
- PARTIAL：问题只在部分入口成立，或已有局部保护，不能按“完全缺失”描述。
- NOT_CONFIRMED：当前证据不足以确认整改规格的原始断言。
- UNVERIFIED：需要完整运行、外部服务或历史环境才能确认。
- BLOCKED：需要外部服务、凭据或未提供的业务事实才能完成验证。

## 1. CURRENT_STATE_VALIDATION

### 1.1 总体判定表

| Issue | 当前判定 | 当前代码事实 | 关键证据 |
|---|---|---|---|
| P0-01 CI 假绿 | CONFIRMED；历史覆盖率数值 UNVERIFIED | CI 将 pytest 输出接到 tail，未启用 pipefail；后续命令可能以 tail 成功码结束 | .github/workflows/ci.yml、pyproject.toml |
| P0-02 Cache 跨用户污染 | RESOLVED（见 P0_02_COMPLETION_REPORT.md） | 三层统一 CachePolicy：个性化回答写入用户作用域（u:hash(user_id)），无身份 fail closed；公开回答 shared；读取端 OR 探测 shared+调用方作用域 | cache/cache_policy.py、cache/response_cache.py、core/graph_builder.py、agents/response_agent.py |
| P0-03 ERP IDOR/AuthZ | CONFIRMED 为缺少资源归属授权；具体可利用性仍需新增端到端测试 | Billing/Aftersales 和 ERP tools 可接受模型或查询字符串提供的订单/客户标识，未执行 ownership check | agents/billing_agent.py、agents/aftersales_agent.py、tools/erp_tools.py |
| P0-04 SSE Identity Context | CONFIRMED，但仅 SSE 入口 | REST 和 WebSocket 传 user_id；共享 SSE helper 创建 graph task 时未传 user_id | api/routes/chat.py、api/routes/chat_multimodal.py、api/routes/ws.py |
| P0-05 Random Embedding Fallback | CONFIRMED | Qdrant 无 embedding 时生成 random vectors；ResponseCache 还有 deterministic-random fallback；现有测试明确接受随机向量 | rag/qdrant_knowledge_base.py、cache/response_cache.py、tests/unit/test_qdrant_knowledge_base.py |
| P1-01 RAG Pipeline 统一 | CONFIRMED | graph prefetch、Agent retrieval、Qdrant query/search/query_multiple 是多条语义不同的检索路径 | core/graph_builder.py、agents/base_agent.py、rag/qdrant_knowledge_base.py |
| P1-02 BM25 Restart Rebuild | CONFIRMED | BM25 纯内存懒初始化；持久化模式跳过种子加载；启动没有从 Qdrant 重建 BM25 | rag/bm25_retriever.py、rag/qdrant_knowledge_base.py、core/container.py |
| P1-03 Stable Qdrant IDs | CONFIRMED | Point ID 使用 Python hash(id_)；逻辑 doc_id 与存储 Point ID 分离且 hash 稳定性未保证 | rag/qdrant_knowledge_base.py |
| P1-04 Agent Reachability | CONFIRMED | order_status、return_policy 正常映射到 billing_agent；aftersales_agent 被创建但无正常意图映射；现有场景测试还接受 billing alias | router/query_router.py、core/container.py、tests/e2e/test_scenarios.py |
| P1-05 Tool Runtime | CONFIRMED | ToolRegistry 缺少统一参数校验、AuthZ、超时、重试、熔断、风险和结构化结果边界 | tools/tool_registry.py、tools/erp_tools.py、agents/base_agent.py |
| P1-06 Global Deadline/Retry Budget | PARTIAL | 存在 RAG、LLM/协作、WebSocket 等局部 wait_for 和局部轮次限制，但 graph 全链路没有统一 deadline/recursion budget | core/graph_builder.py、core/app.py、collaboration/modes.py、api/routes/ws.py |
| P1-07 Router/CircuitBreaker Semantics | CONFIRMED 为语义/文档不一致；当前运行时是可解释的规则 gate + LLM fallback | router 源码注释称并行，但实际先规则分类，低置信度时才调用 LLM；CircuitBreaker 是连续失败与 half-open，不是文档描述的滑窗统计 | router/query_router.py、core/monitoring.py |
| P2-01 FCR Definition | CONFIRMED | first_resolution_rate 以单轮 session/总 session 计算，没有 issue identity、重联窗口和确认解决条件 | core/monitoring.py |
| P2-02 Labor Efficiency | CONFIRMED | labor_savings_estimate 只是 AI handled rate 的字符串，没有人工基线、AHT 或吞吐量数据 | core/monitoring.py、scripts/benchmark_cost.py |
| P2-03 Benchmark Provenance | CONFIRMED | 多个 benchmark 在异常、服务不可用或默认场景时自动模拟；报告缺少 git SHA、数据集 hash、配置和 evidence level | scripts/benchmark_latency.py、scripts/benchmark_ab_test.py、scripts/benchmark_prefetch.py、scripts/benchmark_cost.py |
| P2-04 RAG Metric Semantics | PARTIAL | 当前 evaluate_rag.py 已分别输出 Hit Rate、Recall@3、Precision@3、MRR、NDCG@3；但 provenance 和历史材料仍不完整 | scripts/evaluate_rag.py、tests/eval/rag_benchmark.json |
| P2-05 Dataset/Business Provenance | PARTIAL/UNVERIFIED | 已有 seed/generator/importer，但当前提交的 seed 文件合计 173 条；没有 manifest 证明“5000+”对应的来源、版本和真实业务占比 | data/seed、scripts/generate_knowledge_base.py、scripts/import_real_docs.py |
| P2-06 Claim Evidence Matrix | CONFIRMED | docs/audit/CLAIM_EVIDENCE_MATRIX.md 当前不存在；README、设计文档和历史材料缺少统一证据索引 | README.md、docs、docs/audit |

### 1.2 Issue 逐项规划

#### P0-01 CI 假绿

当前状态：CONFIRMED。CI workflow 中 pytest 管道接 tail，未见 pipefail；coverage 配置有 fail_under=80，但该失败码可能被管道末端吞掉。整改规格中的历史测试/覆盖率数字本轮未复跑，单独标记 UNVERIFIED。

涉及文件：

- .github/workflows/ci.yml
- pyproject.toml
- tests/（用于验证 CI 执行范围和失败传播）
- 计划新增 CI contract 测试或 shell-level validation 文件

预计新增/修改测试：

- test_ci_pipeline_preserves_pytest_exit_code
- test_coverage_threshold_failure_fails_job
- test_ci_runs_required_test_groups
- test_ci_does_not_accept_tail_success_after_pytest_failure

风险：

- CI runner shell、Windows/Unix shell 行为差异。
- 修复失败码传播后可能暴露现有覆盖率或测试缺口。
- 把 CI contract 写成 Python 单测可能无法证明真实 workflow 行为，最终 gate 必须运行实际 workflow 或等价 shell。

实施边界：只处理 CI 失败传播和验证范围；不顺手修测试实现、不调整 coverage threshold。

#### P0-02 Cache Cross-User Leakage

当前状态：CONFIRMED。L1 key 基于 normalized query；L3 Jaccard 不带 metadata；L2 只带 user_role 过滤。graph cache 节点传入 user_role，但没有 user_id/tenant。订单、退款、投诉等个性化回答因此没有可靠的用户隔离边界。

涉及文件：

- cache/response_cache.py
- core/graph_builder.py
- agents/response_agent.py
- core/state.py 或新增 CachePolicy/RequestContext 模块
- tests/unit、tests/integration 中现有 cache/graph 测试

预计新增/修改测试：

- test_cross_user_cache_isolation
- test_personalized_response_not_shared
- test_public_faq_can_be_shared
- test_cache_ttl_policy
- test_cache_scope_consistency
- test_l1_l2_l3_apply_same_cache_policy

风险：

- 过度收紧 scope 会导致 FAQ 命中率下降。
- 只修某一层会产生层间行为差异。
- 用户身份缺失时若默认降级为 shared，仍可能泄漏；若默认禁用，会影响延迟和成本。
- 旧 Cache API 兼容性和历史缓存清理需要单独设计。

实施边界：只建立 cacheability/scope/TTL/sensitivity/version 策略；不重构整个 RAG。

#### P0-03 ERP IDOR / Authorization

当前状态：CONFIRMED 为缺少资源归属授权。BillingAgent、AftersalesAgent 和 ERP tools 可从自然语言或模型参数取得 order_id/customer_id 后直接查询，没有 authenticated identity 到 ERP resource ownership 的统一检查。是否能在当前模拟 ERP 上完成完整越权利用，需要新增端到端测试确认。

涉及文件：

- agents/billing_agent.py
- agents/aftersales_agent.py
- tools/erp_tools.py
- tools/tool_registry.py
- core/state.py 或新增 RequestContext/AuthZ policy 模块
- api/dependencies.py、api/routes/chat.py、api/routes/ws.py（仅用于身份上下文传递）
- tests/unit/test_auth_tools_coverage.py
- tests/integration/test_erp_integration.py
- tests/e2e/ 中认证和聊天场景

预计新增/修改测试：

- test_order_query_requires_authenticated_user
- test_order_owner_can_read_order
- test_non_owner_cannot_read_order
- test_customer_lookup_is_scoped
- test_model_cannot_override_request_identity
- test_billing_and_aftersales_share_authz_policy
- test_missing_identity_fails_closed

风险：

- ERP 适配器字段和真实 ownership 规则可能与 mock 不一致。
- 失败码、脱敏信息和客服体验需要同时定义。
- 在 Agent 层修补而绕过 Tool 层会留下第二条越权路径。
- 身份上下文传播与 P0-04、P1-05 有强依赖。

实施边界：只解决订单/客户资源读取的认证、授权和失败关闭；不扩展到全量 ERP 写操作，除非后续证据证明其属于同一入口。

#### P0-04 SSE Identity Context

当前状态：CONFIRMED，但范围限定为 SSE。REST /api/chat 和 WebSocket 路径会传 user_id；chat.py 的 SSE helper 创建 run_graph task 时没有传 user_id，多模态 SSE 复用该 helper，因此也受影响。

涉及文件：

- api/routes/chat.py
- api/routes/chat_multimodal.py
- api/routes/ws.py
- api/app.py
- core/graph_builder.py
- core/state.py 或新增 RequestContext
- tests/unit/test_api_routes.py
- tests/unit/test_ws_coverage.py
- SSE 相关 integration/e2e tests

预计新增/修改测试：

- test_sse_passes_authenticated_user_id_to_graph
- test_multimodal_sse_preserves_user_id
- test_rest_sse_ws_identity_parity
- test_anonymous_sse_fails_closed_or_uses_explicit_anonymous_scope
- test_mock_run_graph_asserts_user_id_argument

风险：

- 改变 async task 参数会触发 mock、fixture 和客户端兼容问题。
- 只修普通 SSE 而遗漏多模态共享入口。
- user_id 传递正确不等于 session ownership 正确，仍需与 P0-03 分离验证。

实施边界：只统一 SSE graph invocation 的身份上下文；不重做 JWT/session 认证协议。

#### P0-05 Random Embedding Fallback

当前状态：CONFIRMED。QdrantKnowledgeBase 在 embedding 不可用时返回 random vectors；ResponseCache 的 query embedding 失败时使用 deterministic-random vectors。现有 qdrant 单测明确把随机 fallback 作为预期行为，因此修复必须同步改变测试契约。

涉及文件：

- rag/qdrant_knowledge_base.py
- cache/response_cache.py
- rag/api_embedding.py
- core/container.py
- core/monitoring.py（若增加 degraded metric）
- tests/unit/test_qdrant_knowledge_base.py
- cache 相关 unit/integration tests

预计新增/修改测试：

- test_embedding_unavailable_fails_closed
- test_embedding_failure_returns_explicit_degraded_result
- test_no_random_vector_is_emitted
- test_cache_embedding_failure_does_not_create_semantic_hit
- test_embedding_dimension_mismatch_is_rejected
- test_degraded_embedding_metric_and_log_are_emitted

风险：

- 直接失败可能导致服务不可用，需要明确空结果、关键词 fallback 或显式降级。
- Qdrant collection 维度与替代 embedding 模型不兼容。
- 删除 fallback 会改变离线测试和本地开发体验。
- 需要避免把“请求失败”和“随机向量成功”混为可比较的 benchmark 结果。

实施边界：只处理 embedding 不可用时的契约和可观测降级；不在本 Issue 内重构完整 RAG pipeline。

#### P1-01 RAG Pipeline 统一

当前状态：CONFIRMED。classify 前的 graph prefetch 使用单 collection query；Agent retrieval 可能使用 scene-filtered search、query_multiple 的 BM25/RRF/reranker 或普通 query。Agent 若取得 _rag_prefetch 会直接跳过自身检索。当前没有统一 retrieve() contract。

涉及文件：

- core/graph_builder.py
- agents/base_agent.py
- rag/qdrant_knowledge_base.py
- core/protocols.py
- rag/query_rewriter.py
- rag/reranker.py
- tests/unit/test_qdrant_knowledge_base.py
- agents/RAG 相关 unit tests
- graph/integration/e2e tests

预计新增/修改测试：

- test_all_business_paths_use_single_retrieve_contract
- test_prefetch_does_not_bypass_scene_filters
- test_rewrite_bm25_dense_rrf_rerank_order
- test_retrieval_timeout_and_degraded_result_contract
- test_no_duplicate_embedding_or_double_retrieval
- test_collection_and_scene_mapping_are_preserved

风险：

- 统一接口可能改变已有召回顺序和 benchmark。
- prefetch 与 Agent 之间可能出现重复检索、状态污染或竞态。
- Protocol 当前签名与实际 Qdrant 调用约定还存在不一致，迁移时容易掩盖真实兼容问题。
- 这是跨层架构变更，必须先固定行为契约再实现。

实施边界：只统一检索编排和结果契约；不同时改缓存、路由、Agent 角色或 ERP。

#### P1-02 BM25 Restart Rebuild

当前状态：CONFIRMED。BM25Retriever 是进程内存结构；QdrantKnowledgeBase 只在 add_documents 时同步更新；_ensure_bm25 只是懒初始化，没有从 Qdrant scroll/load。ServiceContainer 在 RAG_PERSIST_DIRECTORY 模式明确跳过种子数据，重启后持久化向量仍在但 BM25 为空。

涉及文件：

- rag/bm25_retriever.py
- rag/qdrant_knowledge_base.py
- core/container.py
- rag/seed_data.py
- tests/unit/test_qdrant_knowledge_base.py
- tests/integration/ 中 Qdrant persistence/hybrid tests

预计新增/修改测试：

- test_bm25_rebuilds_from_persisted_qdrant_on_startup
- test_restart_preserves_dense_and_bm25_recall
- test_rebuild_is_idempotent
- test_rebuild_handles_empty_collection
- test_rebuild_failure_is_observable
- test_incremental_add_delete_keeps_bm25_consistent

风险：

- 启动耗时和内存峰值随 corpus 增长。
- rebuild 与在线写入并发可能产生部分索引。
- Qdrant scroll payload 不完整或旧数据缺少 content 时需定义行为。
- 在 P1-03 稳定 ID 和 P1-01 retrieve contract 未确定前，重建结果可能无法稳定复现。

实施边界：只处理 BM25 生命周期与持久化重建；不在此 Issue 内改变 RRF、reranker 或业务召回阈值。

#### P1-03 Stable Qdrant IDs

当前状态：CONFIRMED。add_documents 使用 hash(id_) & 0x7FFFFFFFFFFFFFFF 作为 Point ID。Python hash 可能因进程 hash seed 改变，导致同一逻辑 doc_id 在不同进程得到不同存储 ID；delete 仍通过 payload doc_id 过滤，未消除写入和重建的不稳定性。

涉及文件：

- rag/qdrant_knowledge_base.py
- rag/bm25_retriever.py（若索引使用 storage id）
- scripts/ 或数据迁移工具
- tests/unit/test_qdrant_knowledge_base.py
- tests/integration/ Qdrant persistence tests
- tests/eval/ golden/reference artifacts

预计新增/修改测试：

- test_same_logical_id_produces_same_qdrant_point_id
- test_point_id_is_valid_for_qdrant
- test_duplicate_upsert_is_idempotent
- test_delete_by_logical_id_removes_stable_point
- test_legacy_points_are_migratable
- test_benchmark_references_survive_restart

风险：

- 旧 collection 迁移可能发生 ID 冲突或重复点。
- 改 ID 算法会影响已有 benchmark、删除、更新和缓存引用。
- 需要明确跨 collection 是否允许相同逻辑 ID。
- 迁移不可在无备份或无 dry-run 情况下直接执行。

实施边界：只固定 point ID 生成和迁移契约；不顺带改数据内容和 embedding 模型。

#### P1-04 Agent Reachability

当前状态：CONFIRMED。Router 的 INTENT_AGENT_MAP 把 order_status、return_policy 映射到 billing_agent；container 创建 aftersales_agent，但没有把这些正常意图映射过去。现有 e2e 场景的 alias 允许 billing 作为 order_status/return_policy，可能掩盖可达性问题。

涉及文件：

- router/query_router.py
- core/container.py
- agents/billing_agent.py
- agents/aftersales_agent.py
- tests/unit/test_query_router_coverage.py
- tests/e2e/test_scenarios.py
- router/agent reachability integration tests

预计新增/修改测试：

- test_order_status_routes_to_intended_agent
- test_return_policy_routes_to_intended_agent
- test_aftersales_agent_is_reachable_from_normal_router
- test_aliases_do_not_mask_agent_selection
- test_agent_map_and_container_registration_are_consistent
- test_billing_fallback_is_explicit_when_used

风险：

- 路由切换会改变工具、提示词、知识库 collection 和响应风格。
- 现有场景 expected alias 可能让回归测试继续通过但未证明真实 agent。
- Aftersales 与 Billing 的职责边界、历史兼容意图需要先定义。
- 与 P0-03 的授权边界和 P1-01 的 scene retrieval 有联动。

实施边界：只修意图到 Agent 的可达性和契约测试；不在此 Issue 内重写 Router 算法。

#### P1-05 Tool Runtime

当前状态：CONFIRMED。ToolDefinition 只有名称、描述、参数和 handler；execute 没有统一 schema validation、request identity、authorization、timeout、retry、breaker、risk/idempotency 或结构化 ToolResult。

涉及文件：

- tools/tool_registry.py
- tools/erp_tools.py
- agents/base_agent.py
- core/protocols.py
- core/config.py
- tests/unit/test_tool_registry_streaming.py
- tests/unit/test_auth_tools_coverage.py
- tests/integration/test_erp_integration.py

预计新增/修改测试：

- test_tool_arguments_are_schema_validated
- test_tool_execution_has_request_timeout
- test_tool_authz_is_enforced_before_handler
- test_retry_is_bounded_and_non_idempotent_tools_are_not_retried
- test_circuit_breaker_opens_after_configured_failures
- test_structured_tool_result_preserves_error_code
- test_streaming_callback_does_not_bypass_policy

风险：

- 统一 wrapper 可能破坏现有 handler 签名和 mock。
- 重试有重复执行副作用，尤其是未来写操作。
- Tool 层异常类型和用户可见文案需要分离。
- P0-03 授权必须在 Tool 层与 Agent 层边界一致，不能只依赖 prompt。

实施边界：只建立工具运行时安全与可靠性边界；不增加新的业务工具。

#### P1-06 Global Deadline / Recursion / Retry Budget

当前状态：PARTIAL。当前有 RAG prefetch、协作模式、WebSocket 等局部 wait_for，也有 Agent 工具轮次/局部重试限制；但没有覆盖 graph 全链路的统一 deadline、recursion limit 和总 retry budget。不能把当前状态表述成“完全没有 timeout”。

涉及文件：

- core/app.py
- api/app.py
- core/graph_builder.py
- core/config.py
- agents/base_agent.py
- collaboration/modes.py
- api/routes/ws.py
- tests/stress/
- graph/collaboration timeout tests

预计新增/修改测试：

- test_end_to_end_graph_deadline
- test_deadline_propagates_to_llm_rag_and_tools
- test_cancellation_releases_background_tasks
- test_retry_budget_is_global_not_per_component_only
- test_recursion_limit_stops_cycle
- test_timeout_returns_explicit_degraded_response
- test_deadline_metrics_include_cancelled_requests

风险：

- cancellation 可能留下后台 task、连接或流式输出。
- 不同组件 timeout 相加可能误杀慢但合法请求。
- 重试预算过严会降低临时错误恢复率，过松仍会放大延迟。
- LangGraph 递归/线程配置和业务 deadline 的语义必须分开。

实施边界：只建立全链路预算和取消语义；不在此 Issue 内改变 Agent 推理策略。

#### P1-07 Router/CircuitBreaker Semantics

当前状态：CONFIRMED 为文档/注释与实现语义不一致。Router 注释称规则和 LLM 并行，但实际先完成规则分类，只有低于置信度阈值才调用 LLM；CircuitBreaker 当前是 consecutive failures + recovery time + half-open probe，而非“滑动窗口失败率”模型。

涉及文件：

- router/query_router.py
- core/monitoring.py
- core/config.py
- tests/unit/test_query_router_coverage.py
- tests/unit/test_core_modules.py
- docs/interview/、README 或其他描述 Router/CircuitBreaker 的材料

预计新增/修改测试：

- test_high_confidence_rule_path_skips_llm
- test_low_confidence_rule_path_invokes_llm
- test_rule_override_threshold
- test_circuit_breaker_consecutive_failure_transition
- test_half_open_probe_transition
- test_documented_router_semantics_match_runtime

风险：

- “修语义”可能被误解为改运行时；需要先决定是文档对齐还是算法重构。
- 修改规则和 LLM gate 会直接改变延迟、成本和分类结果。
- breaker 统计口径改变会影响生产告警和恢复行为。

实施边界：第一阶段只对齐文档、注释、测试命名和面试材料；算法改动必须另立变更。

#### P2-01 FCR Definition

当前状态：CONFIRMED。MetricsCollector 的 first_resolution_rate 是单轮 session 数除以 session 总数，未绑定 issue、首次解决状态、重联窗口或客户确认。record_request 中的 total_single_turn_resolved 与 get_kpi_stats 的 single_turn_sessions 也不是同一套严格定义。

涉及文件：

- core/monitoring.py
- core/state.py
- core/session/session_manager.py
- api/routes/feedback.py
- tests/unit/test_core_modules.py
- metrics/evaluation tests

预计新增/修改测试：

- test_fcr_requires_resolved_first_contact
- test_recontact_within_window_is_not_fcr
- test_multiple_issues_in_one_session_are_separated
- test_escalated_or_unresolved_turn_is_not_fcr
- test_fcr_denominator_is_explicit
- test_metric_is_stable_across_restart

风险：

- 没有 issue_id、recontact event 或人工标注时无法得到可信 FCR。
- 改口径会导致历史 dashboard 不可比。
- 把单轮对话直接当解决会继续制造虚高指标。

实施边界：只定义和实现可审计 FCR；不把 FCR 直接转换成业务 ROI。

#### P2-02 Labor Efficiency

当前状态：CONFIRMED。当前 labor_savings_estimate 只是 ai_handled_rate 百分比，没有人工基线、AHT、处理量、转人工率、质量约束或置信区间。

涉及文件：

- core/monitoring.py
- scripts/benchmark_cost.py
- scripts/_benchmark_utils.py
- metrics/evaluation tests
- docs/reports/ 或 KPI 文档

预计新增/修改测试：

- test_labor_efficiency_requires_baseline
- test_aht_and_volume_are_separate_from_ai_handled_rate
- test_cost_estimate_is_not_labeled_as_savings_without_assumptions
- test_metric_handles_missing_baseline
- test_report_contains_assumptions_and_confidence

风险：

- 把 handled rate 当 savings 会产生无法辩护的简历/面试数字。
- 缺少真实人工 baseline 时只能输出估算，不应输出事实性收益。
- 数据脱敏、采样偏差和场景分布会影响结论。

实施边界：只建立指标定义、输入字段和估算假设；不宣称实际降本。

#### P2-03 Benchmark Provenance

当前状态：CONFIRMED。benchmark_latency、benchmark_ab_test、benchmark_prefetch 和 benchmark_cost 在服务不可用、异常或默认场景下会模拟或降级；报告虽有 REAL/SIMULATED 等字段，但缺少 git SHA、dataset hash、model/provider/config 和 evidence level 的统一契约。

涉及文件：

- scripts/benchmark_latency.py
- scripts/benchmark_ab_test.py
- scripts/benchmark_prefetch.py
- scripts/benchmark_cost.py
- scripts/benchmark_cache.py
- scripts/_benchmark_utils.py
- tests/eval/
- tests/unit/ 新增 benchmark contract tests

预计新增/修改测试：

- test_simulation_is_explicitly_marked
- test_real_run_requires_health_and_request_evidence
- test_report_contains_git_sha
- test_report_contains_dataset_sha
- test_report_contains_model_provider_and_config
- test_fallback_does_not_retain_real_label
- test_default_scenario_is_not_labeled_as_production_measurement

风险：

- 真实依赖不可用时 benchmark 可能变慢或无法执行。
- 统一报告 schema 会使历史 JSON 需要迁移。
- 过度自动化“real”判定仍可能把 mock server 当真实服务。

实施边界：只补 provenance、mode/evidence contract 和失败语义；不优化 benchmark 结果本身。

#### P2-04 RAG Metric Semantics

当前状态：PARTIAL。当前 evaluate_rag.py 已区分 Hit Rate (Top-3)、Recall@3、Precision@3、MRR、NDCG@3，不能把当前实现简单判为“全部混用”。但评估报告缺少 dataset SHA、git SHA、模型和配置，历史材料仍可能把不同指标混写。

涉及文件：

- scripts/evaluate_rag.py
- tests/eval/rag_benchmark.json
- tests/eval/ 相关 golden/reference 文件
- docs/reference/、docs/reports/ 中的 RAG 指标材料
- tests/unit/ 新增 evaluator contract tests

预计新增/修改测试：

- test_hit_rate_is_binary_per_query
- test_recall_is_relevant_fraction
- test_precision_is_relevant_fraction_over_retrieved
- test_mrr_uses_first_relevant_rank
- test_ndcg_uses_explicit_relevance_definition
- test_report_labels_macro_averaging_and_k
- test_metric_report_contains_dataset_provenance

风险：

- 修公式或标签会导致历史 benchmark 不可直接比较。
- expected relevance 的定义如果不稳定，指标精确计算也没有意义。
- 把 Hit Rate、Recall 和 Precision 重新合并会重新制造歧义。

实施边界：只冻结指标定义、标签、聚合方式和 provenance；不修改知识库召回逻辑。

#### P2-05 Dataset / Business Provenance

当前状态：PARTIAL/UNVERIFIED。当前仓库有 seed data、generate_knowledge_base.py 和 import_real_docs.py；提交的 data/seed JSON 记录数为 50 + 45 + 35 + 38 + 5 = 173。README/设计文档中的“5000+ docs”没有在本轮通过 manifest、来源、版本和运行产物得到证明，不能直接当作当前线上 corpus 事实。

涉及文件：

- data/seed/*.json
- scripts/generate_knowledge_base.py
- scripts/import_real_docs.py
- scripts/benchmark_*.py
- tests/eval/rag_benchmark.json
- 新增 dataset manifest、source/license metadata
- docs/reference/ 和 README.md

预计新增/修改测试：

- test_dataset_manifest_matches_files
- test_dataset_hash_is_reproducible
- test_synthetic_and_real_records_are_labeled
- test_source_and_license_metadata_are_present
- test_benchmark_dataset_version_is_pinned
- test_report_does_not_claim_unproven_corpus_size

风险：

- 真实业务数据可能涉及隐私、授权和脱敏要求。
- 合成数据与真实数据混合会改变 benchmark 外推意义。
- corpus 规模、去重和 collection 状态可能随部署环境变化。

实施边界：只建立数据来源、版本、规模和授权证据；不扩大数据采集范围。

#### P2-06 Claim Evidence Matrix

当前状态：CONFIRMED 为治理缺口。当前没有 docs/audit/CLAIM_EVIDENCE_MATRIX.md；README、架构文档、benchmark、简历和面试材料缺少一处统一映射到代码、测试、运行产物和证据等级。

涉及文件：

- docs/audit/CLAIM_EVIDENCE_MATRIX.md（计划新增）
- README.md
- docs/design/architecture-design.md
- docs/interview/
- docs/reports/
- scripts/benchmark_*.py
- tests/eval/
- 计划新增 documentation contract test

预计新增/修改测试：

- test_claim_matrix_references_existing_files
- test_claim_matrix_labels_implemented_vs_planned
- test_claim_matrix_labels_estimate_vs_measured
- test_resume_and_interview_claims_have_evidence_level
- test_benchmark_links_are_reproducible

风险：

- 文档测试可能只验证链接存在，不能证明内容真实性。
- 过度依赖矩阵会把旧审计结论再次当成圣旨。
- 证据随代码变化，需要在 CI 或 release gate 中防止静默漂移。

实施边界：只建立证据索引和 claim governance；不替用户编写新的简历成果数字。

## 2. ISSUE_DEPENDENCY_GRAPH

依赖原则：先建立身份、安全和可验证性边界，再处理架构统一，最后刷新评估、文档和对外声明。依赖表示“前置契约或结果需要先稳定”，不表示必须在同一个提交中实现。

~~~text
P0-01 CI exit-code contract
   └──> 所有后续 Phase Gate 的自动验证可信度

P0-04 SSE identity context ───┐
                              ├──> P0-03 ERP ownership AuthZ
                              └──> P0-02 cache scope isolation

P0-05 embedding failure contract
   ├──> P1-01 unified RAG retrieval
   ├──> P1-02 BM25 restart rebuild
   └──> P2-03 benchmark provenance

P1-03 stable Qdrant IDs ──────> P1-02 BM25 rebuild
          └───────────────────> P2-04/P2-05 reproducible evidence

P1-01 unified RAG retrieval ──> P1-02/P2-04
P1-04 agent reachability ─────> P0-03/P1-05/P2-06
P1-05 tool runtime boundary ──> P0-03/P1-06
P1-06 global deadline ────────> P1-05/P2-03

P2-01 FCR definition ─┐
P2-02 labor efficiency ├──> P2-06 claim evidence matrix
P2-03 benchmark proof ┤
P2-04 RAG semantics ──┤
P2-05 dataset proof ──┘
~~~

跨 Issue 的硬约束：

1. P0-03 不得在没有 P0-04 的身份上下文契约时宣称“已完成授权”。
2. P0-02 不得只改 L1；L1/L2/L3 必须共享同一 scope policy。
3. P1-02 不得在 P1-03 的 ID 契约未确定前做不可逆数据迁移。
4. P2-03/P2-04/P2-05 的结果不得在 P2-06 之前升级为简历或面试中的事实性成果。
5. P0-01 是所有实现 Phase 的 gate，但不能以“CI 变红”作为跳过安全修复的理由。

## 3. IMPLEMENTATION_ORDER

### Phase 0：事实冻结与测试契约

目标：把当前判定和测试 baseline 固定下来，不修业务逻辑。

范围：

- 保存当前 main SHA、测试命令、环境变量类别和外部依赖状态。
- 为 18 个 Issue 建立状态表。
- 为 P0-01、P0-02、P0-03、P0-04、P0-05 先写失败性/保护性测试设计。
- 明确 P2-04 的指标定义和 P2-05 的数据 manifest 字段。

Gate：

- 每个 Issue 都有 CONFIRMED/PARTIAL/UNVERIFIED/BLOCKED 判定和证据路径。
- 不能用历史审计数字替代当前运行结果。
- 所有测试变更仍然是规划项；本阶段不修改代码。

### Phase 1：P0 安全与可验证性

推荐顺序：

1. P0-01 CI 假绿
2. P0-04 SSE Identity Context
3. P0-03 ERP IDOR/AuthZ
4. P0-02 Cache Cross-User Leakage
5. P0-05 Random Embedding Fallback

理由：

- 先让失败可见，避免后续 green build 假象。
- 先把 user identity 传到 graph，再在 ERP 和 cache 中消费。
- embedding 失败契约要在 RAG 和 semantic cache 共同稳定后，才能进入架构和 benchmark。

Gate：

- pytest 非零退出一定使 CI job 失败。
- SSE、REST、WebSocket 的身份上下文测试明确区分。
- 非 owner ERP 查询 fail closed；模型参数不能覆盖请求身份。
- 个性化 cache 在不同用户间无命中；公共 FAQ 仍按明确 policy 可共享。
- embedding 不可用时不产生随机向量，并有显式 degraded/error 证据。
- P0 测试均通过；没有将 P1/P2 重构混入变更。

### Phase 2：P1 数据与检索基础设施

推荐顺序：

1. P1-03 Stable Qdrant IDs
2. P1-02 BM25 Restart Rebuild
3. P1-01 RAG Pipeline Unification
4. P1-04 Agent Reachability
5. P1-05 Tool Runtime
6. P1-06 Global Deadline/Retry Budget
7. P1-07 Router/CircuitBreaker Semantics

理由：

- stable ID 是持久化、删除、重建和 benchmark reproducibility 的底层前提。
- BM25 rebuild 先解决重启状态缺口，再统一 retrieval orchestration。
- Agent reachability 先固定真实入口；Tool runtime 再统一工具边界。
- deadline/retry 需要知道实际 tool/RAG 调用边界。
- P1-07 第一阶段优先文档和测试对齐，避免把语义澄清误做成算法重构。

Gate：

- stable ID 在独立进程/重启场景保持一致，旧数据迁移有 dry-run 和回滚方案。
- Qdrant 持久数据重启后 BM25 可用，空 collection、重复 rebuild、并发写入有测试。
- 所有业务检索路径经过统一 contract，且无重复 prefetch 或隐式 bypass。
- 每个正常意图都能被预期 Agent 到达；测试断言真实 agent，不只断言 alias。
- Tool 调用有参数、身份、超时、错误结构和 retry policy。
- graph 具备全链路 deadline、取消清理和总预算，且组件局部 timeout 仍可解释。
- Router/CircuitBreaker 的文档、注释、测试与当前行为一致。

### Phase 3：回归与真实运行验证

目标：在外部依赖允许时验证 Qdrant、Redis、ERP、LLM 真实路径；不把 mock/模拟结果冒充生产事实。

Gate：

- 真实依赖可用性、版本、配置和健康检查有记录。
- 真实请求和失败请求均有 trace/evidence。
- P0/P1 回归不引入跨用户、跨 session、跨 collection 污染。
- 性能变化有 before/after，但未获得真实 baseline 时只输出估算或 blocked。

### Phase 4：P2 评估与证据

推荐顺序：

1. P2-01 FCR Definition
2. P2-02 Labor Efficiency
3. P2-03 Benchmark Provenance
4. P2-04 RAG Metric Semantics
5. P2-05 Dataset/Business Provenance
6. P2-06 Claim Evidence Matrix

理由：

- 先定义业务指标，再生成 benchmark 证据。
- 先冻结 benchmark provenance 和数据来源，再将结果放入统一 claim matrix。
- P2-06 是最后的汇总治理层，不是用来提前证明其他 Issue 已完成。

Gate：

- FCR 有 denominator、issue/recontact 语义和可复算样本。
- labor efficiency 明确 baseline、假设和置信区间；没有 baseline 不输出实际 savings。
- benchmark 报告包含 mode/evidence level、git SHA、dataset SHA、模型/Provider/config。
- Hit Rate、Recall、Precision、MRR、NDCG 的公式、k 和聚合方式有单测。
- 数据 manifest 可复算，合成/真实/导入来源和授权状态明确。
- Claim matrix 中所有 claim 标记 implemented/planned/estimated/measured，并能追溯到代码和测试。

### Phase 5：Documentation Alignment

目标：只在 P0/P1/P2 对应 gate 通过后同步 README、架构说明、简历 claim 和面试 Q&A。

Gate：

- 文档不再把 PARTIAL、UNVERIFIED 或模拟结果写成已实现事实。
- 每个数字都有来源、运行条件、数据版本和 evidence level。
- 历史审计结论保留为历史记录，但不覆盖当前代码验证。
- 对外材料只引用 claim matrix 中允许发布的条目。

## 4. 文档与代码事实不一致清单

1. 整改规格中的覆盖率 77.79%、1380 passed、1403 total 是历史快照；本轮未完整复跑，当前状态为 UNVERIFIED。
2. P0-05 原规格主要列出 rag/qdrant_knowledge_base.py，但 cache/response_cache.py 也存在 deterministic-random embedding fallback，实际整改范围必须明确包含它。
3. 规格中部分路径写成 graph_builder.py；当前实际路径是 core/graph_builder.py。
4. router/query_router.py 的模块注释写“LLM 与规则分类并行执行”，但实际控制流是规则先行、低置信度才调用 LLM。P1-07 应先做语义对齐，不应直接假定需要并行化。
5. P2-04 的原始描述暗示 Hit Rate、Recall、Precision、MRR 混用；当前 scripts/evaluate_rag.py 已分开输出这些指标。当前主要缺口是 provenance、聚合定义和历史材料对齐。
6. P1-04 的“9 个角色/9 个垂直 Agent”说法需要拆开：当前 container 可见 7 个业务 Agent，并另有 ReActAgent、ResponseAgent；aftersales_agent 已创建但正常 Router map 不可达。
7. P1-06 不能表述为“项目完全没有 timeout”：当前存在组件级 asyncio.wait_for 和局部轮次限制，缺的是 graph 全链路 deadline、recursion limit 和总 retry budget。
8. 现有 tests/e2e/test_scenarios.py 的 INTENT_ALIASES 允许 billing 覆盖 order_status/return_policy，这可能掩盖 AftersalesAgent reachability，应在后续测试中明确断言真实 agent。
9. WebSocket 当前存在 DEV_MODE 匿名认证旁路。它与 SSE user_id 丢失不是同一个 Issue，本计划不扩大 P0-04 范围；若要处理，应另立安全 Issue 或明确纳入认证边界。
10. 当前 core/protocols.py 中 KnowledgeBaseProtocol/ToolRegistryProtocol 的签名与实现调用约定存在待对齐点。这是 P1-01/P1-05 的相邻契约风险，本计划不把它未经验证地升级为新的 Issue。
11. 当前 tests/unit/test_ws_coverage.py 的 mock_run_graph 未必断言 user_id；因此“WebSocket 已传 user_id”的静态事实不等于现有测试覆盖了身份参数。
12. 已提交 seed JSON 文件合计 173 条，不足以单独证明 README 或设计文档中的“5000+ docs”；生成脚本存在不等于对应 corpus 已被构建、导入并用于当前服务。

## 5. Final Planning Definition of Done

本规划本身完成的条件：

- 18 个 Issue 均有当前事实判定。
- 每个 Issue 均列出涉及文件、预计测试和风险。
- 依赖关系、实施顺序和 Phase Gate 已定义。
- 所有发现的文档/代码不一致均明确标记。
- 没有把整改规格的断言自动升级为代码事实。
- 没有修改任何业务源码、测试源码或 CI。

后续真正实施时，任何 Issue 只有在其自身 Acceptance Criteria、对应 Phase Gate 和真实验证证据全部满足后，才能从“计划项”升级为“已整改”。

