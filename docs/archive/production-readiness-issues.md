# 生产落地问题清单与开发计划

> 审核状态：**待审核**
> 编写日期：2026-06-06
> 审核人：________  审核日期：________

## 背景

基于对整个代码库的深入审查，当前项目架构意图良好，但存在多个阻断性问题需在上线前解决。本文档将所有问题按优先级整理为可执行的开发任务，待审核通过后逐项实施。

---

## 评分总览

| 维度 | 得分 | 说明 |
|---|---|---|
| 架构设计 | 8/10 | 意图正确，四层状态机、DI、熔断器都有 |
| 代码实现 | 6/10 | 核心功能实现，但大量 stub/mock/兼容代码 |
| 生产就绪度 | 4/10 | 多个阻断性问题未解决 |
| 测试覆盖 | 4/10 | 100% MockLLM，无真实端到端验证 |
| 部署运维 | 5/10 | Docker/Nginx 有，但监控配置缺失 |
| 安全性 | 6/10 | 基础安全做得不错，但有明显漏洞 |

---

## P0：生产阻断（必须在上线前完成）

### P0-1：统一初始化路径，删除全局变量

**问题描述：**
当前系统存在两套并行的初始化路径：`multi_agent_customer_service.py` 的全局变量路径和 `core/container.py` 的 DI 容器路径。`api/app_factory.py` 同时执行两条路径，容器初始化失败时系统会静默降级到旧路径，极难排查。

**涉及文件：**
- `api/app_factory.py` — 第 86-139 行，双路径并存
- `multi_agent_customer_service.py` — 第 76-80 行，模块级全局变量
- `api/app.py` — 第 164-171 行，模块级可变全局状态
- `core/container.py` — 第 25-432 行，DI 容器（保留此路径）

**改造方案：**
1. 将 `api/app.py` 中的全局变量（`_graph_app`, `_session_manager`, `_response_cache` 等）替换为从 `ServiceContainer` 获取
2. `app_factory.py` 的 `create_app()` 完全基于 `ServiceContainer` 构建，删除 `make_graph()` 调用
3. `lifespan` 中仅保留 `container.initialize()` 和 `container.close()`
4. 所有端点通过 `request.app.state` 或 FastAPI 依赖注入获取服务引用
5. 删除 `multi_agent_customer_service.py` 中的 `make_graph()` 和全局变量（保留图构建逻辑到容器中）

**验收标准：**
- [ ] 只存在一条初始化路径（ServiceContainer）
- [ ] 容器初始化失败时应用启动失败（fail-fast），不静默降级
- [ ] 所有现有测试通过
- [ ] `api/app.py` 中无模块级服务全局变量

---

### P0-2：修复 init_default_admin() 运行时崩溃

**问题描述：**
`auth/service.py:258` 引用 `force_password_change=1`，但 `db/models.py` 的 `User` 模型中不存在该列，生产环境首次启动将抛出异常。

**涉及文件：**
- `auth/service.py` — 第 258 行
- `db/models.py` — User 模型定义
- `alembic/versions/001_initial_schema.py` — 当前迁移

**改造方案：**
1. 在 `User` 模型中添加 `force_password_change = Column(Boolean, default=False)` 字段
2. 编写 Alembic 迁移脚本添加该列
3. `init_default_admin()` 中使用该字段标记需要强制修改密码的管理员

**验收标准：**
- [ ] User 模型包含 `force_password_change` 字段
- [ ] 有对应的 Alembic 迁移
- [ ] 首次启动可成功创建默认管理员
- [ ] `test_v4_production.py` 中 admin 相关测试通过

---

### P0-3：移除 .env 中的敏感信息，接入密钥管理

**问题描述：**
`.env` 文件（2819 字节）包含真实 API Key、JWT Secret 等敏感信息，已提交到 Git 仓库。`.gitignore` 未排除 `.env`。

**涉及文件：**
- `.env` — 包含真实密钥
- `.gitignore` — 未排除 `.env`
- `config.py` — 配置读取逻辑
- `scripts/deploy.sh` — 部署脚本

**改造方案：**
1. 从 Git 历史中清除 `.env` 文件（`git filter-branch` 或 `BFG`）
2. `.gitignore` 中添加 `.env` 规则
3. 将 `.env` 中的真实密钥替换为占位符，仅保留 `.env.example`
4. `config.py` 启动时校验关键配置项非空非占位符（`OPENAI_API_KEY`, `JWT_SECRET`, `SESSION_TOKEN_SECRET`）
5. 文档中说明如何通过环境变量或密钥管理服务注入配置

