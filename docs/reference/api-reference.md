# API 参考文档

> 本文档按 `api.app_factory:app` 的真实 FastAPI 路由同步。交互式文档：http://localhost:8000/docs（Swagger UI）。当前 OpenAPI 导出：`docs/openapi.json`。

---

## 当前接口总览

| 类型 | 数量 | 说明 |
|------|------|------|
| HTTP 路径 | 55 | 当前 `app.openapi()` 导出的全部 HTTP 路径（含页面，不含 WebSocket） |
| `/api/*` 业务路径 | 49 | 认证、对话、多模态、会话、监控、知识库、缓存、告警、Prompt 管理等业务接口（共 50 个后端路由中 49 个位于 `/api/*` 下） |
| WebSocket | 1 | `WS /ws/chat` 实时双向对话 |
| HTML 页面 | 5 | `/`、`/login.html`、`/admin.html`、`/widget.html`、`/theme-comparison.html` |

> 说明：OpenAPI 不包含 WebSocket 路由，因此 `WS /ws/chat` 在本文档中单独列出。

---

## WebSocket 实时对话

```javascript
// 连接（JWT / API Key 通过首条消息认证，避免 URL 泄露）
const ws = new WebSocket('ws://localhost:8000/ws/chat');

// 连接打开后 10 秒内发送认证消息；之后再发送业务查询
ws.send(JSON.stringify({
  type: 'auth',
  token: 'your-jwt-token',
  api_key: 'optional-api-key',
  session_token: 'optional-session-token'
}));

ws.send(JSON.stringify({
  query: '这款产品的成分是什么？',
  session_id: 'optional-session-id',
  session_token: 'optional-session-token'
}));

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  switch (data.type) {
    case 'ping':       // 需回复 { type: 'pong' }
    case 'status':     // 处理状态更新
    case 'progress':   // Agent 处理进度
    case 'response':   // 最终响应
    case 'error':      // 错误信息
  }
};
```

**SSE 流式事件类型（10 种）：**

| 事件类型 | 时机 | 字段 |
|---------|------|------|
| `status` | 初始状态消息 | `{ type, content }` |
| `progress` | 处理进度更新 | `{ type, content }` |
| `thinking` | Agent 正在思考 | `{ type, content }` |
| `tool_call` | 工具调用开始 | `{ type, name, args }` |
| `tool_result` | 工具调用结果 | `{ type, name, summary }` |
| `rag_status` | RAG 检索阶段（改写→检索→重排→完成） | `{ type, status, count }` |
| `agent_switch` | Agent 切换 | `{ type, to }` |
| `chunk` | 流式文本片段 | `{ type, content }` |
| `content_complete` | 内容段完成 | `{ type, content }` |
| `done` | 全部完成（含最终元数据） | `{ type, content, agent, mode, elapsed, cached, agents_used, resolution_status, session_id, session_token }` |

**WebSocket 安全特性：**
- 每 IP 连接限制：`WS_MAX_CONNECTIONS_PER_IP=5`，超限返回 4029。
- 每连接消息限流：`WS_MESSAGE_RATE_LIMIT=10` 条/分钟。
- 空闲超时：`WS_IDLE_TIMEOUT=300s`，超时发送 `ping`，无响应断开 4008。
- 认证优先级：API Key（query/header/首条消息 `api_key`）优先，否则 JWT（首条消息 `token` + 可选 `session_token`）。

---

## REST / HTTP API

### 对话、多模态与 TTS

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `POST` | `/api/chat` | 同步对话接口 | `sendChat` |
| `POST` | `/api/chat/stream` | SSE 真流式输出 | `sendChatStream` |
| `POST` | `/api/chat/image` | 图片 + 文本对话 | `sendChatWithImage` |
| `POST` | `/api/chat/multimodal/stream` | 图片 + 文本 SSE 流式对话 | `sendChatStreamWithImage` |
| `POST` | `/api/chat/voice` | 语音识别 + 对话 | `sendVoiceForm` |
| `POST` | `/api/chat/file` | 文件上传对话（图片/视频/PDF/DOCX/文本，当前统一 `5MB` 上限） | `sendChatWithFile` |
| `POST` | `/api/chat/multimodal` | 统一多模态入口：通过 `file-type` Header 指定类型（`auto`/`voice`/`image`/`document`），自动路由到对应处理器 | —（未在前端主应用调用，Widget 使用） |
| `POST` | `/api/tts` | 文本转语音 | `sendTTS` |
| `GET` | `/api/tts/voices` | 获取可用 TTS 声音列表 | `getTTSVoices` |

