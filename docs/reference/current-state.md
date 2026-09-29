# 当前事实入口 (Current State)

> 本文件是当前 runtime 事实的唯一定位入口（Current entry point）。
> 它刻意**不硬编码 Git HEAD**：HEAD 请用 `git rev-parse HEAD` 获取。
> 带日期的历史审计报告（如 docs/reports/plans/**）只是当时的快照，不等于当前事实。
>
> 每个数字的获取方式都标注了命令；不要从旧文档复制这些数字。

## Runtime facts (generated)

以下值由 `python3 scripts/project_facts.py` 在当前 checkout 动态生成（2026-09-30 验证）：

- Runtime version (`core/config.py::VERSION`): **`6.3`**
- Default LLM (`core/config.py::OPENAI_MODEL`): **`Qwen/Qwen3-8B`**
- Default LLM base URL: `https://api.siliconflow.cn/v1`（provider: `siliconflow`）
- Default embedding (`core/config.py::EMBEDDING_MODEL`): **`BAAI/bge-large-zh-v1.5`**（1024 维）
- Default reranker (`core/config.py::RERANKER_MODEL`): **`BAAI/bge-reranker-v2-m3`**
- Vector DB (`core/config.py::VECTOR_DB_MODE`): `qdrant_only`（ChromaDB 已移除）
- Hybrid retrieval (`core/config.py::HYBRID_SEARCH_ENABLED`): `true`（向量 + BM25 + RRF 融合）
- Agent roles (`core/container.py::_init_agents`): **9** 个运行时角色
  （7 领域 Agent + ReActAgent + ResponseAgent；BaseAgent 是抽象基类、
  ResponseEvaluator 是质量评估器，两者不计入运行时角色）
- OpenAPI HTTP paths (`app.openapi()["paths"]`): **`53`**
- RAG benchmark queries (`tests/eval/rag_benchmark.json` metadata): **`649`**

## 验证命令（不要复制数字，重新执行）

```bash
# runtime 事实（版本/模型/路径数/benchmark 数/Agent 角色数）
python3 scripts/project_facts.py

# OpenAPI 快照一致性（docs/openapi.json 与 app.openapi()）
python3 scripts/generate_openapi.py --check

# RAG benchmark metadata 一致性
python3 - <<'PY'
import json
d = json.load(open("tests/eval/rag_benchmark.json", encoding="utf-8"))
assert d["metadata"]["total_queries"] == len(d["queries"])
print("RAG benchmark queries:", len(d["queries"]))
PY

# 当前 pytest 收集数（测试数量不写入任何文档，以此命令为准）
pytest --collect-only -q

# 当前前端测试
npm test

# 文档一致性审计
python3 scripts/audit_doc_consistency.py
```

## 稳定的架构事实

- 四层状态机：缓存检查 → 意图路由（LLM ∥ 规则并行）→ 协作模式（Sequential /
  Parallel / Consultation / Hierarchical / ReAct）→ 响应后处理。
  入口：`core/graph_builder.py::build_graph`。
- **Response Cache**（`cache/response_cache.py` + `cache/cache_policy.py`）:
  L1 Redis 精确 → L2 Qdrant 语义 → L3 Jaccard 回退；scope/version/ttl 由
  CachePolicy 统一决定。
- **Tool Result Context Engineering**（`core/tool_result_*.py`）是独立机制：
  确定性压缩、Top-K/token 预算、历史 compaction、专用 compressor、可选
  offload/store/recovery、scope-safe exact reuse cache、可选 semantic summary。
  它不是 Response Cache 的一部分；Session Memory（会话窗口/摘要）是第三种独立概念。
- RAG 链路：rewrite/filter → vector + BM25 → retrieval contract
  （`rag/retrieval_contract.py`）→ RRF 融合 → rerank（`rag/reranker.py`）→ context。
  BM25 lifecycle: `rag/bm25_lifecycle.py`；确定性 point ID 与迁移：`rag/point_id.py`、
  `rag/point_id_migration.py`。
- Embedding 通过 HTTP API 计算（`rag/api_embedding.py`），应用侧计算、Qdrant 只做存储检索。
- LLM 客户端：`llm/client.py`（指数退避重试 + 熔断 + FC + SSE 流式 + 连接池）；
  降级兜底 `llm/rule_based_llm.py`。

## 配置层级语义（runtime fallback ≠ 模板推荐值）

- `runtime fallback`: `core/config.py` 中 `os.getenv(...)` 的默认值。
- `deployment recommended value`: `.env.example` 与 `deploy/compose/` 模板值。
  两者可以不同，模板覆盖处必须注释说明（例如 `LLM_MAX_TOKENS`、`HTTP_TIMEOUT`、
  `LLM_ROUTER_TIMEOUT`）。
- `test override` / `production override`: 各自的环境配置文件显式覆盖。
- 任何一侧漂移会被 `scripts/audit_doc_consistency.py` 的 canonical config 检查捕获。

## 证据边界

- Provider authentication、provider token usage/billing、生产延迟/SLA、FCR、
  人工效率：`NOT_VERIFIED` / `NOT_MEASURED`（除非链接带 provenance 的当前 artifact）。
- 本地测试/fixture benchmark ≠ 生产证据。详见
  [docs/evaluation/production-evidence.md](../evaluation/production-evidence.md)。
- 历史报告快照位于 `docs/reports/**`（含日期），不作为当前事实入口。

## 历史快照与当前事实的关系

- `docs/reports/plans/**`、`docs/reports/milestone/**`、`docs/reports/releases/**`
  均为 AUDIT / RELEASE SNAPSHOT，只在该快照执行时有效。
- Snapshot SHA != Current HEAD（用 `git rev-parse HEAD` 获取当前 HEAD）。
- ADR 的历史决策正文不修改；当决策被取代时只更新 Status 行（例如 ADR-003 被
  [ADR-007](../decisions/007-current-default-llm.md) 取代）。
