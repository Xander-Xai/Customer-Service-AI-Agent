# 前端全面优化设计文档

**日期：** 2026-06-08
**范围：** 代码重构 + 用户体验增强 + 无障碍改善
**涉及文件：** style.css、chat.js、api.js、index.html、login.html、admin.html

---

## 1. 背景与目标

### 现状

| 文件 | 行数 | 问题 |
|---|---|---|
| style.css | 1727 | 单文件无分层，10+ 处内联 style |
| chat.js | 1221 | 单文件职责过多（消息/会话/监控/快捷键/图片上传） |
| api.js | 445 | WebSocket/REST/SSE/事件混在一起 |
| index.html | 304 | 欢迎页模板与 JS 重复，内联样式 |
| login.html | 145 | 内联 ~90 行 JS |
| admin.html | 290 | 内联 ~200 行 JS |

### 目标

1. **代码质量**：引入 Vite 构建 + ES Module，拆分为职责单一的模块
2. **用户体验**：SSE 流式打字、Markdown 完整支持、消息搜索、错误提示优化
3. **无障碍**：ARIA 标签、键盘导航、对比度修复、移动端适配

---

## 2. 架构设计

### 2.1 构建工具：Vite

**配置要点：**

```js
// vite.config.js
import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',                    // 项目根目录
  build: {
    outDir: 'static/dist',     // 输出到 static/dist/
    rollupOptions: {
      input: {
        main: 'index.html',    // 主聊天页
        admin: 'admin.html',   // 管理页
        login: 'login.html',   // 登录页
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
      '/static': 'http://localhost:8000',
    },
  },
});
```

**开发模式：**
- `npm run dev` → Vite dev server（5173），proxy 到 FastAPI（8000）
- HMR 热更新，无需手动刷新

**生产模式：**
- `npm run build` → 输出到 `static/dist/`
- FastAPI 的 `StaticFiles` 挂载 `static/dist/` 替代原 `templates/` + `static/`
- 带 hash 的文件名，利于缓存

### 2.2 目录结构

```
项目根目录/
├── package.json
├── vite.config.js
├── src/
│   ├── main.js               # 主聊天页入口
│   ├── admin.js              # 管理页入口
│   ├── login.js              # 登录页入口
│   ├── api/
│   │   ├── index.js          # 统一导出
│   │   ├── websocket.js      # WS 连接/心跳/重连/消息队列
│   │   ├── rest.js           # REST API 封装
│   │   ├── sse.js            # SSE 流式封装
│   │   └── events.js         # 事件发布/订阅
│   ├── chat/
│   │   ├── index.js          # 组装入口 + 初始化
│   │   ├── messages.js       # 消息渲染（含 Markdown）
│   │   ├── sessions.js       # 会话管理
│   │   ├── input.js          # 输入区/图片上传/拖拽
│   │   ├── shortcuts.js      # 快捷键
│   │   └── welcome.js        # 欢迎页模板
│   ├── monitor/
│   │   └── index.js          # 监控仪表盘
│   ├── auth/
│   │   └── index.js          # 认证状态管理
│   └── utils/
│       ├── markdown.js        # marked.js + DOMPurify 配置
│       ├── dom.js             # escapeHtml、scrollToBottom
│       ├── format.js          # formatTime、formatFileSize
│       └── agents.js          # Agent 图标/模式映射常量
├── styles/
│   ├── variables.css          # CSS 变量 + 全局重置
│   ├── layout.css             # 布局
│   ├── components.css         # 组件
│   ├── monitor.css            # 监控页
│   ├── admin.css              # 管理页
│   ├── login.css              # 登录页
│   ├── responsive.css         # 响应式
│   └── animations.css         # 动画
├── index.html
├── login.html
└── admin.html
```

### 2.3 FastAPI 集成

**生产模式（`npm run build` 后）：**

