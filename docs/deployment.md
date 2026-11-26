# Deployment

本指南区分**本地标准 Compose 栈**与**需授权和真实外部条件的工业 profile**。容器能启动、健康检查返回 200、CI 通过都不等于企业验收或生产就绪。

## 本地标准 Compose

### 前置条件

- Docker Engine 与 Docker Compose plugin；可拉取 Docker 基础镜像与 Python 依赖。
- 本机未占用 Nginx/Grafana/Alertmanager 端口；可通过 `NGINX_HTTP_PORT`、`NGINX_HTTPS_PORT`、`GRAFANA_PORT`、`ALERTMANAGER_PORT` 调整。
- Compose 要求 `ADMIN_PASSWORD`、`POSTGRES_PASSWORD`、`GRAFANA_PASSWORD`。API 配置还需 `JWT_SECRET`、`SESSION_TOKEN_SECRET` 和 `API_KEY`。
- API/Worker 要求非占位 `OPENAI_API_KEY`；空值或模板占位值会触发 fail-closed。Compose 会调用配置的 provider，可能产生费用；使用获准且限额的测试凭证。没有 provider 凭证时运行根目录[离线演示](../README.md#快速体验)，不把 blank-key HTTP 200 或 degraded health 当作业务 readiness。
- 显式将 `DATABASE_URL` 指向 Compose 内部 PostgreSQL 服务（例如 `postgresql://csai:<password>@postgres:5432/csai`），避免 SQLite 被误作跨 worker 的业务库。

不要将 `.env`、生成凭据或证书提交到 Git。一次性本地密码、provider key 注入、TLS 证书和验证命令示例见根目录 [README](../README.md#快速体验)。

### 服务与协议默认

标准 Compose 定义 FastAPI `app`、Celery `worker`、独立 `beat`、PostgreSQL、Redis、Qdrant、Nginx 和监控服务。普通 Python runtime fallback 关闭；Compose 由一次性 `protocol-init` 在私有 volume 中为合成 fixture 生成随机 bearer 与摘要锁定配置，默认启动只读 ProductDocs MCP server 和独立 A2A after-sales peer。MCP/A2A 经现有 ToolRegistry/SDK 路径启用；peer 不发布宿主端口，合成协议不代表真实目录、ERP、退款资格或企业批准。

- 显式回滚：`MCP_ENABLED=false`、`A2A_AFTERSALES_ENABLED=false`；A2A local peer 转入 dormant 状态。
- `CSAI_RUNTIME_PROFILE=industrial` 不允许 synthetic mode；必须改为 `CSAI_PROTOCOL_MODE=external`，并提供获准目录和 SHA-256、private HTTPS endpoint、CA 与 bearer。标准合成 volume 不能直接升级为工业配置，需按运维流程替换。
- API/Worker/beat 的 provider readiness 与 Compose container health 不等价；本地无 provider key 时，配置 fail-closed 会阻止应用进程运行。外部 provider readiness 的验收状态以 [Production Readiness](production-readiness.md) 为准。

### 启动、检查与停止

```bash
make tls-local-cert  # 仅本机开发；不可用于生产
docker compose -f deploy/compose/docker-compose.yml config
docker compose -f deploy/compose/docker-compose.yml up -d --build
docker compose -f deploy/compose/docker-compose.yml ps
docker compose -f deploy/compose/docker-compose.yml exec -T app \
  curl -fsS http://localhost:8000/api/health
```

App startup runs the Alembic upgrade path. Health endpoint returns JSON; inspect `status` and component fields. HTTP 200 or a healthy Docker probe alone does not establish provider readiness. App host port is not published in the base stack; service access normally goes through Nginx. A local self-signed certificate is untrusted in a browser and is never production TLS.

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

Normal `down` retains PostgreSQL, Redis and Qdrant volumes. Add `-v` only when deliberately deleting this isolated local stack's data.

2026-11-26 本地 WSL2 验证在已合入 PR #87 的源代码上构建了 app/worker/beat 镜像并启动基础设施和协议服务。未提供 provider key 时，API 在配置校验处 fail-closed；worker/beat 未保持 healthy，其独立重启原因未采集。另以 `APP_MODE=dev` 临时测试进程通过合成协议 volume 执行了官方 MCP SDK stdio → ToolRegistry ProductDocs 查询和 A2A SDK → 独立 peer 业务调用（`execution_authorized=false`）。此结果只证明组件协议互操作，不证明标准 app readiness 或图路由。临时容器、网络和 volume 已移除。完整标准 Compose 验收仍开放，见 [Issue #86](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/86)。

## Runtime data duties

- PostgreSQL 保存 durable `AgentRun`、业务记录与 LangGraph checkpoint。
- Redis 用于 session、thread lease/lock、队列 broker 和运行时协调。
- 多 worker / 多副本配置必须共享 PostgreSQL checkpoint、Redis session 和 Redis per-thread lock；配置与 fail-closed 校验见 [配置参考](reference/configuration.md)。
- Qdrant 保存向量数据；BM25 与检索索引需按 RAG 运维流程导入，空库不代表知识检索部署完成。
- Celery `worker` 执行 durable run；独立 `beat` 驱动 stuck-run reconciliation。服务健康不代表业务积压已清空。

## 数据库迁移

部署前审阅 Alembic 迁移与回滚方案，并在已设置 PostgreSQL `DATABASE_URL` 的目标环境执行。该命令不适用于默认 SQLite 示例配置；本次未对真实企业数据库执行迁移：

```bash
alembic upgrade head
```

现有生产数据须先备份并验证恢复；不可在未经数据所有者授权的情况下盘点或迁移真实 session/订单数据。迁移细节见 [生产运维指南](operations/production-operations-guide.md)。

## 工业 profile 与回滚

工业配置是严格准入配置，不是本地 demo 的同义词。需要明确选择 `CSAI_RUNTIME_PROFILE=industrial`，并提供获准的身份映射、PostgreSQL/Redis、密钥、模型、TLS、ERP adapter、批准目录摘要、CA 与业务运营人员。缺失的身份、授权、checkpoint 或安全配置必须失败关闭。

- [Industrial Runtime Admission](architecture/industrial-runtime-admission.md)：准入代码和失败边界。
- [Production Readiness](production-readiness.md)：生产证据与外部阻塞的唯一状态入口。
- [Runtime runbook](operations/distributed-runtime-runbook.md)：worker、恢复、DLQ 和故障处置。

当前没有真实 ERP 写交易或生产运行证据。本地合成协议目录、mock ERP、CI 服务容器都不是生产数据或真实业务验收。回滚需同时检查数据库 schema、审批记录和副作用账本；不能只回滚镜像而忽略已执行的外部动作。

生产 Python 依赖契约是 `requirements.txt` 与 Dockerfile；`requirements-lock.txt` 是历史非权威快照，不是 lockfile。GitHub Release/tag、GHCR image、Actions 和部署是不同产物/证据；未通过 exact-head 发布门禁时不发布稳定版本。
