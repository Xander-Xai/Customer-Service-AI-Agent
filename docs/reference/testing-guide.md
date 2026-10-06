> **文档定位**：CURRENT —— 随 `main` 同步的有效参考。历史快照在 `docs/reports/`，不作为当前事实。
> 首屏与总索引见 [README.md](../../README.md)；当前事实唯一入口是 [current-state.md](current-state.md)。

# 测试指南

测试分层、运行方式与新增测试的落位约定。

> 测试数量以 `pytest --collect-only -q` / `npm test` 的当前输出为准，**不要**在文档里硬编码用例数。


### 测试套件（目录口径）

> 测试数量会随开发持续变化，**不要在任何文档硬编码**；当前 collected 数以
> `pytest --collect-only -q` 输出为准。下表只描述覆盖范围。

| 目录 | 覆盖范围 | 运行方式 |
|---------|------|---------|
| `tests/unit/` | API 路由 / 中间件 / Agent / Session / Cache / Router / RAG / LLM / 工具 / 协作模式 / 查询路由 / 告警 / 知识库 / 认证 / 漂移 / Tool Result / BM25 lifecycle / point-id 迁移 / evidence harness 等 | `pytest tests/unit -q` |
| `tests/integration/` | Mock LLM 图集成 / ERP 适配器 / 多模态 / 音频管道 / 知识库生成 / BM25 重启 | `pytest tests/integration -q` |
| `tests/e2e/` | 全链路 E2E / 生产功能 / 场景路由 / Trace 传播 / 多模态 / 真实 LLM（需 `OPENAI_API_KEY`，标记 `real_llm`） | `pytest tests/e2e -q`（real_llm 默认跳过） |
| `tests/stress/` | 压力测试（`@pytest.mark.stress`）：缓存 / 总线 / 黑板 / 会话 / 路由并发 | `pytest tests/stress -q` |
| `tests/eval/` | RAG 评估资产：`rag_benchmark.json`（649 条基准，metadata 口径）+ golden 数据 | `make rag-eval-649`（`make eval-rag` 为兼容 alias；`scripts/evaluate_rag.py`） |
| `web/src/__tests__/` | Vitest 前端单元：主题 / 聊天状态 / SSE / 对比度 / Agent 映射 / 管理后台 | `npm test` |

**前端测试文件**（Vitest + jsdom）：`theme.test.js`、`chatState.test.js`、`copy.test.js`、`agents.test.js`、`contrast.test.js`、`sse.test.js`、`admin-settings.test.js`（数量以 `npm test` 输出为准）。

### 运行测试

默认测试 lane 是**离线**的：`.env.test` 把 `EMBEDDING_PROVIDER` /
`RERANKER_PROVIDER` / `STT_PROVIDER` / `TTS_PROVIDER` 全部设为 `local`，因此
embedding / rerank / 语音识别 / 语音合成都走进程内确定性实现（`rag/local_provider.py`、
`media/*_processor.py` 的 local 分支），**既不出网也不需要真实 API Key**。

```
# 全量测试（默认离线，无需 API Key，不发公网请求）
make test

# 或直接使用 pytest
python3 -m pytest tests/ -v --ignore=tests/e2e/test_e2e_real_llm.py

# 真实 provider 验证（需真实凭据 + 出网；默认不执行）
make test-real-providers

# 真实 LLM E2E 测试（需配置 OPENAI_API_KEY）
python3 -m pytest tests/e2e/test_e2e_real_llm.py -v -m real_llm

# 带覆盖率报告（最低门槛 80%）
make test-cov

# 快速测试（跳过 stress 标记的慢测试）
make test-fast

# 仅 Mock LLM 集成测试（推荐演示）
python3 -m pytest tests/integration/test_integration.py -v

# 单个测试
python3 -m pytest tests/e2e/test_all.py -v -k "test_router"

# RAG 检索质量评估（兼容入口；正式评测见下方 649 evidence 流程）
make eval-rag

# RAG 649 正式评测全流程（canonical formal evaluation）
make rag-eval-import          # 导入评测语料（幂等，含 gold 覆盖率审计 + manifest）
make rag-eval-649-preflight   # preflight gate（Qdrant/embedding/reranker/BM25）
make rag-eval-649-smoke       # 冒烟（前 16 条，不是正式证据）
make rag-eval-649             # 正式 649 全量 4-config 评测 → evidence artifact

# 代码检查（Ruff）
make lint

# 代码格式化（Ruff）
make format
```

### Locust 压测

```
# 启动 Locust 压测
locust -f tests/performance/locustfile.py --host=http://localhost:8000
# 访问 http://localhost:8089 配置并发用户数
```

---
