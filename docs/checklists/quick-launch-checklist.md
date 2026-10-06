# 🚀 客服 AI Agent 上线前快速检查清单

> **预计耗时**: 30-60分钟  
> **适用场景**: 生产环境首次部署或重大版本升级

---

## ✅ 必做项（Blocking）

### 1. 环境配置 [10分钟]

```bash
# 生成生产配置
python3 scripts/generate_prod_env.py
cp .env.prod.generated .env.prod

# 编辑配置文件
vim .env.prod
```

**必须修改的配置**:
- [ ] `OPENAI_API_KEY` — 填入真实的LLM API密钥
- [ ] `API_KEY` — 生成强随机密钥: `openssl rand -hex 32`
- [ ] `JWT_SECRET` — 生成强随机密钥（≥32字符）
- [ ] `SESSION_TOKEN_SECRET` — 生成强随机密钥
- [ ] `ADMIN_PASSWORD` — 引导 `admin` 账号口令，建议 `openssl rand -base64 24`。
      Compose contract：`app`（及 `canary`）用 `${ADMIN_PASSWORD:?...}` fail fast，
      未配置时 `docker compose config` 直接报错并指名变量；账号已存在后不再需要。
      `worker` 不接收该变量（它不导入 `api.app_factory`，从不执行
      `init_default_admin`）——与 `app` 共用镜像不构成注入理由。生成的账号带
      `force_password_change`，首次登录须改密
- [ ] `REDIS_PASSWORD` — 设置Redis密码
- [ ] `POSTGRES_PASSWORD` — 设置数据库密码
- [ ] `GRAFANA_PASSWORD` — 设置Grafana管理员密码
- [ ] `CORS_ORIGINS` — 设置为实际域名（不要使用*）
- [ ] `DEV_MODE=false` — 确保不是true
- [ ] `QDRANT_HOST` / `QDRANT_PORT` — 确认 Qdrant 连接配置
- [ ] `VECTOR_DB_MODE=qdrant_only` — 生产环境修改为 `qdrant_only`
- [ ] `EMBEDDING_API_KEY` — RAG 向量/语义缓存必需；未单独配置时会回退复用 LLM 的
      `OPENAI_API_KEY`（`core/config.py`），生产建议独立配置
- [ ] `RERANKER_API_KEY` — 独立重排凭据（**不会**回退到 OPENAI_API_KEY）；未配置时
      reranker 不可用、检索按原始顺序返回，preflight 会显式报告

### 1.1 TLS 物料（生产 Blocking Gate）

> **证书是文件，不是环境变量**，所以无法用 compose 的 `${VAR:?}` 在
> `docker compose config` 阶段拦下——而 bind mount 的源目录不存在时 Docker 会
> **自动创建一个空目录**，容器照样启动，直到 nginx 读证书才崩。这一层由两道门补上：
> `make tls-check`（宿主机，完整校验）与 compose 里的 `tls-check` 一次性服务
> （`docker compose up` 阶段的存在性门禁，nginx `depends_on` 它）。

- [ ] `deploy/nginx/ssl/cert.pem` 与 `deploy/nginx/ssl/key.pem` 已由运维/CA 提供
      （目录已被 `.gitignore` 排除，**仓库不提供也不提交私钥**）
- [ ] `make tls-check` 通过。它会校验：文件存在且非空、两者均可解析、
      **证书与私钥配对一致**（最常见的真实故障）、证书未过期（临期 21 天内告警）、
      且**不是自签名证书**
- [ ] 仅本地开发时可用 `make tls-local-cert` 生成本地自签证书。
      ⚠️ 该证书主题写明 `LOCAL DEVELOPMENT ONLY`、SAN 只有 localhost、有效期 30 天，
      且默认会被 `make tls-check` 以「拒绝自签名证书」拦下 —— **禁止用于生产**

本地开发的 HTTPS 入口另有一条路径：`make dev-https`（uvicorn 直接读 `.certs/`，
与 `deploy/nginx/ssl/` 无关），可用
`python3 scripts/generate_local_selfsigned_cert.py --target dev-https` 生成。

### 1.2 分布式 Agent Runtime（生产 Blocking Gate）

> **这一节是硬门禁，不是建议。** `core/config.py::validate_distributed_runtime_settings`
> 与 `validate_checkpoint_settings` 在 `DEV_MODE=false` 时会 **fail-fast 拒绝启动**，
> 配错就是"起不来"，不是"降级运行"。逐项确认后再部署。

**基础设施（必须真实存在，不能用内存替身）**

