# 角色审计报告 — 使用者（Consumer / UX）

> **审计日期**: 2026-06-15
> **审计范围**: 前端 web/ 目录 + 后端 API 路由 + 文档
> **发现总数**: 26 个

---

## 发现列表

| 编号 | 严重 | 问题 + 位置 | 用户体验影响 | 修复建议 |
|---|---|---|---|---|
| **U-1** | 🔴 P0 | **HTTP 错误键不统一**：`api/routes/chat.py:207,212,232,248,252` 全部用 `{"error": ...}`；`api/routes/monitoring.py:35,305` 用 FastAPI `HTTPException(detail=...)`；前端 `web/src/api/rest.js:20` 同时读 `err.detail || err.error`。 | 后端混用两种错误键会导致前端在某些路由（监控/审计/Token）拿不到错误信息，无声失败或显示 `undefined`。 | 在 `api/middleware.py` 注册全局 `@app.exception_handler(Exception)` 统一封装 `{"error": msg}`；或重写 `rest.js` 兼容两种键 |
| **U-2** | 🔴 P0 | **`localStorage` 长时间挂起会话**：`web/src/chat/sessions.js:73` `loadSessionList()` catch 块完全空 `catch (_e) {}`；同时 `auth/index.js:71` `fetchWithAuth` 在 401 重试失败后仍返回 `resp` 给上层，由上层静默处理。 | 用户 Token 过期后看到的是"空会话列表"而非"登录已过期"提示，admin 进入页面看到 `()` 空字符串（用户信息失败时 `displayEl.textContent = '${data.username} (${role})'` 跳过） | catch 块至少 `appendSystemMessage('加载会话失败')`；admin 401 时主动跳 login |
| U-3 | 🟠 P1 | **登录页硬编码红绿色 hex**：`web/src/login.js:76,81` `errorMsg.style.color = '#22c55e'` / `'#ef4444'`。 | 切换深色主题后绿/红仍然刺眼，破坏暗色模式视觉一致；硬编码颜色绕过 `--success/--error` 主题 token。 | 改用 `errorMsg.classList.add('text-success' / 'text-error')` 由 CSS 控制 |
| U-4 | 🟠 P1 | **管理后台大量硬编码 hex 状态色**：`admin-analytics.js:299,421,444`、`admin-knowledge.js:23`、`admin-settings.js:60,306,307`、`admin-tokens.js`（多处）。`'#4ade80'`/`'#fbbf24'`/`'#f87171'`/`'#ef4444'`。 | 与 v5.2 主题系统（4 浅 + 2 深 + 减弱动效）冲突，深色模式下浅绿在深背景上看不清；`contrast.test.js` 只测主题 token，不测这些动态注入的色。 | 抽出 `--status-good/--status-warn/--status-bad` token，通过 className 应用 |
| U-5 | 🟠 P1 | **Toast 背景色硬编码**：`web/src/utils/toast.js:26-29` `'#346538' / '#9F2F2D' / '#956400' / '#1F6C9F'`。 | 深色主题下 success 绿偏暗且未自动适配；浅色主题下深红稍重。 | 改用 `var(--toast-success-bg)` 主题 token |
| U-6 | 🟠 P1 | **登录失败错误信息过于泛化**：`web/src/login.js:50` `throw new Error(data.detail || data.error || '操作失败')`；表单下方只有一个 `#errorMsg` div，缺少字段级提示。 | 用户不知道是用户名错还是密码错，连续输错无法定位问题。 | 增加 `usernameError/passwordError` 字段级提示；区分 401/429/5xx 错误文案 |
| U-7 | 🟠 P1 | **管理后台用户改角色用原生 `confirm()`**：`web/src/admin-users.js:94`、`admin-knowledge.js:63`、`chat/sessions.js:177`。 | 与项目整体设计语言严重不符；样式与暗色主题脱节；屏幕阅读器朗读生硬；移动端体验差。 | 替换为项目内 modal 组件（可基于现有 `.shortcuts-overlay` 的样式扩展） |
| U-8 | 🟠 P1 | **管理后台首屏无骨架屏/Loading 状态**：`web/src/admin-analytics.js:17`、`admin-users.js:7`、`admin-knowledge.js:9` 等都在 `await Promise.all(...)` 后才渲染，但渲染前页面只显示 `--` 占位符；首屏看到一片空白数据卡。 | 进入管理后台有明显 ~500ms-2s 空白期，SLA 卡、缓存卡、Agent 分布图全部 `--`，无 Loading 动画。 | 注入 skeleton CSS class（CSS 已存在 `.skeleton` 类 + `@keyframes skeleton-loading`，见 `monitor.css:315`）；或加 `aria-busy="true"` + 进度条 |
| U-9 | 🟠 P1 | **Widget 入口无 a11y 标签**：`web/widget.html` 整个文件内联 CSS + JS（687 行单文件），按钮缺 `aria-label`，消息列表缺 `role="log" aria-live="polite"`，输入框缺 `aria-describedby`。对比 `index.html` 已用 `role="log" aria-live="polite"`。 | 嵌入第三方站点后违反 WCAG 2.2 SC 4.1.2；盲人用户无法使用 widget。 | 把 `index.html` 的 a11y 模式（skip-link/role=log/aria-live/aria-label）同步到 widget |
| U-10 | 🟡 P2 | **`sessions.js` 删除会话 confirm 文案与其他危险操作不一致**：会话删除"确定要删除这个会话吗？"（无撤销提示），知识库 reseed "确定重新种子？这会覆盖现有数据。"（明确但无 30s 二次确认倒计时）。 | 误操作删除/reseed 后无 undo 机制（数据库本身可能也未实现软删除）。 | 增加 5s 倒计时 + "撤销" toast；或确认框要求输入会话名前缀 |
| U-11 | 🟡 P2 | **TTS 语音选择器轮询 20 次（10s）**：`web/src/main.js:52-56` `setInterval(..., 500)` 直到 `attempts > 20`。 | 慢网络下 API 失败 → 用户看到下拉框永远是"加载中..."直到 10s 后才显示空。 | 失败后立即展示 `加载失败，点击重试` 替代文本；清理 setInterval 泄漏 |
| U-12 | 🟡 P2 | **未实现的"消息搜索"无 Empty/Loading 态**：`web/src/chat/search.js:8` 输入框只处理高亮匹配，搜索会话外消息无意义；侧边栏会话列表搜索框缺 UI 提示。 | 用户看到搜索框以为能搜全部历史，实际只能搜当前会话高亮，无任何反馈。 | 加 placeholder "搜索当前会话"，或实现真正的全会话搜索 |
| U-13 | 🟡 P2 | **Voice 录音错误处理"应用错误时静默 catch"**：`web/src/chat/voice.js:209-211` `if (!resp.ok) return;` 与 `catch (_err) {}`。TTS 播放失败用户完全无感。 | 朗读失败用户以为正常但其实没声音；调试困难。 | 至少 `showToast('语音播放失败', 'warning')` |
| U-14 | 🟡 P2 | **`appendSystemMessage` 系统消息无视觉区分**：与用户消息/AI 消息同色同字号，仅靠 emoji `ℹ️` 区分。`web/src/chat/messages.js:108`。 | 用户难以区分"系统提示"与"AI 回复"，误以为是 AI 说的话。 | 给系统消息加淡灰背景 `var(--bg-secondary)` + 左边框竖线 |
| U-15 | 🟡 P2 | **SSE/WebSocket 流式错误无重试按钮**：`web/src/chat/input.js:142-148` SSE 中断后只 `showToast('流式传输中断', 'warning')`，无重试入口；用户需手动重新编辑发送。 | 网络抖动后用户必须重输消息，体验断裂。 | 增加"重试发送"按钮放在中断的 AI 消息下方 |
| U-16 | 🟡 P2 | **会话列表 "⚠️ N次漂移" 标签色彩孤立**：仅靠 emoji + 红色；色盲用户无法识别；其他状态全用 `tag-active` 同色。`web/src/chat/sessions.js:100`。 | 漂移告警本应是重要信号，但视觉权重不够。 | 加 `class="status-tag warning"`（参考 admin-analytics.js:230 的实现，统一复用） |
| U-17 | 🟡 P2 | **`localStorage` 数据未做兼容性迁移**：`web/src/state/chatState.js:6` 直接读取 `currentSessionId/currentSessionToken`，老用户从 v5.0 升级若字段名变更会丢失所有历史会话；`auth/index.js` 类似。 | 升级后用户看到"空会话列表"，误以为是数据丢失或 bug。 | 加版本号字段 `csai_data_v1`，读到旧版本时迁移/清理 |
| U-18 | 🟡 P2 | **响应式断点 320px 缺失**：`web/styles/responsive.css` 现有断点 1440/1200/1024/768/480px，缺少 ≤320px（iPhone SE 1代等极小屏）。 | 极窄屏幕上 sidebar 抽屉 280px + backdrop 可能挤压主区。 | 加 `@media (max-width: 360px)` 调整 sidebar 宽度 |
| U-19 | 🟡 P2 | **`console.log` 直接输出到生产控制台**：`web/src/api/websocket.js:52,76,98,114,124`、`chat/index.js:24,89`、`chat/input.js:139,196`。9 处 `[WS]...` `[Init]...`。 | 生产环境暴露内部状态/重连次数给用户，专业感不足；攻击者可通过 console 摸清接口逻辑。 | 改用 debug 日志系统（dev 模式 console.log，生产静默或上报） |
| U-20 | 🟡 P2 | **`quick-prompt-card` 快捷提问卡片无 `role="button"` 或键盘焦点**：`web/src/chat/welcome.js:17` 用 `<div>` 加 click 事件，缺 `tabindex`、`role="button"`、`aria-label`，按下 Enter 不会触发。`web/src/__tests__/chatState.test.js` 已覆盖状态但无 a11y 断言。 | 键盘用户无法用 Tab 聚焦到快捷提问卡；屏幕阅读器朗读为普通 div。 | 改为 `<button type="button" class="quick-prompt-card">`；已有 `<kbd>?</kbd>` 暗示支持键盘，应保持一致 |
| U-21 | 🟢 P3 | **无国际化 (i18n) 支持**：`web/src/utils/copy.js` 所有文案硬编码中文；`web/index.html` `lang="zh-CN"` 已写死；`README.md:355` 提到 "ChromaDB 默认 all-MiniLM-L6-v2（英文兜底）"。 | 跨境/英文客户无法使用；未来做海外市场需重构全部前端文案。 | 引入轻量 i18n（如 `@lit/task` 或自实现 `t('key')`），从 copy.js 改为字典表 |
| U-22 | 🟢 P3 | **`progressStatusEl` 全局单例无清理**：若用户在 `showProgressStatus` 与 `removeProgressStatus` 之间触发 `startNewChat`，旧 progress 会留在新会话顶部。`web/src/chat/messages.js:11` 模块级变量。 | 新建对话后偶发看到上一个会话的"正在为您查询..."提示卡住。 | `startNewChat` 内部调用 `removeProgressStatus()` |
| U-23 | 🟢 P3 | **`renderMarkdown` 链接强制 `target="_blank"`**：所有 AI 回复中的链接都开新窗口。`web/src/utils/markdown.js:35`。 | 用户失去当前对话上下文；视觉跳变。 | 区分内外链，或交由用户设置（默认同窗口） |
| U-24 | 🟢 P3 | **README "3 分钟验证入口" 实际超过 3 分钟**：`README.md:21` `make dev` 需先装 Python 3.10+ + pip install + 配置 .env + 启动；首次启动 ChromaDB 下载模型约 30s+。 | 用户首次 onboarding 实际耗时 5-15 分钟，标题"3 分钟"误导。 | 加 "5 分钟首次启动" 真实说明；或提供 `make quickstart` 一键脚本 |
| U-25 | 🟢 P3 | **`selectTTSVoice` 切换语音无试听按钮**：`web/index.html:96-99` 仅 `<select>` 下拉。 | 用户不知道每个语音听起来什么样，需要先保存再听，迭代成本高。 | 加 🔊 "试听" 按钮调用示例句 "您好，有什么可以帮您？" |
| U-26 | 🟢 P3 | **`Widget` 嵌入模式无身份认证 UX**：687 行单文件无登录提示，若嵌入页面是公开页面则任何人都能匿名使用 widget。 | 安全风险：与主系统 `/api/chat` 共用接口，可能被滥用且无审计。 | 在 widget 内检测 `localStorage.token`，缺失时显示"登录后使用" |

