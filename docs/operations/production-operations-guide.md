# 生产环境运维手册

> **Runtime version**: v6.3
> **本次审计**: 2026-09-30（final convergence pass）；旧命令必须先对照当前 Makefile/compose 文件复核。
> **适用环境**: Production / Canary  

> **Provider/production evidence 链路（canonical）**：本仓库**包含**以下两个可执行入口
> （已核对当前 `scripts/`），真实 provider 调用必须走它们，不要用裸 curl 替代正式链路：
>
> ```
> provider real call
>   ↓ scripts/probe_provider_auth.py          # 认证探针（一次最小化请求）
>   ↓ auth passed?
>   ├── no  → STOP / BLOCKED_BY_AUTHENTICATION（修凭据，不烧 token）
>   └── yes
>         ↓ controlled production/staging evidence
>         ↓ scripts/run_production_evidence.py  # 受控 evidence harness（EVAL_REAL_PROVIDER 门控）
> ```
>
> 裸 curl 只作为人工诊断补充（如查询 `/v1/user/usage`），不构成 evidence artifact。
> Provider probe 直接读取 process environment；应用配置使用 `load_dotenv(override=True)`。
> 禁止输出或持久化 secret。

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

## 🔌 端口语义（先读这一节）

生产栈里**只有一部分服务对宿主机发布端口**。把容器端口当成宿主机地址去 `curl`，
在下面这些服务上永远连不上——这不是环境故障，是拓扑事实。

| 服务 | 宿主机端口 | 容器内地址 | 说明 |
|------|-----------|-----------|------|
| `nginx` | `80` / `443`（`${NGINX_HTTP_PORT}` / `${NGINX_HTTPS_PORT}`） | — | **唯一**的应用入口。TLS 终止、安全响应头、canary 流量分割都在这里；`http://` 一律 301 跳 `https://` |
| `grafana` | `3000` | — | 指标看板入口（`GRAFANA_PASSWORD`） |
| `alertmanager` | `9093` | — | 告警路由 |
| `loki` | `3100` | — | 日志查询（`make monitoring-up` / `make prod`） |
| `app` | **不发布**（仅 `expose: 8000`） | `http://localhost:8000` | 只能从 Compose network 内访问 |
| `worker` | 不发布 | — | 无 HTTP 端点 |
| `prometheus` | **不发布**（仅 `expose: 9090`） | `http://localhost:9090` | HTTP API 无鉴权，刻意不对外 |
| `postgres` / `redis` / `qdrant` | **不发布** | 见各自小节 | 同样只走容器网络 |

> 为什么不发布 `app:8000`：绕开 nginx 直连应用会同时丢掉 TLS 终止、安全响应头
> （HSTS / CSP / nosniff）和 canary 流量分割。**不要**为了图方便在生产加
> `ports: 8000:8000`——那等于把这三层一起关掉。

因此本文后续所有诊断命令分两类：

```bash
# 已发布到宿主机：直接 curl（务必分清端口属于哪个服务）
curl -I http://localhost/                                  # nginx，唯一应用入口；http 会 301 到 https
curl -s http://localhost:3000/api/health | jq .           # 这是 **Grafana** 的健康，不是 app 的

# app 未发布端口：即使它提供 /api/health，也只能在 Compose network 内执行
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app curl -s http://localhost:8000/api/health | jq .

# Prometheus：该镜像内只有 promtool / wget，没有 curl
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec prometheus promtool query instant http://localhost:9090 'cache_hit_rate'
```

端口与路径的机器可校验契约：`tests/unit/test_compose_deploy_topology.py`。

---

## 🔍 故障排查（v6.0 复审）

### 常见问题速查表