```python
# 修改 FastAPI 静态文件挂载
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Vite 构建产物
app.mount("/static/dist", StaticFiles(directory="static/dist"), name="dist")

# 页面路由 → 返回 Vite 处理后的 HTML
@app.get("/")
async def serve_index():
    return FileResponse("static/dist/index.html")

@app.get("/login.html")
async def serve_login():
    return FileResponse("static/dist/login.html")

@app.get("/admin.html")
async def serve_admin():
    return FileResponse("static/dist/admin.html")

# 旧的 /static 目录保留（非前端资源如上传文件等）
app.mount("/static/files", StaticFiles(directory="static/files"), name="files")
```

**开发模式（`npm run dev` + `make dev`）：**

- FastAPI 运行在 8000 端口（只处理 API/WebSocket）
- Vite dev server 运行在 5173 端口（处理 HTML/CSS/JS + HMR）
- Vite 的 `server.proxy` 将 `/api`、`/ws`、`/static` 转发到 8000
- 开发时访问 `http://localhost:5173`，生产时访问 `http://localhost:8000`

**开发流程：**
```bash
# 终端 1：后端
make dev  # FastAPI on :8000

# 终端 2：前端
npm run dev  # Vite on :5173，自动 proxy 到 :8000
```

**package.json scripts：**
```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  }
}
```

---

## 3. 代码重构

### 3.1 JS 模块拆分

#### chat/messages.js (~200 行)

```js
// 导出
export function appendUserMessage(content, imageFile) { ... }
export function appendAssistantMessage(content, meta) { ... }
export function appendSystemMessage(content) { ... }
export function showTypingIndicator() { ... }
export function removeTypingIndicator() { ... }
export function showProgressStatus(text) { ... }
export function removeProgressStatus() { ... }

// 使用 marked.js + DOMPurify
import { renderMarkdown } from '../utils/markdown.js';
```

#### chat/sessions.js (~150 行)

```js
export function startNewChat() { ... }
export async function loadSessionList() { ... }
export async function selectSession(sessionId) { ... }
export async function deleteSessionConfirm(sessionId) { ... }
```

#### chat/input.js (~200 行)

```js
export function sendMessage() { ... }
export function triggerImageUpload() { ... }
export function handleImageSelect(event) { ... }
export function clearImageSelection() { ... }
export function _initDragAndDrop() { ... }
export function autoResizeInput(el) { ... }
export function updateSendButton() { ... }
```

#### api/websocket.js (~180 行)

```js
export function connect(sessionId) { ... }
export function send(query, sessionId, sessionToken) { ... }
export function disconnect() { ... }
export function isConnected() { ... }
// 内部：心跳、重连、消息队列
```

#### api/sse.js (~60 行)

```js
export function sendChatStream(query, sessionId, sessionToken, callbacks) { ... }
// callbacks: { onChunk, onDone, onError, onStatus }
```

### 3.2 消除重复

**欢迎页模板：**
- 当前：index.html 中静态 HTML + chat.js `startNewChat()` 中 JS 模板字符串
- 优化：统一由 `chat/welcome.js` 导出模板函数，index.html 中静态 HTML 移除，首次也由 JS 渲染

**Agent 映射常量：**
- 当前：`appendAssistantMessage` 中 `agentIcons` 和 `modeLabels` 局部定义
- 优化：提取到 `utils/agents.js`

```js
// src/utils/agents.js
export const AGENT_ICONS = {
  '产品专家': '🧴',
  '技术支持专家': '🔧',
  '账单专家': '💰',
  '投诉处理专家': '⚠️',
  '通用咨询专家': '📋',
  'response_agent': '📨',
};

export const MODE_LABELS = {
  sequential: '快速通道',
  parallel: '并行处理',
  consultation: '专家会诊',
  hierarchical: '层级协作',
};
```

### 3.3 CSS 拆分

