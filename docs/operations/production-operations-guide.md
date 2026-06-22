# 生产环境运维手册

> **版本**: v6.0  
> **最后更新**: 2026-06-20  
> **适用环境**: Production / Canary  

---

## 📋 目录

1. [快速启动](#快速启动)
2. [日常运维](#日常运维)
3. [故障排查](#故障排查) ⭐ **新增**
4. [性能优化](#性能优化)
5. [安全维护](#安全维护)
6. [备份恢复](#备份恢复)
7. [扩容升级](#扩容升级)

---

## 🔍 故障排查（v6.0 复审）

### 常见问题速查表

| 问题现象 | 可能原因 | 解决方案 | 优先级 |
|---------|---------|---------|--------|
| LLM API超时 | API配额耗尽/网络问题 | 检查配额，切换备用Provider | P0 |
| 缓存命中率低 | 查询多样性高/TTL过短 | 调整TTL，启用预热 | P1 |
| 数据库连接池耗尽 | 并发过高/慢查询 | 增加连接数，优化查询 | P0 |
| Redis连接失败 | Redis宕机/网络分区 | 检查Redis状态，重启服务 | P0 |
| Qdrant不可用 | 容器故障/磁盘满/配置错误 | 重启容器，或降级到 chroma_legacy 模式 | P0 |
| 会话数据丢失 | Session过期/Redis故障 | 检查TTL配置，验证Redis | P1 |
| 响应时间过长 | LLM延迟/资源不足 | 检查SLA，扩容实例 | P0 |
| 告警频繁触发 | 阈值过低/真实故障 | 调整阈值，排查根因 | P1 |

---

### Q1: LLM API调用超时或失败

**症状**:
- 响应时间 > 30s
- 错误日志: `LLM API timeout`、`Rate limit exceeded` 或 `LLMServiceError`
- Prometheus指标: `csai_errors_total` 激增
- **启动日志** (v6.0+): `[HealthCheck] ⚠️ LLM 端点不可用`，说明启动健康检查已捕获供应商不可用并自动降级

**诊断步骤**:

```bash
# 0. 检查启动时 LLM 健康检查日志（v6.0+）
docker logs customer-service-app --tail 50 | grep "HealthCheck"
# 预期: [HealthCheck] ✅ LLM 端点连通正常
# 异常: [HealthCheck] ⚠️ LLM 端点不可用 — 系统以降级模式运行

# 1. 检查LLM配额使用情况（通用OpenAI兼容API）
# SiliconFlow配额查询（默认Provider）:
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.siliconflow.cn/v1/user/usage \
     | jq '.'

# 其他Provider请使用对应官网控制台查询

# 2. 列出可用模型确认连通性
curl -s -H "Authorization: Bearer $OPENAI_API_KEY" \
     ${OPENAI_BASE_URL:-https://api.siliconflow.cn/v1}/models \
     | jq '.data[].id' | head -10

# 3. 检查熔断器状态
curl http://localhost:8000/api/circuit-breaker | jq .
# 预期输出: {"state": "closed", ...}
# 如果 state=open，说明LLM服务不可用

# 4. 查看最近错误日志
docker logs customer-service-app --tail 100 | grep -i "error\|timeout"

# 5. 测试LLM连通性（使用项目自身的LLM客户端，或通用curl）
# 方式A: 通过项目健康端点
curl -s http://localhost:8000/api/health | jq '.llm'

# 方式B: 通用curl（适配所有OpenAI兼容API，包括SiliconFlow/DeepSeek/OpenAI）
curl -s -X POST ${OPENAI_BASE_URL:-https://api.siliconflow.cn/v1}/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{
    "model": "'"${OPENAI_MODEL:-Qwen/Qwen3-8B}"'",
    "messages": [{"role": "user", "content": "回复OK即可"}],
    "max_tokens": 10
  }' | jq '.choices[0].message.content'
```

**解决方案**:

```bash
# 方案A: 切换到备用Provider（紧急）
export LLM_PROVIDER=deepseek
export DEEPSEEK_API_KEY=your_deepseek_key
docker compose restart app

# 方案B: 增加超时时间（临时）
echo "HTTP_TIMEOUT=60" >> .env.prod
docker compose restart app

# 方案C: 预热高频缓存（长期）
python3 scripts/warm_cache.py http://localhost:8000

# 方案D: 联系API提供商提升配额
# OpenAI: https://platform.openai.com/account/limits
```

**预防措施**:
- ✅ 配置多Provider故障转移
- ✅ 设置合理的熔断器阈值
- ✅ 监控API配额使用率（<80%时告警）

---

### Q2: 缓存命中率低于预期

**症状**:
- Prometheus指标: `cache_hit_rate < 0.3`
- 响应时间波动大
- LLM调用次数异常高

**诊断步骤**:

```bash
# 1. 检查缓存统计
curl http://localhost:8000/api/cache/stats | jq .
# 关注: hit_rate, l1_size, l2_size

# 2. 查看缓存命中详情
curl http://localhost:9090/api/v1/query?query=cache_hit_rate | jq .

# 3. 分析查询多样性
python3 scripts/analyze_query_diversity.py
# 输出: 唯一查询数 / 总查询数

# 4. 检查TTL配置
grep CACHE_TTL .env.prod
```

**解决方案**:

```bash
# 方案A: 调整TTL（根据业务特点）
# 产品咨询类: 长TTL（24小时）
# 订单查询类: 短TTL（5分钟）
echo "CACHE_TTL_PRODUCT=86400" >> .env.prod
echo "CACHE_TTL_BILLING=300" >> .env.prod

# 方案B: 启用缓存预热
python3 scripts/warm_cache.py http://localhost:8000

# 方案C: 优化L2语义匹配阈值
echo "CACHE_SEMANTIC_THRESHOLD_SHORT=0.7" >> .env.prod  # 降低阈值
echo "CACHE_SEMANTIC_THRESHOLD_LONG=0.55" >> .env.prod

# 方案D: 增加缓存容量
echo "CACHE_L1_MAX=1000" >> .env.prod
echo "CACHE_L2_MAX=5000" >> .env.prod
docker compose restart app
```

**预防措施**:
- ✅ 定期分析热门查询，预加载到缓存
- ✅ 监控缓存淘汰率（>20%/小时需扩容）
- ✅ A/B测试不同TTL策略

---

### Q3: 数据库连接池耗尽

**症状**:
- 错误日志: `QueuePool limit of size 10 overflow 20 reached`
- 请求排队，响应时间飙升
- Prometheus指标: `db_connections_active` 接近上限

**诊断步骤**:

```bash
# 1. 检查当前连接数
psql -U postgres -d customer_service -c "
SELECT count(*) as active_connections 
FROM pg_stat_activity 
WHERE datname = 'customer_service';"

# 2. 查看慢查询
psql -U postgres -d customer_service -c "
SELECT query, mean_exec_time, calls 
FROM pg_stat_statements 
ORDER BY mean_exec_time DESC 
LIMIT 10;"

# 3. 检查连接池配置
grep DB_POOL_SIZE .env.prod
```

**解决方案**:

```bash
# 方案A: 增加连接池大小（短期）
echo "DB_POOL_SIZE=20" >> .env.prod
echo "DB_POOL_OVERFLOW=40" >> .env.prod
docker compose restart app

# 方案B: 优化慢查询（长期）
# 添加缺失索引
alembic upgrade head

# 使用JOIN替代N+1查询
# 修改代码使用 selectinload

# 方案C: 启用查询缓存
echo "QUERY_CACHE_ENABLED=true" >> .env.prod

# 方案D: 读写分离（高并发场景）
# 配置主从复制，读操作走从库
```

**预防措施**:
- ✅ 定期审查慢查询日志
- ✅ 监控连接池使用率（>80%时告警）
- ✅ 为高频查询添加复合索引

---

### Q4: Redis连接失败或超时

**症状**:
- 错误日志: `ConnectionError: Error connecting to Redis`
- Session数据丢失
- 缓存失效，LLM调用激增

**诊断步骤**:

```bash
# 1. 检查Redis服务状态
docker ps | grep redis
docker logs customer-service-redis

# 2. 测试Redis连通性
docker exec -it customer-service-redis redis-cli ping
# 预期输出: PONG

# 3. 检查Redis内存使用
docker exec -it customer-service-redis redis-cli info memory
# 关注: used_memory_human, maxmemory

# 4. 查看连接数
docker exec -it customer-service-redis redis-cli info clients
```

**解决方案**:

```bash
# 方案A: 重启Redis（紧急）
docker compose restart redis

# 方案B: 清理过期Key（内存不足时）
docker exec -it customer-service-redis redis-cli FLUSHDB

# 方案C: 增加Redis内存限制
echo "REDIS_MAXMEMORY=2gb" >> .env.prod
docker compose up -d redis

# 方案D: 启用Redis持久化
echo "REDIS_APPENDONLY=yes" >> .env.prod
docker compose up -d redis
```

**预防措施**:
- ✅ 配置Redis内存告警（>80%时通知）
- ✅ 启用AOF持久化防数据丢失
- ✅ 监控Key过期率（异常高说明TTL配置问题）

---

### Q5: 会话数据丢失或混乱

**症状**:
- 用户反馈"聊天记录不见了"
- 多用户会话内容交叉
- Session ID无效

**诊断步骤**:

```bash
# 1. 检查Session TTL配置
grep SESSION_TTL .env.prod
# 预期: SESSION_TTL=3600 (1小时)

# 2. 验证Session隔离
python3 -c "
import redis
r = redis.Redis.from_url('redis://localhost:6379')
keys = r.keys('session:*')
print(f'活跃会话数: {len(keys)}')
for key in keys[:5]:
    print(f'{key.decode()}: {r.ttl(key)}s')
"

# 3. 检查黑板Session隔离
grep BLACKBOARD_SESSION_ISOLATION .env.prod
```

**解决方案**:

```bash
# 方案A: 延长Session TTL
echo "SESSION_TTL=7200" >> .env.prod  # 2小时
docker compose restart app

# 方案B: 修复Session隔离bug
# 确认 ContextVar 正确传递 session_id
# 检查 BaseAgent.process_with_retry() 中的设置

# 方案C: 启用Session持久化
echo "SESSION_PERSIST_TO_DB=true" >> .env.prod

# 方案D: 清理僵尸Session
python3 scripts/cleanup_expired_sessions.py
```

**预防措施**:
- ✅ 监控Session创建/销毁速率
- ✅ 定期审计Session隔离有效性
- ✅ 用户登出时主动清理Session

---

### Q6: 告警频繁触发（告警风暴）

**症状**:
- Slack/邮件收到大量重复告警
- 关键告警被淹没
- 运维人员疲劳

**诊断步骤**:

```bash
# 1. 检查告警历史
curl http://localhost:8000/api/alerts?limit=50 | jq '.alerts[] | {severity, timestamp}'

# 2. 分析告警类型分布
curl http://localhost:9090/api/v1/query?query=rate(csai_errors_total[5m]) | jq .

# 3. 检查抑制窗口配置
grep ALERT_SUPPRESSION_WINDOW .env.prod
```

**解决方案**:

```bash
# 方案A: 调整告警阈值
echo "SLA_ALERT_THRESHOLD=50" >> .env.prod  # 提高阈值
echo "SLA_ALERT_COOLDOWN=600" >> .env.prod  # 10分钟冷却

# 方案B: 启用告警分级
# warning: 仅Webhook
# critical: Webhook + Email
# emergency: Webhook + Email + SMS

# 方案C: 配置告警聚合
# 相同类型告警5分钟内合并为一条

# 方案D: 设置维护窗口
# 发布期间暂时禁用非critical告警
```

**预防措施**:
- ✅ 定期审查告警规则有效性
- ✅ 建立告警分级响应机制
- ✅ 每月进行一次告警演练

---

### Q7: 响应时间不符合SLA

**症状**:
- Sequential模式 > 15s
- Parallel模式 > 20s
- ReAct模式 > 30s
- Prometheus指标: `csai_sla_violation_rate > 30%`

**诊断步骤**:

```bash
# 1. 检查SLA违约详情
curl http://localhost:8000/api/metrics | jq '.metrics.sla'

# 2. 分析各阶段耗时
# Graph节点 -> Agent执行 -> LLM调用 -> 后处理
docker logs customer-service-app | grep "elapsed="

# 3. 检查资源使用
docker stats customer-service-app
# 关注: CPU%, Memory%, Network I/O
```

**解决方案**:

```bash
# 方案A: 优化LLM提示词（减少Token）
# 精简System Prompt，移除冗余说明
# 使用Few-shot示例代替长篇描述

# 方案B: 并行化串行任务
# 将独立的Agent调用改为Parallel模式

# 方案C: 增加实例数量
docker compose -f deploy/compose/docker-compose.scale.yml up -d --scale app=3

# 方案D: 启用流式输出
# SSE模式可显著降低首字延迟
```

**预防措施**:
- ✅ 每周分析P95响应时间趋势
- ✅ 建立性能基线，变更前后对比
- ✅ 压力测试新功能的性能影响

---

### Q8: 前端页面加载缓慢或白屏

**症状**:
- 首屏加载 > 5s
- Console报错: `Failed to fetch` 或 CORS错误
- 部分功能不可用

**诊断步骤**:

```bash
# 1. 检查前端构建产物
ls -lh web/dist/
# 确认文件存在且大小合理

# 2. 检查CSP头配置
curl -I http://localhost:8000/ | grep Content-Security-Policy

# 3. 浏览器Console检查
# F12 -> Console -> 查看错误信息

# 4. 网络请求分析
# F12 -> Network -> 查看加载时间
```

**解决方案**:

```bash
# 方案A: 重新构建前端
cd web && npm run build
docker compose restart nginx

# 方案B: 启用Gzip压缩
# nginx.conf中添加:
# gzip on;
# gzip_types application/javascript text/css;

# 方案C: 配置CDN加速
# 将静态资源托管到CDN
# 修改 vite.config.js 中的 base URL

# 方案D: 修复CSP策略
# 确保nonce正确注入
# 避免unsafe-inline
```

**预防措施**:
- ✅ 监控前端资源加载时间
- ✅ 定期更新依赖，修复安全漏洞
- ✅ 使用Lighthouse进行性能审计

### Q9: Qdrant 向量数据库不可用或查询慢

**症状**:
- 健康检查显示 `qdrant.connected = false`
- RAG 检索返回空结果或超时
- 错误日志: `Connection refused to qdrant:6333` 或 `timeout`

**诊断步骤**:

```bash
# 1. 检查 Qdrant 服务状态
docker ps | grep qdrant
docker logs customer-service-qdrant --tail 50

# 2. 测试 Qdrant REST API 连通性（v6.0+ HTTP 端口 6333）
curl -s http://localhost:6333/collections | jq '.result.collections[].name'

# 3. 检查集合状态和向量配置
curl -s http://localhost:6333/collections/product_knowledge | jq '.result'

# 4. 检查健康端点
curl -s http://localhost:8000/api/health | jq '.components.qdrant'
```

**解决方案**:

```bash
# 方案A: 重启 Qdrant 容器
docker compose restart qdrant

# 方案B: 切换回 ChromaDB 兼容模式（紧急降级）
echo "VECTOR_DB_MODE=chroma_legacy" >> .env.prod
docker compose restart app

# 方案C: 重建 Qdrant 集合（数据损坏时）
# 注意：会清空现有数据
python3 scripts/migrate_chroma_to_qdrant.py --force-recreate

# 方案D: 检查 Qdrant 磁盘空间
docker exec customer-service-qdrant df -h /qdrant/storage
```

**预防措施**:
- ✅ 监控 Qdrant 磁盘使用率（>80% 告警）
- ✅ 定期备份 Qdrant 快照（`docker cp qdrant:/qdrant/storage ./backups/`）
- ✅ 为 Qdrant 容器配置资源限制（CPU 2-4 核，内存 4-8GB）
- ✅ 生产环境建议设置 `gRPC` 端口（6334）以提高性能
- ✅ 配置 `VECTOR_DB_MODE=qdrant_only` 完全启用 Qdrant 模式

---

## 📚 日常运维

### 健康检查

```
# API 健康端点
curl http://localhost:8000/api/health

# 详细状态（需要认证）
curl -H "X-API-Key: YOUR_API_KEY" http://localhost:8000/api/health?detail=true
```

### 日志管理

```
# 查看应用日志
docker compose logs -f --tail=100 app

# 查看 Nginx 访问日志
docker compose logs -f nginx

# 导出最近一小时日志
docker compose logs --since 1h app > /tmp/app_logs_$(date +%Y%m%d_%H%M%S).log

# 清理旧日志文件（保留最近5天）
find logs/ -name "*.gz" -mtime +5 -delete
```

### 数据库维护

```
# 连接 PostgreSQL
docker compose exec postgres psql -U csai -d csai

# 查看表大小
\dt+

# 查看慢查询
SELECT query, mean_time, calls 
FROM pg_stat_statements 
ORDER BY mean_time DESC 
LIMIT 10;

# 重建索引（定期执行）
REINDEX DATABASE csai;
```

### 缓存管理

```
# 连接 Redis
docker compose exec redis redis-cli

# 查看内存使用
INFO memory

# 查看键数量
DBSIZE

# 清除过期键
KEYS "csai:session:*" | xargs redis-cli DEL

# 刷新整个缓存（谨慎使用）
FLUSHDB
```

---

## 📊 监控与告警

### Prometheus 指标

```
# HTTP 请求率
rate(http_requests_total[5m])

# 响应时间百分位
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))

# LLM API 调用成功率
rate(llm_calls_total{status="success"}[5m]) / rate(llm_calls_total[5m])

# 缓存命中率
rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))

# 活跃会话数
count(csai_session_active)
```

### Grafana 仪表板

```
# 系统概览
http://localhost:3000/d/system-overview

# 应用性能
http://localhost:3000/d/application-performance

# LLM 服务
http://localhost:3000/d/llm-service

# 会话分析
http://localhost:3000/d/session-analysis

# RAG 检索
http://localhost:3000/d/rag-retrieval

```

### 告警规则配置

```
groups:
  - name: application_alerts
    rules:
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "高错误率 detected"
          
      - alert: HighLatency
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 10
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "P95 延迟超过 10s"
          
      - alert: LowCacheHitRate
        expr: rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m])) < 0.3
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "缓存命中率低于 30%"
```

---

## 🐛 故障排查

### 常见问题及解决方案

#### 1. 服务无法启动

```
# 检查容器日志
docker compose logs app

# 常见原因：
# - 端口被占用: lsof -i :8000
# - 环境变量缺失: docker compose config
# - 依赖服务未就绪: docker compose ps
```

#### 2. LLM API 调用失败

```
# 检查 API Key 配置
echo $OPENAI_API_KEY | wc -c

# 测试 API 连通性
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.siliconflow.cn/v1/models

# 查看熔断器状态
curl http://localhost:8000/api/health | jq '.circuit_breaker'
```

#### 3. 高延迟问题

```
# 检查资源使用
docker stats

# 分析慢查询
grep "response_time" logs/app.log | awk '{print $NF}' | sort -n | tail

# 检查缓存命中率
curl http://localhost:8000/api/metrics | grep cache_hit_rate

# 临时解决方案：增加 worker 数量
export GUNICORN_WORKERS=4
docker compose restart app
```

#### 4. 数据库连接池耗尽

```
# 查看当前连接数
docker compose exec postgres psql -U csai -d csai -c "SELECT count(*) FROM pg_stat_activity;"

# 调整连接池大小
# 在 .env.prod 中设置:
# DATABASE_POOL_SIZE=20
# DATABASE_MAX_OVERFLOW=10
```

#### 5. Redis 内存溢出

```
# 查看内存使用情况
docker compose exec redis redis-cli INFO memory

# 清理过期会话
docker compose exec redis redis-cli KEYS "csai:session:*" | xargs docker compose exec redis redis-cli TTL | awk '$2 < 0 {print $1}' | xargs docker compose exec redis redis-cli DEL

# 调整最大内存
# 在 docker-compose.prod.yml 中设置:
# command: ["redis-server", "--maxmemory", "512mb", "--maxmemory-policy", "allkeys-lru"]
```

#### 6. Qdrant 检索缓慢（v6.0 从 ChromaDB 迁移）

```
# 检查集合大小
docker compose exec app python3 -c "
from rag.knowledge_base import CosmeticsKnowledgeBase
kb = CosmeticsKnowledgeBase(persist_directory='data/rag')
for name in kb._collections:
    print(f'{name}: {kb._collections[name].count()} documents')
"

# 优化建议：
# - 定期重建索引
# - 限制返回结果数量 (RAG_N_RESULTS=3)
# - 启用查询缓存
```

### 紧急恢复流程

#### 服务完全不可用

```
# 1. 停止所有服务
make prod-down

# 2. 清理容器和网络
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml down -v

# 3. 重新构建和启动
make prod-build
make prod

# 4. 验证恢复
curl http://localhost:8000/api/health
```

#### 数据损坏恢复

```
# 1. 停止服务
make prod-down

# 2. 从备份恢复
ls -lt backups/  # 查看最新备份
# Qdrant 使用 Docker 卷持久化，可通过卷快照或 qdrant snapshot 命令备份

# 3. 恢复数据库
pg_restore -U csai -d csai backups/YYYYMMDD_HHMMSS/postgres_csai.dump

# 4. 重启服务
make prod
```

---

## ⚡ 性能优化

### 调优参数参考

#### Gunicorn 配置

```
# 根据服务器配置调整
GUNICORN_WORKERS=$(( $(nproc) * 2 + 1 ))  # CPU核心数 * 2 + 1
GUNICORN_THREADS=2
GUNICORN_TIMEOUT=120
GUNICORN_KEEPALIVE=5
```

#### 缓存优化

```
# 增大缓存容量
CACHE_L1_MAX=1000
CACHE_L2_MAX=5000
CACHE_TTL=7200  # 2小时

# 调整语义匹配阈值
CACHE_SEMANTIC_THRESHOLD_SHORT=0.75
CACHE_SEMANTIC_THRESHOLD_LONG=0.6
```

#### 数据库优化

```
-- 添加常用查询索引
CREATE INDEX idx_chat_histories_user_created ON chat_histories(user_id, created_at DESC);
CREATE INDEX idx_audit_logs_action_timestamp ON audit_logs(action, timestamp DESC);

-- 定期清理旧数据（保留90天）
DELETE FROM chat_histories WHERE created_at < NOW() - INTERVAL '90 days';
VACUUM ANALYZE chat_histories;
```

#### RAG 检索优化

```
# 调整检索参数
RAG_N_RESULTS=5  # 增加返回结果数
RAG_QUERY_REWRITING=true  # 启用查询改写

# 使用更快的 embedding 模型
# 在 knowledge_base.py 中选择轻量级模型
```

---

## 🔒 安全维护

### 定期安全检查清单

#### 每周检查

- [ ] 审查访问日志中的异常模式
- [ ] 检查 failed login 尝试次数
- [ ] 验证 SSL 证书有效期
- [ ] 更新依赖包安全补丁

#### 每月检查

- [ ] 轮换 API 密钥和 JWT 密钥
- [ ] 审计用户权限分配
- [ ] 检查防火墙规则
- [ ] 审查 CORS 配置

#### 每季度检查

- [ ] 全面安全扫描（使用 Trivy/Snyk）
- [ ] 渗透测试
- [ ] 备份恢复演练
- [ ] 灾难恢复计划评审

### 密钥轮换流程

```
# 1. 生成新密钥
openssl rand -hex 32  # JWT_SECRET
openssl rand -hex 32  # SESSION_TOKEN_SECRET
openssl rand -hex 32  # API_KEY

# 2. 更新 .env.prod
vim .env.prod

# 3. 滚动重启服务（零停机）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d --no-deps app

# 4. 验证服务正常
curl http://localhost:8000/api/health

# 5. 通知所有客户端更新 API Key
```

### 安全事件响应

```
# 检测到异常访问
# 1. 立即封禁 IP
iptables -A INPUT -s ATTACKER_IP -j DROP

# 2. 撤销可疑会话
docker compose exec redis redis-cli KEYS "csai:session:*" | xargs docker compose exec redis redis-cli DEL

# 3. 强制所有用户重新登录
# 清空 JWT 黑名单
docker compose exec redis redis-cli FLUSHDB

# 4. 收集证据
cp logs/app.log /tmp/security_incident_$(date +%Y%m%d_%H%M%S).log
```

---

## 💾 备份与恢复

### 自动备份配置

```
# 添加到 crontab（每天凌晨2点执行）
0 2 * * * /path/to/scripts/backup.sh /data/backups >> /var/log/backup.log 2>&1

# 手动执行备份
./scripts/backup.sh ./backups
```

### 备份验证

```
# 1. 检查备份文件大小
ls -lh backups/latest/

# 2. 验证数据库备份完整性
pg_restore --list backups/YYYYMMDD_HHMMSS/postgres_csai.dump

# 3. 测试恢复流程（在测试环境）
docker compose -f deploy/compose/docker-compose.test.yml up -d
./scripts/restore.sh backups/YYYYMMDD_HHMMSS/
```

### 异地备份策略

```
# 同步到远程存储（如 AWS S3）
aws s3 sync ./backups/ s3://your-backup-bucket/customer-service-ai/ --delete

# 或使用 rsync
rsync -avz ./backups/ user@remote-server:/backup/location/
```

---

## 📈 扩容与升级

### 水平扩展

```
# 扩展到 3 个实例
make scale N=3

# 查看负载分布
docker compose -f deploy/compose/docker-compose.scale.yml ps

# 通过 Nginx 负载均衡验证
watch 'curl -s http://localhost/api/health | jq ".instance_id"'
```

### 垂直扩展

```
# 修改 docker-compose.prod.yml 中的资源限制
deploy:
  resources:
    limits:
      cpus: '4'      # 增加 CPU
      memory: 2G     # 增加内存

# 重启服务
make prod-down && make prod
```

### 金丝雀发布

```
# 部署新版本（10% 流量）
make canary

# 监控新版本指标
watch 'curl -s http://localhost:8001/api/health | jq .'

# 如果正常，全量发布
docker compose -f deploy/compose/docker-compose.canary.yml down
make prod

# 如果异常，回滚
make canary-down
```

### 版本升级流程

```
# 1. 备份当前状态
./scripts/backup.sh ./pre_upgrade_backup

# 2. 拉取新代码
git pull origin main

# 3. 检查迁移脚本
alembic history

# 4. 执行数据库迁移
make db-upgrade

# 5. 重新构建镜像
make prod-build

# 6. 滚动更新
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d --no-deps app

# 7. 验证功能
./scripts/smoke_test.sh

# 8. 如有问题，回滚
git checkout PREVIOUS_TAG
make prod
```

---

## 📞 支持联系

### 内部支持

- **运维团队**: ops-team@company.com
- **开发团队**: dev-team@company.com
- **紧急联系**: +86-XXX-XXXX-XXXX

### 外部支持

- **LLM Provider**: SiliconFlow Support
- **PostgreSQL**: Community Forum
- **Redis**: Enterprise Support (if applicable)

### 文档资源

- [项目 README](README.md)
- [生产准备度检查清单](../checklists/production-readiness-checklist.md)
- [API 文档](http://localhost:8000/docs)
- [架构设计文档](docs/design/architecture-design.md)

---

*更多运维指南请参考: [docs/checklists/production-readiness-checklist.md](../checklists/production-readiness-checklist.md)*