| 问题现象 | 可能原因 | 解决方案 | 优先级 |
|---------|---------|---------|--------|
| LLM API超时 | API配额耗尽/网络问题 | 检查配额，切换备用Provider | P0 |
| 缓存命中率低 | 查询多样性高/TTL过短 | 调整TTL，启用预热 | P1 |
| 数据库连接池耗尽 | 并发过高/慢查询 | 增加连接数，优化查询 | P0 |
| Redis连接失败 | Redis宕机/网络分区 | 检查Redis状态，重启服务 | P0 |
| Qdrant不可用 | 容器故障/磁盘满/配置错误 | 重启容器，重建 Qdrant 集合 | P0 |
| 会话数据丢失 | Session过期/Redis故障 | 检查TTL配置，验证Redis | P1 |
| 请求返回 409 THREAD_BUSY | 同一 thread 有正在执行的 Run（分布式锁未释放/长请求） | 检查 `agent:thread-lock:{thread_id}` TTL 与慢请求；客户端稍后重试 | P1 |
| nginx 不启动 / `cannot load certificate` | `deploy/nginx/ssl` 无 cert.pem+key.pem（缺失目录被 Docker 自动建成空目录，容器能起但 nginx 读不到证书） | 运维提供证书对后 `make tls-check`；见 TLS runbook §TLS-1 | P0 |
| 证书在但报 `key values mismatch` | cert.pem 与 key.pem 不是同一次签发 | `make tls-check` 会直接检出配对不一致 | P0 |
| 启动失败：SESSION_STORAGE_BACKEND | 生产配了 memory（fail-fast） | 改为 `SESSION_STORAGE_BACKEND=redis` 并确保 Redis 可用 | P0 |
| 启动失败：GUNICORN_WORKERS gate | 多 worker 但 checkpoint/session/lock 非分布式 | 配 postgres checkpoint + redis session + redis lock | P0 |
| Run 长期 QUEUED / 无 worker 消费 | worker 未启动、broker 不可达，或 `AGENT_RUN_DISPATCH=inline` | 检查 worker 进程与 `celery inspect active`；见 runtime runbook §3.1 | P0 |
| Run 卡在 RUNNING | worker 崩溃但消息未回到队列，或 worker 仍在跑 | 比对 `lease_expires_at`/`heartbeat_at`；不要手工改状态 | P1 |
| DEAD_LETTER 堆积 | retry 耗尽（transient 抖动或 permanent 缺陷） | 按 `error_type` 分类；`python scripts/replay_dead_run.py <run_id>`（**目前无告警，需巡检**） | P1 |
| 怀疑副作用工具执行两次 | 写工具未注册 `side_effect=True` | 查 `tool_side_effects` 与 `agent_tool_idempotency_hit_total`；见 runtime runbook §3.5 | P0 |
| Run 事件流无数据 | worker 未发布，或 Redis 不可用（事件发布降级为 no-op 不影响 Run） | `redis-cli XLEN 'agent:run:<run_id>:events'`；见 runtime runbook §3.6 | P2 |
| 响应时间过长 | LLM延迟/资源不足 | 检查SLA，扩容实例 | P0 |
| 告警频繁触发 | 阈值过低/真实故障 | 调整阈值，排查根因 | P1 |

---

### TLS-1: nginx TLS 物料（证书由运维提供）

**契约**：生产 TLS 物料**不由仓库提供，也不提交私钥**。`deploy/nginx/ssl/` 已被
`.gitignore` 排除，由运维/CA 填充 `cert.pem` 与 `key.pem`。

**为什么需要专门的检查**：证书是**文件**而非环境变量，无法用 compose 的
`${VAR:?}` 在 `docker compose config` 阶段拦下——那个阶段只校验 schema 与插值，
不看文件系统。而 bind mount 的源目录不存在时，Docker 会**自动创建一个空目录**，
于是容器照常启动，直到 nginx 读证书才崩。所以有两道门：

| 门 | 时机 | 覆盖范围 |
|----|------|---------|
| `make tls-check` | `docker compose up` **之前** | 存在 / 非空 / 可解析 / **证书私钥配对** / 有效期（临期 21 天告警）/ 自签名策略 |
| compose `tls-check` 一次性服务 | `docker compose up` 期间，nginx `depends_on` | 仅存在且非空（必须能在无 openssl 的最小镜像里跑完） |

```bash
# 完整校验（缺什么会直接说明）
make tls-check

# 只看结论与关键项
python3 scripts/check_tls_material.py --ssl-dir deploy/nginx/ssl
```

失败时的典型输出与处置：

| 报错 | 含义 | 处置 |
|------|------|------|
| `TLS 目录不存在` | bind mount 源缺失（Docker 会自动建空目录） | 由运维放入证书对 |
| `0 字节` | 文件存在但为空 | 与不存在等价，重新拷贝 |
| `证书与私钥不匹配` | 两份材料来自不同次签发 | 确认同一次签发；nginx 自身的 `key values mismatch` 在启动日志里极易漏看 |
| `已过期` | 证书不在有效期内 | 续期后重新提供 |
| `拒绝自签名证书` | 自签名证书不是生产可用的信任锚 | 换成受信任证书；本地开发见下 |

**关于自签名证书**：它能让服务"跑起来"，但客户端不信任，故障表现为
**端口通、健康检查绿、浏览器报错** —— 比启动失败难查得多。所以预检**默认拒绝**，
而不是 warn 一下放行。

