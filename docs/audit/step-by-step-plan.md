# 审查修复执行计划（详细版）

> **生成日期**: 2026-06-15
> **依据**: `docs/audit/audit_execution_plan.md` + 代码库实际状态侦察
> **当前状态**: Milestone 1 已完成, Milestone 2 & 3 待执行

## 侦察摘要

通过代码库实际检查，发现 `audit_execution_plan.md` 中部分描述与现状有偏差：

| 计划描述 | 实际现状 | 调整 |
|----------|----------|------|
| admin.js 近 800 行 | 已拆分 → 228 行，imports 3 个子模块 | Task 2.2 调整为拆分子模块 |
| 无 E2E 测试 | `__e2e__/flows.spec.js` 已存在（3 条链路） | Task 3.1 调整为 **扩展** 测试 |
| `monitor/` 目录 | 已不存在（已清理） | 图表逻辑已在 admin-analytics.js 内 |
| innerHTML 全量"滥用" | 多数是 `el.innerHTML = ''` + `createElement()` → 安全但可改进 | 保留 Task 2.1，聚焦清理残留风险 |

---

## Phase 0：准备工作（git worktree）

```bash
git checkout -b fix/audit-milestone-2
```

### 任务间的依赖关系

```
Task 2.1 (innerHTML 重构)
  │
  ├── Task 2.2 (admin-settings.js 拆解) — 需 2.1 先清理 innerHTML
  │
  └── Task 2.3 (网络请求中央管控) — 依赖于 rest.js 现有架构，独立
          │
          └── Task 3.3 (CSP unsafe-inline 移除) — 需 2.3 完成后确认 fetch 路径
                  │
                  └── Task 3.2 (Token 配额) — 独立后端任务
                          │
                          └── Task 3.1 (E2E 测试补全) — 需 3.2 完成后测试新配额
```

---

## Milestone 2：安全加固与代码债务重构

### Task 2.1: 消除 `innerHTML` XSS 隐患

**范围**: admin-analytics.js, admin-settings.js, admin-users.js, main.js, welcome.js, auth/index.js

**当前模式分析**:
- `el.innerHTML = ''`（清空容器）→ **安全**，但 `createElement` + `textContent` 更优
- `el.innerHTML = '<div>...'`（含用户数据拼接）→ **高风险**
- 测试文件中的 `body.innerHTML` → 可跳过（测试环境）

**具体步骤**:

1. **admin-users.js** (1 处)
   - L16: `tbody.innerHTML = ''` → 改为 `tbody.replaceChildren()`

2. **admin-analytics.js** (6 处)
   - L49: `container.innerHTML = ''` → `container.replaceChildren()`
   - L192: `tbody.innerHTML = ''` → `tbody.replaceChildren()`
   - L246: `container.innerHTML = ''` → `container.replaceChildren()`
   - L292: `el.innerHTML = ''` → `el.replaceChildren()`
   - L353: `el.innerHTML = ''` → `el.replaceChildren()`
   - L417: `el.innerHTML = ''` → `el.replaceChildren()`

3. **admin-settings.js** (12 处)
   - 所有 `el.innerHTML = ''` 清空模式 → `el.replaceChildren()`

4. **auth/index.js** (1 处)
   - L191: `document.body.innerHTML = ...` → 评估是否有 XSS 风险，使用 `setSafeHtml` 替代

5. **main.js** (1 处)
   - L41: `voiceSelect.innerHTML = entries` → 使用 `createElement` + `appendChild`

6. **welcome.js** (1 处)
   - L28: `container.innerHTML = WELCOME_HTML` → `setSafeHtml(container, WELCOME_HTML)`

**验收标准**:
- [ ] 所有 `innerHTML` 赋值已替换为 DOM API（`createElement` / `textContent` / `replaceChildren` / `setSafeHtml`）
- [ ] 功能无回归（各页面加载正常）
- [ ] Biome Lint 无新增错误

**涉及文件**:
- `web/src/admin-analytics.js`
- `web/src/admin-settings.js`
- `web/src/admin-users.js`
- `web/src/auth/index.js`
- `web/src/main.js`
- `web/src/chat/welcome.js`

**估算**: M（5 个文件，约 20 处替换）

---

### Task 2.2: 重构巨石模块（admin-settings.js + admin-analytics.js）

**当前状态**:
- `admin-settings.js`: 608 行（管理设置 + 知识库 + 告警 + Prompt + Token + 系统健康）
- `admin-analytics.js`: 495 行（质量趋势 + 热点问题 + 满意度 + 会话 + 告警历史）

**注意**: admin.js 已拆分，但子模块仍然过大。

**具体步骤**:

1. **从 admin-settings.js 提取独立模块**:
   - 提取 `loadKnowledgeStats`, `reseedKnowledge`, `syncFromErp`, `handleAddDocs` → `admin-knowledge.js`
   - 提取 `loadAlertConfig`, `loadAlertHistory`, `testAlert` → `admin-alerts.js`
   - 提取 `loadTokenUsage` → `admin-tokens.js`
   - 剩余 (`loadSystemHealth`, `loadMetricsStats`, `loadFeedbackStats`, `loadPromptAgents/ Versions`, `handleCreatePrompt`, `loadAuditLog`, `loadKnowledgeStats`) → `admin-settings.js`（核心设置）