> 上传约束：前端和后端当前均按 `5MB` 统一限制；图片格式白名单为 `image/jpeg`、`image/png`、`image/webp`。

**`POST /api/chat/multimodal` 请求参数：**

| 参数 | 位置 | 类型 | 默认值 | 说明 |
|------|------|------|--------|------|
| `file` | Form body | UploadFile | — | 上传文件（可选，无文件时走纯文本） |
| `message` | Form body | string | `""` | 文本消息 |
| `file-type` | **Header** | string | `"auto"` | 文件类型提示：`auto`（自动检测）、`voice`、`image`、`document`。注意：HTTP Header 名为 `file-type`（连字符），对应 FastAPI 参数 `file_type`（下划线） |
| `session_id` | Form body | string | `""` | 会话 ID |
| `session_token` | Form body | string | `""` | 会话令牌 |

**`POST /api/chat/multimodal` 响应 Schema：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 文件类型：`voice` / `image` / `document` / `text` |
| `response` | string | AI 对话回复（voice/image 时返回） |
| `agent` | string | 处理 Agent 名称 |
| `mode` | string | 协作模式（默认 `sequential`） |
| `elapsed` | float | 处理耗时（秒） |
| `session_id` | string | 会话 ID |
| `session_token` | string | 会话令牌 |
| `transcription` | string | 语音转录文本（仅 `type=voice`） |
| `image_url` | string | 图片 data URL（仅 `type=image`） |
| `message` | string | 原始消息文本（voice 时为转录文本，image 时为用户输入） |

### 认证与 RBAC

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `POST` | `/api/auth/register` | 用户注册 | `web/src/login.js` |
| `POST` | `/api/auth/login` | 用户登录 | `web/src/login.js` |
| `POST` | `/api/auth/refresh` | 刷新 access token | `refreshToken` / `fetchWithAuth` |
| `POST` | `/api/auth/logout` | 登出并吊销 JWT | `logout` |
| `GET` | `/api/auth/me` | 当前用户信息 | `getUserMe` |
| `GET` | `/api/auth/users` | 用户列表 | `getUsers` |
| `GET` | `/api/auth/audit` | 审计日志 | `getAuditLog` |
| `PUT` | `/api/auth/users/{user_id}/role` | 修改用户角色 | `updateUserRole` |

### 会话与历史

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `GET` | `/api/sessions` | 会话列表 | `getSessions` |
| `GET` | `/api/sessions/{session_id}` | 会话详情 | `getSession` |
| `DELETE` | `/api/sessions/{session_id}` | 删除会话 | `deleteSession` |
| `GET` | `/api/sessions/{session_id}/checkpoint` | LangGraph checkpoint 状态 | `getSessionCheckpoint` |
| `GET` | `/api/history` | 历史会话列表 | `getHistory` |
| `GET` | `/api/history/{session_id}/messages` | 会话消息历史 | `getHistoryMessages` |

### 反馈

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `POST` | `/api/feedback` | 客户满意度反馈 | `submitFeedback` / `submitRating` |
| `GET` | `/api/feedback/stats` | 反馈统计 | `getFeedbackStats` |

### 监控与运行状态

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `GET` | `/api/health` | 健康检查 | `getHealth` |
| `GET` | `/api/metrics` | 性能指标 + 缓存统计 | `getMetrics` |
| `GET` | `/api/kpi` | 业务 KPI | `getKPI` |
| `GET` | `/api/cache/stats` | 缓存统计 | `getCacheStats` |
| `POST` | `/api/cache/invalidate` | 按条件删除缓存（主动失效，支持 `product_id` / `intent_type` 过滤） | —（未在前端直接调用） |
| `GET` | `/api/circuit-breaker` | 熔断器状态 | `getCircuitBreaker` |
| `GET` | `/metrics/prometheus` | Prometheus 文本指标（`text/plain`） | `getPrometheusMetrics` |
| `GET` | `/api/monitoring/quality-trends` | 质量评分趋势 | `getQualityTrends` |
| `GET` | `/api/monitoring/hot-questions` | 高频问题 | `getHotQuestions` |
| `GET` | `/api/monitoring/satisfaction` | 满意度统计 | `getSatisfaction` |
| `GET` | `/api/monitoring/token-quota` | 当前用户 Token Quota 状态 | `getTokenQuota` |
| `GET` | `/api/monitoring/tokens` | LLM Token 用量统计 | `getTokenUsage` |