**仅本地开发**：`make tls-local-cert` 生成本地自签证书，主题写明
`LOCAL DEVELOPMENT ONLY`、SAN 只有 localhost、有效期 30 天，且默认会被预检拦下。
`make dev-https` 走的是另一条路径（uvicorn 直接读 `.certs/`），用
`python3 scripts/generate_local_selfsigned_cert.py --target dev-https` 生成。

**已知边界**：即使 TLS 物料齐备，nginx 仍可能因自身配置问题起不来（与本节无关）。
先看 `docker compose logs nginx | grep emerg`——缺证书与配置错误的报错形态不同。

### Q0: 分布式 Agent Runtime 排障入口

Run 卡住、线程锁冲突、dead-letter 堆积、副作用重复、事件流缺失等问题的完整排障
路径（SQL/Redis 命令 + 指标速查 + 已知限制）见
[distributed-runtime-runbook.md](distributed-runtime-runbook.md)。
设计语义见 [agent-runtime.md](../design/agent-runtime.md)。

日常验收：

```bash
make runtime-e2e      # 真实 PG + Redis
make runtime-chaos    # worker kill -9 混沌验收（结构化证据 JSON）
```

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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/circuit-breaker | jq .
# 预期输出: {"state": "closed", ...}
# 如果 state=open，说明LLM服务不可用

# 4. 查看最近错误日志
docker logs customer-service-app --tail 100 | grep -i "error\|timeout"

# 5. 测试LLM连通性（使用项目自身的LLM客户端，或通用curl）
# 方式A: 通过项目健康端点
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
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
# 运行时 LLM 客户端始终读取 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
# （见 core/container.py）；LLM_PROVIDER 仅作为监控/评测的 provider 标签，
# 不改变连接目标。仓库不存在 DEEPSEEK_API_KEY 等按 provider 命名的变量。
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
export OPENAI_API_KEY=your_deepseek_key
export LLM_PROVIDER=deepseek
docker compose restart app

# 方案B: 增加超时时间（临时）
echo "HTTP_TIMEOUT=60" >> .env.prod
docker compose restart app

# 方案C: 预热高频缓存（长期）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/cache/stats | jq .
# 关注: hit_rate, l1_size, l2_size

# 2. 查看缓存命中详情
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec prometheus promtool query instant http://localhost:9090 'cache_hit_rate'

# 3. 分析查询多样性
# 当前通过 Prometheus / Grafana 查询 cache_hit_rate 与请求指标；
# 原 scripts/analyze_query_diversity.py 已移除，不再作为活动入口。

# 4. 检查TTL配置
grep CACHE_TTL .env.prod
```

**解决方案**:

```bash
# 方案A: 调整TTL（按业务意图自动分级）
# TTL 由 core/config.py 的 CACHE_TTL_POLICY 按 intent_type 决定：
# knowledge_qa: 7天, pricing_stock: 5分钟, policy_rule: 24小时,
# order_status: 5分钟, after_sales: 1小时, chitchat: 10分钟
# 注意：不存在 CACHE_TTL_PRODUCT / CACHE_TTL_BILLING 独立变量（历史遗留写法已移除）；
# 需要调整请修改 CACHE_TTL_POLICY 或部署侧覆盖 CACHE_TTL（default 兜底）
echo "CACHE_TTL=3600" >> .env.prod

# 方案B: 启用缓存预热
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  python3 scripts/warm_cache.py http://localhost:8000

# 方案C: 优化L2语义匹配阈值
echo "CACHE_SEMANTIC_THRESHOLD_SHORT=0.7" >> .env.prod  # 降低阈值
echo "CACHE_SEMANTIC_THRESHOLD_LONG=0.55" >> .env.prod

# 方案D: 调整缓存 TTL 策略
# 缓存按业务类型自动分级（v6.2+）：
# knowledge_qa: 7天, pricing_stock: 5分钟, order_status: 5分钟
echo "CACHE_TTL=3600" >> .env.prod  # 默认 1 小时
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
# 连接池大小当前硬编码在 db/database.py（engine pool_size=10, max_overflow=20，
# 仅 PostgreSQL 生效）；当前不存在 DB_POOL_SIZE / DB_POOL_OVERFLOW 环境变量
```

**解决方案**:

```bash
# 方案A: 增加连接池大小（短期）
# 需修改 db/database.py 中的 pool_size / max_overflow（当前为代码级配置，
# 无对应环境变量）；改后重启：
docker compose restart app

# 方案B: 优化慢查询（长期）
# 添加缺失索引
alembic upgrade head

# 使用JOIN替代N+1查询
# 修改代码使用 selectinload

