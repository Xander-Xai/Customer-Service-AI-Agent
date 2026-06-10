# 前后端对齐修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复前后端对齐审查中发现的 5 项问题——清理死代码、激活未连接的 UI 组件、确保所有后端端点有前端对应。

**Architecture:** 纯前端修改，不涉及后端改动。删除 2 个死代码函数 + 1 个死代码文件，为 index.html 添加 TTS 语音选择器 `<select>` 元素（激活 main.js 已有逻辑），激活会话详情侧面板（CSS 已就绪）。

**Tech Stack:** Vanilla JavaScript (ES modules), HTML, CSS, Vitest

---

## File Map

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `web/src/api/rest.js` | 删除 `getHistory()` 和 `getHistoryMessages()` |
| Modify | `web/src/api/index.js` | 删除对应的 re-export |
| Modify | `web/index.html` | 添加 `#selectTTSVoice` 元素 + TTS 选择器容器 |
| Modify | `web/src/chat/sessions.js` | 激活会话详情侧面板（点击会话时打开、显示元信息） |
| Modify | `web/styles/components.css` | 添加 TTS 选择器样式 |
| Delete | `web/src/monitor/index.js` | 死代码文件（223 行），admin-analytics.js 已完全替代 |

---

### Task 1: 删除死代码 API 函数

**Files:**
- Modify: `web/src/api/rest.js`（删除第 85-97 行）
- Modify: `web/src/api/index.js`（删除第 34-35 行）

**背景:** `getHistory()` 和 `getHistoryMessages()` 在 rest.js 中定义、在 api/index.js 中 re-export，但整个前端没有任何代码调用它们。`sessions.js` 使用的是 `getSession()`（走 `/api/sessions/{id}`），该端点已返回 messages。

- [ ] **Step 1: 从 rest.js 删除两个函数**

删除以下代码块（rest.js 第 85-97 行）:

```javascript
export function getHistory() {
  return _request('GET', '/api/history');
}

export function getHistoryMessages(sessionId) {
  const token = localStorage.getItem('currentSessionToken') || '';
  return _request(
    'GET',
    `/api/history/${sessionId}/messages`,
    null,
    token ? { 'X-Session-Token': token } : {},
  );
}
```

- [ ] **Step 2: 从 api/index.js 删除对应的 re-export**

删除以下两行（api/index.js 第 34-35 行）:

```javascript
  getHistory: rest.getHistory,
  getHistoryMessages: rest.getHistoryMessages,
```

- [ ] **Step 3: 确认无其他引用**

Run:
```bash
grep -rn "getHistory\|getHistoryMessages" web/src/ --include="*.js" | grep -v node_modules
```

Expected: 无输出

- [ ] **Step 4: 运行前端测试**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vitest run --config vite.config.js
```

Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/api/rest.js web/src/api/index.js
git commit -m "chore: remove dead getHistory/getHistoryMessages API functions"
```

---

### Task 2: 删除死代码文件 monitor/index.js

**Files:**
- Delete: `web/src/monitor/index.js`（223 行）

**背景:** 该文件是早期监控页面的实现，包含 `switchPage`、`initMonitor`、`refreshMonitorData` 及多个渲染函数。但没有被任何文件 import——admin 页面直接使用 `admin-analytics.js`（提供相同的 `refreshMonitorData` 等功能）。

- [ ] **Step 1: 确认文件未被引用**

Run:
```bash
grep -rn "monitor/index\|from.*['\"]\.\./monitor\|from.*['\"]\.\/monitor" web/src/ --include="*.js" | grep -v node_modules
```

Expected: 无输出（admin-analytics.js 引用的是 `./utils/monitor-render.js`，不是 `./monitor/index.js`）

- [ ] **Step 2: 删除文件和空目录**

```bash
rm web/src/monitor/index.js
rmdir web/src/monitor 2>/dev/null || true
```

- [ ] **Step 3: 运行前端测试**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vitest run --config vite.config.js
```

Expected: 所有测试 PASS

- [ ] **Step 4: Commit**

```bash
git add -A web/src/monitor/
git commit -m "chore: delete dead monitor/index.js (superseded by admin-analytics.js)"
```

---

### Task 3: 添加 TTS 语音选择器到聊天页面

**Files:**
- Modify: `web/index.html`（在输入提示下方添加 `<select>` 元素）
- Modify: `web/styles/components.css`（添加选择器样式）

**背景:** `main.js` 已有完整的 TTS 选择器初始化逻辑（第 26-61 行）：轮询 `getAvailableVoices()` 填充 `<option>`、中文标签映射、`change` 事件持久化到 localStorage。但 `#selectTTSVoice` 元素在 index.html 中不存在，导致这段代码永远不执行。只需添加 HTML 元素即可激活。

- [ ] **Step 1: 在 index.html 添加 TTS 语音选择器元素**

在 `web/index.html` 第 94 行（`input-hint` div 的结束标签 `</div>`）之后、第 95 行（`<input type="file"`）之前，插入：

```html
        <div class="tts-selector" id="ttsSelectorContainer">
          <label for="selectTTSVoice" class="tts-label">🔊 语音：</label>
          <select id="selectTTSVoice" class="tts-voice-select" aria-label="选择 TTS 语音">
            <option value="">加载中...</option>
          </select>
        </div>
```

- [ ] **Step 2: 添加 TTS 选择器 CSS 样式**

在 `web/styles/components.css` 文件末尾追加：

