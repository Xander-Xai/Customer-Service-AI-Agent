# 主题切换与无障碍偏好系统 — 实施开发文档

**配套设计文档**: `docs/superpowers/specs/2026-06-09-theme-switcher-design.md`
**日期**: 2026-06-09
**状态**: ⏸️ 等待审核

---

## 0. 当前进度

| 步骤 | 内容 | 状态 |
|-----|------|------|
| 1 | 调研现状（layout/login/monitor/admin/responsive/animations CSS + 三个 HTML + main/admin/login.js 入口） | ✅ |
| 2 | `web/styles/variables.css` 重构为 3 层 token 架构 + 旧名 alias | ✅ **已写未提交** |
| 3 | `web/styles/theme-light.css`（4 种浅色变体） | ⏳ 待审核 |
| 4 | `web/styles/theme-dark.css`（2 种深色变体 + 系统深色） | ⏳ 待审核 |
| 5 | `web/styles/theme-a11y.css`（字号/行高/动画） | ⏳ 待审核 |
| 6 | `web/src/utils/theme.js`（主题管理 JS） | ⏳ 待审核 |
| 7 | 三个 HTML 嵌入早期防 FOUC 脚本（同步阻塞） | ⏳ 待审核 |
| 8 | 设置面板 UI（齿轮按钮 + 侧边抽屉） | ⏳ 待审核 |
| 9 | 硬编码颜色 → 语义 token（components.css / layout.css / login.css） | ⏳ 待审核 |
| 10 | 设置面板 CSS 样式 | ⏳ 待审核 |
| 11 | main.js / admin.js / login.js 接入 theme.js 初始化 | ⏳ 待审核 |
| 12 | `web/src/__tests__/theme.test.js` 单测 | ⏳ 待审核 |
| 13 | 跑测试 + 浏览器自检 + git commit | ⏳ 待审核 |

**已落盘文件**：`web/styles/variables.css`（重构版）。
**未落盘改动**：上述 3-13 步骤。审核通过后继续。

---

## 1. 设计文档中的 5 处隐含问题（先报告）

我对照现有代码后，发现设计文档需要补充/修正的 5 点，**写代码前需你确认处理方式**：

### 1.1 Vite 静态资源如何引入防 FOUC 脚本？

设计文档说"防 FOUC 早期脚本必须是**同步** `<script>`"——但当前项目用 Vite 打包（`vite.config.js` + `<script type="module" src="/src/main.js">`），所有 JS 都被打包成 module。

**方案 A**（推荐）：把防 FOUC 脚本内联到 `index.html` / `admin.html` / `login.html` 的 `<head>` 中（不引用外部文件），同步阻塞执行。这是设计文档里的默认方案，**Vite 不参与这段内联代码**——HTML 由 Vite dev server / 静态构建输出，`<script>` 内容是字面量。

**方案 B**：构建时把脚本作为 raw asset 注入。但需要改 Vite 配置。

✅ **采用方案 A**，无需 Vite 配置变更。

### 1.2 Vite 是否需要在 import 链中加新 CSS？

设计文档列出 `variables.css` / `theme-light.css` / `theme-dark.css` / `theme-a11y.css` / `theme.js` —— 都需要被前端模块引用才能打包。

**确认方案**：在 `src/main.js` / `src/admin.js` / `src/login.js` 中按顺序 import：

```js
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/theme-dark.css';
import '../styles/theme-a11y.css';
import { initSettingsPanel } from './utils/theme.js';
```

⚠️ **顺序敏感**：`variables.css`（默认暖骨白 token）必须先 import；`theme-light.css` 和 `theme-dark.css` 通过 `[data-theme-light=*]` / `[data-color-mode=dark]` 选择器**覆盖**基础 token。

### 1.3 设置面板的齿轮按钮放哪？

设计文档说"在导航栏增加齿轮图标"。但 `admin.html` 顶部导航里**没有用户头像**或右侧操作组（只有 `userDisplay` + `退出`），空间够。`index.html` 已经有 `退出` / `管理` 链接，可放入。

✅ **方案**：在三个页面的 `nav-status` 区域（登录页可在 logo 旁）插入齿轮按钮。**登录页**：放在 `.login-card` 头部右侧（不影响布局）。