# 方案C: 读写分离（高并发场景）
# 配置主从复制，读操作走从库
# 注意：当前仓库无 QUERY_CACHE_ENABLED 环境变量（历史写法已移除）
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
# Redis 参数由 compose command 控制（deploy/compose/docker-compose.yml:
# --maxmemory 256mb --maxmemory-policy allkeys-lru）；.env.prod 不支持
# REDIS_MAXMEMORY 环境变量（历史写法已移除），需修改 compose 或部署侧覆盖
docker compose up -d redis

# 方案D: 启用Redis持久化
# AOF 已在 compose command 中启用（--appendonly yes）；如需调整同样修改 compose
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
grep SESSION_IDLE_TTL .env.prod
# 预期: SESSION_IDLE_TTL=3600 (1小时, core/config.py 默认值)

# 2. 验证Session隔离
python3 -c "
import redis
r = redis.Redis.from_url('redis://localhost:6379')
keys = r.keys('session:*')
print(f'活跃会话数: {len(keys)}')
for key in keys[:5]:
    print(f'{key.decode()}: {r.ttl(key)}s')
"

# 3. 验证黑板 Session 隔离（ContextVar 自动隔离，无需环境变量配置）
python3 -c "
import os, sys; sys.path.insert(0, '.')
os.environ['OPENAI_API_KEY'] = 'test'; os.environ['LLM_PROVIDER'] = 'test'
from core.graph_builder import MessageBus
print(f'MessageBus 已加载，session_id 通过 ContextVar 自动隔离')
"
```

**解决方案**:

```bash
# 方案A: 延长Session TTL
echo "SESSION_IDLE_TTL=7200" >> .env.prod  # 2小时
docker compose restart app

# 方案B: 修复Session隔离bug
# 确认 ContextVar 正确传递 session_id
# 检查 BaseAgent.process_with_retry() 中的设置

# 方案C: 启用Session持久化
# 使用 SESSION_STORAGE_BACKEND（memory|redis，core/config.py）；当前无
# SESSION_PERSIST_TO_DB 变量（历史写法已移除）
echo "SESSION_STORAGE_BACKEND=redis" >> .env.prod

# 方案D: 清理僵尸Session
# 当前由 SessionManager 的 TTL 清理任务处理；原
# scripts/cleanup_expired_sessions.py 已移除，不再作为活动入口。
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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s 'http://localhost:8000/api/alerts?limit=50' | jq '.alerts[] | {severity, timestamp}'

# 2. 分析告警类型分布
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec prometheus promtool query instant http://localhost:9090 'rate(csai_errors_total[5m])'

# 3. 检查抑制窗口配置
# 抑制窗口当前硬编码在 alerts/notifier.py（默认 300 秒 / 5 分钟）；
# 当前不存在 ALERT_SUPPRESSION_WINDOW 环境变量（历史写法已移除）
```

**解决方案**:

```bash
# 方案A: 调整告警阈值
echo "SLA_ALERT_THRESHOLD=50" >> .env.prod  # 提高阈值（core/config.py 默认 30.0）
echo "SLA_ALERT_COOLDOWN=600" >> .env.prod  # 10分钟冷却（默认 300）

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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/metrics | jq '.metrics.sla'

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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -sI http://localhost:8000/ | grep Content-Security-Policy

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
# 注意：生产 Compose 的 qdrant 只有 expose（无主机端口映射），
# 必须从 Compose network 内执行（与 qdrant 服务 healthcheck 的 curl 用法一致）：
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec qdrant curl -fsS http://localhost:6333/collections | jq '.result.collections[].name'

# 3. 检查集合状态和向量配置
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec qdrant curl -fsS http://localhost:6333/collections/product_knowledge | jq '.result'

# 4. 检查健康端点
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health | jq '.components.qdrant'
```

**解决方案**:

```bash
# 方案A: 重启 Qdrant 容器
docker compose restart qdrant

# 方案B: 重建 Qdrant 集合（数据损坏时）
# 注意：会清空现有数据，需重新导入种子数据
docker compose restart app

# 方案C: 从备份恢复数据

# 方案D: 检查 Qdrant 磁盘空间
docker exec customer-service-qdrant df -h /qdrant/storage
```

**预防措施**:
- ✅ 监控 Qdrant 磁盘使用率（>80% 告警）
- ✅ 定期备份 Qdrant 快照（`docker cp qdrant:/qdrant/storage ./backups/`）
- ✅ 为 Qdrant 容器配置资源限制（CPU 2-4 核，内存 4-8GB）
- ✅ 生产环境建议设置 `gRPC` 端口（6334）以提高性能
- ✅ 配置 `VECTOR_DB_MODE=qdrant_only` 仅使用 Qdrant（生产推荐；ChromaDB 已于 v6.3 完全移除——历史迁移记录）

---

## 📚 日常运维

### 健康检查

```
# API 健康端点
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health

