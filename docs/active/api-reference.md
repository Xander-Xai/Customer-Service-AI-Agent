# API 参考文档

> 完整 API 端点列表。交互式文档：http://localhost:8000/docs（Swagger UI）

---

## WebSocket 实时对话

```javascript
// 连接（JWT 通过首条消息认证，非 URL 参数）
const ws = new WebSocket('ws://localhost:8000/ws/chat');

// 首条消息：认证 + 查询（10 秒内必须发送，否则断开 4002）
ws.send(JSON.stringify({
    token: "your-jwt-token",
    query: "这款产品的成分是什么？",
    session_id: "optional-session-id"
}));

// 接收消息（渐进式推送 — MessageBus 实时通知）
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    switch (data.type) {
        case 'ping':       // 空闲超时探测（需回复 pong）
        case 'status':     // 处理状态更新
        case 'progress':   // Agent 处理进度
        case 'response':   // 最终响应
        case 'error':      // 错误信息
    }
};
```

**WebSocket 安全特性：**
- 每 IP 连接限制（`WS_MAX_CONNECTIONS_PER_IP=5`），超限返回 4029
- 每连接消息限流（`WS_MESSAGE_RATE_LIMIT=10` 条/分钟）
- 空闲超时（`WS_IDLE_TIMEOUT=300s`），超时发送 ping，无响应断开 4008
- 认证方式：API Key（query param/header）或 JWT（首条消息 `token` 字段）

---

## REST API 端点

### 聊天接口

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `POST` | `/api/chat` | 同步对话接口 | JWT / API Key |
| `POST` | `/api/chat/stream` | SSE 真流式输出 | JWT / API Key |
| `POST` | `/api/chat/image` | 多模态图片 + 文本（REST） | JWT / API Key |
| `POST` | `/api/chat/multimodal/stream` | 多模态图片 + 文本（SSE 流式） | JWT / API Key |
| `POST` | `/api/chat/voice` | 语音识别 + 对话 | JWT / API Key |
| `POST` | `/api/chat/file` | 文件上传（图片/视频/PDF/DOCX/文本） | JWT / API Key |
| `POST` | `/api/tts` | 文本转语音（Edge TTS） | JWT / API Key |
| `GET` | `/api/tts/voices` | 获取可用 TTS 声音列表 | JWT / API Key |
| `WS` | `/ws/chat` | WebSocket 实时对话 | 首条消息 JWT |

### 认证接口

| 方法 | 路径 | 说明 | 限流 |
|------|------|------|------|
| `POST` | `/api/auth/register` | 用户注册 | 3 次/h |
| `POST` | `/api/auth/login` | 用户登录（返回 access + refresh token） | 5 次/5min |
| `POST` | `/api/auth/refresh` | 刷新 access_token | - |
| `POST` | `/api/auth/logout` | 登出（吊销 JWT） | - |
| `GET` | `/api/auth/me` | 当前用户信息 | JWT |
| `GET` | `/api/auth/users` | 用户列表 | JWT (admin) |
| `GET` | `/api/auth/audit` | 审计日志 | JWT (admin) |
| `PUT` | `/api/auth/users/{user_id}/role` | 修改用户角色 | JWT (admin) |

### 知识库管理

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/knowledge/stats` | 知识库统计 | JWT / API Key |
| `POST` | `/api/knowledge/seed` | 重新种子数据 | JWT (admin) |
| `POST` | `/api/knowledge/{collection}/add` | 添加文档 | JWT (admin) |
| `POST` | `/api/knowledge/sync` | 从 ERP 同步 | JWT (admin) |

### 告警管理

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/alerts/config` | 告警配置 | JWT (admin) |
| `POST` | `/api/alerts/test` | 测试告警通知 | JWT (admin) |
| `GET` | `/api/alerts/history` | 告警历史 | JWT (admin) |

### 监控与运维

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/health` | 健康检查 | 无 |
| `GET` | `/api/metrics` | 性能指标 + 缓存统计 | Admin Token |
| `GET` | `/api/kpi` | 业务 KPI | Admin Token |
| `GET` | `/api/cache/stats` | 缓存统计 | Admin Token |
| `GET` | `/api/sessions` | 会话列表 | JWT |
| `GET` | `/api/sessions/{id}` | 会话详情 | JWT |
| `DELETE` | `/api/sessions/{id}` | 删除会话 | JWT |
| `GET` | `/api/history` | 历史会话列表 | JWT |
| `GET` | `/api/history/{id}/messages` | 会话消息历史 | JWT |
| `POST` | `/api/feedback` | 客户满意度反馈 | JWT |
| `GET` | `/api/feedback/stats` | 反馈统计 | Admin Token |
| `GET` | `/api/alerts` | SLA 告警记录 | Admin Token |
| `GET` | `/api/circuit-breaker` | 熔断器状态 | Admin Token |
| `GET` | `/metrics/prometheus` | Prometheus 指标 | Admin Token |
| `GET` | `/api/monitoring/quality-trends` | 质量评分趋势 | Admin Token |
| `GET` | `/api/monitoring/hot-questions` | 高频问题 | Admin Token |
| `GET` | `/api/monitoring/satisfaction` | 满意度统计 | Admin Token |
| `GET` | `/api/monitoring/tokens` | Token 用量 | Admin Token |
| `GET` | `/api/sessions/{id}/checkpoint` | LangGraph 检查点 | JWT |
| `POST` | `/api/prompts` | Prompt 版本管理 | JWT (admin) |

### Prompt 版本管理

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/admin/prompts/agents` | 列出所有 Agent 及其 Prompt 版本 | JWT (admin) |
| `GET` | `/api/admin/prompts/{agent_name}` | 查询指定 Agent 的所有版本 | JWT (admin) |
| `POST` | `/api/admin/prompts/{agent_name}` | 创建新 Prompt 版本 | JWT (admin) |
| `PUT` | `/api/admin/prompts/{agent_name}/activate` | 激活指定版本（停用其他版本） | JWT (admin) |
| `GET` | `/api/admin/prompts/{agent_name}/active` | 查询当前激活版本 | JWT (admin) |

### 前端页面

| 路径 | 说明 | 认证 |
|------|------|------|
| `GET` `/` | 主页面（对话 + 监控） | 无 |
| `GET` `/login.html` | 登录/注册页 | 无 |
| `GET` `/admin.html` | 管理后台 | JWT (admin) |
| `GET` `/widget.html` | 嵌入式对话组件 | 无 |
| `GET` `/theme-comparison.html` | 主题预览页 | 无 |

---

## 错误码

| 错误码 | 含义 | 处理建议 |
|--------|------|----------|
| `400` | 请求参数错误 | 检查 query 字段长度和格式 |
| `401` | 认证失败 | 检查 API Key 或 JWT |
| `403` | 权限不足 | 使用 Admin Token 或 admin 角色 JWT |
| `429` | 请求过于频繁 | 降低请求频率，等待限流窗口重置 |
| `500` | 服务内部错误 | 检查日志 |
| `503` | LLM 服务不可用 | 检查 `/api/circuit-breaker` 状态 |
