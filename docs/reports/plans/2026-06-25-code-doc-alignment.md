# 2026-06-25 代码与文档对齐记录

## 目标

基于当前仓库真实代码（HEAD `e67f578`），完成又一轮前后端全量复核、文档更新和发现的问题修复。

## 本轮阅读范围

- 后端：`api/routes/` 全部 8 个路由文件、`core/config.py`
- 前端：`web/src/` 全部 JS 模块（`api/`、`chat/`、`state/`、`utils/`）、`web/src/admin*.js`
- 活文档：`README.md`、`docs/README.md`、`docs/reference/api-reference.md`、`docs/checklists/production-readiness-checklist.md`、`docs/reports/releases/changelog.md`
- 开发进度材料：`docs/reports/plans/2026-06-23-code-doc-alignment.md`

## 本轮发现的问题

### 1. API 参考文档与实际路由不一致

问题：
- `docs/reference/api-reference.md` 声明 "51 个 HTTP 路径"，实际 FastAPI 导出 53 个
- 缺失 2 个端点：`POST /api/cache/invalidate`、`POST /api/chat/multimodal`
- SSE 事件 `rag_status` 字段误写为 `detail`，实际为 `count`

处理：
- HTTP 路径计数 51 → 53
- 新增两个端点说明
- 修正 `rag_status` 字段

### 2. 前端缺少 `/api/cache/invalidate` 的封装

问题：
- 后端 `POST /api/cache/invalidate` 端点已存在
- `web/src/api/rest.js` 和 `web/src/api/index.js` 均未导出对应函数

处理：
- 新增 `invalidateCache(filter)` 函数并导出

### 3. 版本号未同步

问题：
- `core/config.py` 中 `VERSION = "6.0"`，但代码已进入 v6.1 证据缺口修复阶段

处理：
- 更新为 `VERSION = "6.1"`

### 4. 变更日志缺少 v6.1 之后的 commits

问题：
- v6.1 之后的 12 个 commits 未记录（多模态统一入口、Widget 增强、Prometheus 防重注册等）

处理：
- 新增 v6.1.1 版本节

### 5. 生产检查清单日期过时

问题：
- 最后复核日期为 2026-06-23
- 缺少 v6.1.1 新增的验证项

处理：
- 更新日期至 2026-06-25
- 补充 7 项已验证条目

## 本轮更新的文件

### Markdown 文档
- `README.md` — 版本号 v6.0→v6.1，状态日期 2026-06-23→2026-06-25，更新 HTTP 路径计数和已验证项
- `docs/README.md` — 最后更新日期 2026-06-23→2026-06-25，plans 目录补全对齐记录
- `docs/reference/api-reference.md` — HTTP 路径 51→53，新增 2 个端点，修正 `rag_status` 字段
- `docs/checklists/production-readiness-checklist.md` — 日期更新，OpenAPI 路径更新，新增 7 项已验证
- `docs/reports/releases/changelog.md` — 新增 v6.1.1 版本节

### 代码
- `core/config.py` — `VERSION` "6.0"→"6.1"
- `web/src/api/rest.js` — 新增 `invalidateCache` 函数
- `web/src/api/index.js` — 导出 `invalidateCache`

### 自动生成
- `docs/openapi.json` — 从 FastAPI 重新导出，53 个 HTTP 路径

---

## 第二轮复核 (2026-06-25)

在首次修复基础上，进行第二轮全量前后端联调复核。

### 本轮阅读范围
- 后端：`api/routes/` 全部 8 个路由文件 + `api/middleware/` + `auth/router.py` + `knowledge/router.py` + `alerts/router.py` + `api/routes/prompts.py`
- 前端：`web/src/api/` 全部 5 个文件 + `web/src/chat/` 全部文件 + `web/src/admin*.js` 全部文件 + `web/src/auth/index.js` + `web/src/login.js`
- 文档：已更新的全部活文档
- 配置：`core/config.py`

### 复核结果

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 前端 API 封装 vs 后端路由 | ✅ 100% 覆盖 | 48 个 API 端点全部有前端封装 |
| 前后端版本号一致性 | ✅ 已修复 | admin.html v6.0→v6.1 |
| OpenAPI JSON 时效性 | ✅ 已重生 | 53 个 HTTP 路径（48 API + 5 页面） |
| 前端测试 | ✅ 60/60 | 7 个测试文件，包括新增 admin-settings.test.js |
| 后端响应字段 vs 前端消费 | ✅ 全部对齐 | feedback/stats、monitoring/tokens、monitoring/satisfaction 等 |
| 新版管理后台功能完整性 | ✅ 全部可用 | 用户/知识库/Prompt/告警/Token Quota/熔断器/Prometheus 预览 |
| SSE 流式事件类型 | ✅ 类型匹配 | 前端 10 种事件类型与后端 SSE 生成器一致 |
| API auth 中间件角色映射 | ✅ 正确 | admin/supervisor/customer 三级权限正确映射 |

### 本轮新发现的问题

#### 1. `admin-history.js` 死代码
- **发现**：`web/src/admin-history.js` 导出 3 个函数（`loadHistoryList`、`loadHistoryMessages`、`renderHistoryMessages`），但无任何模块导入引用
- **影响**：低。属于未使用的模块，不影响功能
- **建议**：移除该文件或为其创建管理后台"历史记录"视图

#### 2. CSRF 在生产模式的潜在问题
- **发现**：CSRF 中间件在 `DEV_MODE=false` 时对无 Bearer Token 的 POST 请求执行 cookie/header 双重校验。前端登录页（`login.js`）直接使用 `fetch()` 而非 `fetchWithAuth()`，POST 登录请求无 Bearer Token
- **影响**：中。开发环境（`DEV_MODE=true`）CSRF 被跳过，但生产环境登录请求可能因缺少 `X-CSRF-Token` header 而失败
- **当前防护**：CSRF 中间件在第 312-314 行检查到 `Authorization: Bearer` 时已跳过 CSRF 校验。登录前用户无 Cookie → 同样跳过 CSRF

#### 3. `/api/auth/logout` 路由缺少 `prefix`
- **发现**：`auth/router.py` 使用 `prefix="/api/auth"`，但 `logout` 路径为 `@router.post("/logout")` → 正确解析为 `/api/auth/logout` ✅

### 本轮更新的文件

#### 代码修复
- `web/admin.html` — 版本号 v6.0→v6.1

#### 文档更新
- `docs/reference/api-reference.md` — `/api/*` 计数 42+→48
- `docs/checklists/production-readiness-checklist.md` — 前端测试计数 56→60，新增死代码和 CSRF 提醒项
- `docs/reports/releases/changelog.md` — v6.1.1 补充文档更新、代码清理、路由验证项
- `docs/reports/plans/2026-06-25-code-doc-alignment.md` — 本轮记录（本文）

#### 自动生成
- `docs/openapi.json` — 重新导出确认（53 路径，48 API）