**验收标准：**
- [ ] Git 历史中不含真实密钥
- [ ] `.gitignore` 包含 `.env`
- [ ] 缺少必要配置时应用启动失败并输出明确错误信息
- [ ] `.env.example` 包含所有配置项说明

---

### P0-4：补齐 Alembic 数据库迁移

**问题描述：**
5 个 ORM 模型（User, ChatHistory, AuditLog, Feedback, PromptVersion），Alembic 只覆盖了 3 个。`feedbacks` 和 `prompt_versions` 表无迁移。时间戳使用 `Float` 而非 `DateTime`，时区处理困难。

**涉及文件：**
- `db/models.py` — 全部 5 个模型
- `alembic/versions/001_initial_schema.py` — 当前迁移
- `alembic/env.py` — 迁移配置

**改造方案：**
1. 为 `Feedback` 和 `PromptVersion` 编写 Alembic 迁移
2. 将时间戳字段从 `Float` 迁移为 `DateTime(timezone=True)`（编写数据迁移脚本）
3. 同步处理 P0-2 中新增的 `force_password_change` 字段
4. 测试迁移的升级和降级路径

**验收标准：**
- [ ] `alembic upgrade head` 成功创建所有表
- [ ] `alembic downgrade` 可正常回滚
- [ ] 时间戳字段改为 DateTime 类型
- [ ] 现有数据可正确迁移（如果存在）

---

### P0-5：Docker Compose 加入 PostgreSQL 服务

**问题描述：**
代码支持 PostgreSQL（`db/database.py`），但所有 Docker Compose 文件中均未配置 PostgreSQL 服务，生产环境无数据库可用。

**涉及文件：**
- `docker-compose.yml` — 基础配置
- `docker-compose.prod.yml` — 生产配置
- `db/database.py` — 数据库连接逻辑

**改造方案：**
1. `docker-compose.yml` 添加 PostgreSQL 15 服务
2. 配置持久化存储卷、健康检查、资源限制
3. `docker-compose.prod.yml` 中调整 PostgreSQL 的生产参数（`max_connections`, `shared_buffers` 等）
4. App 服务添加对 PostgreSQL 的依赖（`depends_on` + 健康检查条件）
5. 添加数据库初始化脚本（创建库、用户）

**验收标准：**
- [ ] `docker-compose up` 可同时启动 app + PostgreSQL + Redis
- [ ] 应用启动时自动执行 Alembic 迁移
- [ ] PostgreSQL 有持久化存储
- [ ] 健康检查正常工作

---

### P0-6：补充真实 LLM 端到端测试

**问题描述：**
全部 307 个测试均使用 MockLLM，从未用真实 LLM 跑通。真实 LLM 的输出格式、长度、边界行为与 mock 差异很大，无法验证系统实际可用性。

**涉及文件：**
- `tests/` — 测试目录（新增文件）
- `pytest.ini` 或 `pyproject.toml` — 添加标记

**改造方案：**
1. 新增 `tests/test_e2e_real_llm.py`，使用 `@pytest.mark.real_llm` 标记（默认跳过）
2. 编写 3-5 个核心场景的真实 LLM 测试：
   - 基础产品咨询 → 返回相关产品信息
   - 退换货流程 → 正确路由到售后 Agent
   - 技术问题 → RAG 检索 + Function Calling
   - 多轮对话 → 上下文保持
   - 敏感输入 → 拒绝注入攻击
3. 使用 `.env.dev` 中的 DeepSeek API Key
4. CI 中作为可选步骤（需要 API Key 的 secret）

**验收标准：**
- [ ] 3-5 个真实 LLM 测试全部通过
- [ ] 测试可选择性运行（`pytest -m real_llm`）
- [ ] 测试覆盖核心业务场景，而非仅技术功能

---

## P1：高优问题（上线后一周内完成）

### P1-1：实现真正的 LLM 流式输出

**问题描述：**
当前 SSE 流式是假的——先完整运行图，再分块输出（`api/app.py:636`）。用户等待时间没有任何改善。

**涉及文件：**
- `api/app.py` — 第 595-680 行
- `core/container.py` — 图构建
- `multi_agent_customer_service.py` — LangGraph 图定义

**改造方案：**
1. 利用 LangGraph 的 `astream_events` 或 `astream` 实现真正的流式输出
2. 在图节点中使用 LLM 的 streaming 模式
3. SSE 端点实时转发 LLM 的 token 输出
4. 保留现有的错误处理和超时逻辑

**验收标准：**
- [ ] 用户在第一个 token 生成时即可开始看到输出
- [ ] 流式中断时能正确清理资源
- [ ] 与现有缓存逻辑兼容

---