- [ ] **PostgreSQL** 可达，且 `DATABASE_URL` 是 `postgresql://` 协议
- [ ] **Redis** 可达（`REDIS_URL`），并已设置 `REDIS_PASSWORD`
- [ ] 数据库迁移已执行：`make db-upgrade`（含 `agent_runs` / `agent_dead_letters` /
      `tool_side_effects` 三张表）

**关键配置（`DEV_MODE=false` 时强制）**

- [ ] `LANGGRAPH_CHECKPOINT_BACKEND=postgres` — 生产**不允许** `memory`；
      留空会自动解析为 postgres。初始化失败是 fail-closed，**不会**静默回退 MemorySaver
- [ ] `SESSION_STORAGE_BACKEND=redis` — 进程内 session 在多 worker 下会分片、
      重启丢失，生产启动会直接拒绝
- [ ] `AGENT_EXECUTION_MODE=queued` — 等价历史变量 `AGENT_RUN_DISPATCH=celery`。
      `inline` 会让异步 Run 在 API 进程内执行，失去 worker 解耦，生产禁止
- [ ] `AGENT_RUN_THREAD_LOCK_ENABLED=true` 且 `AGENT_RUN_THREAD_LOCK_BACKEND=redis`
- [ ] `CELERY_BROKER_URL` — 留空安全复用 `REDIS_URL`，显式设置时必须指向同一 Redis

**多副本一致性（`GUNICORN_WORKERS>1` 时额外强制）**

- [ ] `LANGGRAPH_CHECKPOINT_BACKEND=postgres`
- [ ] `SESSION_STORAGE_BACKEND=redis`
- [ ] `AGENT_RUN_THREAD_LOCK_ENABLED=true` / `AGENT_RUN_THREAD_LOCK_BACKEND=redis`
- [ ] `AGENT_RUN_THREAD_LOCK_TTL_SECONDS` **>** `AGENT_RUN_TASK_TIME_LIMIT` + 30
      （默认 300 > 180 + 30 已满足；改过任一项就必须重算，否则锁可能在任务仍在执行时过期）

**Worker 服务**

- [ ] 独立 `worker` service 已随 compose 启动（`docker compose ps worker`）
- [ ] `celery -A runtime.celery_app:celery_app inspect ping` 能收到 pong
- [ ] worker 的 `LANGGRAPH_CHECKPOINT_BACKEND` / `AGENT_RUN_DISPATCH` 与 API 进程一致

> **不配置的后果**：app 会 fail-fast 拒绝启动，或在多副本下静默地丢状态 /
> 并发写同一会话。这不是"性能问题"，是**正确性问题**。

### 1.2b HITL 人工审批治理（可选开启；默认关闭）

> **默认 `HITL_ENABLED=false`。** 不配置时高风险工具**直接执行**（不拦）。
> 生产建议打开——关闭等于主动放弃该治理边界。开启前先确认工具已声明
> `risk_level` / `side_effect`，否则 HIGH 工具会以「未声明 side_effect」被显式
> 拒绝执行（fail loud，不会静默裸执行）。

- [ ] 已决定 `HITL_ENABLED` 的取值；**若为 `false`**，确认这是有意识的决定并记录在案
- [ ] `HITL_HIGH_RISK_TOOLS` — 强制审批的工具名（逗号分隔，大小写不敏感）
- [ ] `HITL_MEDIUM_RISK_TOOLS` — 只记录不拦截的工具名
- [ ] `HITL_HIGH_AMOUNT_THRESHOLD` — 金额阈值（`0` = 关闭金额维度）；
      用金额字段兜底白名单之外的大额写操作
- [ ] `HITL_APPROVAL_TTL_SECONDS` — 超时按拒绝收敛（`EXPIRED`），**绝不默认放行**；
      按「人工响应时长」设，别照抄默认值
- [ ] `human_approvals` 表已迁移（`alembic upgrade head`，单 head `006_add_human_approvals`）
- [ ] 审批值班路径已确认（无主动通知，只能靠接口拉取）：
      `GET /api/approvals?status=PENDING` → `POST /api/approvals/{id}/decision`
- [ ] 知情边界：`/api/chat` 实时快路径**不在** HITL 边界内（无 run 上下文）；
      真实 ERP 写操作仍 `NOT_VERIFIED`

### 1.3 RAG / 检索依赖就绪（可选但生产知识库必做）

```bash
# 若使用正式 RAG 评测链，先确认语料/索引/凭据：
make rag-eval-import          # 幂等导入 + BM25 rebuild + gold 覆盖审计 + manifest
make rag-eval-649-preflight   # provider auth / Qdrant 计数 / BM25 就绪 gate
# preflight 显示 BLOCKED 时先解决 blocker，再跑正式评测；smoke 不是正式证据
```

