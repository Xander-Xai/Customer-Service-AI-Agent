# 2026-06-23 代码与文档对齐记录

## 目标

基于当前仓库真实代码，完成一轮更彻底的前后端联调、上线阻碍排查和活文档回填。

## 本轮阅读范围

- 后端：`api/`、`auth/`、`core/`、`db/`、`knowledge/`、`alerts/`、`media/`
- 前端：`web/src/`、`web/widget.html`、`web/admin.html`
- 开发进度/现状材料：`docs/reports/plans/2026-06-22-code-doc-alignment.md`、`docs/reports/releases/changelog.md`
- 活文档：`README.md`、`docs/README.md`、`docs/reference/api-reference.md`、`docs/checklists/production-readiness-checklist.md`

## 本轮确认的问题

### 1. 语音上传链路和后端 STT 契约不一致

问题：
- `api/routes/chat_multimodal.py` 把 `audio.filename` 传给 `AudioProcessor.transcribe(...)`
- `AudioProcessor` 实际需要的是 MIME 类型，例如 `audio/wav`
- 结果是合法语音也会走错校验路径，且非法语音容易冒成 500

处理：
- 改为传递 `audio.content_type`
- `chat_with_voice()` 新增错误分层：非法输入 `400`、依赖缺失 `503`、其他异常 `500`
- 音频过大时返回的 `JSONResponse` 也显式短路，不再误入 `run_graph()`

### 2. Widget 没有保存后端返回的会话状态

问题：
- `web/widget.html` 调用了 `/api/chat` 和 `/api/chat/stream`
- 但没有保存 `session_id/session_token`
- 每次发送都像新对话，无法真正复用服务端上下文

处理：
- 新增 `sessionStorage` 持久化
- REST 和 SSE 都会回写 `session_id/session_token`
- 后续请求自动带上这两个字段

### 3. 后端已有能力未在管理后台呈现

问题：
- `/api/circuit-breaker` 和 `/metrics/prometheus` 已有后端实现
- 前端 `rest.js` 也已封装
- 但管理后台没有对应展示入口

处理：
- `web/admin.html` 新增熔断器详情卡片和 Prometheus 文本预览区域
- `web/src/admin-settings.js` 补齐加载逻辑

### 4. 数据库迁移失败仍会静默回退 `create_all`

问题：
- `db/database.py` 在 Alembic 失败时会直接 `Base.metadata.create_all()`
- 这在真实生产环境里会掩盖迁移失败，导致 schema 漂移

处理：
- 保留开发模式回退便利
- 在 `DEV_MODE=false` 下改为直接抛错阻断启动

## 本轮更新的 Markdown

- `README.md`
- `docs/README.md`
- `docs/reference/api-reference.md`
- `docs/checklists/production-readiness-checklist.md`
- `docs/reports/releases/changelog.md`
- 本文件

## 本轮验证

### 通过

- `npm test`
  - 56/56 通过
- `npm run build`
  - 通过
- `app.openapi()`
  - 当前导出 51 个 HTTP 路径

### 仍需说明

- `tests/unit/test_api_routes.py` 这类大文件不适合拿来直接宣称“后端全量验收通过”
- 真实部署环境下的 PostgreSQL / Redis / Qdrant 连通性，本轮未做在线验证
- Widget 当前虽然已修复会话连续性，但 `api_key` 仍主要通过 URL 参数注入，更适合作为演示/受控场景方案

## 当前结论

- 主聊天页、管理后台、Widget 与当前后端主要契约已经重新对齐
- 本轮修复了至少 4 个会直接影响真实联调或上线口径的问题
- 文档口径已从“复述历史结论”切换为“按当前 HEAD 事实表述”
