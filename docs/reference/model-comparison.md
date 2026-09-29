# 模型选型与 Token 成本分析

> **当前口径（2026-09-30 与 runtime 事实收敛）**：
> - 默认 LLM：`Qwen/Qwen3-8B`（`core/config.py::OPENAI_MODEL`，ADR-007）。
> - Embedding：`BAAI/bge-large-zh-v1.5`（1024 维，HTTP API 计算，`rag/api_embedding.py`；ADR-008）。
> - Reranker：`BAAI/bge-reranker-v2-m3`（`rag/reranker.py::ApiReranker`）。
> - 向量库：Qdrant（`VECTOR_DB_MODE=qdrant_only`；ChromaDB 已移除）。
> - 本文中的延迟/价格/成本数字均为 **历史估算或示例假设**（2026-06 整理），
>   当前 checkout 没有可复现的 provider latency/billing 测量，属
>   `NOT_MEASURED` / `NOT_VERIFIED`。引用时必须注明"历史估算，非当前事实"。
>
> 用于回答"用过哪些模型？怎么选型？"和"Token 成本怎么控制？"。

---

## 1. Current implementation（当前实现）

### 1.1 当前默认模型

| 组件 | 当前配置 | 代码位置 |
|------|---------|---------|
| LLM | `Qwen/Qwen3-8B`（OpenAI-compatible API） | `core/config.py::OPENAI_MODEL` / [llm/client.py](../../llm/client.py) |
| Embedding | `BAAI/bge-large-zh-v1.5`（1024 维，HTTP API，连接池复用） | `core/config.py::EMBEDDING_MODEL` / [rag/api_embedding.py](../../rag/api_embedding.py) |
| Reranker | `BAAI/bge-reranker-v2-m3`（API reranker） | `core/config.py::RERANKER_MODEL` / [rag/reranker.py](../../rag/reranker.py) |
| 向量库 | Qdrant（Cosine，vectors.size=1024） | `core/config.py::QDRANT_COLLECTION_CONFIG` / [rag/qdrant_knowledge_base.py](../../rag/qdrant_knowledge_base.py) |
| Embedding Key 回退 | `EMBEDDING_API_KEY` 为空时复用 `OPENAI_API_KEY` | `core/config.py` |

Provider 切换（`LLM_PROVIDER` = siliconflow / deepseek / openai / custom）只改
`OPENAI_BASE_URL`/`OPENAI_MODEL`/`OPENAI_API_KEY`，接口协议不变。
运行手册：[llm-provider-switch.md](../operations/llm-provider-switch.md)。

### 1.2 Response Cache 与 Token 相关的当前事实

Response Cache 与 Tool Result Context Engineering 是**两套独立机制**（ADR-006）：

- **Response Cache**（[cache/response_cache.py](../../cache/response_cache.py)）：
  L1 Redis MD5 精确 → L2 Qdrant 语义 → L3 Jaccard 回退。
- **Tool Result Context Engineering**（[core/tool_result_optimizer.py](../../core/tool_result_optimizer.py) 等）：
  确定性压缩、Top-K、token 预算、历史 compaction、可选 offload/store/recovery、
  scope-safe exact reuse、可选 semantic summary。
- **Session Memory**（[core/session/session_manager.py](../../core/session/session_manager.py)）：
  滑动窗口 + 摘要，第三种独立概念。

缓存命中直接跳过 LLM 调用，是确定的 Token 消耗减少路径；但**命中率与实际
节省额度取决于真实查询分布，没有当前测量值**（`NOT_MEASURED`），
上线后通过 `/api/cache/stats` 观察。

### 1.3 Token 追踪（当前实现）

**代码**：[core/token_tracker.py](../../core/token_tracker.py)

系统按 Agent / 模型维度追踪每次 LLM 调用的输入/输出 Token 估算值，
暴露 `/api/monitoring/tokens`。注意：本地估算 ≠ provider 计费口径；
provider 侧 usage 字段只在受控 staging harness（`scripts/run_production_evidence.py`）
中按 `PROVIDER_REPORTED` 采集，生产 billing 属 `NOT_AVAILABLE`。

---

## 2. Token 成本分析（历史估算 / 示例假设，非当前事实）

> 以下所有数字是 2026-06 编写的**估算模型与示例假设**，用于说明方法论。
> 它们不构成 provider billing 证据。生产成本必须由带 provenance 的
> `PROVIDER_REPORTED` usage 数据得出。

