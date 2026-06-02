# 安全模型

## 安全架构

### 纵深防御

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 0: 网络层                                             │
│  - TLS 加密（可选）                                         │
│  - CORS 限制                                               │
│  - 安全头（HSTS/CSP/X-Frame-Options）                       │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: 认证层                                             │
│  - API Key 认证（默认开启）                                  │
│  - Admin Token（管理端点）                                   │
│  - Session Token 签名（防会话劫持）                          │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: 限流层                                             │
│  - 请求限流（60 req/min/IP）                                │
│  - WebSocket 连接限制（5 连接/IP）                          │
│  - 消息速率限制（10 msg/min/连接）                           │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: 验证层                                             │
│  - Pydantic 模型验证                                        │
│  - 字段长度约束                                             │
│  - SQL 注入防护                                             │
│  - Prompt 注入防护                                          │
└─────────────────────────────────────────────────────────────┘
```

## 认证与授权

### API Key 认证

```python
# 认证流程
1. 客户端在请求头中携带 X-API-Key
2. 服务器使用 hmac.compare_digest 进行时序安全比较
3. 验证通过后处理请求，失败返回 401
```

**配置**：
```bash
API_KEY_ENABLED=true
API_KEY=your-secure-api-key-here
```

### 管理端点认证

```python
# 受保护的端点（需要 Admin Token）
- /api/metrics
- /api/kpi
- /api/cache/stats
- /api/alerts
- /api/circuit-breaker

# 认证方式
Header: X-Admin-Token: your-admin-token
```

### 会话安全

```python
# Session Token 签名
1. 生成 UUID 作为 session_id
2. 使用 SESSION_TOKEN_SECRET 签名
3. 验证时检查签名完整性
```

## 输入验证

### Pydantic 模型

```python
class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    session_id: Optional[str] = Field(default=None, pattern=r'^[\w-]{1,128}$')
```

### 字段约束

| 字段 | 最大长度 | 格式 |
|------|----------|------|
| `query` | 2000 字符 | 非空 |
| `session_id` | 128 字符 | UUID 格式 |
| `feedback` | 1000 字符 | 非空 |

## 注入防护

### SQL 注入防护

```python
def sanitize_erp_input(value: str, max_length: int = 100) -> str:
    # 白名单字符
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_中文中文简体字符 ')
    filtered = ''.join(c for c in value if c in allowed)
    # 转义单引号
    escaped = filtered.replace("'", "''")
    # 限制长度
    return escaped[:max_length]
```

### LIKE 通配符转义

```python
def escape_like(value: str) -> str:
    # 转义 SQL LIKE 的特殊字符
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
```

### Prompt 注入防护

```python
def sanitize_prompt(user_input: str) -> str:
    # 移除可能的 prompt 注入标记
    dangerous_patterns = [
        '</s>', '<|end', '<|system', '<|user', '<|assistant',
        '[INST]', '[/INST]', '{{}', '}}'
    ]
    for pattern in dangerous_patterns:
        user_input = user_input.replace(pattern, '')
    return user_input
```

## 限流保护

### 请求限流

```python
# 中间件实现
- 60 requests/minute per IP
- 超出限制返回 429 Too Many Requests
- 滑动窗口算法
```

### WebSocket 限制

```python
# 连接限制
WS_MAX_CONNECTIONS_PER_IP = 5  # 每 IP 最大连接数
WS_MESSAGE_RATE_LIMIT = 10     # 每分钟每连接最大消息数
WS_IDLE_TIMEOUT = 300          # 空闲超时（秒）
```

## 安全头

```python
# 响应头配置
- Strict-Transport-Security: max-age=31536000; includeSubDomains
- Content-Security-Policy: default-src 'self'
- X-Frame-Options: DENY
- X-Content-Type-Options: nosniff
- Referrer-Policy: strict-origin-when-cross-origin
```

## 错误处理

### 错误脱敏

```python
# 工具执行错误处理
try:
    result = await tool.execute(args)
except Exception as e:
    logger.error(f"Tool {tool.name} failed: {e}")  # 详细日志写服务端
    return "请求处理失败，请稍后重试"  # 客户端返回通用消息
```

### 日志脱敏

```python
# 敏感信息脱敏
def sanitize_log_message(message: str) -> str:
    # 移除可能的敏感信息
    patterns = [r'password=[^\s]+', r'token=[^\s]+', r'key=[^\s]+']
    for pattern in patterns:
        message = re.sub(pattern, '***REDACTED***', message)
    return message
```

## 配置参考

```bash
# 安全配置
API_KEY_ENABLED=true
MAX_QUERY_LENGTH=2000
MAX_SESSIONS=10000
SESSION_IDLE_TTL=3600

# v3.7 新增
MONITORING_ADMIN_TOKEN=       # 管理端点 Token
WS_MAX_CONNECTIONS_PER_IP=5   # WebSocket 连接限制
WS_MESSAGE_RATE_LIMIT=10      # WebSocket 消息限流
SESSION_TOKEN_SECRET=         # 会话签名密钥
TLS_CERT_FILE=                # TLS 证书
TLS_KEY_FILE=                 # TLS 私钥
```

## 安全检查清单

- [x] API Key 认证（默认开启）
- [x] Admin Token 保护管理端点
- [x] 请求限流（60 req/min/IP）
- [x] WebSocket 连接限制
- [x] Pydantic 模型验证
- [x] SQL 注入防护
- [x] Prompt 注入防护
- [x] 安全头配置
- [x] Session Token 签名
- [x] 错误消息脱敏
- [x] 日志脱敏