---

## 重点扫描结果摘要

### 【1. Web 前端体验】

✅ **整体良好**: index.html 用 `role="log" aria-live="polite"`、`skip-link`、`aria-label` 完备；
❌ **register.html 不存在**（登录页 JS 内置注册切换）。

### 【2. 错误处理友好性】

❌ **严重问题**: sessions.js 空 catch + chat/voice.js 三处空 catch；loadSessionList 无错误反馈。

### 【3. 无障碍】

✅ `lang="zh-CN"` 已设、type="button" 已加、跳过链接存在；
❌ 但 quick-prompt-card 用 div 不可键盘聚焦；widget.html a11y 严重缺失。

### 【4. 响应式】

✅ 1440/1200/1024/768/480 五档；移动端抽屉+触摸目标 44×44；
❌ 缺 320/360px 极小屏。

### 【5. 主题一致性】

✅ 主题 token 完善（4+2+字号+行高+动画）；
❌ 9 处硬编码 hex 颜色绕过主题系统（U-3/4/5）。

### 【6. 微交互/动效】

✅ animations.css + 主题减弱动效；
❌ 管理后台完全无骨架屏（U-8）；消息加载无 Loading 三态。

### 【7. WebSocket/SSE 流式】

✅ 指数退避 + 心跳 + 队列 + 401 重试；
❌ 中断后无重试按钮（U-15）。

