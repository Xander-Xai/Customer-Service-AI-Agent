# API 参考

## 概述

本系统提供 REST API 和 WebSocket 两种接口方式。所有需要认证的端点需要在请求头中携带 API Key。

## 认证

### API Key 认证

```bash
# REST API
curl -H "X-API-Key: your-api-key" https://api.example.com/api/endpoint

# WebSocket
const ws = new WebSocket('ws://api.example.com/ws/chat', {
    headers: { 'X-API-Key': 'your-api-key' }
});
```

### Admin Token 认证

```bash
# 管理端点需要 Admin Token
curl -H "X-Admin-Token: your-admin-token" https://api.example.com/api/metrics
```

## REST API

### 聊天接口

**POST** `/api/chat`

发送对话请求。

**请求**：
```json
{
    "query": "这款产品的成分是什么？",
    "session_id": "optional-session-id"
}
```

**参数**：
| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 用户查询（1-2000 字符） |
| `session_id` | string | 否 | 会话 ID（不提供则自动生成） |

**响应**：
```json
{
    "response": "这款产品含有以下成分：...",
    "session_id": "abc123",
    "cached": false,
    "query_type": "product_info",
    "agents_used": ["product_agent"],
    "resolution_status": "resolved"
}
```

### 健康检查

**GET** `/api/health`

健康检查接口，无需认证。

**响应**：
```json
{
    "status": "healthy",
    "version": "3.7.0"
}
```

### 性能指标

**GET** `/api/metrics`（需要 Admin Token）

**响应**：
```json
{
    "total_requests": 1000,
    "successful_requests": 950,
    "failed_requests": 50,
    "average_response_time": 3.5,
    "cache_hit_rate": 0.35,
    "sla_compliance_rate": 0.92,
    "circuit_breaker_status": "closed"
}
```

### 业务 KPI

**GET** `/api/kpi`（需要 Admin Token）

**响应**：
```json
{
    "first_resolve_rate": 0.78,
    "ai_processing_rate": 0.95,
    "resolution_rate": 0.85,
    "average_response_time": 4.2,
    "escalation_rate": 0.05
}
```

### 缓存统计

**GET** `/api/cache/stats`（需要 Admin Token）

**响应**：
```json
{
    "l1": {
        "size": 250,
        "max_size": 500,
        "hits": 800,
        "misses": 200,
        "hit_rate": 0.80
    },
    "l2": {
        "size": 1500,
        "max_size": 2000,
        "hits": 300,
        "misses": 500,
        "hit_rate": 0.38
    }
}
```

### 会话列表

**GET** `/api/sessions`

**响应**：
```json
{
    "sessions": [
        {
            "session_id": "abc123",
            "created_at": "2026-06-03T10:00:00Z",
            "last_message_at": "2026-06-03T10:05:00Z",
            "message_count": 10
        }
    ],
    "total": 1
}
```

### 会话详情

**GET** `/api/sessions/{id}`

**响应**：
```json
{
    "session_id": "abc123",
    "created_at": "2026-06-03T10:00:00Z",
    "messages": [
        {
            "role": "user",
            "content": "这款产品怎么样？"
        },
        {
            "role": "assistant",
            "content": "这是一款优质的产品..."
        }
    ],
    "metadata": {
        "query_type": "general_inquiry",
        "agents_used": ["general_agent"],
        "resolution_status": "resolved"
    }
}
```

### 删除会话

**DELETE** `/api/sessions/{id}`

**响应**：
```json
{
    "success": true,
    "message": "Session deleted"
}
```

### 客户反馈

**POST** `/api/feedback`

**请求**：
```json
{
    "session_id": "abc123",
    "rating": 5,
    "comment": "服务很好！"
}
```

**参数**：
| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 会话 ID |
| `rating` | integer | 是 | 评分（1-5） |
| `comment` | string | 否 | 反馈内容（最大 1000 字符） |

**响应**：
```json
{
    "success": true,
    "message": "Feedback received"
}
```

### SLA 告警

**GET** `/api/alerts`（需要 Admin Token）

**响应**：
```json
{
    "alerts": [
        {
            "timestamp": "2026-06-03T10:00:00Z",
            "type": "sla_breach",
            "message": "SLA 违约率超过 30%",
            "current_rate": 0.35
        }
    ]
}
```

### 熔断器状态

**GET** `/api/circuit-breaker`（需要 Admin Token）

**响应**：
```json
{
    "status": "closed",
    "failure_count": 0,
    "last_failure": null,
    "recovery_time_remaining": null
}
```

## WebSocket

### 连接

**WebSocket** `/ws/chat`

**连接参数**：
```
ws://host:port/ws/chat?session_id=optional-session-id
```

**请求头**：
```
X-API-Key: your-api-key
```

### 消息格式

**客户端 → 服务器**：
```json
{
    "type": "query",
    "query": "这款产品的成分是什么？",
    "session_id": "optional-session-id"
}
```

**服务器 → 客户端（进度）**：
```json
{
    "type": "progress",
    "stage": "routing",
    "content": "正在分析您的请求..."
}
```

```json
{
    "type": "progress",
    "stage": "agent_processing",
    "content": "产品专家 Agent 正在检索信息..."
}
```

**服务器 → 客户端（最终响应）**：
```json
{
    "type": "response",
    "content": "这款产品含有以下成分：...",
    "session_id": "abc123",
    "query_type": "product_info",
    "agents_used": ["product_agent"],
    "cached": false,
    "resolution_status": "resolved"
}
```

**服务器 → 客户端（错误）**：
```json
{
    "type": "error",
    "code": "rate_limit_exceeded",
    "message": "请求过于频繁，请稍后再试"
}
```

### 断开连接

正常关闭 WebSocket 连接即可。

## 错误码

| HTTP 状态码 | 错误码 | 说明 | 处理建议 |
|-------------|--------|------|----------|
| 400 | `invalid_request` | 请求参数错误 | 检查 query 字段长度和格式 |
| 400 | `session_id_invalid` | session_id 格式错误 | 使用有效的 UUID 格式 |
| 401 | `unauthorized` | 认证失败 | 检查 API Key 是否正确 |
| 403 | `forbidden` | 权限不足 | 使用 Admin Token 访问管理端点 |
| 429 | `rate_limit_exceeded` | 请求过于频繁 | 降低请求频率 |
| 500 | `internal_error` | 服务内部错误 | 检查日志，联系管理员 |
| 503 | `service_unavailable` | LLM 服务不可用 | 检查 CircuitBreaker 状态，等待恢复 |

## 速率限制

| 端点 | 限制 | 说明 |
|------|------|------|
| REST API | 60 req/min/IP | 超出返回 429 |
| WebSocket | 10 msg/min/连接 | 超出断开连接 |
| WebSocket | 5 连接/IP | 超出拒绝连接 |

## 超时配置

| 操作 | 超时 | 说明 |
|------|------|------|
| LLM 路由 | 8 秒 | 路由超时降级为规则分类 |
| 工具调用 | 10 秒 | ERP 查询等工具超时 |
| WebSocket 空闲 | 300 秒 | 空闲连接自动断开 |