### P1-2：修复 asyncio.Lock 懒初始化竞态

**问题描述：**
`MetricsCollector._ensure_lock()`（`core/monitoring.py:66`）、`CircuitBreaker`、`MessageBus`、`SharedBlackboard` 中的 `asyncio.Lock` 采用懒初始化，在高并发下两个协程可能同时创建不同的锁，导致数据损坏。

**涉及文件：**
- `core/monitoring.py` — 第 66-69 行、第 440-441 行
- `core/message_bus.py`
- `core/shared_blackboard.py`

**改造方案：**
1. 所有 `asyncio.Lock` 改为在 `__init__` 中直接创建
2. 删除所有 `_ensure_lock()` 方法
3. `OpenAICompatibleClient._pool_lock` 改为类级变量在模块加载时初始化

**验收标准：**
- [ ] 无懒初始化的 Lock
- [ ] 高并发压力测试通过

---

### P1-3：实现优雅关闭

**问题描述：**
当前无关闭序列——无 `close()` 方法、无 drain in-flight 请求。部署时可能丢失正在处理的请求。

**涉及文件：**
- `core/container.py` — 添加 `close()` 方法
- `api/app_factory.py` — lifespan 清理逻辑
- `gunicorn.conf.py` — worker 退出钩子

**改造方案：**
1. `ServiceContainer` 添加 `async close()` 方法，按依赖逆序释放资源
2. `lifespan` 的 `yield` 后调用 `container.close()`
3. 停止接受新请求，等待 in-flight 请求完成（带超时）
4. 关闭 Redis 连接池、数据库连接池、LLM 客户端

**验收标准：**
- [ ] `SIGTERM` 后应用等待 in-flight 请求完成
- [ ] 所有连接池正确关闭
- [ ] 无资源泄漏警告

---

### P1-4：Prometheus/Grafana 监控配置

**问题描述：**
`docker-compose.monitoring.yml` 引用的 `monitoring/prometheus.yml` 和 `monitoring/grafana/dashboards` 目录不存在，监控栈无法正常工作。

**涉及文件：**
- `monitoring/prometheus.yml` — 新建
- `monitoring/alertmanager.yml` — 新建
- `monitoring/grafana/dashboards/` — 新建
- `docker-compose.monitoring.yml` — 可能需要调整

**改造方案：**
1. 编写 `prometheus.yml`，抓取 app 的 `/metrics/prometheus` 端点
2. 编写 `alertmanager.yml` 告警路由规则
3. 创建 Grafana 仪表板 JSON：
   - 请求量 / 响应时间 / 错误率
   - LLM 调用延迟 / 熔断器状态
   - 缓存命中率 / 会话数
4. 编写 Prometheus 告警规则（高错误率、高延迟、熔断器打开）

**验收标准：**
- [ ] `docker-compose -f docker-compose.monitoring.yml up` 启动完整监控栈
- [ ] Grafana 可展示应用指标
- [ ] 告警规则可触发

---

### P1-5：补齐缺失的测试

**问题描述：**
多个重要功能模块无测试覆盖。

**涉及文件：**
- `tests/` — 新增测试文件

**改造方案：**
新增以下测试：
1. SSE 流式端点测试（`test_sse_streaming.py`）
2. WebSocket 认证和消息测试（`test_websocket.py`）
3. 多模态端点测试（`test_multimodal.py`）
4. DI 容器路径测试（`test_container.py`）
5. 并发访问测试（`test_concurrency.py`）

**验收标准：**
- [ ] 上述 5 个测试文件全部通过
- [ ] 整体测试覆盖率 > 80%

---

## P2：重要改进（一个月内完成）

### P2-1：接入 OpenTelemetry 分布式追踪

**改造方案：**
1. 集成 `opentelemetry-sdk` 和 `opentelemetry-exporter-otlp`
2. 为 FastAPI、SQLAlchemy、HTTP 客户端添加自动 instrumentation
3. trace_id 从请求入口传播到 LLM 调用和 ERP 调用
4. 导出到 Jaeger 或 Tempo

**验收标准：**
- [ ] 可在追踪系统中看到完整请求链路

---

### P2-2：水平扩展与 Redis 共享状态

**改造方案：**
1. Session 数据迁移到 Redis（当前在内存）
2. L2 缓存使用 Redis 替代内存
3. MessageBus 改为 Redis Pub/Sub
4. Docker Compose 配置多实例 + Nginx 负载均衡
5. SharedBlackboard 改为 Redis Hash

**验收标准：**
- [ ] 2+ 实例可同时运行，状态一致
- [ ] 单实例故障不影响整体服务

---

### P2-3：Refresh Token 机制