```css
/* ===== TTS 语音选择器 ===== */
.tts-selector {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 4px;
  margin-top: 4px;
}

.tts-label {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}

.tts-voice-select {
  font-size: 11px;
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg-base);
  color: var(--text-primary);
  max-width: 200px;
  cursor: pointer;
}

@media (max-width: 640px) {
  .tts-selector {
    display: none;
  }
}
```

- [ ] **Step 3: 确认 main.js 无需修改**

验证 `web/src/main.js` 第 26 行 `document.getElementById('selectTTSVoice')` 能匹配新元素。现有代码包含完整逻辑：
- `VOICE_LABELS` 中文标签映射（晓晓/云希/云健/晓伊）
- 轮询 `getAvailableVoices()` 填充 `<option>`（每 500ms，最多 20 次）
- `change` 事件 → `setVoice()` + `localStorage.setItem('ttsVoice', ...)`
- 启动时从 localStorage 恢复已选语音

无需修改 main.js。

- [ ] **Step 4: 运行前端测试**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vitest run --config vite.config.js
```

Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/styles/components.css
git commit -m "feat: add TTS voice selector to chat page (activates existing main.js logic)"
```

---

### Task 4: 激活会话详情侧面板

**Files:**
- Modify: `web/src/chat/sessions.js`

**背景:** index.html 第 101-107 行定义了 `#sessionDetailPanel` 侧面板（含 `#sessionDetailContent` 和 `#btnClosePanel`）。CSS 中 `.session-detail-panel` 和 `.session-detail-panel.open` 样式已就绪（components.css 第 626-643 行）：默认 `transform: translateX(100%)` 隐藏，`.open` 时 `translateX(0)` 滑入。但当前点击会话只加载消息到主聊天区，从不打开此面板。

- [ ] **Step 1: 在 sessions.js 添加 _showSessionDetail 函数**

在 `web/src/chat/sessions.js` 的 `deleteSessionConfirm` 函数之后追加：

```javascript
/** 显示会话详情侧面板 */
function _showSessionDetail(sessionId, session) {
  const panel = document.getElementById('sessionDetailPanel');
  const content = document.getElementById('sessionDetailContent');
  if (!panel || !content) return;

  const msgCount = session.message_count || (session.messages?.length ?? 0);
  const createdAt = session.created_at ? formatTime(session.created_at) : '--';
  const lastActivity = session.last_activity ? formatTime(session.last_activity) : '--';

  content.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">会话 ID</div>
        <div style="font-family:monospace;font-size:12px;word-break:break-all">${escapeHtml(sessionId)}</div>
      </div>
      <div style="display:flex;gap:16px">
        <div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">消息数</div>
          <div style="font-size:14px;font-weight:600">${msgCount}</div>
        </div>
        <div>
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">漂移次数</div>
          <div style="font-size:14px;font-weight:600">${session.drift_count || 0}</div>
        </div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">创建时间</div>
        <div style="font-size:13px">${createdAt}</div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">最后活动</div>
        <div style="font-size:13px">${lastActivity}</div>
      </div>
      ${session.summary ? `
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px">摘要</div>
        <div style="font-size:13px">${escapeHtml(session.summary)}</div>
      </div>
      ` : ''}
    </div>
  `;

  panel.classList.add('open');
}
```

- [ ] **Step 2: 在 selectSession 中调用面板展示**

修改 `selectSession` 函数，在 `const session = data.session;` 之后、渲染消息之前，添加面板调用。找到以下代码段：

```javascript
    const data = await API.getSession(sessionId);
    const session = data.session;
    const messages = session.messages || [];
```

在其后添加一行：

```javascript
    _showSessionDetail(sessionId, session);
```

- [ ] **Step 3: 添加面板关闭按钮事件绑定**

在 sessions.js 文件末尾追加：

```javascript
// 初始化面板关闭按钮
(function _initPanelClose() {
  const btn = document.getElementById('btnClosePanel');
  const panel = document.getElementById('sessionDetailPanel');
  if (btn && panel) {
    btn.addEventListener('click', () => panel.classList.remove('open'));
  }
})();
```

- [ ] **Step 4: 运行前端测试**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vitest run --config vite.config.js
```

Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add web/src/chat/sessions.js
git commit -m "feat: activate session detail side panel on session click"
```

---

### Task 5: 全量验证

- [ ] **Step 1: 运行全部前端测试**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vitest run --config vite.config.js
```

Expected: 所有测试 PASS

- [ ] **Step 2: 运行 Python 后端测试（确保无副作用）**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && make test-fast
```

Expected: 所有测试 PASS

- [ ] **Step 3: Lint 检查**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && make lint
```

Expected: 无新增 error

- [ ] **Step 4: 构建验证**

Run:
```bash
cd /home/dev/projects/customer-service-ai-agent && npx vite build --config vite.config.js
```

Expected: 构建成功，`static/dist/` 输出正常

---

## 修复总结

| # | 问题 | 修复方式 | 影响文件 |
|---|------|---------|---------|
| 1 | `getHistory`/`getHistoryMessages` 死代码 | 删除函数 + re-export | rest.js, index.js |
| 2 | `monitor/index.js` 死代码文件 | 删除文件 | monitor/index.js |
| 3 | TTS 语音选择器无 DOM 元素 | 添加 `<select>` + CSS | index.html, components.css |
| 4 | 会话详情侧面板未激活 | 添加 JS 逻辑 + 事件绑定 | sessions.js |
| 5 | `/api/sessions/{id}/checkpoint` 无前端 | 不修复（保留后端，未来可扩展） | 无 |