2. **从 admin-analytics.js 提取公共图表工具**:
   - 柱状图渲染函数 `renderBarChart(container, items, valueKey, labelKey, maxVal)` → `utils/chart.js`
   - 保留业务逻辑在 admin-analytics.js 中

**验收标准**:
- [ ] admin-settings.js ≤ 400 行
- [ ] admin-analytics.js ≤ 300 行（提取图表工具后）
- [ ] 所有功能无回归（导航、加载、渲染正常）
- [ ] Biome Lint 通过

**涉及文件**:
- `web/src/admin.js`（更新 import）
- `web/src/admin-settings.js`
- `web/src/admin-analytics.js`
- `web/src/admin-knowledge.js`（新）
- `web/src/admin-alerts.js`（新）
- `web/src/admin-tokens.js`（新）
- `web/src/utils/chart.js`（新）

**估算**: L（需提取多个模块，但逻辑独立）

---

### Task 2.3: 网络请求中央管控

**当前状态**:
- `rest.js` 已提供完整封装（包含 JWT + API Key 双认证、错误处理）
- 但仍有以下直接 `fetch()` 调用绕过 `rest.js`:

| 文件 | 路径 | 行号 |
|------|------|------|
| `admin.js` | `/api/auth/me` | L79 |
| `chat/voice.js` | `/api/chat/voice` | L149 |
| `chat/voice.js` | `/api/tts` | L208 |
| `api/sse.js` | `/api/chat/stream` | L21 |
| `api/sse.js` | `/api/chat/multimodal/stream` | L95 |
| `login.js` | `/api/auth/login` | L44 |
| `login.js` | `/api/auth/register` | L44 |
| `auth/index.js` | `/api/auth/refresh` | L23 |
| `auth/index.js` | `fetchWithAuth` 内部 | L77, L85 |
| `auth/index.js` | `/api/auth/logout` | L133 |

**策略**:
- **SSE 流式**（`api/sse.js`）和 **fetchWithAuth 内部**（`auth/index.js`）— 绕过是必要的（不能修改 fetch 行为）
- **`admin.js:79`** `/api/auth/me` — 添加 `import { fetchWithAuth }` 替代直接 fetch
- **`chat/voice.js`** — `/api/chat/voice`（FormData POST）和 `/api/tts`（FormData POST）— 添加 auth header 自动注入
- **`login.js`** — 登录/注册不需要 auth header（登录前无 token），但结构上可封装

**具体步骤**:

1. `admin.js`:
   - 替换 L79: `const resp = await fetch('/api/auth/me', { headers })` → `fetchWithAuth`

2. `chat/voice.js`:
   - 确保所有 `fetch()` 调用携带 Authorization header

3. `login.js`:
   - 保持现状（登录前无需认证），但添加注释说明

4. 在 `rest.js` 中添加 `getUserMe()` 导出函数

**验收标准**:
- [ ] 所有需要认证的 API 调用统一通过 `fetchWithAuth`
- [ ] `admin.js` 中 `/api/auth/me` 使用 `fetchWithAuth`
- [ ] 用户信息、语音、SSE 等非 JSON 请求的认证头正确携带
- [ ] 功能无回归

**涉及文件**:
- `web/src/api/rest.js`（新增 `getUserMe()`）
- `web/src/admin.js`（替换 fetch 为 fetchWithAuth）
- `web/src/chat/voice.js`（添加 auth headers）
- `web/src/login.js`（添加注释说明）

**估算**: S（4 个文件，3 处实质修改）

---

## Milestone 3：质量防线与防刷单机制

### Task 3.1: 扩展前端 E2E 测试

**已存在**: `web/src/__e2e__/flows.spec.js` — 3 条链路（登录/对话/管理后台）

**需补充**: 覆盖以下关键场景

**具体步骤**:

1. **注册流程测试**:
   - 表单填充、注册 API mock、注册成功后跳转
   - 错误处理（密码不匹配、用户名已存在）

2. **Token 刷新降级测试**:
   - 401 后自动刷新 → 重试成功
   - Token 过期后跳转登录页

3. **管理后台导航与权限测试**:
   - admin/supervisor 角色可见不同的 section
   - 导航切换正确激活对应 section
   - SPA 哈希路由/切换不刷新页面

4. **会话持久化与恢复测试**:
   - 新会话创建
   - 会话列表加载
   - 切换会话后上下文正确恢复

5. **状态管理测试**:
   - 加载中状态显示
   - 空数据友好提示
   - 错误状态 Toast 提示

**验收标准**:
- [ ] E2E 测试覆盖至少 3 个以上的新业务场景
- [ ] `npx playwright test` 全部通过（mock 模式）
- [ ] 测试可独立运行（不依赖真实后端）

**涉及文件**:
- `web/src/__e2e__/flows.spec.js`（扩展）
- `playwright.config.js`（无需修改）

**估算**: M（扩展现有测试文件）

---

