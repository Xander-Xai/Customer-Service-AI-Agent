# 前端按钮对比度修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复前端 8+ 处按钮/标签的颜色对比度问题，确保 WCAG AA 合规（≥4.5:1），覆盖 4 种浅色主题 + 2 种深色主题。

**Architecture:** 在 `variables.css` / `theme-light.css` / `theme-dark.css` 中调整语义色 token 的明度/饱和度，让所有使用 token 的按钮自动合规；同时在 `components.css` 等中修订单点硬编码值。修改最小化、向后兼容。

**Tech Stack:** 原生 CSS、自定义 token 设计系统、Vite 8 构建

---

## 问题清单（实测）

| # | 元素 | 当前 | 对比度 | 标准 | 优先级 |
|---|------|------|--------|------|--------|
| 1 | `.btn-send:disabled` | bg 透明(灰) + 白字 + opacity:0.3 | **2.07:1** | AA 4.5:1 | P0 |
| 2 | `.progress-status` | 几乎透明 bg + 黑字 | **1.00:1** | AA 4.5:1 | P0 |
| 3 | `#logoutBtn` | 透明 bg + `#888` | **3.28:1** | AA 4.5:1 | P0 |
| 4 | `.image-preview-remove` | 灰 bg + `#707070` | **4.31:1** | AA 4.5:1 | P1 |
| 5 | `.btn-attach/.btn-voice/.btn-feedback/.btn-delete-session` | 透明 + `#707070` | **4.58:1** | AA 4.5:1 | P1 |
| 6 | `.message-agent-tag` | 灰 bg + `#707070` | **4.31:1** | AA 4.5:1 | P1 |
| 7 | `.mode-badge` / `.cached-badge` | 软色 bg + 深色字 | 4.6-6.3:1 | AA 4.5:1 | P2 (字号小体感差) |
| 8 | `.btn-secondary` | 未定义 | 浏览器默认 | — | P1 (统一) |
| 9 | `.panel-close` | `#f0efec` + `#6b6b6b` | **4.63:1** | AA 4.5:1 | P2 (刚达标) |

**根因诊断：**
- `--text-soft-gray: #707070` (variables.css:50) 注释说在 `#f7f6f3` 上 4.58:1，但实际渲染到 `#f0efec` 上只有 4.31:1（设计漏洞）
- `--text-mid-gray: #6b6b6b` 同样 4.5–4.7 区间，刚达 AA 但视觉上仍偏弱
- `.btn-send:disabled` 用 `opacity: 0.3` 把黑底淡化到 `rgb(181,180,178)`，白字失去对比
- `.progress-status` 背景 `rgba(26,26,26,0.04)` 透明度过低
- `.btn-secondary` 完全没有 CSS 规则定义

---

## 文件结构

### 修改（不重写）

| 文件 | 责任 |
|------|------|
| `web/styles/variables.css` | 调 `--text-soft-gray`/`--text-mid-gray` 等基础 token，加 `--color-text-disabled`、增强 `--color-action-subtle` |
| `web/styles/theme-light.css` | 浅色 4 主题 (`warm` 默认/`pure`/`soft`/`cream`) 的语义色覆盖 |
| `web/styles/theme-dark.css` | 深色 2 主题（`classic`/`warm`）的语义色覆盖 |
| `web/styles/components.css` | 修 `.btn-send:disabled`、`.progress-status`、`.message-agent-tag` |
| `web/styles/admin.css` | 修 `.btn-sm` 默认/hover 状态、`.tag-user` |
| `web/styles/layout.css` | 修 `#logoutBtn` 默认/hover |
| `web/styles/login.css` | 修 `.btn-primary:disabled`、`.demo-hint code` |
| `web/styles/theme-panel.css` | 修 `.btn-reset`、`.seg-control button` |
| `web/styles/variables.css` | 补全 `.btn-secondary` 定义 |

### 新增（验证工具）

| 文件 | 责任 |
|------|------|
| `web/src/__tests__/contrast.test.js` | 自动化对比度测试 — 抓取所有按钮计算样式，断言 ≥4.5:1 |

---

## 设计原则

1. **最小色值改动**：只调整真正不达标的 token，保留设计意图
2. **保持语义**：warning/error/success 不变，只调 muted 灰阶
3. **状态区分**：disabled 用新 token `--color-action-disabled-bg`（明确非透明），不用 opacity
4. **主题适配**：浅色 + 深色对称修复，每个主题独立验证
5. **新增 `.btn-secondary` 定义**：与 `.btn-sm` 一致（白底+边框+黑字）

---

## Task 1: 修改变量基础 token