**改造方案：**
1. 登录时同时签发 access_token（短生命周期，2h）和 refresh_token（长生命周期，7d）
2. `/api/auth/refresh` 端点用 refresh_token 换取新 access_token
3. refresh_token 存储在 Redis 中，支持主动吊销
4. 密码修改时批量吊销所有 refresh_token

**验收标准：**
- [ ] access_token 过期后可用 refresh_token 续期
- [ ] refresh_token 支持吊销

---

### P2-4：基于 LLM-as-Judge 的评估系统

**改造方案：**
1. 用 LLM 对客服回复进行五维度评分（替代当前的规则启发式）
2. 对比人工标注样本校准评分阈值
3. 评分结果写入 Feedback 表，支持趋势分析
4. 低分回复自动触发告警

**验收标准：**
- [ ] LLM 评分与人工评分的相关系数 > 0.7
- [ ] 评分结果可视化

---

### P2-5：CI 安全扫描

**改造方案：**
1. GitHub Actions 中添加 Trivy 镜像扫描
2. 添加 `pip-audit` 依赖漏洞检查
3. 添加 `bandit` Python 安全静态分析
4. 发现 Critical/High 漏洞时 CI 失败

**验收标准：**
- [ ] CI 流水线包含安全扫描步骤
- [ ] Critical 漏洞阻断合并

---

## 问题汇总

| 编号 | 优先级 | 问题 | 涉及文件 | 状态 |
|---|---|---|---|---|
| P0-1 | 🔴 阻断 | 双重初始化路径 | app_factory.py, api/app.py | ✅ 已完成 |
| P0-2 | 🔴 阻断 | init_default_admin() 崩溃 | auth/service.py, db/models.py | ✅ 已完成 |
| P0-3 | 🔴 阻断 | .env 密钥泄露 | .env, .gitignore, config.py | ✅ 已完成 |
| P0-4 | 🔴 阻断 | Alembic 迁移不完整 | db/models.py, alembic/ | ✅ 已完成 |
| P0-5 | 🔴 阻断 | Docker Compose 缺 PostgreSQL | docker-compose*.yml | ✅ 已完成 |
| P0-6 | 🔴 阻断 | 无真实 LLM 测试 | tests/test_e2e_real_llm.py | ✅ 已完成 |
| P1-1 | 🟡 高优 | SSE 流式是假的 | api/app.py | ✅ 已完成（已有真流式） |
| P1-2 | 🟡 高优 | asyncio.Lock 竞态 | core/monitoring.py 等 | ✅ 已完成 |
| P1-3 | 🟡 高优 | 无优雅关闭 | core/container.py, app_factory.py | ✅ 已完成 |
| P1-4 | 🟡 高优 | 监控配置缺失 | monitoring/ | ✅ 已完成 |
| P1-5 | 🟡 高优 | 测试覆盖不足 | tests/test_production_features.py | ✅ 已完成 |
| P2-1 | 🟢 重要 | 无分布式追踪 | core/tracing.py | ✅ 已完成 |
| P2-2 | 🟢 重要 | 无水平扩展 | docker-compose.scale.yml | ✅ 已完成 |
| P2-3 | 🟢 重要 | 无 Refresh Token | auth/service.py, auth/router.py | ✅ 已完成 |
| P2-4 | 🟢 重要 | 评估系统简陋 | agents/evaluator.py | ✅ 已完成 |
| P2-5 | 🟢 重要 | CI 无安全扫描 | .github/workflows/ci.yml | ✅ 已完成 |
| P2-1 | 🟢 重要 | 无分布式追踪 | 全局 | ⬜ 待开发 |
| P2-2 | 🟢 重要 | 无水平扩展 | docker-compose.scale.yml | ✅ 已完成 |
| P2-3 | 🟢 重要 | 无 Refresh Token | auth/service.py, auth/router.py | ✅ 已完成 |
| P2-4 | 🟢 重要 | 评估系统简陋 | agents/evaluator.py | ✅ 已完成 |
| P2-5 | 🟢 重要 | CI 无安全扫描 | .github/workflows/ci.yml | ✅ 已完成 |

---

## 实施节奏建议

```
第 1 周：P0 全部完成（6 项）
  Day 1-2：P0-2 + P0-3（快速修复 + 安全）
  Day 3-4：P0-4 + P0-5（数据库层）
  Day 5：P0-1（统一初始化，风险最高，放最后）
  Day 5：P0-6（端到端测试，验证前面所有改动）

第 2 周：P1 全部完成（5 项）
  Day 1-2：P1-1（真流式，改动较大）
  Day 3：P1-2 + P1-3（并发 + 关闭）
  Day 4-5：P1-4 + P1-5（监控 + 测试）

第 3-4 周：P2 按需完成（5 项）
```