| 文件 | 内容来源（style.css 行号范围） | 预估行数 |
|---|---|---|
| variables.css | :root (8-67) + 全局重置 (69-91) + 滚动条 (96-103) | ~100 |
| layout.css | header (105-211) + sidebar (219-308) + chat-area (310-316) + app-layout (213-217) | ~200 |
| components.css | 消息 (410-632) + 输入区 (662-736) + 反馈 (738-767) + 操作按钮 (1516-1598) | ~400 |
| monitor.css | 仪表盘 (769-928) + 告警 (980-1045) + 表格 (938-978) | ~350 |
| admin.css | 管理后台 (1253-1365) | ~100 |
| login.css | 登录页 (1383-1515) | ~100 |
| responsive.css | 所有 @media (1176-1191, 1718-1727) | ~60 |
| animations.css | @keyframes (208-211, 424-438, 629-632, 656-659, 1054-1057, 1197-1200) | ~50 |

---

## 4. 用户体验增强

### 4.1 SSE 流式打字动画

**数据流：**
```
sendMessage() → showTypingIndicator()
→ SSE 请求开始
→ onChunk: 移除打字指示器，创建空消息气泡 + 闪烁光标
→ onChunk: 追加文本到气泡（requestAnimationFrame 批量更新）
→ onDone: 移除光标，renderMarkdown() 最终渲染，记录到 messageHistory
→ onError: 降级到 WebSocket 模式
```

**闪烁光标 CSS：**
```css
.streaming-cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: var(--primary);
  animation: blink 1s step-end infinite;
  vertical-align: text-bottom;
  margin-left: 1px;
}

@keyframes blink {
  50% { opacity: 0; }
}
```

**降级策略：**
- SSE 请求失败（网络错误/超时）→ 自动切换到 WebSocket `API.send()`
- 首次使用 SSE，如果后端未支持，也能正常回退

### 4.2 Markdown 渲染（marked.js + DOMPurify）

**依赖：**
- `marked` (~30KB gzip) — Markdown 解析
- `dompurify` (~10KB gzip) — XSS 过滤

**配置文件 `src/utils/markdown.js`：**

```js
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { copyCodeBlock } from './dom.js';

// 自定义 renderer
const renderer = new marked.Renderer();

// 代码块：保留语言标签 + 复制按钮
renderer.code = function({ text, lang }) {
  const langLabel = lang ? `<span class="code-lang">${lang}</span>` : '';
  return `<div class="code-block-wrapper">${langLabel}
    <button class="code-copy-btn">📋</button>
    <pre><code>${text}</code></pre></div>`;
};

// 链接：新窗口打开
renderer.link = function({ href, text }) {
  return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
};

// 表格：添加样式类
renderer.table = function({ header, body }) {
  return `<div class="table-wrapper"><table class="markdown-table"><thead>${header}</thead><tbody>${body}</tbody></table></div>`;
};

marked.setOptions({ renderer, breaks: true, gfm: true });

export function renderMarkdown(text) {
  if (!text) return '';
  const rawHtml = marked.parse(text);
  return DOMPurify.sanitize(rawHtml, {
    ADD_TAGS: ['button'],
    ADD_ATTR: ['onclick', 'class'],
  });
}
```

**新增 CSS（components.css 中追加）：**

```css
/* 表格 */
.markdown-table { width: 100%; border-collapse: collapse; margin: 8px 0; }
.markdown-table th,
.markdown-table td { padding: 8px 12px; border: 1px solid var(--border); text-align: left; }
.markdown-table th { background: var(--bg-hover); font-weight: 600; }
.table-wrapper { overflow-x: auto; }

/* 引用 */
blockquote { border-left: 3px solid var(--primary); padding: 8px 16px; margin: 8px 0; background: var(--primary-subtle); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }

/* 标题 */
.message-bubble h1, .message-bubble h2, .message-bubble h3 { margin: 12px 0 6px; }
.message-bubble h1 { font-size: 18px; }
.message-bubble h2 { font-size: 16px; }
.message-bubble h3 { font-size: 15px; }

/* 链接 */
.message-bubble a { color: var(--primary); text-decoration: underline; }
.message-bubble a:hover { color: var(--primary-hover); }
```

