# Deployment

> 🟢 CURRENT — 本文是**部署入口与配置理由**。
> 具体运维操作与排障见 [operations/](operations/)，逐项发布检查见
> [checklists/](checklists/)。
>
> 部署栈是 **Docker Compose**。Kubernetes manifest **不在本仓库**
> （理由见 [limitations.md](limitations.md)）。

---

## 1. 拓扑：6 个服务，1 个真相源

```
                          ┌──────────────┐
   client ─── HTTPS ────► │    Nginx     │  TLS 终止 / WS upgrade / 静态资源
                          └──────┬───────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
            ┌───────▼────────┐        ┌───────▼────────┐
            │  app (FastAPI) │        │  app (副本 2+) │   gunicorn workers
            │  快路径 inline  │        │                │
            └───┬────────┬───┘        └────────────────┘
                │        │
    ┌───────────┘        └──────────────────┐
    │                                       │
┌───▼──────────┐   ┌──────────────┐   ┌─────▼────────┐
│ Redis        │   │ PostgreSQL   │   │ Qdrant       │
│ session      │   │ 业务库       │   │ 向量库       │
│ thread lock  │   │ AgentRun     │   │              │
│ broker       │   │ approvals    │   └──────────────┘
│ tool store   │   │ side_effects │
│ run events   │   │ LangGraph    │
└───┬──────────┘   │ checkpoints  │
    │              └──────────────┘
    │  ┌──────────────┐
    └──►│ celery worker│  异步路径：claim run → 锁 → 跑图 → 终态
       │  (N 副本)     │  崩溃可恢复；重投靠 at-least-once + 幂等
       └──────────────┘
```

**服务清单**（`deploy/compose/docker-compose.yml`）：

| 服务 | 角色 | 伸缩 |
|---|---|---|
| `nginx` | 反向代理 / TLS / 静态资源 | 1 |
| `app` | FastAPI（快路径 + runs API + approvals API） | N（需一致配置，见下） |
| `worker` | Celery 执行 AgentRun | N |
| `postgres` | 业务库 + LangGraph checkpoint | 1（有状态） |
| `redis` | session / thread lock / broker / tool store / run events | 1（有状态） |
| `qdrant` | 向量库 | 1（有状态） |

其他 compose 变体：`override`（开发挂载）、`canary`（90/10）、
`scale`（`make scale N=3`）、`monitoring`（Prometheus/Grafana/Alertmanager/Loki/Promtail）、
`otel`（OTel Collector）。

---

## 2. 启动前置校验（fail-fast，不是 warning）

生产启动会**拒绝启动**而不是打一条 log 继续跑。这是有意的：
带病启动的服务会在几分钟后以更难排查的方式失败。

### 2.1 凭据类（`core/config.py`）

| 变量 | 要求 |
|---|---|
| `JWT_SECRET` | 长度 ≥ 32 |
| `SESSION_TOKEN_SECRET` | 必须设置 |
| `API_KEY` | 必须设置 |

### 2.2 分布式运行时（`core/config.py::validate_distributed_runtime_settings`）

| 变量 | 要求 | 为什么 |
|---|---|---|
| `SESSION_STORAGE_BACKEND` | 必须 `redis` | 多 worker 下内存 session 会让同一会话在不同 worker 看到不同历史 |
| `AGENT_RUN_DISPATCH` | 必须 `celery`（等价 canonical 旋钮 `AGENT_EXECUTION_MODE=queued`） | 否则异步 run 无人执行 |
| `LANGGRAPH_CHECKPOINT_BACKEND` | 生产**不得**为 `memory` | 内存 checkpoint 多副本下会"看起来正常但状态已丢"，比启动失败危险 |
| `AGENT_RUN_THREAD_LOCK_TTL_SECONDS` | **>** `AGENT_RUN_TASK_TIME_LIMIT` + 30 | 锁先过期 = 同一 thread 并发执行 = 幂等键可能被绕过 |

### 2.3 多 worker 一致性 gate（`GUNICORN_WORKERS>1` 时额外要求）