### 2.1 单次对话 Token 消耗（估算口径示例）

| 组件 | Token 数（示例估算） | 说明 |
|------|---------|------|
| System Prompt | ~300-500 | 角色定义 + 安全规则 + 专业领域 |
| RAG 上下文 | ~200-800 | Top-3 检索结果注入 |
| 对话历史 | ~100-600 | 滑动窗口，最近 10 轮 |
| 用户输入 | ~50-200 | 平均 50-100 字 |
| LLM 输出 | ~200-500 | 平均 100-200 字回复 |
| **单次调用总计** | **~850-2,600** | 含上下文注入 |

### 2.2 不同协作模式的 Token 成本（示例假设）

| 协作模式 | LLM 调用次数 | 估算 Token/对话 | 适用比例 |
|---------|-------------|----------------|---------|
| Sequential | 1 次 | ~1,500 | ~60% |
| Parallel | 2-3 次 | ~3,500 | ~15% |
| Consultation | 2 次 | ~2,500 | ~15% |
| Hierarchical | 3-4 次 | ~5,000 | ~5% |
| ReAct | 2-5 次 | ~6,000 | ~5% |

### 2.3 月度成本估算（示例）

> 假设日均 1 万次对话、按 2.2 的模式分布、`Qwen/Qwen3-8B` 输入 ¥0.35/1M +
> 输出 ¥0.70/1M（2026-06 估算价，未复核）时的演算示例：

```text
日均对话: 10,000 次
加权平均 Token/对话: ≈ 2,350 tokens（按 2.2 分布演算）
月均 Token: ≈ 705M tokens
月成本演算: ≈ ¥300（不含查询改写/质量评估等附加调用）
含附加调用的示例区间: ¥500-800/月
```

该演算的每个输入（日均对话量、模式分布、单价）都是假设，不是测量值。

### 2.4 Token 优化策略（当前实现的机制）

| 机制 | 实现 | 状态 |
|------|------|---------|
| **Response Cache** | L1 Redis 精确 + L2 Qdrant 语义 + L3 Jaccard | 已实现；命中率 `NOT_MEASURED` |
| **滑动窗口** | 会话历史 token 级截断（`SESSION_MAX_TOKENS`） | 已实现 |
| **路由分流** | 简单问题走 Sequential 快速通道 | 已实现 |
| **熔断降级** | LLM 故障时降级规则引擎（零 Token） | 已实现 |
| **Tool Result 预算** | 确定性压缩 + Top-K + 预算（默认关闭） | 已实现；本地估算 ≠ provider billing |
| **Token Quota** | 用户级每日/每月限额 | 已实现 |

### 2.5 模型对比矩阵（延迟/价格为历史估算，2026-06）

| 模型 | 提供商 | 中文能力 | FC 支持 | 延迟(首token) | 价格(输入/输出) | 状态 |
|------|--------|-------|---------|--------------|----------------|---------|
| **Qwen/Qwen3-8B** | SiliconFlow | ⭐⭐⭐⭐ | ✅ | ~0.8s（历史估算） | ¥0.35/¥0.70 per 1M（历史估算） | **当前默认** |
| Qwen2.5-7B-Instruct | SiliconFlow | ⭐⭐⭐⭐ | ✅ | — | — | **历史默认（ADR-003）** |
| DeepSeek-V3 (deepseek-chat) | DeepSeek | ⭐⭐⭐⭐⭐ | ✅ | ~1.2s（历史估算） | ¥1.0/¥2.0 per 1M（历史估算） | 可切换 |
| GPT-4o-mini | OpenAI | ⭐⭐⭐ | ✅ | ~0.6s（历史估算） | $0.15/$0.60 per 1M（历史估算） | 可切换 |

> 所有延迟与价格列为**历史估算**（2026-06，来源未在当前 checkout 复现验证），
> 不得作为采购/SLA 依据。当前计价请以 provider 官方页为准并注明日期。

---

## 3. Embedding / Reranker：Current vs Historical

### 3.1 当前实现（2026-09-30 收敛）

- Embedding 由 **HTTP API** 计算（`rag/api_embedding.py`，httpx.AsyncClient 连接池，
  超时 10s）；本地不再加载 sentence-transformers。