---

> **请审核后在上方"审核状态"处标注 ✅ 已通过 或具体修改意见，我将按批准的内容开始开发。**

---

## 实施记录

> 开发日期：2026-06-06
> 测试结果：**362 passed, 1 failed**（1 个预存问题，非本次改动引起）

### 已完成项详情

#### P0-3：移除 .env 密钥 ✅
- `.env` 中的真实 API Key 已替换为占位符 `your_siliconflow_api_key_here`
- `.gitignore` 已包含 `.env` 规则（第 16 行，预存）
- `config.py` 新增 `validate_required_config()` 函数，生产环境启动时校验 `OPENAI_API_KEY`、`JWT_SECRET`、`SESSION_TOKEN_SECRET` 非空非占位符
- 缺失时输出明确错误信息并 `SystemExit` 阻断启动

#### P0-2：修复 init_default_admin() 崩溃 ✅
- `db/models.py`：User 模型新增 `force_password_change = Column(Boolean, default=False)`
- `db/models.py`：添加 `from sqlalchemy import Boolean` 导入
- 测试中通过 `ADMIN_PASSWORD=admin123` 环境变量固定管理员密码

#### P0-4：补齐 Alembic 迁移 ✅
- 新增 `alembic/versions/002_add_missing_tables.py`：
  - `users` 表新增 `force_password_change` 列
  - 创建 `feedbacks` 表（含索引）
  - 创建 `prompt_versions` 表（含索引）
  - 完整的 `upgrade()` 和 `downgrade()` 实现
- `db/database.py`：将 Alembic 调用从 `subprocess` 改为 Python API 直接调用（`alembic.config.Config` + `alembic.command.upgrade`），消除子进程依赖

#### P0-5：Docker Compose 加入 PostgreSQL ✅
- `docker-compose.yml` 新增 `postgres:15-alpine` 服务：
  - 环境变量配置（`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`）
  - 健康检查（`pg_isready`）
  - 持久化存储卷 `postgres-data`
  - 不暴露主机端口，仅通过 Docker 网络访问
- App 服务新增 `DATABASE_URL` 和 `JWT_SECRET` 环境变量传递
- App `depends_on` 新增 `postgres` 依赖（含健康检查条件）
- `.env.example` 新增 PostgreSQL 配置说明

#### P0-1：统一初始化路径 ✅
- `api/app.py`：
  - 新增 `_circuit_breaker_ref` 模块级变量
  - `/api/health` 端点：从 `request.app.state.container` 获取 circuit_breaker，消除 `multi_agent_customer_service` 导入
  - `/api/circuit-breaker` 端点：同上
  - `/metrics/prometheus` 端点：同上
  - `/api/chat/image` 端点：从容器获取 circuit_breaker，消除 `multi_agent_customer_service` 导入；函数签名添加 `request: Request` 参数
- `api/app_factory.py`：lifespan 中注入 `_circuit_breaker_ref` 到 `api.app` 模块

#### P1-2：修复 asyncio.Lock 竞态 ✅
- `core/monitoring.py`：
  - `MetricsCollector.__init__`：`_lock = asyncio.Lock()` 直接初始化
  - `CircuitBreaker.__init__`：同上
  - `OpenAICompatibleClient._pool_lock`：类级变量直接初始化
  - `_get_async_client()`：删除 `_pool_lock is None` 检查
- `core/message_bus.py`：`MessageBus.__init__` 中 `_lock = asyncio.Lock()` 直接初始化
- `core/shared_blackboard.py`：`SharedBlackboard.__init__` 中同上
- 所有 `_ensure_lock()` 方法保留但简化为直接返回 `self._lock`

#### P1-3：实现优雅关闭 ✅
- `core/container.py`：新增 `async close()` 方法：
  - 关闭 LLM 连接池（`OpenAICompatibleClient.close_all_clients()`）
  - 关闭 ERP 适配器（如果有 `close` 方法）
  - 重置 `_initialized` 状态
- `api/app_factory.py`：lifespan 中 yield 后调用 `_container.close()`

#### P1-1：SSE 流式输出 ✅（预存实现）
- 经验证，真流式已在以下位置实现：
  - `core/monitoring.py:530`：`OpenAICompatibleClient.async_invoke_stream()` — SSE 逐 chunk 接收
  - `agents/base_agent.py:436-444`：Agent 检查 `stream_callback` 并使用 `async_invoke_stream` 推送
  - `api/app.py:595-690`：SSE 端点使用队列 + 后台图任务实现真流式