### 告警

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `GET` | `/api/alerts` | SLA 告警记录 | `getAlerts` |
| `GET` | `/api/alerts/config` | 告警配置 | `getAlertConfig` |
| `POST` | `/api/alerts/test` | 发送测试告警 | `testAlert` |
| `GET` | `/api/alerts/history` | 告警历史 | `getAlertHistory` |

### 知识库

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `GET` | `/api/knowledge/stats` | 知识库统计 | `getKnowledgeStats` |
| `POST` | `/api/knowledge/seed` | 重新种子数据 | `seedKnowledge` |
| `POST` | `/api/knowledge/{collection}/add` | 添加文档 | `addKnowledgeDocs` |
| `POST` | `/api/knowledge/sync` | 从 ERP 同步 | `syncKnowledge` |

### Prompt 版本管理

| 方法 | 路径 | 说明 | 前端封装 |
|------|------|------|----------|
| `GET` | `/api/admin/prompts/agents` | 列出所有 Agent 及其 Prompt 版本 | `getPromptAgents` |
| `GET` | `/api/admin/prompts/{agent_name}` | 查询指定 Agent 的所有版本 | `getPromptVersions` |
| `POST` | `/api/admin/prompts/{agent_name}` | 创建新 Prompt 版本 | `createPromptVersion` |
| `PUT` | `/api/admin/prompts/{agent_name}/activate` | 激活指定版本 | `activatePromptVersion` |
| `GET` | `/api/admin/prompts/{agent_name}/active` | 查询当前激活版本 | `getActivePrompt` |

### 前端页面与资产

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 主页面（对话 + 监控） |
| `GET` | `/login.html` | 登录/注册页 |
| `GET` | `/admin.html` | 管理后台（监控/Prompt/用户/知识库/告警/Token Quota/熔断器/Prometheus 预览） |
| `GET` | `/widget.html` | 嵌入式对话组件（API Key 专用认证，无 JWT；已保存 `session_id/session_token`，支持多轮连续对话） |
| `GET` | `/theme-comparison.html` | 主题对比预览 |

---

## 认证约定

| 认证方式 | 适用场景 | 前端实现 |
|----------|----------|----------|
| JWT Bearer | 终端用户登录后访问业务 API | `fetchWithAuth()` 自动注入 `Authorization` 并在 401 时刷新 token |
| API Key | 系统间调用或开发模式兜底 | `web/src/api/rest.js` 从 `localStorage.api_key` 注入 `X-API-Key` |
| Session Token | 会话详情、删除、历史消息等会话级校验 | `currentSessionToken` 注入 `X-Session-Token` 或请求体 |
| Admin Token / RBAC | 监控、管理、Prompt、知识库、告警 | 后端中间件和路由级 `require_admin` / `require_supervisor_or_admin` 校验 |

> **Widget 页面认证**：`/widget.html` 使用 API Key 专用认证（通过 URL 参数 `?api_key=xxx` 传入），不依赖 JWT 登录流程，适用于嵌入式场景。

> **Widget CSP 策略**：`widget.html` 的 `frame-ancestors` 设为 `'self'`（允许 iframe 嵌入），其他 HTML 页面的 `frame-ancestors` 设为 `'none'`（禁止嵌入）。

## 当前前端映射说明

- 主聊天页已经覆盖：`/api/chat`、`/api/chat/stream`、`/api/chat/image`、`/api/chat/multimodal/stream`、`/api/chat/file`、`/api/chat/voice`、会话与反馈相关接口。
- 管理后台已经覆盖：健康检查、监控指标、KPI、缓存、SLA 告警、知识库、用户、审计日志、Prompt 管理、Token Quota、Token 用量、熔断器详情、Prometheus 文本预览。
- Widget 当前通过 `/api/chat` 和 `/api/chat/stream` 进行交互，并把服务端返回的 `session_id/session_token` 保存在 `sessionStorage` 中，以保持同一浏览器标签页内的多轮上下文。

---

## 错误码

| 错误码 | 含义 | 处理建议 |
|--------|------|----------|
| `400` | 请求参数错误 | 检查字段长度、类型和格式 |
| `401` | 认证失败或登录过期 | 检查 JWT / refresh token / API Key |
| `403` | 权限不足 | 使用具备 admin 或 supervisor 权限的账号 |
| `429` | 请求过于频繁 | 降低请求频率，等待限流窗口重置 |
| `500` | 服务内部错误 | 查看应用日志和 `/api/health` |
| `503` | LLM 服务不可用 | 查看 `/api/circuit-breaker` 和降级日志 |