- [ ] 评测/知识库 embedding 凭据真实可用（401 会阻塞导入与向量实验）
- [ ] Qdrant 集合已部署且非空（生产数据导入依赖 embedding 凭据）

### 2. 预部署检查 [5分钟]

```bash
# 运行自动化检查脚本
chmod +x scripts/pre_deploy_check.sh
./scripts/pre_deploy_check.sh
```

**期望结果**: 
- ✅ 所有检查通过
- ⚠️ 如有警告，确认可以接受
- ❌ 如有失败，必须修复后继续

### 3. 构建与测试 [15分钟]

```bash
# 3.0 文档/配置一致性审计（可选但推荐）
python3 scripts/audit_doc_consistency.py

# 3.1 运行单元测试
make test

# 3.2 构建生产镜像
make prod-build

# 3.3 本地验证（可选但推荐）
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d
curl http://localhost:8000/api/health
docker compose down
```

**期望结果**:
- ✅ pytest 在当前 checkout 全量通过（collected 数以 `pytest --collect-only -q` 输出为准，不要沿用任何历史数字；覆盖率门槛见 `make test-cov`）
- ✅ Docker镜像构建成功
- ✅ 健康检查返回200

### 4. 存储准备 [5分钟]

```bash
# 4.1 执行数据库迁移
make db-upgrade

# 4.2 验证迁移状态
alembic current

# 4.3 验证 Qdrant 可访问
# 注意：生产 Compose 的 Qdrant 没有 ports: 主机映射（只有 expose: 6333/6334），
# 宿主机 curl localhost:6333 不可靠。必须从 Compose network 内验证：
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml \
  exec qdrant curl -fsS http://localhost:6333/collections | jq '.result'
# 该命令与 Compose 中 qdrant 服务的 healthcheck（curl http://localhost:6333/healthz）一致。
```

**期望结果**:
- ✅ 显示当前迁移版本为HEAD
- ✅ Qdrant 返回集合列表（默认创建 product_knowledge / faq / tech_support / complaint_knowledge）

### 5. 数据目录准备 [2分钟]

```bash
# 创建必要目录
mkdir -p data logs backups chat_sessions
chmod 755 data logs backups chat_sessions
```

---

## ✅ 推荐项（Recommended）

### 6. 监控配置 [10分钟]

```bash
# 6.1 配置告警通知渠道
# 在 .env.prod 中设置:
# ALERT_WEBHOOKS='[{"name":"钉钉","url":"https://oapi.dingtalk.com/robot/send?access_token=xxx","type":"dingtalk"}]'

# 6.2 启动监控栈（可选）
make monitoring-up
```

**验证**:
- [ ] Grafana可访问: http://localhost:3000
- [ ] Prometheus可访问: http://localhost:9090
- [ ] Loki可访问: http://localhost:3100

### 7. 备份配置 [5分钟]

```bash
# 7.1 执行首次备份
./scripts/backup.sh ./backups

# 7.2 配置定时备份（crontab）
# 0 2 * * * /path/to/scripts/backup.sh /data/backups >> /var/log/backup.log 2>&1
```

**验证**:
- [ ] 备份文件已生成
- [ ] 备份文件大小合理

### 8. 安全加固 [5分钟]

```bash
# 8.1 检查防火墙规则
sudo ufw status

# 8.2 仅开放必要端口
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow 22/tcp    # SSH
sudo ufw enable
```

**注意**: 根据实际需求调整端口

---

## 🚀 执行部署

### 方式一: Make命令（推荐）

```bash
# 一键部署
make prod

# 验证部署
make prod-ps
curl http://localhost:8000/api/health
```

### 方式二: Docker Compose手动部署

```bash
# 启动服务
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/docker-compose.prod.yml \
               up -d --build

# 查看日志
docker compose logs -f app

# 等待服务就绪（约30-60秒）
sleep 60

# 健康检查
curl http://localhost:8000/api/health
```

---

## ✅ 部署后验证

### 9. 功能验证 [10分钟]

```bash
# 9.1 健康检查
curl http://localhost:8000/api/health | jq

# 9.2 测试聊天接口
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "你好", "session_id": "test-session"}' | jq

# 9.3 检查日志无错误
docker compose logs app | grep -i error | tail -5

# 9.4 验证监控指标
curl http://localhost:8000/api/metrics | head -20
```

