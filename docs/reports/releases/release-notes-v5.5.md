# 药妆智多星 v5.5 发布说明

**发布日期**: 2026-06-18  
**版本类型**: Minor Release  
**主题**: LLM 降级增强、启动健康检查、前后端 API 与文档对齐

---

## 核心变更

- **账单 Agent 降级增强**：LLM 不可用时，账单场景 fallback 会保留已查询到的 ERP 订单数据，避免用户只看到空泛系统错误。
- **LLM 启动健康检查**：`ServiceContainer.initialize()` 非阻塞验证 LLM 端点可用性，失败时记录日志并继续以降级模式启动。
- **API Key 占位符校验加固**：识别 `test-`、`mock-`、`sk-placeholder`、`sk-xxx` 等非生产 Key，防止测试 Key 误判为可用生产配置。
- **前端 API 导出补齐**：`web/src/api/index.js` 统一导出知识库、告警历史、Prompt 版本、用户角色、TTS/Voice 等 REST 封装。
- **文档按真实代码重同步**：`docs/reference/api-reference.md` 与 `docs/openapi.json` 从 `api.app_factory:app` 的真实路由生成/校对。

---

## 当前真实接口盘点

| 类型 | 数量 | 来源 |
|------|------|------|
| `/api/*` 路径 | 45 | FastAPI OpenAPI |
| REST/HTTP 操作 | 48 | 47 个业务操作 + `GET /metrics/prometheus` |
| WebSocket | 1 | `api/routes/ws.py` 的 `WS /ws/chat` |
| 后端直出页面 | 4 | `/`、`/login.html`、`/admin.html`、`/widget.html` |
| favicon 资产 | 2 | `/favicon.ico`、`/favicon.svg` |

> OpenAPI 当前包含 52 个 HTTP 路径；WebSocket 不属于 OpenAPI schema，因此单独统计。

---

## 受影响文件

| 文件 | 变更 |
|------|------|
| `agents/billing_agent.py` | 账单 fallback 保留 ERP 查询上下文 |
| `agents/base_agent.py` | LLM 异常时尝试 RuleBasedLLM 智能降级 |
| `core/container.py` | LLM 启动健康检查与 API Key 占位符校验 |
| `core/config.py` | 应用默认版本为 `5.5` |
| `llm/rule_based_llm.py` | 提供 `async_invoke` 兼容异步降级调用 |
| `web/src/api/index.js` | 补齐统一 API 导出，避免后端能力只存在于底层 REST 模块 |
| `docs/reference/api-reference.md` | 按真实路由重写 API 参考 |
| `docs/openapi.json` | 从 FastAPI 应用重新导出 |
| `README.md` / `package.json` / `pyproject.toml` | 版本和端点统计对齐到 `5.5` |

---

## 验证

- `python3 - <<'PY' ... app.openapi() ... PY`：成功导出 52 个 OpenAPI HTTP 路径。
- `docs/reference/api-reference.md`：列出全部 48 个 REST/HTTP 操作、1 个 WebSocket 和 4 个后端页面。
- 前端统一入口 `API` 已导出底层 REST 模块中的管理、知识库、告警、Prompt、TTS/Voice 能力。