### Task 3.2: 完善 LLM Token 预算限制（Quota 控制）

**当前状态**:
- 仅存在请求频率限流（`60 req/min/IP`）
- 无基于用户账户的 Token 消耗硬性上限
- E2E 测试 `test_token_budget_never_exceeded` 仅验证滑动窗口裁剪，未验证配额

**具体步骤**:

1. **设计 Token Quota 数据模型**:
   ```python
   @dataclass
   class TokenQuota:
       user_id: str
       daily_limit: int       # 每日 Token 上限（例如 100000）
       monthly_limit: int     # 每月 Token 上限（例如 2000000）
       used_today: int        # 今日已用
       used_this_month: int   # 本月已用
       last_reset: datetime   # 上次重置时间
   ```

2. **实现 Quota Manager**:
   - 在 `core/session/` 或 `core/` 下新建 `token_quota.py`
   - 提供 `check_quota(user_id) → bool`
   - 提供 `consume_tokens(user_id, tokens) → bool`
   - 存储层支持 Redis（生产）或内存（开发/测试）
   - 自动重置周期（日/月）

3. **集成到 LLM 调用链路**:
   - 在 `llm/client.py` 中调用 LLM 前检查 quota
   - 在 LLM 返回后记录消耗的 tokens
   - 配额不足时返回友好的错误消息

4. **测试**:
   - 单元测试：配额检查、消耗、重置
   - 集成测试：配额耗尽→降级行为
   - 更新 `test_token_budget_never_exceeded` 以验证硬性上限

**验收标准**:
- [ ] Token Quota 管理器实现（存储、检查、消耗、重置）
- [ ] 配额耗尽时返回明确的降级响应（非崩溃）
- [ ] 单元测试覆盖配额功能
- [ ] 配置可通过 `.env` 自定义（`TOKEN_DAILY_LIMIT`, `TOKEN_MONTHLY_LIMIT`）

**涉及文件**:
- `core/token_quota.py`（新）
- `core/config.py`（添加配置项）
- `llm/client.py`（集成 quota 检查）
- `tests/unit/test_token_quota.py`（新）
- `tests/e2e/test_all.py`（更新）

**估算**: M-L（新模块 + 集成）

---

### Task 3.3: 评估并移除 CSP `unsafe-inline`

**当前状态**:
- `api/middleware.py:168`: `style-src 'self' 'unsafe-inline'`
- 原因是前端存在大量内联样式（`style=` 属性和 `.style.` 赋值）

**评估结果**:
- 前端代码中确实存在很多 `.style.xxx` 的 JS 赋值
- 这些无法全部迁移到外部 CSS（动态样式如颜色、可见性）
- 可行的策略：**style nonce** 替代 `unsafe-inline`

**具体步骤**:

1. **传播 nonce 到前端**:
   - 在 HTML 模板中添加 `meta` 标签传递 nonce
   - 后端中间件在渲染 HTML 时注入 nonce

2. **替换 `style-src` 中的 `unsafe-inline`**:
   - `style-src 'self' 'nonce-{nonce}'`
   - 内联 `<style>` 标签添加 `nonce` 属性

3. **评估动态 `.style.` 赋值**:
   - 审查发现：所有 `.style.` 赋值都是程序化动态样式（主题色、响应式布局）
   - 这些 **不会被 CSP 阻止**，因为 CSP 只拦截 `<style>` 标签和 `style=` 属性
   - 不需要改动 `.style.` 赋值

4. **为内联 `<style>` 标签添加 nonce**:
   - 检查 HTML 文件中是否有内联 `<style>` 标签
   - 添加 nonce 属性

**验收标准**:
- [ ] `style-src` 从 `'unsafe-inline'` 改为 `'nonce-{nonce}'`
- [ ] 所有内联 `<style>` 标签带有正确的 nonce
- [ ] 前端主题切换正常
- [ ] 无 CSP 错误（浏览器控制台）
- [ ] `npm run build` 通过

**涉及文件**:
- `api/middleware.py`（修改 CSP header）
- 相关的 HTML 模板文件

**估算**: S（2-3 个文件，配置改动为主）

---

## 执行顺序 & 检查点

```
Phase 1: Task 2.1 → Task 2.2
  Checkpoint: npm run lint + npm run build 通过

Phase 2: Task 2.3 → Task 3.3
  Checkpoint: 页面加载无 CSP 错误

Phase 3: Task 3.2
  Checkpoint: make test 通过（新增 token quota 测试）

Phase 4: Task 3.1
  Checkpoint: npx playwright test 全部通过
```

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| innerHTML 重构导致渲染断裂 | 高 | 每处替换后验证页面加载 |
| 移除 CSP unsafe-inline 导致内联样式失败 | 中 | 先在 preview 模式测试 |
| Token Quota 影响现有 LLM 调用链路 | 中 | 使用 Feature Flag 控制开启/关闭 |

## 注

- 已根据实际代码库状态调整了 Task 2.2 和 Task 3.1 的范围
- 所有改动应在同一个 worktree/分支上完成（任务间有依赖，不宜并行）
- 每一步完成后执行 `make lint` / `npm run lint` 确保无规范倒退