**期望结果**:
- ✅ 健康检查返回正常状态
- ✅ 聊天接口返回有效响应
- ✅ 日志无ERROR级别错误
- ✅ 指标端点返回Prometheus格式数据

### 10. 分布式 Runtime 冒烟（Blocking）

> 部署完必须实际跑一次异步 Run —— 只测 `/api/chat` 等于没验证分布式部分。
> 完整路径见 `docs/operations/distributed-runtime-runbook.md`。

```bash
# 10.1 Worker 健康
docker compose ps worker                       # worker 容器应为 running
docker compose exec worker \
  celery -A runtime.celery_app:celery_app inspect ping     # 期望收到 pong

# 10.2 AgentRun smoke：创建 → 查询终态
RUN=$(curl -s -X POST http://localhost:8000/api/runs \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query":"你好","session_id":"smoke-1"}' | jq -r '.run_id')
echo "run_id=$RUN"
for i in $(seq 1 30); do
  ST=$(curl -s "http://localhost:8000/api/runs/$RUN" -H "X-API-Key: YOUR_API_KEY" | jq -r '.status')
  echo "status=$ST"; case "$ST" in SUCCEEDED|FAILED|DEAD_LETTER|CANCELLED) break;; esac; sleep 2
done
# 期望终态 SUCCEEDED；若停在 QUEUED，说明 worker 没消费或没起来

# 10.3 状态真相源确实是数据库（不是队列）
docker compose exec postgres psql -U postgres -d cosmetics_ai \
  -c "SELECT run_id,status,attempt,worker_id,task_id FROM agent_runs ORDER BY queued_at DESC LIMIT 5;"

# 10.4 同 thread 串行：并发发两个同 session_id 的 Run，不应同时 RUNNING
#     （拿不到锁的会被延迟重调度，而不是并发执行）

# 10.5 DLQ 可查询（当前应为空）
curl -s "http://localhost:8000/api/runs/dead" -H "X-API-Key: YOUR_API_KEY" | jq

# 10.6 重放工具可用（不真跑，只确认 CLI 存在且能拒绝非 DLQ run）
python3 scripts/replay_dead_run.py --help
```

- [ ] worker 容器 running，`inspect ping` 有 pong
- [ ] AgentRun 到达终态 `SUCCEEDED`，`agent_runs` 表有对应行（含 `worker_id`）
- [ ] 同一 thread 的两个 Run 执行区间**不重叠**（查 `agent_runs` 的
      `started_at`/`finished_at`）
- [ ] `GET /api/runs/dead` 返回空（无意外 DLQ）

**HITL 冒烟（仅当 `HITL_ENABLED=true`；Blocking）**

```bash
# 10.7 审批队列可达且为空（无审批挂起）
curl -s "http://localhost:8000/api/approvals?status=PENDING" \
  -H "Authorization: Bearer <ADMIN_JWT>" | jq

# 10.8 表已迁移
docker compose exec postgres psql -U postgres -d cosmetics_ai \
  -c "SELECT approval_id,run_id,action,risk_level,status,expires_at FROM human_approvals ORDER BY requested_at DESC LIMIT 10;"
```

- [ ] `GET /api/approvals?status=PENDING` 返回 200（非 401/403）—— 确认
      reviewer 身份解析链路可用（JWT 或 `X-Reviewer-Id` 都拿不到时应为 401，
      **不会**退化成匿名固定 reviewer）
- [ ] `human_approvals` 表存在且为空
- [ ] 值班须知已确认：审批**无主动通知**，无人处理会在
      `HITL_APPROVAL_TTL_SECONDS` 后落 `EXPIRED`（等同拒绝）

**幂等与崩溃恢复验收（有独立基础设施时跑，不是启动前置条件）**

```bash
make runtime-e2e       # tests/integration/runtime：真实 PG + Redis + 多进程 Celery
make runtime-chaos     # SIGKILL 整个 worker 进程组 → 续跑 + 副作用不重复
make runtime-verify    # 产出 artifacts/distributed-runtime/<ts>/report.json
```

- [ ] `make runtime-e2e` 通过（基础设施缺失时是硬 FAIL，不静默 skip）
- [ ] `make runtime-chaos` 的 JSON 里 `result == "PASS"`，且
      `steps[].side_effect_deduplicated` 显示副作用计数**仍为 1**
- [ ] `make runtime-verify` 产出 artifact，`overall_status == "PASS"`，
      带 `tested_code_sha` + `generated_at`

### 11. 性能基线 [5分钟]

