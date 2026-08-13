# 客服 AI Agent 项目生产准备度检查清单

> 最后复核：2026-06-27（v6.3 全量前后端联调 + Widget DOMPurify 本地化 + Embedding Key 独立 + CORS 加固 + 文档全面同步）
> 口径：只记录当前 HEAD 已验证的事实，不复述历史阶段报告里的”已完成”结论。

## 1. 当前已验证

- [x] 前端单测通过：`npm test` = 60/60（7 个测试文件）
- [x] 前端生产构建通过：`npm run build`
- [x] OpenAPI 当前可正常生成：`app.openapi()` = `53` 个 HTTP 路径（v6.1.1 新增 `/api/cache/invalidate`、`/api/chat/multimodal`）
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
- [x] OpenAPI 重新生成：`docs/openapi.json` = 53 个 HTTP 路径（48 API + 5 页面）
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
- [x] `docs/design/architecture-design.md` — ChromaDB 遗留模式引用已移除，新增 BM25Retriever/api_embedding/v7.0 混合检索说明 (v6.3)
- [x] `docs/design/security.md` — 修复 `api/middleware.py` 已删除文件引用到 `api/middleware/__init__.py` (v6.3)
- [x] `.env.example` — 废弃的 CACHE_L1_MAX/CACHE_L2_MAX 已移除 (v6.3)
- [x] `docs/reports/releases/release-notes-v6.0.md` — ChromaDB 遗留模式和文件引用已更新 (v6.3)
- [x] `docs/operations/production-operations-guide.md` — ChromaDB 引用和废弃缓存参数已清理 (v6.3)

## 2. 当前仍不能直接宣称“真实上线就绪”的项目

- [ ] 真实生产密钥、域名、证书、外部依赖连通性尚未按生产环境复核
- [ ] `tests/unit/test_api_routes.py` 这类大文件当前仍不应直接等同于“后端全量验收完成”
- [ ] 健康检查、监控、Qdrant、Redis、PostgreSQL 的联机状态尚未在真实部署环境下复核
- [ ] `.env.test`、README、历史验收报告中仍保留大量开发/历史口径，不能替代真实发布验收
- [ ] ~~仓库中存在 `secrets/keys.json` 这类运维台账文件~~ — 已添加到 .gitignore，下次提交后将从跟踪中移除
- [ ] CSRF 保护在生产模式下（`DEV_MODE=false`）依赖 Bearer Token 跳过校验，登录页等无 Bearer Token 的 POST 请求需验证 CSRF 兼容性
- [ ] `.env` 文件中仍存在明文 SiliconFlow API Key，虽然被 gitignore，但存在意外泄露风险
- [ ] 生产配置文件 `.env.prod` 中 TLS 为关闭状态，需要配置真实证书路径
- [ ] 生产配置文件 `.env.prod` 中 ERP 模式为 mock，需要连接真实金蝶 API
- [ ] CORS_ORIGINS 在生产环境未配置时将导致前端跨域请求失败
- [ ] 仍无真实生产密钥/域名/证书的验证记录
- [ ] Qdrant、Redis、PostgreSQL 尚未在真实部署环境下验证
- [ ] `rag/api_embedding.py` 的 embedding API Key 复用 `OPENAI_API_KEY`，生产环境应配置独立视角的 embedding 服务
- [ ] CORS_ORIGINS 为空时已在生产启动校验中阻断启动

## 3. 上线前必须逐项补齐

- [ ] 用生产环境真实密钥替换所有占位符，并确认不再把任何真实密钥提交到仓库
- [ ] 为 Widget 选择不泄露凭据的接入方式；当前 `widget.html?api_key=...` 仍更适合作为演示/受控内网场景，而不是公开分发方案
- [ ] 在目标环境执行后端完整验证：单测、关键集成测试、健康检查、登录、聊天、会话、监控
- [ ] 确认 `VECTOR_DB_MODE`、`DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`、`SESSION_TOKEN_SECRET`、`API_KEY`、`MONITORING_ADMIN_TOKEN` 已按生产值配置
- [ ] 验证 `/api/health`、`/api/metrics`、`/api/kpi`、`/metrics/prometheus` 在目标环境下的权限和返回格式
- [ ] 验证上传链路在 Nginx / 反向代理 / FastAPI 三级限制下仍保持一致
- [ ] 复核备份、恢复、告警路由和通知通道，而不是只看文档说明

## 4. 推荐验收顺序

1. `npm test`
2. `npm run build`
3. `pytest tests/unit/test_app_factory.py tests/unit/test_ws_coverage.py tests/unit/test_auth_tools_coverage.py -q --maxfail=1`
4. 目标环境启动后验证 `/api/health`
5. 手工验证登录、聊天、会话切换、文件上传、管理后台
6. 目标环境验证 Redis / PostgreSQL / Qdrant / Prometheus / Grafana / Alertmanager

## 5. 相关文档

- [README.md](../../README.md)
- [API 参考](../reference/api-reference.md)
- [生产运维手册](../operations/production-operations-guide.md)
- [本轮代码与文档对齐记录](../reports/plans/2026-06-25-v6.2-code-doc-alignment.md)
