# 客服 AI Agent 项目生产准备度检查清单

> 本清单于 2026-09-30 重新审计，2026-10-01 复核。Runtime version 仍为 v6.3；下方旧日期/旧数字均为历史快照，不能替代本次执行结果。
> 口径：本清单只记录**要执行的 gate 与要复核的项**；动态 PASS/计数结果不在此写死，
> 进入带日期的 audit 报告（`docs/reports/audit/**`）。测试数量一律以
> `pytest --collect-only -q` / `npm test` 当前输出为准。
>
> **证据分级（每项必须能对应到其中一级，禁止把低级别冒充高级别）**：
> `IMPLEMENTED`（代码存在）→ `LOCALLY VERIFIED`（本地命令通过）→
> `CI VERIFIED`（CI lane 通过）→ `CONTROLLED STAGING`（受控 provider 调用）→
> `PRODUCTION NOT_VERIFIED`（无生产 artifact）。
> **当前 provider auth / provider billing / 生产延迟 / 真实 Redis / 真实 ERP /
> 正式 649-query RAG metrics 均为 PRODUCTION NOT_VERIFIED（无 artifact）。**

## 0. Current HEAD gates

- [ ] `scripts/audit_doc_consistency.py` 通过
- [ ] `python3 scripts/project_facts.py --check docs/reference/current-state.md` 通过
- [ ] `python3 scripts/generate_openapi.py --check` 通过
- [ ] Tool Result feature flags、rollback switches、Redis store/TTL 已验证
- [ ] Tool Result scope isolation、cache safety 已验证
- [ ] BM25 lifecycle restart、Qdrant point-id migration dry-run/rollback 已验证
- [ ] **LangGraph checkpoint 持久化**：生产 `LANGGRAPH_CHECKPOINT_BACKEND=postgres`、
      官方 saver `setup()` 成功、连接失败 fail closed（本地/受控环境验证；真实多副本
      恢复仍属 PRODUCTION NOT_VERIFIED）
- [ ] **分布式 Agent Runtime**：API/Worker 双进程、`POST /api/runs` → 202 →
      worker 消费 → SUCCEEDED → GET 结果（本地/集成验证；目标环境
      `docker compose up` 双进程行为仍属 PRODUCTION NOT_VERIFIED）
- [ ] **RAG evidence pipeline preflight 已通过**（`make rag-eval-import` →
      `make rag-eval-649-preflight`；当前状态 NOT_VERIFIED——已提交的 preflight
      artifact 显示 provider auth blocker，见
      [docs/reference/rag-evaluation.md](../reference/rag-evaluation.md) §3.4；
      smoke run 不是正式证据）
- [ ] production evidence harness 已生成脱敏且带 provenance 的 artifact
- [ ] provider auth/staging 已验证；HTTP 401、token usage/billing unavailable 仍是 **NOT_VERIFIED**

## 1. 结构性实现事实（IMPLEMENTED / LOCALLY VERIFIED；不构成 production validation）