```bash
# 记录初始性能指标
echo "=== 资源使用 ==="
docker stats --no-stream

echo "=== 响应时间 ==="
time curl -s http://localhost:8000/api/health > /dev/null

echo "=== 缓存状态 ==="
docker compose exec redis redis-cli INFO stats | grep hits
```

**记录这些数据用于后续对比**

---

## 📊 上线后监控（前72小时）

### 每小时检查
- [ ] 错误率 < 1%
- [ ] P95延迟 < 10s
- [ ] CPU使用率 < 80%
- [ ] 内存使用率 < 85%
- [ ] 磁盘空间充足

### 每天检查
- [ ] 用户反馈评分趋势
- [ ] LLM API调用成本
- [ ] 缓存命中率
- [ ] 活跃会话数
- [ ] 告警通知是否正常

---

## 🐛 常见问题速查

### 问题1: 服务启动失败
```bash
# 检查日志
docker compose logs app

# 常见原因:
# - 端口被占用: lsof -i :8000
# - 环境变量缺失: docker compose config | grep -A5 environment
# - 依赖服务未就绪: docker compose ps
```

### 问题1b: `app` 容器 crash loop，日志含 `ADMIN_PASSWORD environment variable must be set`

**现象**：`app` 反复重启，日志出现

```
ValueError: ADMIN_PASSWORD environment variable must be set to initialize the admin account.
  File "/app/api/app_factory.py", line 32, in <module>
    init_default_admin()
  File "/app/auth/service.py", line 533, in init_default_admin
```

**为什么发生**：该 `raise` 在 `api/app_factory.py` 的**模块导入期**执行，不在
lifespan 内，所以进程在服务任何请求之前就退出。用已存在的数据库（`users` 表中已有
`admin` 行）不会触发——它只在**首次**引导时读取该变量。

**处置**：

```bash
# 1. 在 .env 中设置（Compose 对 app/canary 是 fail fast 的，缺失时下面第 2 步会报错）
echo "ADMIN_PASSWORD=$(openssl rand -base64 24)" >> .env

# 2. 确认 contract 已生效（能看到 app 拿到该变量）
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod.yml config | grep -A2 ADMIN_PASSWORD

# 3. 重新拉起
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.prod.yml up -d app
```

> `worker` 报同一个 `ValueError` 说明它被误配进了 worker 的 `environment`——它不需要
> 该变量（不导入 `api.app_factory`），且注入只会扩大高权限口令的暴露面。

### 问题2: LLM API调用失败
```bash
# 验证API Key
echo $OPENAI_API_KEY | wc -c

# 测试连通性
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.siliconflow.cn/v1/models
```

### 问题3: 数据库连接失败
```bash
# 检查PostgreSQL状态
docker compose logs postgres

# 验证连接字符串
echo $DATABASE_URL

# 测试连接
docker compose exec postgres psql -U csai -d csai -c "SELECT 1;"
```

### 问题4: Redis连接失败
```bash
# 检查Redis状态
docker compose logs redis

# 测试连接
docker compose exec redis redis-cli ping
```

---

## 📞 紧急回滚

如果上线后发现严重问题：

```bash
# 1. 立即停止服务
make prod-down

# 2. 清理容器
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/docker-compose.prod.yml \
               down -v

# 3. 恢复上一版本代码
git checkout PREVIOUS_TAG

# 4. 重新部署
make prod

# 5. 验证恢复
curl http://localhost:8000/api/health
```

---

## ✅ 上线成功标志

完成以下所有项即表示上线成功：

- [ ] 服务正常运行超过24小时
- [ ] 无ERROR级别日志
- [ ] 用户可正常使用聊天功能
- [ ] 监控指标在正常范围
- [ ] 告警系统正常工作
- [ ] 备份自动执行成功
- [ ] 性能满足SLA要求

---

## 📝 检查清单签署

| 项目 | 执行人 | 完成时间 | 备注 |
|------|--------|----------|------|
| 环境配置 | ________ | ____-__-__ __:__ | |
| 预部署检查 | ________ | ____-__-__ __:__ | |
| 构建与测试 | ________ | ____-__-__ __:__ | |
| 数据库准备 | ________ | ____-__-__ __:__ | |
| 执行部署 | ________ | ____-__-__ __:__ | |
| 功能验证 | ________ | ____-__-__ __:__ | |
| 监控配置 | ________ | ____-__-__ __:__ | |

**最终确认**: □ 可以上线  □ 需要延期

**确认人**: ________________  
**日期**: ____-__-__

---

**祝上线顺利！🎉**

如需帮助，请参考:
- 📖 [完整运维手册](../operations/production-operations-guide.md)
- ✅ [生产准备度检查清单](production-readiness-checklist.md)