| 变量 | 要求 |
|---|---|
| `LANGGRAPH_CHECKPOINT_BACKEND` | `postgres` |
| `SESSION_STORAGE_BACKEND` | `redis` |
| `AGENT_RUN_THREAD_LOCK_ENABLED` | `true` |
| `AGENT_RUN_THREAD_LOCK_BACKEND` | `redis` |

**为什么多 worker 要额外加一层？** 单 worker 时"忘了配 checkpoint"最多丢一次会话；
多 worker 时同样的疏漏会让**两个副本各自认为自己是唯一真相源**，
进而在同一 thread 上并发执行。这类 bug 在开发环境（一 worker）永远不会出现。

### 2.4 Checkpointer 初始化：三层 fail-closed

1. `LANGGRAPH_CHECKPOINT_BACKEND` 值非法 → `ConfigurationError`（`core/config.py:637`）
2. 生产初始化失败 → **抛错拒绝回退 MemorySaver**（`core/container.py:341-351`）
3. 图构建时 checkpointer 为 `None` + 非 dev → **抛错**（`core/container.py:278-289`）

只有 dev/test 才允许降级到 `MemorySaver`，且降级会**显式标记**
`status="degraded"` 并在健康检查里暴露——**静默降级是本项目明令禁止的**。

---

## 3. 生成生产环境变量

```bash
python3 scripts/generate_prod_env.py     # 生成 .env.prod（高熵随机凭据）
```

生成后**必须**人工确认并补齐（脚本不会替你决定）：

| 变量 | 说明 |
|---|---|
| `ERP_MODE=real` + 金蝶凭据 | 生产应接真实 ERP；配置不全时 `erp/factory.py:41-45` 会**静默降级到 mock** |
| `LLM_PROVIDER` / `LLM_API_KEY` | 默认硅基流动 `Qwen/Qwen3-8B` |
| `POSTGRES_URL` / `REDIS_URL` | 供 checkpoint / session / lock 共用 |
| `QDRANT_URL` | 向量库 |
| `MCP_SERVERS` | 默认空 → **MCP 完全不启用**（fail-closed） |
| `HITL_*` | 默认 `HITL_ENABLED=false`；见下方警告 |