### 1.4 设置面板 UI 是新组件还是全局 modal？

设计文档画了"侧边设置面板"草图。

✅ **采用**：**右侧滑出式抽屉**（类似 `session-detail-panel` 模式），复用其 transform 动画机制。点击齿轮打开/关闭；点遮罩关闭；按 Esc 关闭；焦点陷阱。

### 1.5 Widget 是否要防 FOUC 脚本？

设计文档明确说"Widget 不会被 data-* 属性影响（实施时在 theme.js 中加作用域检查）"。

✅ **方案**：
- `widget.html` **不嵌入** 防 FOUC 脚本（Widget 是嵌入式浮窗，FOUC 风险小）
- `theme.js` 检测到 `body` 包含 `.widget-*` 类时**跳过初始化**
- 这样主题切换器不污染 Widget

---

## 2. 后续 12 步详细实施方案

### 步骤 3：`web/styles/theme-light.css`

**目的**：4 种浅色主题 variant（CSS 选择器覆盖默认 token 值）。

**实现要点**：
```css
/* 默认暖骨白已在 variables.css 中定义（`:root` 选择器）。
   本文件定义其他 3 种浅色变体 + 浅色模式显式选择器。 */

[data-color-mode="light"] {
  /* 与 :root 等价的完整浅色 token 集（确保 data-color-mode="light" 时强制覆盖） */
}

/* 4 种浅色变体 — 通过 [data-theme-light] 切换 */
[data-theme-light="pure"]  { /* 纯白 #ffffff base */ }
[data-theme-light="soft"]  { /* 柔和灰 #f8f9fa base */ }
[data-theme-light="cream"] { /* 奶油米 #faf7f2 base */ }
/* warm 已在 :root 默认 */
```

**关键约束**：
- 4 种变体只覆盖**主背景**相关的 token（`--color-surface-base/elevated/sunken/hover/active` + `--color-border/border-hover` + `--color-text-*`）
- 动作主色、语义色保持不变（保证品牌一致）
- 4 个变体使用**相同结构**，只改色值

**代码量预估**：约 80-100 行。

---

### 步骤 4：`web/styles/theme-dark.css`

**目的**：2 种深色变体 + 显式深色模式选择器。

**实现要点**：
```css
/* 显式深色模式（覆盖 @media 跟随系统的规则） */
[data-color-mode="dark"] {
  /* 与 :root:not([data-color-mode="light"]) 等价的完整深色 token 集 */
}

/* 2 种深色变体 */
[data-color-mode="dark"][data-theme-dark="classic"] { /* 经典 #0f1117 base */ }
[data-color-mode="dark"][data-theme-dark="warm"]    { /* 暖调 #1a1814 base */ }
```

**关键约束**：
- 语义色必须**降饱和度**（设计文档已列出：`#8fcf94` 而非 `#346538`）
- `data-theme-dark` 仅在 `data-color-mode="dark"` 下生效（用复合选择器限定）
- 浅色模式 + `data-theme-dark=*` 不应改变外观（CSS 规则不匹配即可）

**代码量预估**：约 100-130 行。

---

### 步骤 5：`web/styles/theme-a11y.css`

**目的**：字号（4 档）、行高（3 档）、减少动画开关。

**实现要点**：
```css
/* 字号：影响 html 根元素 */
[data-font-size="small"]  html, html[data-font-size="small"] { font-size: 0.875rem; }
[data-font-size="medium"] html, html[data-font-size="medium"] { font-size: 1rem; }
[data-font-size="large"]  html, html[data-font-size="large"]  { font-size: 1.0625rem; }
[data-font-size="xlarge"] html, html[data-font-size="xlarge"] { font-size: 1.125rem; }

/* 行高：影响 body */
[data-line-height="compact"]  body, body[data-line-height="compact"]  { line-height: 1.5; }
[data-line-height="standard"] body, body[data-line-height="standard"] { line-height: 1.6; }
[data-line-height="relaxed"]  body, body[data-line-height="relaxed"]  { line-height: 1.8; }

/* 减少动画 */
[data-motion="reduced"] *,
[data-motion="reduced"] *::before,
[data-motion="reduced"] *::after {
  animation-duration: 0.01ms !important;
  animation-iteration-count: 1 !important;
  transition-duration: 0.01ms !important;
  scroll-behavior: auto !important;
}
```