**代码块复制按钮事件绑定：**
- 在 `renderMarkdown` 后，通过事件委托绑定 `chatMessages` 上的 `.code-copy-btn` 点击事件
- 避免 innerHTML 中的 onclick（DOMPurify 会过滤）

### 4.3 消息搜索

**位置：** 侧边栏标题下方，新建对话按钮上方

**实现：**
```js
// chat/search.js
export function initSearch(inputEl) {
  inputEl.addEventListener('input', debounce(() => {
    const query = inputEl.value.trim().toLowerCase();
    highlightMatches(query);
  }, 200));
}

function highlightMatches(query) {
  // 清除旧高亮
  document.querySelectorAll('.search-highlight').forEach(el => {
    el.replaceWith(el.textContent);
  });
  if (!query) return;
  // 在当前消息中搜索
  const messages = document.querySelectorAll('.message-bubble');
  messages.forEach(msg => {
    // 使用 TreeWalker 遍历文本节点，包裹匹配文本
    // ...
  });
  // 滚动到第一个匹配
  const first = document.querySelector('.search-highlight');
  if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
```

**UI：**
```html
<div class="sidebar-search">
  <input type="text" placeholder="搜索消息..." class="search-input" id="searchInput">
  <span class="search-count" id="searchCount"></span>
</div>
```

### 4.4 错误提示（Toast 通知系统）

**新增 `utils/toast.js`：**

```js
export function showToast(message, type = 'info', duration = 3000) {
  const container = getOrCreateContainer();
  const toast = document.createElement('div');
  toast.className = `toast-item toast-${type}`;
  toast.setAttribute('role', 'alert');
  toast.setAttribute('aria-live', 'assertive');
  toast.innerHTML = `
    <span class="toast-icon">${typeIcon(type)}</span>
    <span class="toast-text">${escapeHtml(message)}</span>
    <button class="toast-close" aria-label="关闭">✕</button>
  `;
  container.appendChild(toast);
  // 关闭按钮
  toast.querySelector('.toast-close').addEventListener('click', () => removeToast(toast));
  // 自动消失
  if (duration > 0) setTimeout(() => removeToast(toast), duration);
}
```

**替换计划：**
- 网络断开 → `showToast('连接已断开，正在重连...', 'warning', 0)`（不自动消失）
- 网络恢复 → 移除断开 toast，`showToast('已重新连接', 'success')`
- 消息发送失败 → 消息气泡旁显示重试按钮 + toast
- API 错误 → `showToast(error.message, 'error')`

### 4.5 加载骨架屏

**会话列表骨架：**
```js
function showSessionSkeleton(container) {
  container.innerHTML = Array(5).fill(`
    <div class="session-item skeleton-item">
      <div class="skeleton" style="width:70%;height:14px;margin-bottom:6px"></div>
      <div class="skeleton" style="width:40%;height:10px"></div>
    </div>
  `).join('');
}
```

**监控页面骨架：** 在 API 返回前，4 个指标卡片显示骨架占位。

---

## 5. 无障碍 + 响应式

### 5.1 ARIA 标签清单

| 元素 | 添加的 ARIA |
|---|---|
| `.chat-messages` | `role="log" aria-live="polite" aria-label="对话消息"` |
| `.message` | `role="article" aria-label="{发言人} 说"` |
| `.nav-header` | 改为 `<header>` + `aria-label="主导航"` |
| `.sidebar` | 改为 `<aside>` + `aria-label="会话历史"` |
| `.chat-area` | 改为 `<main>` |
| `#chatInput` | `aria-label="输入消息" aria-describedby="inputHint"` |
| `#btnSend` | `aria-label="发送消息"` |
| `.quick-prompt-card` | `role="button" aria-label="{标题}: {描述}"` |
| `.shortcuts-overlay` | `role="dialog" aria-modal="true" aria-label="键盘快捷键"` |
| `.toast-item` | `role="alert" aria-live="assertive"` |
| 监控环形图 | `role="img" aria-label="SLA 达标率 95%"` |
| 监控柱状图 | `role="img" aria-label="Agent 负载分布摘要"` |