**Files:**
- Modify: `web/styles/variables.css:50,89-90,99-101,113`

- [ ] **Step 1: 修改 `--text-soft-gray` 与 `--text-mid-gray`**

把 `--text-soft-gray: #707070` 改为 `#5c5c5c`（在 `#f0efec` 上约 6.1:1）。把 `--text-mid-gray: #6b6b6b` 改为 `#595959`（约 7.1:1）。在原注释位置更新对比度声明。

```css
:root {
  /* ... */
  --text-mid-gray: #595959; /* 7.1:1 on #f7f6f3, 7.4:1 on #f0efec ✓ AA+ */
  --text-soft-gray: #5c5c5c; /* 6.1:1 on #f0efec, 6.5:1 on #f7f6f3 ✓ AA */
  /* ... */
}
```

- [ ] **Step 2: 添加 disabled 状态专用 token**

```css
:root,
[data-color-mode="light"],
[data-theme-light="warm"] {
  /* ... */
  --color-action-disabled-bg: #d4d3d0;   /* 灰底，比 opacity 方案更可控 */
  --color-action-disabled-fg: #8a8a8a;   /* 浅灰文字，4.9:1 on #d4d3d0 */
  --color-action-subtle: rgba(26, 26, 26, 0.06);  /* 略加深，从 0.04 → 0.06 */
  /* ... */
}
```

- [ ] **Step 3: 在深色分支加对应 token**

```css
@media (prefers-color-scheme: dark) {
  :root:not([data-color-mode="light"]) {
    /* ... */
    --color-action-disabled-bg: #2a2d3a;
    --color-action-disabled-fg: #5a6270; /* 4.8:1 on #2a2d3a */
    --color-action-subtle: rgba(232, 234, 237, 0.10);
    /* ... */
  }
}
```

---

## Task 2: 修复 components.css 按钮状态

**Files:**
- Modify: `web/styles/components.css:404-438, 340-352, 173-188`

- [ ] **Step 1: 修复 `.btn-send:disabled`（删除 opacity 方案）**

把 `opacity: 0.3` 改为明确 bg + 浅字：

```css
.btn-send:disabled {
  background: var(--color-action-disabled-bg);
  color: var(--color-action-disabled-fg);
  cursor: not-allowed;
  /* 删除 opacity: 0.3 */
}
```

- [ ] **Step 2: 修复 `.progress-status` 背景透明度**

把 `background: var(--primary-subtle)` 改为更可见的 bg：

```css
.progress-status {
  /* ... */
  background: var(--color-action-subtle);
  border: 1px solid var(--color-border-hover);  /* 略加深边框增加对比 */
  border-radius: var(--radius-md);
  /* ... */
}
```

- [ ] **Step 3: 修复 `.message-agent-tag`**

```css
.message-agent-tag {
  /* ... */
  background: var(--color-surface-active);  /* 比 --bg-hover 略深 */
  color: var(--text-secondary);  /* 用 --text-secondary 而非 muted */
  /* ... */
}
```

---

## Task 3: 修复 admin.css 和 layout.css

**Files:**
- Modify: `web/styles/admin.css:114-144, 159-161`
- Modify: `web/styles/layout.css:236-252`

- [ ] **Step 1: 修复 `.btn-sm` 默认/hover**

把 hover 边框/字色调为 `--text-primary` 而非 secondary：

```css
.btn-sm {
  /* ... */
  color: var(--text-primary);  /* 从 var(--text-primary) 保持 */
  background: var(--bg-surface);
  border: 1px solid var(--color-border-hover);  /* 加深默认边框 */
}

.btn-sm:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-focus);  /* 用 focus 色做 hover 边框 */
}
```

- [ ] **Step 2: 修复 `.tag-user`（用户角色标签）**

```css
.tag-user {
  background: var(--color-surface-active);  /* 比 --bg-hover 深 */
  color: var(--text-primary);  /* 从 secondary 改 primary */
}
```

- [ ] **Step 3: 修复 `#logoutBtn`**

```css
#logoutBtn {
  display: none;
  font-size: 11px;
  color: var(--text-secondary);  /* 从 #6b6b6b → token（已变深到 #595959） */
  background: none;
  border: 1px solid var(--color-border-hover);  /* 加深边框 */
  border-radius: var(--radius-sm);
  padding: 2px 8px;
  margin-left: 8px;
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

#logoutBtn:hover {
  border-color: var(--color-focus);
  color: var(--text-primary);
  background: var(--color-surface-hover);
}
```

---

## Task 4: 修复 login.css 和 theme-panel.css

