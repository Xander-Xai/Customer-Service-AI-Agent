# 部署指南

## 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 必需 |
| Redis | 7.0+ | 可选，用于持久化 |
| Docker | 20.10+ | 可选 |
| Docker Compose | 2.0+ | 可选 |

## 部署方式

### 方式一：Docker Compose（推荐）

```bash
# 启动所有服务（应用 + Redis）
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

**包含的服务**：
- `app`：多智能体客服系统
- `redis`：Redis 7.0（可选，如不需要持久化可移除）

### 方式二：直接运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Redis（可选）
redis-server --daemonize yes

# 启动服务
uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --workers 4
```

### 方式三：LangGraph CLI

```bash
# 安装 langgraph
pip install langgraph

# 启动
langgraph up
```

## 环境变量配置

### 必需配置

```bash
# .env
OPENAI_API_KEY=sk-xxx                    # API Key（必填）
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
OPENAI_MODEL=Qwen/Qwen3-8B
```

### 安全配置

```bash
# API 认证（默认开启）
API_KEY_ENABLED=true
API_KEY=your-secure-api-key-here

# 监控端点 Token（用于 /api/metrics 等管理端点）
MONITORING_ADMIN_TOKEN=your-admin-token

# 会话令牌签名密钥（防会话劫持）
SESSION_TOKEN_SECRET=your-session-secret

# TLS 配置（可选）
TLS_CERT_FILE=/path/to/cert.pem
TLS_KEY_FILE=/path/to/key.pem
```

### ERP 配置

```bash
# Mock 模式（默认）
ERP_MODE=mock

# Real 模式（需填写以下配置）
# ERP_MODE=real
# ERP_BASE_URL=https://xxx.kingdee.com
# ERP_APP_ID=your-app-id
# ERP_APP_SECRET=your-app-secret
# ERP_DB_ID=your-db-id
```

### Redis 配置

```bash
# 默认使用内存存储
# SESSION_STORAGE_BACKEND=memory

# 如需持久化，使用 Redis
SESSION_STORAGE_BACKEND=redis
REDIS_URL=redis://localhost:6379
```

## Docker 部署

### 构建镜像

```bash
# 构建
docker build -t customer-service-ai .

# 运行
docker run -p 8000:8000 \
  -e OPENAI_API_KEY=sk-xxx \
  -e API_KEY_ENABLED=true \
  customer-service-ai
```

### Docker Compose 配置

```yaml
version: '3.8'
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - API_KEY=${API_KEY}
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

volumes:
  redis_data:
```

## 生产环境配置

### 性能优化

```bash
# 增加 worker 数量
uvicorn api.app_factory:app --workers 8

# 使用 gunicorn
gunicorn api.app_factory:app -w 4 -k uvicorn.workers.UvicornWorker
```

### 安全加固

```bash
# 启用 TLS
uvicorn api.app_factory:app \
  --ssl-certfile=/path/to/cert.pem \
  --ssl-keyfile=/path/to/key.pem

# 限制来源
CORS_ORIGINS=https://your-domain.com,https://www.your-domain.com
```

### 监控配置

```bash
# 配置 Admin Token
MONITORING_ADMIN_TOKEN=your-secure-token

# 告警配置
SLA_ALERT_THRESHOLD=30.0
SLA_ALERT_COOLDOWN=300
```

## 健康检查

```bash
# 检查服务状态
curl http://localhost:8000/api/health

# 检查缓存统计
curl -H "X-Admin-Token: your-token" http://localhost:8000/api/cache/stats

# 检查熔断器状态
curl -H "X-Admin-Token: your-token" http://localhost:8000/api/circuit-breaker
```

## 日志管理

```bash
# 查看实时日志
docker-compose logs -f app

# 查看错误日志
docker-compose logs app | grep ERROR

# 日志级别配置
LOG_LEVEL=DEBUG  # DEBUG / INFO / WARNING / ERROR
```

## 故障排除

### 常见问题

#### 1. Redis 连接失败

```
错误：Redis 初始化失败
解决：检查 REDIS_URL 配置，或将 SESSION_STORAGE_BACKEND 设置为 memory
```

#### 2. LLM 调用超时

```
错误：LLM 请求超时
解决：检查 OPENAI_API_KEY 配置，或调整 LLM_ROUTER_TIMEOUT 配置
```

#### 3. 熔断器触发

```
状态：CircuitBreaker OPEN
解决：等待 60 秒自动恢复，或检查网络/服务状态
```

#### 4. 认证失败

```
错误：401 Unauthorized
解决：检查 API_KEY 是否正确配置
```

### 调试模式

```bash
# 启用调试日志
LOG_LEVEL=DEBUG

# Python 直接运行
python3 -m uvicorn api.app_factory:app --reload --log-level debug
```

## 备份与恢复

### 数据备份

```bash
# Redis 数据备份
redis-cli SAVE
# 或
docker exec redis redis-cli SAVE

# 备份数据卷
docker run --rm -v volume_name:/data alpine tar czf /tmp/backup.tar.gz -C /data .
```

### 数据恢复

```bash
# 恢复 Redis 数据
redis-cli -e "FLUSHALL"
# 将备份文件复制到 volume 中

# 恢复数据卷
docker run --rm -v volume_name:/data alpine tar xzf /tmp/backup.tar.gz -C /data
```