- 主模型 `BAAI/bge-large-zh-v1.5`（`EMBEDDING_DIM=1024`）；Qdrant 集合按 1024 维 Cosine 创建。
- Reranker 为 `ApiReranker`（`BAAI/bge-reranker-v2-m3`）；API 不可用时回退原始
  排序（fail-open to order），不再有本地 BM25 重排器。
- 无"三级模型降级链"——embedding 服务不可用时检索按
  [rag/embedding_status.py](../../rag/embedding_status.py) 显式降级
  （fail-closed 语义见 `cache/response_cache.py` 与单元测试）。

### 3.2 历史实验（2026-06，仅作对比，不是当前配置）

以下链路已被当前实现取代（历史记录，见 ADR-004 / ADR-008）：

```text
优先级 1: BAAI/bge-small-zh-v1.5    (768 维，本地 sentence-transformers；历史模型)
优先级 2: shibing624/text2vec-base-chinese（历史备选）
优先级 3: all-MiniLM-L6-v2（历史兜底）
```

历史评测数字（30 条查询评估集，2026-06-06/06-10 报告，非当前 649 条基准）：

| Embedding 模型（历史） | Hit Rate@3 | MRR | 说明 |
|---------------|------------|-----|------|
| all-MiniLM-L6-v2 (英文) | 63.3% | 0.500 | 改进前基线 |
| bge-small-zh-v1.5（本地） | 80.0% | 0.778 | 2026-06-10 评估报告值 |

当前检索质量的当前值需要用 `python3 scripts/evaluate_rag.py`（649 条基准）
重跑生成新的带日期 artifact 后引用。

### 3.3 历史文档链接修正

早期文档曾引用 `rag/knowledge_base.py:61/114/145` 作为实现位置。当前
`rag/knowledge_base.py` 只是对 `QdrantKnowledgeBase` 的兼容别名（8 行薄层），
真实实现位置：

- 多 collection 并行检索 / RRF 融合：[rag/qdrant_knowledge_base.py](../../rag/qdrant_knowledge_base.py)
- 混合检索契约：[rag/retrieval_contract.py](../../rag/retrieval_contract.py)
- BM25 通道与 lifecycle：[rag/bm25_retriever.py](../../rag/bm25_retriever.py) / [rag/bm25_lifecycle.py](../../rag/bm25_lifecycle.py)
- Embedding API：[rag/api_embedding.py](../../rag/api_embedding.py)
- Reranker：[rag/reranker.py](../../rag/reranker.py)

---

## 4. 面试话术（与证据边界一致）

### Q: "Token 成本怎么控制？"

> 系统提供了这些确定性机制：1) Response Cache 三层（L1 Redis 精确 / L2 Qdrant
> 语义 / L3 Jaccard 回退），命中即跳过 LLM；2) 会话滑动窗口 token 级截断；
> 3) 路由分流，简单问题走 Sequential；4) 熔断降级，故障时规则引擎零 Token；
> 5) Tool Result Context Engineering（压缩/预算/offload），默认关闭、可回滚。
> 实际节省比例取决于真实查询分布，本地/生产都没有当前测量值——这是 NOT_MEASURED，
> 不能引用某个具体百分比作为生产事实。

### Q: "Embedding 模型怎么选的？"

> 当前用 BAAI/bge-large-zh-v1.5（1024 维），通过 HTTP API 计算嵌入，应用侧
> embed、Qdrant 只做存储检索。历史上（2026-06）曾用本地 sentence-transformers
> 加载 bge-small-zh-v1.5 三级降级链，当时的 30 条查询评估显示中文模型把
> Hit Rate@3 从 63.3% 提升到 80%（历史数据）。当前 649 条基准上的当前指标需要
> 重跑 `scripts/evaluate_rag.py` 才能引用。检索链路是 rewrite/filter →
> 向量 + BM25 → retrieval contract → RRF(k=60) → bge-reranker-v2-m3 重排。

### Q: "为什么从本地模型迁移到 API embedding？"

> 三个原因：1) 部署一致性——不用在应用容器内安装/下载 sentence-transformers
> 模型；2) 与 LLM 使用同一 OpenAI-compatible provider 面板，便于运维；
> 3) 超时与重试行为可显式配置（10s 超时 + 连接池复用）。代价是 embedding 依赖
> 网络，因此系统按 embedding 不可用场景做了 fail-closed 语义（缓存侧不降级写入、
> 检索侧显式降级路径）。