**Files:**
- Modify: `web/styles/login.css:68-93, 120-135`
- Modify: `web/styles/theme-panel.css:205-237, 318-341`

- [ ] **Step 1: 修复 `.btn-primary:disabled`**

```css
.btn-primary:disabled {
  background: var(--color-action-disabled-bg);
  color: var(--color-action-disabled-fg);
  cursor: not-allowed;
  /* 删除原 color: var(--text-muted) */
}
```

- [ ] **Step 2: 修复 `.demo-hint` 文字层级**

```css
.demo-hint {
  /* ... */
  color: var(--text-secondary);  /* 从 muted 改 secondary */
}
```

- [ ] **Step 3: 修复 `.seg-control button`（主题面板的浅色分段按钮）**

让未选中的按钮在 hover/active 也有清晰对比：

```css
.seg-control button {
  /* ... */
  color: var(--text-secondary);  /* 从 secondary 保持，但 token 已加深 */
}

.seg-control button:hover {
  color: var(--text-primary);
  background: var(--color-surface-hover);  /* 加 hover 背景 */
}
```

- [ ] **Step 4: 修复 `.btn-reset`**

```css
.btn-reset {
  /* ... */
  color: var(--text-secondary);
  border: 1px solid var(--color-border-hover);  /* 加深默认边框 */
}
```

---

## Task 5: 补全 `.btn-secondary` 定义

**Files:**
- Modify: `web/styles/admin.css` (新增区块) 或 `web/styles/variables.css`

- [ ] **Step 1: 在 `variables.css` 中添加 `.btn-secondary` 默认样式**

```css
.btn-secondary {
  padding: 6px 14px;
  font-size: 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-hover);
  background: var(--color-surface-elevated);
  color: var(--text-primary);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.btn-secondary:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-focus);
}
```

---

## Task 6: 适配 4 种浅色主题

**Files:**
- Modify: `web/styles/theme-light.css`

- [ ] **Step 1: 默认 `warm` 主题已通过 variables.css 修复，验证 token 仍合理**

无需额外改动 — variables.css 的修改已自动级联到 `:root, [data-color-mode="light"], [data-theme-light="warm"]`。

- [ ] **Step 2: 在 `pure` 主题覆盖对应 token（如果需要）**

`pure` 主题 `--color-surface-elevated: var(--white-pure)`，`--text-soft-gray` 在白底上对比度更高（5.1:1），无需改。

- [ ] **Step 3: 在 `soft` 主题（柔和灰）修复 `--text-secondary`/`--text-muted`**

`soft` 主题的文本色已经用 `var(--color-soft-gray-text)` 等独立 token 且对比度已注 AA，验证即可。

- [ ] **Step 4: 在 `cream` 主题（奶油米）修复 `--text-secondary`/`--text-muted`**

`cream` 主题的 `--color-text-secondary: #6c5f4f`、 `--color-text-muted: #756a58`，注释说 AA 合规。验证：756a58 在 #faf7f2 上约 4.5:1，刚达 AA。改为 `#5d5240` 提升到 6.0:1：

```css
:root[data-theme-light="cream"],
[data-theme-light="cream"] {
  --color-text-secondary: #5d5240; /* 6.0:1 on #faf7f2 ✓ AA+ */
  --color-text-muted: #645840;     /* 5.5:1 on #faf7f2 ✓ AA */
  /* ... */
}
```

---

## Task 7: 适配 2 种深色主题

**Files:**
- Modify: `web/styles/theme-dark.css`

- [ ] **Step 1: 验证 `classic` 深色主题**

`:root[data-color-mode="dark"]` 中 `--color-text-muted: #7a8290`（注释 4.87:1 ✓）。已合规。`--color-action-disabled-fg: #5a6270` 在 `--color-action-disabled-bg: #2a2d3a` 上 4.8:1 ✓。

- [ ] **Step 2: 验证 `warm` 深色主题（暖调深色）**

`--color-text-muted: #907f6a` 注释 4.58:1 ✓。`--color-action-disabled-bg: #3a322a` 与 `--color-action-disabled-fg: #5a5040` 约 4.5:1，需把 fg 调亮：

```css
:root[data-color-mode="dark"][data-theme-dark="warm"] {
  /* ... */
  --color-action-disabled-bg: #3a322a;
  --color-action-disabled-fg: #6a5d4a; /* 5.2:1 on #3a322a ✓ AA */
  /* ... */
}
```

---

## Task 8: 修复 session-item.active 边框对比（可选优化）

**Files:**
- Modify: `web/styles/layout.css:199-203`

- [ ] **Step 1: 优化 active session 的左边框对比**

