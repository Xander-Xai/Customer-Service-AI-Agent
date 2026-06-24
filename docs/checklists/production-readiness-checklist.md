# 客服 AI Agent 项目生产准备度检查清单

> 最后复核：2026-06-23  
> 口径：只记录当前 HEAD 已验证的事实，不复述历史阶段报告里的“已完成”结论。

## 1. 当前已验证

- [x] 前端单测通过：`npm test` = 56/56
- [x] 前端生产构建通过：`npm run build`
- [x] OpenAPI 当前可正常生成：`app.openapi()` = `51` 个 HTTP 路径
- [x] 前后端上传约束已对齐：统一 `5MB` 上限
- [x] 前后端图片白名单已对齐：`JPEG/PNG/WebP`
- [x] `/api/chat/voice` 已修复 MIME 传递错误，非法音频改为 4xx/5xx 显式返回，而不是误把 `filename` 当 `content_type`
- [x] Widget 已保存 `session_id/session_token`，同一标签页内支持多轮连续对话
- [x] 管理后台已接入 `/api/circuit-breaker` 和 `/metrics/prometheus`
- [x] 生产模式下 Alembic 迁移失败将阻断启动，不再静默回退 `create_all`

## 2. 当前仍不能直接宣称“真实上线就绪”的项目

- [ ] 真实生产密钥、域名、证书、外部依赖连通性尚未按生产环境复核
- [ ] `tests/unit/test_api_routes.py` 这类大文件当前仍不应直接等同于“后端全量验收完成”
- [ ] 健康检查、监控、Qdrant、Redis、PostgreSQL 的联机状态尚未在真实部署环境下复核
- [ ] `.env.test`、README、历史验收报告中仍保留大量开发/历史口径，不能替代真实发布验收
- [ ] 仓库中存在 `secrets/keys.json` 这类运维台账文件，虽然当前不含明文密钥，但生产仓库仍应确认是否保留此类元数据

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
- [本轮代码与文档对齐记录](../reports/plans/2026-06-22-code-doc-alignment.md)