- [x] 前端单测可运行：`npm test`（当前 7 个 Vitest 测试文件；通过数以命令输出为准）
- [x] 前端生产构建通过：`npm run build`
- [x] OpenAPI 当前可正常生成：`app.openapi()` = `62` 个 HTTP 路径（校验命令 `make openapi-check`；v6.1.1 新增 `/api/cache/invalidate`、`/api/chat/multimodal`；异步 Run API 新增 `/api/runs` 等 5 条 + HITL 审批 `/api/approvals` 4 条）
- [x] 前后端上传约束已对齐：统一 `5MB` 上限
- [x] 前后端图片白名单已对齐：`JPEG/PNG/WebP`
- [x] `/api/chat/voice` 已修复 MIME 传递错误，非法音频改为 4xx/5xx 显式返回，而不是误把 `filename` 当 `content_type`
- [x] Widget 已保存 `session_id/session_token`，同一标签页内支持多轮连续对话
- [x] 管理后台已接入 `/api/circuit-breaker` 和 `/metrics/prometheus`
- [x] 生产模式下 Alembic 迁移失败将阻断启动，不再静默回退 `create_all`
- [x] 统一多模态入口 `/api/chat/multimodal` 已支持自动文件类型路由（voice/image/document）
- [x] Prometheus 指标已修复重复注册问题，使用 `not_started_unless_registered` 安全注册
- [x] 三级缓存架构重构完成：L1 Redis + L2 Qdrant + L3 Jaccard 回退
- [x] 后端已有 Knowledge Base 5000 条文档（成分数据 1500+ / FAQ 2500+ / 场景文档 1000+）
- [x] LLM 客户端已实现指数退避 + 全抖动重试策略
- [x] `POST /api/cache/invalidate` 端点已实现主动缓存失效
- [x] OpenAPI 重新生成：`docs/openapi.json` = 62 个 HTTP 路径（56 个 `/api/*` + `/metrics/prometheus` + 5 个页面路径；操作数 64，快照由 `scripts/generate_openapi.py` 生成）
- [x] admin.html 版本号同步至 v6.3
- [x] _revoked_jtis 已吊销 JTI 集合新增容量限制 10000，防止内存泄漏
- [x] mypy 已从 requirements.txt 移除，移到 requirements-dev.txt
- [x] config 启动校验新增 4 项生产环境警告（SESSION_STORAGE_BACKEND/ERP_MODE/SESSION_ENCRYPTION_KEY）
- [x] web/index.html 版本号同步至 v6.3
- [x] README 测试计数/Agent 表/缓存架构全部按代码回填
- [x] Widget file-type header aligned: frontend sends `file-type` (hyphen) matching FastAPI Header() param conversion (v6.3)
- [x] Widget session continuity: sessionId/sessionToken tracked from API responses (v6.3)
- [x] Widget SSE parsing aligned with backend (\n\n delimiters) (v6.3)
- [x] CSP frame-ancestors allows widget iframe embedding (frame-ancestors 'self') (v6.3)
- [x] Widget DOMPurify XSS protection added (本地 `/static/vendor/dompurify.min.js`) (v6.3)
- [x] Token refresh race condition fixed (single-flight promise dedup) (v6.3)
- [x] Redis rate-limit DoS fixed (three-state return: allow/deny/unavailable) (v6.3)
- [x] sendVoiceForm/sendTTS X-API-Key header added (v6.3)
- [x] ERP sync confirmation dialog added (v6.3)
- [x] submitFeedback deduplicated (alias to submitRating) (v6.3)
- [x] getSessions pagination support (offset/limit) (v6.3)
- [x] `docs/design/architecture-design.md` — ChromaDB 遗留模式引用已移除，新增 BM25Retriever/api_embedding/混合检索说明 (v6.3，历史修复记录)
- [x] `docs/design/security.md` — 修复已删除的 `api/middleware.py` 文件引用到 `api/middleware/__init__.py` (v6.3，历史修复记录)
- [x] `.env.example` — 废弃的 CACHE_L1_MAX/CACHE_L2_MAX 已移除 (v6.3，历史修复记录)
- [x] `docs/reports/releases/release-notes-v6.0.md` — ChromaDB 遗留模式和文件引用已更新 (v6.3，历史修复记录)
- [x] `docs/operations/production-operations-guide.md` — ChromaDB 引用和废弃缓存参数已清理 (v6.3，历史修复记录)
- [x] RAG evidence pipeline 就绪：`scripts/evaluate_rag.py`（4-config ablation、
      multi-K 指标、实测 stage latency、failure taxonomy、provenance）、
      `scripts/import_eval_corpus.py`（幂等导入 + manifest）、Make 目标
      `rag-eval-import/-preflight/-smoke/rag-eval-649`；提交有 preflight v1
      证据与 import manifest (PR #19)
- [x] LangGraph Checkpoint 后端生命周期已实现（`core/checkpointer.py`）：
      开发/测试 MemorySaver、生产官方 `langgraph-checkpoint-postgres`
      (`AsyncPostgresSaver` + psycopg 异步连接池)；生产初始化失败 fail closed，
      不静默回退；`/api/health` 暴露脱敏 `langgraph_checkpoint` 状态
      （本地单测 + 可选真实 PostgreSQL 集成测试；真实多副本恢复仍 PRODUCTION NOT_VERIFIED）
- [x] 分布式 Agent Runtime 已实现（`runtime/`）：`AgentRun`（PostgreSQL 真相源）+
      Celery worker + `/api/runs` 异步 API；队列只传 `run_id`；状态机与原子迁移；
      失败重试/DEAD、取消、broker 失败 503；`AGENT_RUN_DISPATCH=inline` 开发 fallback
      （本地单测 + 集成测试；Kubernetes / 真实生产 HA 未验证）
- [x] Run 可靠性已实现：同一 thread 串行（Redis 分布式锁 owner+TTL+Lua
      compare-and-delete，竞争 QUEUED 延迟重调度）、不同 thread 并行；HTTP
      `Idempotency-Key`（user+endpoint 作用域）+ DB 唯一约束；worker lease/heartbeat
      防重复投递；写操作工具 `tool_side_effects` 应用层幂等（崩溃重投不重复退款）；
      transient 指数退避 retry / permanent 不 retry / attempts 用尽 DEAD；
      `GET /api/runs/dead` 管理员观测；投递语义为 at-least-once + application-level
      idempotency（本地单测 + 集成测试 + 真实 Redis 锁；**不声称端到端 exactly-once**）
- [x] 跨进程 Agent 事件流已实现（`runtime/events.py` / `event_stream.py` /
      `event_publisher.py`）：worker 把 graph `stream_callback` 事件写入 Redis
      Streams（XADD MAXLEN + TTL + 大小/敏感字段限制），API
      `GET /api/runs/{id}/stream` 转 SSE，支持 `Last-Event-ID` 续读；Stream 过期后
      回退 PostgreSQL 终态；SSE 校验 run ownership；legacy `/api/chat/stream` 保持
      兼容（本地单测 + 真实 Redis Streams 集成测试；生产 HA 未验证）
- [x] 分布式 runtime 故障注入/恢复测试套件（`tests/integration/distributed_runtime/`）：
  worker crash→checkpoint 恢复（含跨 worker PostgreSQL）、API restart 不丢 run、
  duplicate delivery 不重复执行、same/different thread、side-effect 崩溃重放去重、
  SSE reconnect、可观测性（trace/metrics/不泄露 query）。验证报告：
  [2026-10-02 分布式 runtime 验证](../reports/audit/2026-10-02-distributed-runtime-verification.md)
  （`LOCALLY VERIFIED`；容器级/K8s/生产 HA 仍 `NOT_VERIFIED`）
- [x] MCP 外部工具适配已实现（`tools/mcp_adapter.py`，叠加在现有 `ToolRegistry`，
      不替代 native Function Calling）：discover / schema 归一化 / invoke / timeout /
      错误映射；`MCP_ENABLED`（默认 false）、`MCP_SERVERS` allowlist、transport 白名单、
      payload 上限、只读优先；`mcp_tool_call_total`/`mcp_tool_error_total`/
      `mcp_tool_duration_seconds`；本地单测 + 真实 MCP stdio server 集成测试
      （真实第三方 MCP server / 生产连通性 `NOT_VERIFIED`）
- [x] Human-in-the-loop 高风险操作人工审批已实现（`core/hitl/` + `human_approvals`）：
  仅 HIGH 风险 Tool（退款/改单/高额赔付/投诉升级/ERP 写操作）拦截，普通问答不审批；
  LangGraph `interrupt()` + PostgreSQL Checkpointer 暂停（`AgentRun=WAITING_APPROVAL`），
  RBAC 审批 API（supervisor/admin，不能自审）approve/reject/edit 后
  `Command(resume=...)` 恢复；approve 后仍经 Tool idempotency；本地单测 + 真实
  interrupt/Command 集成测试（真实多副本跨进程 resume `NOT_VERIFIED`）
- [x] Kubernetes 最小部署清单已提供（`deploy/k8s/`）：API 与 worker 独立
  Deployment/Service/HPA、Ingress（SSE/WS 注解）、ConfigMap + Secret 示例（仅占位符）、
  演示依赖（PG/Redis/Qdrant，单副本 emptyDir）；状态全部外置，无本地 PVC/hostPath；
  清单结构本地校验（`tests/unit/test_k8s_manifests.py`）；worker 队列深度
  custom-metric autoscaling 明确为后续项（未引入 KEDA/Prometheus Adapter）

## 2. 当前仍不能直接宣称"真实上线就绪"的项目

- [ ] 真实生产密钥、域名、证书、外部依赖连通性尚未按生产环境复核
- [ ] `tests/unit/test_api_routes.py` 这类大文件当前仍不应直接等同于"后端全量验收完成"
- [ ] 健康检查、监控、Qdrant、Redis、PostgreSQL 的联机状态尚未在真实部署环境下复核
- [ ] `.env.test`、README、历史验收报告中仍保留大量开发/历史口径，不能替代真实发布验收
- [ ] 历史遗留 tracked+ignored 文件已按 2026-09-30 审计处理：`secrets/keys.json`、
      `tests/data/csai.db-shm`、`tests/data/csai.db-wal` 已 `git rm --cached`
      从跟踪中移除（本地文件保留）；guard `scripts/audit_doc_consistency.py`
      的 hygiene check 会拦截再次混入
- [ ] CSRF 保护在生产模式下（`DEV_MODE=false`）依赖 Bearer Token 跳过校验，登录页等无 Bearer Token 的 POST 请求需验证 CSRF 兼容性
- [ ] 生产部署环境的本地配置文件若含真实密钥，存在意外泄露风险——使用
      `scripts/check_secrets.py` 检查当前树；真实密钥的管理遵循
      [docs/security/public-repository-secret-policy.md](../security/public-repository-secret-policy.md)
      （本项描述部署环境风险，不指向任何具体机器状态）
- [ ] 生产配置文件 `.env.prod` 中 TLS 为关闭状态，需要配置真实证书路径
- [ ] 生产配置文件 `.env.prod` 中 ERP 模式为 mock，需要连接真实金蝶 API
- [ ] CORS_ORIGINS 在生产环境未配置时将导致前端跨域请求失败
- [ ] 仍无真实生产密钥/域名/证书的验证记录
- [ ] Qdrant、Redis、PostgreSQL 尚未在真实部署环境下验证
- [ ] LangGraph checkpoint 的多副本共享/进程重启恢复尚未在真实生产环境验证
      （本地/受控 PostgreSQL 集成测试通过，但不等于生产验证）
- [ ] 分布式 Agent Runtime 的 API/Worker 双进程行为、Celery broker 故障恢复、
      worker 崩溃重投递尚未在目标环境验证；未宣称 Kubernetes / 生产 HA
- [ ] 端到端 exactly-once 未实现（仅 at-least-once + 应用层幂等）；远程副作用成功与
      本地记录提交之间的极小窗口未在生产验证
- [ ] MCP 真实第三方 server（SSE/远程鉴权）与写操作 MCP 工具幂等未验证；
      当前仅本地真实 stdio server 集成测试（`NOT_VERIFIED`）
- [ ] HITL 真实多副本 PostgreSQL 跨进程「第二天审批 resume」未验证（本地用
      MemorySaver 模拟 graph 重启；PG gated 测试仅覆盖 checkpoint 跨进程）
- [ ] Kubernetes 清单未在真实集群验证（`kubectl apply` / HPA / Ingress SSE/WS /
      `kubectl delete pod` 故障演练均 `NOT_VERIFIED`）；镜像未发布到 registry
- [ ] `rag/api_embedding.py` 的 embedding API Key 复用 `OPENAI_API_KEY`，生产环境应配置独立视角的 embedding 服务
- [ ] CORS_ORIGINS 为空时已在生产启动校验中阻断启动

## 3. 上线前必须逐项补齐

- [ ] 用生产环境真实密钥替换所有占位符，并确认不再把任何真实密钥提交到仓库
- [ ] 评测/重排/embedding 提供商凭据就绪后执行 `make rag-eval-649` 产生正式
      RAG evidence artifact（正式指标当前 NOT_VERIFIED；历史 30-query 快照不作当前证据）
- [ ] 为 Widget 选择不泄露凭据的接入方式；当前 `widget.html?api_key=...` 仍更适合作为演示/受控内网场景，而不是公开分发方案
- [ ] 在目标环境执行后端完整验证：单测、关键集成测试、健康检查、登录、聊天、会话、监控
- [ ] 确认 `VECTOR_DB_MODE`、`DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`、`SESSION_TOKEN_SECRET`、`API_KEY`、`MONITORING_ADMIN_TOKEN` 已按生产值配置
      （`EMBEDDING_API_KEY` / `RERANKER_API_KEY` 可独立配置；向量化与重排凭据必须实际可用
      ——embedding 未单独配置时会复用 `LLM` 的 `OPENAI_API_KEY`，见 `core/config.py`）
- [ ] 确认 `LANGGRAPH_CHECKPOINT_BACKEND=postgres` 且 checkpoint 表
      （由官方 saver `setup()` 创建）在目标 PostgreSQL 上初始化成功；健康检查
      `langgraph_checkpoint.status=healthy`
- [ ] 验证 `docker compose up` 后 API 与 Worker 为两个独立进程：
      `POST /api/runs` 返回 202 → worker 执行 → `GET /api/runs/{id}` 为 SUCCEEDED；
      停止 API 容器不影响 worker 中已开始的 run
- [ ] 验证跨进程事件流：`GET /api/runs/{id}/stream` 在 API/Worker 分进程下收到
      worker 事件；断线重连 `Last-Event-ID` 续读；Stream 过期后仍能从
      `GET /api/runs/{id}` 读到最终结果（目标环境验证；本地已用真实 Redis 验证）
- [ ] 验证 `/api/health`、`/api/metrics`、`/api/kpi`、`/metrics/prometheus` 在目标环境下的权限和返回格式
- [ ] 验证上传链路在 Nginx / 反向代理 / FastAPI 三级限制下仍保持一致
- [ ] 复核备份、恢复、告警路由和通知通道，而不是只看文档说明

## 4. 推荐验收顺序

1. `npm test`（数量以输出为准）
2. `npm run build`
3. `pytest tests/unit/test_app_factory.py tests/unit/test_ws_coverage.py tests/unit/test_auth_tools_coverage.py -q --maxfail=1`
4. `python3 scripts/audit_doc_consistency.py` + `python3 scripts/project_facts.py --check docs/reference/current-state.md`
5. 目标环境启动后验证 `/api/health`
6. 手工验证登录、聊天、会话切换、文件上传、管理后台
7. 目标环境验证 Redis / PostgreSQL / Qdrant / Prometheus / Grafana / Alertmanager
8. （凭据就绪时）`make rag-eval-import` → `make rag-eval-649-preflight` → `make rag-eval-649`

## 5. 相关文档

- [README.md](../../README.md)
- [当前事实入口](../reference/current-state.md)
- [RAG 评估方案](../reference/rag-evaluation.md)
- [生产证据边界](../evaluation/production-evidence.md)
- [API 参考](../reference/api-reference.md)
- [生产运维手册](../operations/production-operations-guide.md)
- [分布式 Runtime 运维 Runbook](../operations/distributed-runtime-runbook.md)
- [分布式 Agent Runtime 设计](../design/distributed-agent-runtime.md)
- [2026-09-30 文档收敛审计 v3](../reports/audit/2026-09-30-documentation-convergence-v3.md)
- [2026-09-29 全仓对齐审计](../reports/plans/2026-09-29-code-doc-alignment.md)（历史快照）