> ⚠️ **关于 `HITL_*` 的一处已知 fail-open**：只设 `HITL_ENABLED=true`
> 而不设 `HITL_HIGH_RISK_TOOLS` 与 `HITL_HIGH_AMOUNT_THRESHOLD` 时，
> 风险优先级链全部落空 → 判定为 `LOW` → **什么都不拦且不报警**。
> 详见 [limitations.md](limitations.md) 与
> [PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P0-3。
> 部署前请显式设置 `HITL_HIGH_RISK_TOOLS`（或把阈值设成非零）。

---

## 4. 三条关键 TTL 关系（配错就会出事）

| 关系 | 要求 | 违反后果 |
|---|---|---|
| `AGENT_RUN_THREAD_LOCK_TTL_SECONDS` > `AGENT_RUN_TASK_TIME_LIMIT` + 30 | 启动时校验（fail-fast） | 锁过期后另一 worker 接管同一 thread → **同一幂等键并发执行** |
| `AGENT_RUN_VISIBILITY_TIMEOUT` ≥ 任务实际时长 | 需按最长任务设置 | 任务还在跑就被重投 → 依赖幂等兜底，但浪费资源 |
| `HITL_APPROVAL_TTL_SECONDS` > 审批人响应预期 | 业务判断 | 过短会把还没来得及看的审批变成 `EXPIRED`（**按拒绝处理**） |

Celery 侧的三个关键开关（`runtime/celery_app.py:34-67`）：

```python
task_acks_late = True                       # 执行完才 ack
task_acks_on_failure_or_timeout = False      # 关键：失败不 ack → 会被重投
task_reject_on_worker_lost = True            # worker 死了 → 重新入队
broker_transport_options = {"visibility_timeout": ...}
task_ignore_result = True                    # 结果后端不是真相源
```

**`task_acks_on_failure_or_timeout=False` 是最容易被写错的一个。**
它是 Celery 的**反直觉默认值**（默认为 `True`），写成 `True` 会导致
失败任务**永远不再投递**——崩溃恢复与重试退避全部静默失效。

**明确不是 exactly-once**：`acks_late` + `reject_on_worker_lost` + `visibility_timeout`
构成的是 **at-least-once**。正确性由三层幂等承担：

| 层 | 机制 | 防什么 |
|---|---|---|
| run 级 | 终态重复投递 no-op + `idempotency_key` 唯一约束 | 同一请求重复提交产生两个 run |
| thread 级 | Redis 锁 + lease | 同 thread 并发执行 |
| 工具级 | `tool_side_effects` ledger，`operation_key = run_id:tool_call_id` | **做了一次被重做** |

---

## 5. 上线步骤

### 5.1 迁移

```bash
make db-upgrade           # Alembic 升级到 head
# 空库全链路验证（CI 有对应 job）
bash scripts/verify_fresh_db_migration.sh
```

必需表：`agent_runs`、`agent_dead_letters`、`tool_side_effects`、
`human_approvals`（Alembic `006`）、以及 LangGraph checkpoint 表
（由 `AsyncPostgresSaver.setup()` 在**跨进程 advisory lock 保护下**创建，
多副本同时启动不会互相踩）。

### 5.2 启动顺序

```bash
make prod                 # 或 docker compose -f deploy/compose/docker-compose.yml up -d
```

依赖顺序由 compose 保证：`postgres`/`redis`/`qdrant` healthy →
`app` 与 `worker` 启动。`worker` 用 **Celery 原生健康检查**
（`inspect ping` → `pong`），不是 `docker compose ps`——
后者只要 compose 文件能解析就返回 0。

### 5.3 上线后必须确认

```bash
# 1) 基础设施健康
curl -s localhost:8000/api/health

# 2) checkpoint 后端不是 memory，且没有静默降级
curl -s localhost:8000/api/health | jq '.checkpointer'
#   期望 status=healthy, backend=postgres
#   若为 degraded → 查 DB 连通性，不要"先上线再说"

# 3) session 后端
curl -s localhost:8000/api/health | jq '.session'

# 4) worker 真的在消费（不只是进程存在）
docker compose -f deploy/compose/docker-compose.yml exec worker \
  celery -A runtime.celery_app:celery_app inspect active

# 5) 熔断器是 closed（OPEN 说明 LLM 侧已经降级）
curl -s localhost:8000/api/circuit-breaker

# 6) DLQ 为空
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8000/api/runs/dead
```

逐项检查清单见 [checklists/production-readiness-checklist.md](checklists/production-readiness-checklist.md)。

---

## 6. 卡死 run 的兜底

`runtime/retry.py:33-66` 的 `reconcile_stuck_runs` 扫描
长期停在 `QUEUED` / `RETRYING` 但没有活跃 worker 的 run，重新投递。

> ⚠️ **当前是手动 / cron 脚本**（`scripts/reconcile_stuck_runs.py`），
> **没有接 Celery Beat，也没有 CI 契约保证它会跑**。
> 生产请显式配置 cron / K8s CronJob 调用它。
> 接线方案见 [PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P1-5。

---

## 7. DLQ 与重放

重试耗尽 → 写 `agent_dead_letters` + 状态 `DEAD_LETTER`。

```bash
# 列出（需 admin）
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "localhost:8000/api/runs/dead?limit=50"

# 人工重放（复用原 run_id，绕过工具幂等键的问题）
python3 scripts/replay_dead_run.py --run-id <RUN_ID>    # make runtime-replay-help
```

**为什么重放必须复用原 `run_id`？** 幂等键是 `run_id:tool_call_id`。
换一个 `run_id` 就换了一套幂等键，**已执行过的副作用会真的再执行一次**。
所以 `requeue_dead_letter`（`runtime/run_service.py:512-553`）
在代码层面就禁止生成新 run_id。

排障细节见 [operations/distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md)。

---

## 8. 监控栈

```bash
make monitoring-up
```

| 组件 | 作用 | 备注 |
|---|---|---|
| Prometheus | 抓取 | ⚠️ 见下方断链说明 |
| Alertmanager | 告警路由 | `monitoring/alertmanager.yml` |
| Grafana | 看板 | `monitoring/grafana/dashboards/csai-overview.json` |
| Loki + Promtail | 日志聚合 | 独立 compose 变体 |
| OTel Collector | trace 接收 | ⚠️ 仅 `debug` exporter，**无持久化后端** |

### ⚠️ 两处必须知道的观测缺口

1. **DLQ 告警规则当前不可能触发。** 唯一的 Prometheus 端点
   `GET /metrics/prometheus`（`api/routes/monitoring.py:332`）手工输出
   10 个 `csai_*` 聚合指标，而 `agent_run_dead_letter_total` 等约 70 个
   `prometheus_client` 注册指标**从未被序列化输出**
   （全仓无 `generate_latest`）。`monitoring/alert_rules.yml` 的
   `AgentRunDeadLetterDetected` 依赖一个不存在的时间序列。
   **在修复前，DLQ 必须靠人看或靠查询。**
2. **Grafana 有 6 个面板永远为空**（引用了从未输出的指标名）。

两项的修复方案见 [limitations.md](limitations.md) 与
[PROJECT_FINALIZATION_PLAN.md](reports/audit/PROJECT_FINALIZATION_PLAN.md) P0-1 / P1-6。

**告警自动化的兜底**：除了 Prometheus，应用内还有
`alerts/notifier.py`（webhook + SMTP）与 `SLAAlertManager`
（`core/monitoring.py:1101+`，带严重度升级），可通过 `GET /api/alerts` 查询。

---

## 9. 敏感信息

| 文件 | 策略 |
|---|---|
| `.env` | gitignore，本地当前激活配置 |
| `.env.example` | 占位符，**安全提交** |
| `.env.test` | **提交到仓库**（全部 Mock，无真实凭据） |
| `.env.dev` / `.env.prod` | gitignore |

守卫：`scripts/check_secrets.py`（含 `--history` 全历史扫描，
CI 每周 cron 跑）、`pre-commit` 钩子、CI 硬编码 key grep。
策略全文见 [security/public-repository-secret-policy.md](security/public-repository-secret-policy.md)。

---

## 10. 环境切换

```bash
make env-dev     # 宽松认证 / 详细日志 / SQLite
make env-prod    # 严格认证 / JSON 日志 / PostgreSQL + Redis
make env-test    # 最小依赖 / Mock 一切
make env-check   # 查看当前环境
```

---

## 11. 本仓库不提供的部署方式

| 不提供 | 理由 | 替代 |
|---|---|---|
| Kubernetes manifests | 没有真实集群与多副本一致性验证的 manifest 是**装饰性声明**，比没有更糟 | 路线图项，见 [limitations.md](limitations.md) |
| Serverless 部署 | checkpoint + 长时 worker + Redis 锁模型与 FaaS 生命周期冲突 | — |
| 蓝绿/金丝雀自动切换 | compose 有 `canary`（90/10 流量分割），但自动判据与回滚未经真实流量验证 | `make canary` 需人工盯 |
| 托管的 trace 后端 | collector 只配了 `debug` exporter | 接入 Jaeger/Tempo 见路线图 |

---

## 12. 延伸阅读

| 你想知道 | 去 |
|---|---|
| 生产运维手册 | [operations/production-operations-guide.md](operations/production-operations-guide.md) |
| 分布式 Runtime 排障 / DLQ 重放 | [operations/distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md) |
| LLM 提供商切换 | [operations/llm-provider-switch.md](operations/llm-provider-switch.md) |
| E2E 验证方法论 | [operations/e2e-verification-guide.md](operations/e2e-verification-guide.md) |
| 生产就绪逐项检查 | [checklists/production-readiness-checklist.md](checklists/production-readiness-checklist.md) |
| 新环境快速启动 | [checklists/quick-launch-checklist.md](checklists/quick-launch-checklist.md) |
| 配置项全表 | [reference/configuration.md](reference/configuration.md) |
| 架构与状态归属 | [architecture.md](architecture.md) |
| 已知不宣称项 | [limitations.md](limitations.md) |