### 5.2 键盘导航

**焦点可见样式：**
```css
:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

/* 移除鼠标点击的焦点环 */
:focus:not(:focus-visible) {
  outline: none;
}
```

**焦点陷阱（弹窗）：**
```js
function trapFocus(modal) {
  const focusable = modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  const first = focusable[0];
  const last = focusable[focusable.length - 1];

  modal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
}
```

**Skip to content 链接：**
```html
<a href="#chatInput" class="skip-link">跳到内容</a>
```
```css
.skip-link {
  position: absolute;
  top: -40px;
  left: 0;
  padding: 8px 16px;
  background: var(--primary);
  color: white;
  z-index: 9999;
  transition: top 0.2s;
}
.skip-link:focus { top: 0; }
```

### 5.3 对比度修复

```css
:root {
  /* 修复前 #6b7280 (对比度 ~3.2:1) → 修复后 */
  --text-muted: #94a3b8;    /* 对比度 ≥ 4.5:1 on #0f1117 */
}
```

### 5.4 移动端优化

**侧边栏抽屉模式：**
```css
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0; top: var(--header-height); bottom: 0;
    width: 280px;
    z-index: 150;
    transform: translateX(-100%);
    transition: transform var(--duration-normal) var(--ease-out);
  }
  .sidebar.open { transform: translateX(0); }
  .sidebar-backdrop {
    position: fixed;
    inset: 0;
    background: var(--bg-overlay);
    z-index: 140;
    display: none;
  }
  .sidebar-backdrop.open { display: block; }

  /* 汉堡菜单按钮 */
  .nav-menu-btn { display: flex; }
}
```

**触摸目标：**
```css
@media (max-width: 768px) {
  .btn-feedback, .btn-msg-action, .btn-delete-session {
    min-width: 44px;
    min-height: 44px;
  }
}
```

**虚拟键盘适配：**
```js
// chat/input.js
if ('visualViewport' in window) {
  window.visualViewport.addEventListener('resize', () => {
    const inputArea = document.querySelector('.chat-input-area');
    inputArea.style.bottom = `${window.innerHeight - window.visualViewport.height}px`;
  });
}
```

---

## 6. 实施分阶段

### Phase 1：Vite 基础 + 代码重构
- 初始化 Vite 项目（package.json + vite.config.js）
- CSS 拆分为 8 个文件
- JS 拆分为模块
- HTML 内联代码提取
- 消除重复代码
- 确保功能完全不变（重构不改行为）

### Phase 2：UX 增强
- 引入 marked.js + DOMPurify，替换 Markdown 渲染
- SSE 流式打字动画
- Toast 通知系统
- 消息搜索
- 骨架屏

### Phase 3：无障碍 + 响应式
- ARIA 标签
- 键盘导航 + 焦点管理
- 对比度修复
- 移动端侧边栏抽屉
- 触摸目标优化
- Skip to content

### Phase 4：FastAPI 集成 + 测试
- 修改 FastAPI 静态文件挂载
- 开发/生产模式配置
- 全端测试验证

---

## 7. 依赖清单

| 包 | 版本 | 用途 | 体积 |
|---|---|---|---|
| vite | latest | 构建工具 | devDep |
| marked | ^15 | Markdown 解析 | ~30KB gzip |
| dompurify | ^3 | XSS 过滤 | ~10KB gzip |

仅 2 个运行时依赖，总计 ~40KB gzip。