**关键约束**：
- 字号选择器在 `html` 上，但 `data-*` 属性也在 `html` 上 → 选择器写成 `html[data-font-size="..."]`
- 默认状态：`html` 没有 `data-font-size` 属性，继承 `html { font-size: 14px }`（在 `variables.css` 中）
- `[data-font-size="medium"]` 与 `html { font-size: 14px }` 等价（1rem × 14px = 14px），保证向后兼容

**代码量预估**：约 30-40 行。

---

### 步骤 6：`web/src/utils/theme.js`

**目的**：暴露 `themeManager` 对象：getPrefs / setPref / 6 个 apply 方法 / reset / 监听系统偏好变化。

**API 设计**：
```js
export const THEME_STORAGE_KEY = 'user-prefs';

export const themeManager = {
  // 读取偏好（localStorage → 默认值）
  getPrefs(): { themeLight, colorMode, themeDark, fontSize, lineHeight, motion }

  // 设置单个偏好并应用
  setPref(key, value): void
  //   key ∈ 'themeLight' | 'colorMode' | 'themeDark' | 'fontSize' | 'lineHeight' | 'motion'
  //   value: 对应类型
  // 行为：写入 localStorage + 立即调用对应 apply + dispatch 'themechange' 事件

  // 6 个 apply 方法
  applyThemeLight(name): void       // 设置 data-theme-light
  applyColorMode(mode): void         // 设置 data-color-mode（'light' | 'dark' | 'system'）
  applyThemeDark(name): void         // 设置 data-theme-dark
  applyFontSize(size): void          // 设置 data-font-size
  applyLineHeight(height): void      // 设置 data-line-height
  applyMotion(motion): void          // 设置 data-motion

  // 恢复默认
  reset(): void

  // 监听系统偏好变化（用于 'system' 模式）
  watchSystem(): () => void          // 返回取消监听函数
};

export const DEFAULT_PREFS = {
  themeLight: 'warm',
  colorMode: 'system',
  themeDark: 'classic',
  fontSize: 'medium',
  lineHeight: 'standard',
  motion: 'full',
};

// 初始化：在 DOMContentLoaded 中调用
export function initTheme(): void
//   - 读取 localStorage
//   - 应用到 <html>
//   - 监听系统颜色方案变化（如果 colorMode === 'system'）
//   - 不初始化设置面板 UI（这是 initSettingsPanel 的职责）

// 设置面板 UI 初始化
export function initSettingsPanel(): void
//   - 找到齿轮按钮 + 抽屉
//   - 绑定事件
//   - 渲染当前状态
```

**关键约束**：
- localStorage 不可用时降级到内存（`window.__userPrefs`）
- `applyColorMode('system')` 不设置 `data-color-mode`，依赖 CSS `@media (prefers-color-scheme)` 生效
- 监听 `matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ...)`
- 事件：`window.dispatchEvent(new CustomEvent('themechange', { detail: { key, value } }))`

**代码量预估**：约 180-220 行（含 JSDoc 注释）。

---

### 步骤 7：3 个 HTML 嵌入防 FOUC 早期脚本

**实施位置**：
- `web/index.html` 第 4-5 行（`<meta charset>` 之后，`<title>` 之前）
- `web/admin.html` 第 4-5 行（同上）
- `web/login.html` 第 4-5 行（同上）

**脚本内容**（**所有页面统一**，约 18 行）：
```html
<script>
  (function() {
    try {
      var pref = JSON.parse(localStorage.getItem('user-prefs') || '{}');
      var html = document.documentElement;
      if (pref.themeLight) html.dataset.themeLight = pref.themeLight;
      if (pref.colorMode && pref.colorMode !== 'system') html.dataset.colorMode = pref.colorMode;
      if (pref.themeDark) html.dataset.themeDark = pref.themeDark;
      if (pref.fontSize) html.dataset.fontSize = pref.fontSize;
      if (pref.lineHeight) html.dataset.lineHeight = pref.lineHeight;
      if (pref.motion) html.dataset.motion = pref.motion;
    } catch(e) {}
  })();
</script>
```