# 详细状态（需要认证）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s -H "X-API-Key: YOUR_API_KEY" 'http://localhost:8000/api/health?detail=true'
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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health | jq '.circuit_breaker'
```

#### 3. 高延迟问题

```
# 检查资源使用
docker stats

# 分析慢查询
grep "response_time" logs/app.log | awk '{print $NF}' | sort -n | tail

# 检查缓存命中率
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/metrics | grep cache_hit_rate

# 临时解决方案：增加 worker 数量
export GUNICORN_WORKERS=4
docker compose restart app
```

#### 4. 数据库连接池耗尽

```
# 查看当前连接数
docker compose exec postgres psql -U csai -d csai -c "SELECT count(*) FROM pg_stat_activity;"

# 调整连接池大小
# 注意：连接池大小当前硬编码在 db/database.py（pool_size=10, max_overflow=20，
# 仅 PostgreSQL 生效）；不存在 DATABASE_POOL_SIZE / DATABASE_MAX_OVERFLOW
# 环境变量，调整需修改代码后重启。
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

#### 6. Qdrant 检索缓慢

```
# 检查集合大小（v6.0+ 使用 Qdrant REST API；生产 Compose 内 qdrant 无主机端口映射，
# 从 Compose network 内执行）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec qdrant curl -fsS http://localhost:6333/collections | jq '.result.collections[] | {name, vectors_count}'

# 或通过项目健康端点
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health | jq '.components.qdrant'

# 检查具体集合详情
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec qdrant curl -fsS http://localhost:6333/collections/product_knowledge | jq '.result.points_count'

# 优化建议：
# - 定期优化 Qdrant 索引（curl -X POST http://localhost:6333/collections/{name}/index）
# - 限制返回结果数量 (RAG_N_RESULTS=3)
# - 启用查询改写 (RAG_QUERY_REWRITING=true)
# - 使用 gRPC 端口（6334）替代 HTTP 端口（6333）以获得更高吞吐量
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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health
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
# GUNICORN_WORKERS 是唯一 env 可调项（gunicorn.conf.py 通过 os.getenv 读取）：
export GUNICORN_WORKERS=$(( $(nproc) * 2 + 1 ))  # CPU核心数 * 2 + 1
# timeout=120 / keepalive=5 当前硬编码在 gunicorn.conf.py（非环境变量）；
# threads 未启用（uvicorn 异步 worker）；调整请直接编辑 gunicorn.conf.py。
```

#### 缓存优化

```
# 缓存 TTL 按业务类型自动分级（v6.2+）
# knowledge_qa: 7天, pricing_stock: 5分钟, policy_rule: 24小时
# order_status: 5分钟, after_sales: 1小时, chitchat: 10分钟
CACHE_TTL=3600  # 默认 1 小时

# 调整语义匹配阈值
CACHE_SEMANTIC_THRESHOLD_SHORT=0.75
CACHE_SEMANTIC_THRESHOLD_LONG=0.6

# 注意：CACHE_L1_MAX / CACHE_L2_MAX 已废弃（v6.2）
# L1 Redis 缓存容量由 Redis maxmemory 控制
# L2 Qdrant 向量缓存容量由 Qdrant 集合配置控制
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
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health

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
# 当前仓库无 scripts/restore.sh（历史写法已移除）；恢复由
# scripts/backup.sh + pg_restore 手工完成：
#   pg_restore -U csai -d csai backups/YYYYMMDD_HHMMSS/postgres_csai.dump
#   并按需恢复 Qdrant 快照（docker cp / qdrant snapshot）
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
# 当前仓库无 scripts/smoke_test.sh（历史写法已移除）；用健康检查与
# docs/checklists/quick-launch-checklist.md 的验证命令代替：
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec app \
  curl -s http://localhost:8000/api/health | jq .

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

- [项目 README](../../README.md)
- [生产准备度检查清单](../checklists/production-readiness-checklist.md)
- [API 文档](../reference/api-reference.md)（生产经 nginx HTTPS 入口的 `/docs`）
- [架构设计文档](../design/architecture-design.md)

---

*更多运维指南请参考: [docs/checklists/production-readiness-checklist.md](../checklists/production-readiness-checklist.md)*
