# Customer Service AI Agent

> A production-oriented customer-service Agent system built with LangGraph, featuring multi-agent orchestration, durable execution, governed tool calling, human handoff, and reproducible evaluation.

这是一个独立维护的客服 Agent 工程参考实现：将客户请求路由到领域处理流程，并在需要时使用受控工具、持久化任务状态和人工审批/坐席接管。项目展示可检查、可运行的 Agent Runtime 与业务边界，不是通用 Agent Framework，也不代表任何企业的官方或生产系统。

[![Main CI](https://github.com/Xander-Xai/Customer-Service-AI-Agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Xander-Xai/Customer-Service-AI-Agent/actions/workflows/ci.yml)
![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)
[![Latest Release](https://img.shields.io/github/v/release/Xander-Xai/Customer-Service-AI-Agent)](https://github.com/Xander-Xai/Customer-Service-AI-Agent/releases)

## 项目特点

- **Multi-Agent orchestration**：LangGraph 以领域路由为主，仅在多领域任务需要时执行并行或固定主从协作；[架构与编排边界](docs/architecture.md) · [`core/graph_builder.py`](core/graph_builder.py)
- **Durable Agent Runtime**：API 快路径与异步耐久任务分流；异步执行由 `AgentRun`、PostgreSQL checkpoint、Redis、Celery 和恢复协调器支撑；[Runtime 设计](docs/design/agent-runtime.md) · [`runtime/`](runtime/)
- **Governed tool execution**：Function Calling 与 MCP 工具经 `ToolRegistry`；直接 ERP 读取走独立授权 adapter。风险、回执和幂等边界由服务端控制；HITL 审批不等于业务动作已完成；[工具执行边界](docs/design/tool-execution-boundaries.md)
- **Task memory and recovery**：恢复依据 checkpoint、run ledger、审批记录及已授权工具回执；对话摘要只作为上下文，不是订单事实或执行凭证；[Memory 定位](docs/reference/memory-engineering-positioning-20261124.md)
- **MCP / A2A**：标准 Compose 默认以合成数据启用只读 ProductDocs MCP 与独立售后 A2A peer；普通 Python runtime 的 fallback 仍关闭，工业 profile 拒绝合成配置。完整应用 Compose readiness、真实 provider、企业目录/TLS 与 ERP 验收仍未完成；[当前状态](docs/reference/current-state.md) · [Issue #86](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/86)
- **Human handoff**：高风险动作审批与客户转人工是两种不同流程；工单及坐席工作台代码已入主线，真实坐席、通知与 SLA UAT 尚未完成；[工单运维与验收](docs/human-handoff-operations.md)
- **Evaluation and observability**：离线 Agent Eval 测编排/治理行为；指标有显式分子、分母和证据边界；[Agent Eval](docs/reference/agent-evaluation.md) · [评测总览](docs/evaluation.md)

## 架构概览

```mermaid
flowchart LR
    C[客户]
    W[Web / API]
    C --> W
    H[人工坐席]
    T[持久化工单]
    H <--> T
    A[认证与权限]
    W --> A
    A --> T
    F[Fast path\n同步只读请求]
    R[Durable path\nAgentRun + Celery]
    A --> F
    A --> R
    G[LangGraph\nRouter / Orchestration]
    F --> G
    R --> G
    M[领域 Agent / bounded ReAct]
    G --> M
    TR[ToolRegistry]
    M --> TR
    Q[RAG\nQdrant + BM25]
    M --> Q
    ERP[授权 ERP Adapter]
    TR --> ERP
    MCP[MCP\nread-only synthetic catalog]
    TR --> MCP
    A2A[A2A\nafter-sales peer]
    TR --> A2A
    HITL[HITL approval]
    G --> HITL
    X[Response postprocessor]
    G --> X
    PG[(PostgreSQL\nAgentRun + checkpoint)]
    REDIS[(Redis\nsession / lease / broker)]
    CW[Celery worker + beat]
    O[Evaluation / OTel / Prometheus]
    X --> C
    R --> PG
    R --> REDIS
    R --> CW
    G --> PG
    G --> REDIS
    X --> O
    G --> T
```

图中是当前 `main` 的概念边界；标准 Compose 的协议目录是合成数据，不代表企业目录、provider、ERP 或退款资格已验收。运行时恢复依据 durable run/checkpoint/授权回执；本地账本不保证 ERP 端到端 exactly-once。详见 [Architecture](docs/architecture.md)。

## 快速体验

### 路径 A：离线、无 API Key 的确定性演示

要求 Python 3.10–3.12、仓库声明的依赖及 `make`。该路径使用 Mock LLM 和本地 fixture，不访问外网：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
make demo-offline
```

成功时会打印 `MOCK/OFFLINE` proof card，并写入 gitignored 的 `artifacts/demo/`。proof card 会记录实际执行时的 SHA、时间、命令与测试锚点；运行使用 egress guard，不需要 provider key。它验证的是 Mock LLM 下的工程路径，不是模型质量或生产验收。细节见 [offline demo guide](docs/guides/offline-demo.md)。

### 路径 B：标准 Docker Compose 本地栈

要求 Docker Engine / Compose plugin、可拉取依赖的网络及获准的限额 LLM 测试凭证。Compose 启动时会请求配置的 provider，可能产生费用；不要将 `.env`、生成密钥或自签证书提交到仓库。

```bash
# 当前 shell 内生成一次性本地密码；关闭终端后需要重新导出。
export ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export DATABASE_URL="postgresql://csai:${POSTGRES_PASSWORD}@postgres:5432/csai"
export GRAFANA_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SESSION_TOKEN_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
read -r -s -p "Authorized LLM provider test key: " OPENAI_API_KEY; printf '\n'; export OPENAI_API_KEY
export NGINX_HTTP_PORT=8080 NGINX_HTTPS_PORT=8443
make tls-local-cert
docker compose -f deploy/compose/docker-compose.yml config
docker compose -f deploy/compose/docker-compose.yml up -d --build
docker compose -f deploy/compose/docker-compose.yml ps
docker compose -f deploy/compose/docker-compose.yml exec -T app \
  curl -fsS http://localhost:8000/api/health
```

App startup runs Alembic upgrade. The health endpoint returns JSON; inspect `status` and component fields. HTTP 200 or a healthy Docker probe alone does not establish provider readiness. Standard Compose also initializes the synthetic read-only ProductDocs MCP and A2A peer; neither uses real customer or ERP data.

停止服务：

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

标准多 worker 部署须共享 PostgreSQL checkpoint、Redis session 和 Redis thread lock（`AGENT_RUN_THREAD_LOCK_BACKEND=redis`，per-thread）。工业 profile 与外部授权要求见[部署指南](docs/deployment.md)及[准入契约](docs/architecture/industrial-runtime-admission.md)。

### Compose 验收状态

2026-11-26 在 WSL2/Docker 29.5.2 + Compose 5.1.4 上，PR #87 代码构建了新 app/worker/beat 镜像，隔离基础服务和协议容器启动；API 因未提供 `OPENAI_API_KEY` 在配置校验处 fail-closed，worker/beat 未保持 healthy（其各自原因未单独采集），所以完整标准 Compose readiness 未通过。另用同一 app image 的临时测试进程（`APP_MODE=dev` / `DEV_MODE=true`）及私有 synthetic protocol volume，分别通过官方 MCP SDK → stdio → `ToolRegistry` SKU 查询和 A2A SDK → 独立售后 peer 业务调用（`execution_authorized=false`）。该回环证明协议组件，不证明标准 app graph readiness 或路由。临时容器、网络、volume 已清理；外部验收继续见 [Issue #86](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues/86)。

## 业务场景与评测

表中输入来自合成 fixture / 测试，不是真实客户对话：

| 场景与输入 | 处理过程 | 输出 / 状态边界 |
|---|---|---|
| 商品咨询：“这款精华液多少钱？” | 单专家路由；离线测试用可控 Mock LLM，验证 cache miss、分类和响应后处理 | 断言响应非空、路由字段存在；模拟答复不可作为商品事实。见 [`test_integration.py`](tests/integration/test_integration.py) |
| 商品支持文档查找 | Compose 合成 ProductDocs 由 MCP stdio server 只读查询，注册至 ToolRegistry | 回执标记 `SYNTHETIC_LOCAL_FIXTURE`；不是批准企业目录。见 [MCP 产品文档](docs/operations/mcp-product-documents-20261125.md) |
| 售后流程查询 | A2A SDK 将枚举化问题/阶段委派给独立规则 peer | 合成结果不授权退款或证明退款资格；`execution_authorized=false`。见 [A2A 售后专家](docs/architecture/a2a-after-sales-business-20261125.md) |
| 高风险动作或需要人工 | 耐久 run → 风险识别 → 审批 interrupt；客户转人工进入独立工单生命周期 | `WAITING_APPROVAL` 不是动作完成；真实 ERP 写入与坐席运营 UAT 未验证。见 [HITL](docs/design/human-in-the-loop.md) · [Handoff](docs/human-handoff-operations.md) |

Agent Eval scripted baseline 衡量执行编排与治理，不衡量真实模型效果；115-case 数据 `human_confirmed_only=0`，整体 `INCONCLUSIVE`。**当前 649-query 正式指标：`NOT_VERIFIED`**（RAG benchmark gold 缺少人工相关性判断）。详见[当前事实](docs/reference/current-state.md)、[生产准备度](docs/production-readiness.md)及[RAG 评测参考](docs/reference/rag-evaluation.md)。

Runtime E2E 曾出现 Gate11 worker-recovery 间歇性 `CheckpointEpisodeMismatch`；相同 SHA CI 重跑与 main commit `31a11f91d9a09ba6d5e8763db84151be930f9a27` 上的本地 PostgreSQL/Redis 定向测试通过，但根因尚未定位。详见[当前事实](docs/reference/current-state.md#runtime-gate11-间歇性失败观察)；一次通过不等于竞态已修复。

常用验证命令：

```bash
make test
make lint
make audit-docs
make agent-eval
make runtime-e2e       # 需要真实 PostgreSQL + Redis；缺失时失败，不会假装通过
make runtime-chaos     # 真实依赖的 worker crash/recovery 场景
```

## 文档与工程阅读路线

| 时间 | 阅读入口 |
|---|---|
| 5 分钟 | 本页架构图、[业务场景](#业务场景与评测)、[当前事实](docs/reference/current-state.md) |
| 15 分钟 | `make demo-offline`、[offline demo guide](docs/guides/offline-demo.md)、[配置参考](docs/reference/configuration.md) |
| 30 分钟 | [Architecture](docs/architecture.md) → [`api/`](api/) → [`core/graph_builder.py`](core/graph_builder.py) → [`runtime/`](runtime/) → [`core/session/`](core/session/) → [`tools/tool_registry.py`](tools/tool_registry.py) → [`evaluation/agent_eval/`](evaluation/agent_eval/) |

完整导航：[入门与部署](docs/deployment.md) · [Architecture](docs/architecture.md) · [Runtime](docs/design/agent-runtime.md) · [Memory](docs/reference/memory-engineering-positioning-20261124.md) · [Tool governance](docs/design/tool-execution-boundaries.md) · [Security](docs/design/security.md) · [Evaluation](docs/evaluation.md) · [Operations](docs/operations/distributed-runtime-runbook.md) · [Troubleshooting](docs/operations/) · [Limitations](docs/limitations.md) · [Contributing](CONTRIBUTING.md)。

## 限制与数据来源

- 本仓库是独立维护的工程参考实现；不代表任职期间部署、企业客户验收或真实生产流量。
- 示例业务/商品/协议目录使用合成或明确授权可公开数据；数据来源、许可与例外见 [DATA_PROVENANCE.md](DATA_PROVENANCE.md)。
- 实际 ERP 写操作、生产多副本与容量、真实 provider 质量、独立人工金标、坐席通知/SLA 和历史会话 provenance 尚未全部验收；分项状态见 [Production Readiness](docs/production-readiness.md)。
- Run delivery 是 at-least-once；本地 CAS/ledger 不代表外部系统 exactly-once。MCP/A2A 不是安全边界；工业集成须使用批准目录、TLS 信任与凭证。

持续任务以 [GitHub Issues](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues) 为准；稳定版历史以 [GitHub Releases](https://github.com/Xander-Xai/Customer-Service-AI-Agent/releases) 与 tag 为准。当前最新公开 release 为 `v1.0.1`；Runtime version 与 Release/API version 独立管理。

## License

[Apache License 2.0](LICENSE)。