**关键约束**：
- 同步脚本（**没有** `type="module"`、**没有** `defer`）
- 放在 `<head>` 顶部，CSS `<link>` 之前
- 不依赖任何外部资源
- 异常吞掉（即使 localStorage 被禁用也不影响首屏）

**注意**：Vite 打包后，HTML 中的内联 `<script>` 会原样保留在 `dist/index.html` 中（Vite 不改内联脚本内容）。✅

---

### 步骤 8：设置面板 UI（HTML + 内联样式？）

**方案 A（推荐）**：HTML 结构放在 3 个 HTML 文件中（齿轮按钮 + 抽屉），样式放在新的 `theme-panel.css`（步骤 10）。

**HTML 结构**（约 60 行，每个 HTML 都嵌入）：
```html
<button class="btn-settings" id="btnSettings" aria-label="打开偏好设置" title="偏好设置">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
</button>

<div class="settings-panel" id="settingsPanel" role="dialog" aria-modal="true" aria-label="偏好设置" aria-hidden="true">
  <div class="settings-backdrop" id="settingsBackdrop"></div>
  <aside class="settings-drawer">
    <div class="settings-header">
      <h2>⚙️ 偏好设置</h2>
      <button class="settings-close" id="btnSettingsClose" aria-label="关闭">✕</button>
    </div>
    <div class="settings-body">
      <fieldset class="settings-group">
        <legend>外观（浅色主题）</legend>
        <div class="theme-light-grid">
          <label class="theme-option"><input type="radio" name="themeLight" value="pure"><span>纯白简洁</span></label>
          <label class="theme-option"><input type="radio" name="themeLight" value="warm" checked><span>暖骨白 ⭐</span></label>
          <label class="theme-option"><input type="radio" name="themeLight" value="soft"><span>柔和灰</span></label>
          <label class="theme-option"><input type="radio" name="themeLight" value="cream"><span>奶油米</span></label>
        </div>
      </fieldset>

      <fieldset class="settings-group">
        <legend>颜色模式</legend>
        <div class="seg-control" data-key="colorMode">
          <button data-value="light" aria-pressed="true">浅色</button>
          <button data-value="dark" aria-pressed="false">深色</button>
          <button data-value="system" aria-pressed="false">跟随系统</button>
        </div>
      </fieldset>

      <fieldset class="settings-group" data-show-when="dark">
        <legend>深色主题</legend>
        <div class="theme-dark-grid">
          <label class="theme-option"><input type="radio" name="themeDark" value="classic" checked><span>经典深色</span></label>
          <label class="theme-option"><input type="radio" name="themeDark" value="warm"><span>暖调深色</span></label>
        </div>
      </fieldset>

      <fieldset class="settings-group">
        <legend>字号</legend>
        <div class="seg-control" data-key="fontSize">
          <button data-value="small">小</button>
          <button data-value="medium" aria-pressed="true">中</button>
          <button data-value="large">大</button>
          <button data-value="xlarge">超大</button>
        </div>
      </fieldset>

      <fieldset class="settings-group">
        <legend>行高</legend>
        <div class="seg-control" data-key="lineHeight">
          <button data-value="compact">紧凑</button>
          <button data-value="standard" aria-pressed="true">标准</button>
          <button data-value="relaxed">宽松</button>
        </div>
      </fieldset>

      <fieldset class="settings-group">
        <legend>动画</legend>
        <label class="switch-row">
          <input type="checkbox" id="chkMotion" checked>
          <span>启用动画效果</span>
        </label>
      </fieldset>

      <div class="settings-footer">
        <button class="btn-sm" id="btnSettingsReset">恢复默认</button>
      </div>
    </div>
  </aside>
</div>
```

**关键约束**：
- `role="dialog" aria-modal="true" aria-hidden="true"` 默认隐藏
- 单选 radio + 分段控件 (button + aria-pressed) 两种交互
- 焦点管理：打开时焦点移入抽屉，关闭时回到齿轮按钮
- Esc 关闭

---

### 步骤 9：硬编码颜色替换