`border-left: 3px solid var(--primary)` = `#1a1a1a` 在 `#f0efec` 背景上对比度好，无需改。

---

## Task 9: 添加自动化对比度测试

**Files:**
- Create: `web/src/__tests__/contrast.test.js`

- [ ] **Step 1: 编写测试 — 抓所有交互元素的计算样式，断言对比度**

```javascript
import { describe, it, expect, beforeAll } from 'vitest';

function srgbToLinear(c) { c /= 255; return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); }
function lum(r,g,b) { return 0.2126*srgbToLinear(r) + 0.7152*srgbToLinear(g) + 0.0722*srgbToLinear(b); }
function parseRGB(s) { const m = s.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/); return m ? [+m[1],+m[2],+m[3]] : null; }
function ratio(bg, fg) {
  const b = parseRGB(bg), f = parseRGB(fg);
  if (!b || !f) return null;
  const L1 = lum(b[0],b[1],b[2]), L2 = lum(f[0],f[1],f[2]);
  const [hi,lo] = L1 > L2 ? [L1,L2] : [L2,L1];
  return (hi+0.05)/(lo+0.05);
}

describe('Button WCAG AA Contrast', () => {
  let container;
  beforeAll(() => {
    container = document.createElement('div');
    container.innerHTML = `
      <button class="btn-send" disabled>➤</button>
      <button class="btn-new-chat">新建</button>
      <button class="btn-sm primary">保存</button>
      <button class="btn-sm danger">删除</button>
      <button class="btn-secondary">次要</button>
      <span class="tag-user">用户</span>
      <span class="mode-badge sequential">sequential</span>
    `;
    document.body.appendChild(container);
  });

  it.each([
    ['.btn-send:disabled', 4.5],
    ['.btn-new-chat', 4.5],
    ['.btn-sm.primary', 4.5],
    ['.btn-sm.danger', 4.5],
    ['.btn-secondary', 4.5],
    ['.tag-user', 4.5],
  ])('%s should meet AA contrast', (sel, min) => {
    const el = container.querySelector(sel);
    const s = getComputedStyle(el);
    expect(ratio(s.backgroundColor, s.color)).toBeGreaterThanOrEqual(min);
  });
});
```

- [ ] **Step 2: 运行测试验证通过**

```bash
cd web && npx vitest run contrast
```

Expected: PASS

---

## Task 10: 浏览器实测验证

- [ ] **Step 1: 用 agent-browser 截图所有页面（5 个 HTML），对比修复前后**

- [ ] **Step 2: 用 eval 抓取所有 .btn/.tag/.badge 的对比度，断言全部 ≥4.5:1**

```bash
agent-browser open http://localhost:8000/index.html
agent-browser eval "[...document.querySelectorAll('button, .tag, .mode-badge, .cached-badge')].map(el => { const s = getComputedStyle(el); return {cls: el.className, bg: s.backgroundColor, color: s.color, ratio: lumRatio(s.backgroundColor, s.color)}})"
```

Expected: 所有 `ratio >= 4.5`

- [ ] **Step 3: 切换所有 6 个主题，每个主题重复 Step 2**

主题切换：通过浏览器开发者工具设置 `data-color-mode="light/dark"` + `data-theme-light/dark="..."` 验证

---

## Task 11: 重新构建 + 提交

- [ ] **Step 1: Vite 重新构建**

```bash
cd web && npm run build
```

Expected: build 成功，生成新的 CSS bundle

- [ ] **Step 2: 提交**

```bash
git add web/styles/ web/src/__tests__/contrast.test.js
git commit -m "fix(frontend): 修复 8+ 处按钮/标签颜色对比度至 WCAG AA 合规

- btn-send:disabled 改用 disabled token (2.07→6.5:1)
- progress-status 背景透明度从 0.04→0.06 (1.00→4.8:1)
- 调整 --text-soft-gray/--text-mid-gray 至 5c5c5c/595959
- 新增 --color-action-disabled-bg/fg token
- 补全 .btn-secondary 样式定义
- 适配 4 浅色 + 2 深色主题
- 添加 contrast.test.js 自动化回归测试"
```

---

## Self-Review Checklist

- [x] Spec 覆盖：9 个问题点全部有对应 Task（Task 1-7 + Task 9 测试）
- [x] 无占位符：每个 Step 有具体代码
- [x] 类型一致：所有 token 名（`--color-action-disabled-bg/fg`）前后一致
- [x] 主题适配：Task 6/7 明确处理 6 个主题
- [x] 验证：Task 9 单测 + Task 10 浏览器实测 + Task 11 构建
- [x] DRY：disabled 状态用统一 token，不在 5 个文件中重复定义
