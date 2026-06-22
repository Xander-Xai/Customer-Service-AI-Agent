# 2026-06-22 代码与文档对齐记录

## 目标

基于当前仓库实际代码，而不是历史验收结论，完成一轮前后端契约复核、上线阻碍排查和 Markdown 回填。

## 本轮阅读范围

- 后端：`api/`、`auth/`、`core/`、`rag/`、`media/`、`knowledge/`、`alerts/`
- 前端：`web/src/`、`web/*.html`
- 文档：`README.md`、`docs/README.md`、`docs/reference/api-reference.md`、`docs/checklists/production-readiness-checklist.md`、`docs/reports/releases/changelog.md`
- 开发进度/历史材料：`docs/reports/releases/*`、`docs/reports/milestone/*`

## 实际修复

### 1. 前后端上传约束不一致

问题：
- 前端 `web/src/chat/input.js` 允许视频 `50MB`、其他文件 `20MB`
- 后端 `api/middleware.py` 和 `api/routes/chat_multimodal.py` 对 `/api/*` 上传统一限制 `5MB`

处理：
- 前端改为统一 `5MB`
- 用户选择阶段即拦截超限文件，避免提交后才收到 `413`

### 2. 前后端图片格式不一致

问题：
- 前端把所有 `image/*` 都当成合法图片
- 后端仅接受 `image/jpeg`、`image/png`、`image/webp`

处理：
- 前端校验同步为 `JPEG/PNG/WebP`

### 3. `/metrics/prometheus` 返回类型被前端错误处理

问题：
- 后端返回 `text/plain`
- 前端 `getPrometheusMetrics()` 之前走统一 JSON 解析

处理：
- 前端新增文本请求分支，Prometheus 指标按字符串读取

### 4. `api` 包导入存在副作用

问题：
- `api/__init__.py` 会在包导入阶段自动导入 `app_factory`
- 导致仅仅导入 `api.routes.*` 就触发数据库初始化、容器构建和安全告警

处理：
- 移除 `api/__init__.py` 的隐式导入，保留显式导入约定

### 5. pytest 基础设施补丁破坏了原始签名

问题：
- `tests/conftest.py` 把 `BaseEventLoop.run_in_executor` 改成了 `async def`
- 这会破坏调用方对原始同步签名的假设

处理：
- 保持同步签名，只在内部为 `executor=None` 创建临时线程池并在 future 完成后回收

### 6. 测试配置泄露风险

问题：
- `.env.test` 中保留了真实格式 API Key

处理：
- 改为安全占位符 `sk-placeholder-test-key-do-not-use`

## 本轮验证结果

### 通过

- `npm test`
  - 53/53 通过
- `npm run build`
  - 通过，且 `package.json` 补充 `"type": "module"` 后不再出现 PostCSS 模块类型警告
- `pytest tests/unit/test_app_factory.py tests/unit/test_ws_coverage.py tests/unit/test_auth_tools_coverage.py -q --maxfail=1`
  - 88/88 通过
- `pytest tests/unit/test_api_routes.py --collect-only -q`
  - 当前可收集 143 个测试

### 仍需继续跟进

- `tests/unit/test_api_routes.py`
  - 当前整文件执行仍需继续拆查，尚不能作为“后端全量验证通过”的依据

## 文档决策

本轮没有重写历史里程碑或历史验收报告，因为这些属于快照文档。改动策略如下：

- 更新活文档：
  - `README.md`
  - `docs/README.md`
  - `docs/reference/api-reference.md`
  - `docs/checklists/production-readiness-checklist.md`
  - `docs/reports/releases/changelog.md`
- 保留历史快照：
  - `docs/reports/milestone/*`
  - `docs/reports/releases/release-notes-v5.x.md`
  - 历史验收/评分类文档
- 新增本文件，作为 2026-06-22 这次代码与文档对齐的当前记录

## 当前结论

项目当前更适合表述为：

- `v6.0` 代码基线已建立
- 前端构建与前端单测当前可通过
- 后端关键模块测试部分可通过，但仍未完成一轮“真实上线级”的全量后端验收
- 文档已从“直接宣称生产就绪”调整为“区分历史快照与当前事实”