**文件清单**（设计文档 + 实际代码扫描）：

| 文件 | 硬编码颜色 | 替换为 |
|-----|-----------|--------|
| `components.css` L126 | `#6B6B6B` (用户头像) | `var(--color-text-secondary)` |
| `components.css` L165 | `rgba(149, 100, 0, 0.15)` | `var(--warning-soft-border, rgba(149, 100, 0, 0.15))` 或新增 `--color-warning-border` |
| `components.css` L249 | `#F3EFFE` / `#6B21A8` | `var(--color-mode-react-bg)` / `var(--color-mode-react-text)` |
| `components.css` L592 | `rgba(255, 255, 255, 0.2)` (用户气泡边框) | 保留（半透明白，用于在深色 primary 上）|
| `components.css` L696 | `#F0EFEC` (代码块背景) | `var(--color-surface-hover)` |
| `components.css` L961 | `rgba(149, 100, 0, 0.2)` (搜索高亮) | `var(--color-search-hl)` |
| `layout.css` L4 | `#FAFAF8` (nav-header 背景) | `var(--color-surface-elevated)` |
| `layout.css` L127 | `#FAFAF8` (sidebar 背景) | `var(--color-surface-elevated)` |
| `layout.css` L154 | `white` (新建对话按钮) | `var(--color-text-inverse)` |
| `login.html` L23-24 | `#1A1A1A` (logo stroke) | `currentColor`（继承主题色） |

**额外发现**（设计文档未列出但需替换）：
- `admin.js` 中有 8 处内联 `style="color:..."`（硬编码 `#4ade80` / `#fbbf24` / `#f87171` / `#ef4444`）
- 这些是 JS 渲染的指标色，**保持不变**（设计文档没要求改 JS 内联颜色，监控指标色是设计系统的一部分）

**策略**：
- CSS 文件中硬编码 → 全部替换为 token
- JS 渲染的内联 style → 保持现状（这些是数据可视化的语义色，不属于"界面主题"）

---

### 步骤 10：设置面板 CSS 样式

**新文件**：`web/styles/theme-panel.css`（约 250-300 行）

**包含内容**：
- 齿轮按钮样式（`.btn-settings`）
- 抽屉背景遮罩（`.settings-backdrop`）
- 抽屉本体（`.settings-drawer`）—— 右侧滑出
- 分组字段集（`.settings-group`）
- 主题选项 radio 卡片（`.theme-option`）
- 分段控件（`.seg-control`）
- 开关（`.switch-row`）
- 响应式（移动端全屏抽屉）

**关键约束**：
- 触摸目标 ≥ 44×44px（WCAG 2.5.8）
- 焦点指示器对比度 ≥ 3:1
- 所有颜色用 token（不能硬编码）
- 与现有动画系统一致（`var(--duration-normal)` 等）

---

### 步骤 11：JS 入口接入

**`web/src/main.js`** 改动：
```js
import '../styles/variables.css';
import '../styles/theme-light.css';
import '../styles/theme-dark.css';
import '../styles/theme-a11y.css';
import '../styles/theme-panel.css';
// ... 原有 imports
import { initSettingsPanel } from './utils/theme.js';

document.addEventListener('DOMContentLoaded', () => {
  initSettingsPanel();  // 新增
  init();               // 原有
});
```

**`web/src/admin.js`** 改动：同上。

**`web/src/login.js`** 改动：同上。

**Widget 隔离**：`theme.js` 中：
```js
export function initSettingsPanel() {
  // 跳过 widget
  if (document.body.classList.contains('widget-page') ||
      document.querySelector('.widget-container')) {
    return;
  }
  // ... 正常初始化
}
```

---

### 步骤 12：单元测试

**新文件**：`web/src/__tests__/theme.test.js`

**测试用例**（约 8-12 个）：