### 【8. 语音/多模态】

✅ 错误分类（NotAllowed/NotFound/NotReadable）处理优秀；
❌ TTS 播放失败静默（U-13）。

### 【9. 管理后台】

✅ 角色权限 + 数据可视化完整；
❌ 危险操作用原生 confirm（U-7）、无骨架屏（U-8）、表格 30 行直接渲染无分页。

### 【10. 文档/引导】

✅ README 详细，5 份 active 文档齐全；
❌ "3 分钟验证"实际 5-15 分钟（U-24）。

### 【11. 国际化】

❌ 完全硬编码中文，无 i18n（U-21）。

---

## 关键文件路径

- `/home/dev/projects/customer-service-ai-agent/web/src/chat/sessions.js:73` (U-2 空 catch)
- `/home/dev/projects/customer-service-ai-agent/web/src/chat/voice.js:209` (U-13 TTS 静默)
- `/home/dev/projects/customer-service-ai-agent/web/src/login.js:76,81` (U-3 硬编码色)
- `/home/dev/projects/customer-service-ai-agent/web/src/admin-analytics.js:299,421,444` (U-4 硬编码状态色)
- `/home/dev/projects/customer-service-ai-agent/web/src/utils/toast.js:26-29` (U-5 Toast 硬编码色)
- `/home/dev/projects/customer-service-ai-agent/web/src/chat/welcome.js:17` (U-20 快捷卡无 a11y)
- `/home/dev/projects/customer-service-ai-agent/web/widget.html` 整体 (U-9 无 a11y)
- `/home/dev/projects/customer-service-ai-agent/api/routes/chat.py:207,212` (U-1 错误键不统一)
- `/home/dev/projects/customer-service-ai-agent/web/styles/responsive.css` (U-18 缺 320px 断点)
- `/home/dev/projects/customer-service-ai-agent/web/src/chat/messages.js:11` (U-22 progressStatus 全局单例)