| # | 用例 | 断言 |
|---|------|------|
| 1 | `getPrefs()` 默认值 | 返回 DEFAULT_PREFS 副本 |
| 2 | `getPrefs()` 读取 localStorage | localStorage 写入后读取一致 |
| 3 | `getPrefs()` localStorage 损坏时降级 | 返回默认值不抛错 |
| 4 | `setPref('colorMode', 'dark')` | `html.dataset.colorMode === 'dark'`，localStorage 更新 |
| 5 | `setPref('colorMode', 'system')` | 不设置 `data-color-mode`（依赖 CSS media query） |
| 6 | `applyThemeLight('pure')` | `html.dataset.themeLight === 'pure'` |
| 7 | `applyFontSize('large')` | `html.dataset.fontSize === 'large'` |
| 8 | `reset()` 清除 localStorage | 恢复默认值，移除所有 dataset |
| 9 | `setPref` 触发 `themechange` 事件 | spy event 收到正确 detail |
| 10 | `applyMotion('reduced')` | `html.dataset.motion === 'reduced'` |
| 11 | widget 跳过初始化 | 注入 `.widget-container` 元素，不挂载按钮 |

**mock 策略**：
- `localStorage` 用 jsdom 默认提供
- `matchMedia` mock（jsdom 不支持）

**代码量**：约 100-150 行。

---

### 步骤 13：测试 + 自检 + commit

**测试命令**：
```bash
cd /home/dev/projects/customer-service-ai-agent
npm test -- --run theme
```

**视觉自检**（手工）：
- [ ] 打开 `web/index.html`（Vite dev server）
- [ ] 切换 4 浅 + 2 深 = 8 种组合
- [ ] 字号 4 档、行高 3 档、减少动画
- [ ] 刷新页面偏好持久化
- [ ] 跨 3 个页面偏好一致
- [ ] 浏览器 DevTools 看 `data-*` 属性正确
- [ ] 检查 `widget.html` 不受主站主题影响

**commit 信息**：
```
feat(web): 主题切换与无障碍偏好系统

- 新增 4 浅 × 2 深 = 8 种主题变体
- 新增字号/行高/减少动画 3 类无障碍偏好
- 防 FOUC 早期脚本（同步阻塞）
- localStorage 持久化 + 系统偏好跟随
- 3 层 token 架构（原始层 + 语义层 + 组件层）
- 设置面板 UI（齿轮按钮 + 侧边抽屉）
- 全部 WCAG 2.2 AA 达标
- widget 作用域隔离
```

---

## 3. 风险与回滚

| 风险 | 缓解 |
|-----|------|
| 旧组件依赖 `--primary` 等旧名，删除后回归 | **保留所有旧名作为 alias**（已确认步骤 2 实现） |
| 主题切换导致 SVG `stroke` 颜色不一致 | 替换 SVG 内 `#1A1A1A` 为 `currentColor`（login.html logo） |
| Vite 生产构建去除内联 `<script>` | Vite **不会**改内联 `<script>`（仅打包 `type="module"` 引用） |
| 监控页面内联颜色（`#4ade80` 等）破坏深色模式 | **保持不变**（设计文档范围外；监控语义色不属界面主题） |
| 设置面板 UI 干扰现有快捷键（`?` 触发快捷键帮助） | 设置面板打开时屏蔽 `?` 快捷键 |

---

## 4. 实施顺序总结

我准备按以下顺序提交（**每步可独立验证**）：

```
1. variables.css (已写)  ← 审核后保留
2. theme-light.css
3. theme-dark.css
4. theme-a11y.css
5. theme-panel.css
6. theme.js
7. theme.test.js
8. 3 个 HTML 改 head + 嵌入面板 UI
9. main.js / admin.js / login.js 接入
10. 替换硬编码颜色（components/layout/login css）
11. npm test
12. 视觉自检
13. git add + commit
```

每完成 1-2 步会做一次 `git diff` 自检，避免大爆炸改动。

---

## 5. 请审核

✅ 通过的话回复"继续"，我从步骤 3 开始写代码。
🔧 需要调整的话指出哪一步要改。
❓ 有疑问的话直接问。

**关键确认点**：
1. **Widget 隔离方案**（步骤 6：检测 `.widget-container` 跳过初始化）— OK？
2. **JS 内联颜色不替换**（步骤 9：监控指标色属设计系统）— OK？
3. **保留所有旧变量名作为 alias**（步骤 2 已实现）— OK？
4. **不写新依赖、不改 Vite 配置** — OK？
5. **登录页齿轮按钮放在 logo 区域右侧** — OK？
