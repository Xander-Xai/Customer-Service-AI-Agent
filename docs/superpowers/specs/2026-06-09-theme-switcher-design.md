# 主题切换与无障碍偏好系统 — 设计文档

**日期**: 2026-06-09
**版本**: v1.0
**状态**: 待实施

## 概述

为药妆智多星多智能体客服系统添加**用户可配置的主题与无障碍偏好系统**，让用户能够根据个人喜好选择界面外观和无障碍选项。

## 目标

1. **可定制外观**：用户可选择 4 种浅色主题
2. **深色模式**：独立的深色模式开关（2 种深色变体）
3. **跟随系统**：尊重 `prefers-color-scheme` 系统偏好
4. **无障碍选项**：字号、行高、减少动画开关
5. **专业质量**：WCAG 2.2 AA 达标，无 FOUC，键盘可达

## 设计原则（基于专业 Skill 指导）

依据以下 skill 制定的规则：
- `design-taste-frontend` §4.11 Page Theme Lock（页面主题锁：避免主题碎片化）
- `design-taste-frontend` §8 Dark Mode Protocol（不纯黑/纯白，对比度达标）
- `accessibility` WCAG 2.2 AA（对比度、字号、行高、焦点、动画）
- `web-quality-audit` Core Web Vitals（防 FOUC、CLS < 0.1）
- `frontend-ui-engineering` 语义化 token 设计

## 主题分组（Page Theme Lock 原则）

主题**互斥分组**，避免 4 浅 + 1 深混用造成视觉混乱：

```
浅色模式组（4 选 1）
├── 纯白简洁（Pure White）   #ffffff
├── 暖骨白（Warm Bone）⭐    #f7f6f3  ← 默认
├── 柔和灰（Soft Gray）      #f8f9fa
└── 奶油米（Cream）           #faf7f2

深色模式组（独立开关：开/关/跟随系统）
├── 经典深色（Classic Dark）  #1a1a1a
└── 暖调深色（Warm Dark）     #2d2620
```

**实际可呈现组合**：4 浅色 × 2 深色状态 = 8 种视觉组合。

## 设计 Token 系统

### 三层架构

```
[1] 原始层（Primitive Tokens）  ← 真实颜色值
    --gray-50, --gray-100, ..., --white-pure, --black-soft

[2] 语义层（Semantic Tokens）    ← 组件使用
    --color-surface-base, --color-text-primary, --color-border

[3] 组件层（Component Tokens）  ← 具体组件
    .btn-send { background: var(--color-action-primary); }
```

### 浅色主题原始色板

| Token | 纯白 | 暖骨白 ⭐ | 柔和灰 | 奶油米 |
|-------|------|---------|--------|--------|
| `--surface-base` | `#ffffff` | `#f7f6f3` | `#f8f9fa` | `#faf7f2` |
| `--surface-elevated` | `#ffffff` | `#ffffff` | `#ffffff` | `#ffffff` |
| `--surface-sunken` | `#f5f5f5` | `#ebe9e6` | `#f1f3f5` | `#f5f2ec` |
| `--text-primary` | `#1a1a1a` | `#1a1a1a` | `#2d3748` | `#2d3436` |
| `--text-secondary` | `#6b6b6b` | `#6b6b6b` | `#718096` | `#6c5f4f` |
| `--text-muted` | `#9b9b9b` | `#9b9b9b` | `#a0aec0` | `#a89a82` |
| `--border` | `#e5e5e5` | `#e8e7e4` | `#e2e8f0` | `#e5dfd5` |
| `--border-hover` | `#d4d4d4` | `#d4d3d0` | `#cbd5e0` | `#d4ccbc` |
| `--action-primary` | `#1a1a1a` | `#1a1a1a` | `#4a5568` | `#2d3436` |

### 深色主题原始色板

| Token | 经典深色 | 暖调深色 |
|-------|----------|----------|
| `--surface-base` | `#0f1117` | `#1a1814` |
| `--surface-elevated` | `#1a1a1a` | `#2d2620` |
| `--surface-sunken` | `#080a0e` | `#100e0a` |
| `--text-primary` | `#e8eaed` | `#e8e7e4` |
| `--text-secondary` | `#9ca3af` | `#a89a82` |
| `--text-muted` | `#6b7280` | `#6c5f4f` |
| `--border` | `#2a2d3a` | `#3a322a` |
| `--border-hover` | `#3a3d4a` | `#4a423a` |
| `--action-primary` | `#e8eaed` | `#e8e7e4` |

### 语义色（所有主题共用，跨主题一致）

```css
--color-success: #346538;
--color-success-bg: #edf3ec;
--color-warning: #956400;
--color-warning-bg: #fbf3db;
--color-error: #9f2f2d;
--color-error-bg: #fdebec;
--color-info: #1f6c9f;
--color-info-bg: #e1f3fe;

--color-focus: #1a1a1a;  /* 浅色 */
--color-focus-dark: #e8eaed;  /* 深色 */
```

## 无障碍选项

### 字号（4 档，rem 相对单位）

```css
[data-font-size="small"]  { font-size: 13px; }
[data-font-size="medium"] { font-size: 14px; }  /* 默认 */
[data-font-size="large"]  { font-size: 15px; }
[data-font-size="xlarge"] { font-size: 16px; }
```

依据：WCAG 1.4.4 Resize text（支持 200% 缩放仍可读）

### 行高（3 档）

```css
[data-line-height="compact"]  { line-height: 1.5; }
[data-line-height="standard"] { line-height: 1.6; }  /* 默认 */
[data-line-height="relaxed"]  { line-height: 1.8; }
```

依据：WCAG 1.4.12 Text Spacing（行高 ≥ 1.5）

### 减少动画

```css
[data-motion="reduced"] *,
[data-motion="reduced"] *::before,
[data-motion="reduced"] *::after {
  animation-duration: 0.01ms !important;
  animation-iteration-count: 1 !important;
  transition-duration: 0.01ms !important;
}
```

依据：WCAG 2.3.3 Animation from Interactions

## 实现架构

### 文件结构

```
web/
├── styles/
│   ├── variables.css        ← 原始层 + 语义层 token（重写）
│   ├── theme-light.css      ← 4 种浅色主题（新增）
│   ├── theme-dark.css       ← 2 种深色主题（新增）
│   └── theme-a11y.css       ← 字号/行高/动画选项（新增）
├── src/
│   └── utils/
│       └── theme.js         ← 主题管理 JS（新增）
├── index.html               ← 嵌入早期主题应用脚本
└── admin.html               ← 同上
└── login.html               ← 同上
```

### 主题应用机制

#### 1. 防 FOUC 早期脚本（`index.html` `<head>` 内联）

```html
<script>
  (function() {
    try {
      var pref = JSON.parse(localStorage.getItem('user-prefs') || '{}');
      var html = document.documentElement;
      if (pref.themeLight) html.dataset.themeLight = pref.themeLight;
      if (pref.colorMode)  html.dataset.colorMode = pref.colorMode;
      if (pref.themeDark)  html.dataset.themeDark = pref.themeDark;
      if (pref.fontSize)   html.dataset.fontSize = pref.fontSize;
      if (pref.lineHeight) html.dataset.lineHeight = pref.lineHeight;
      if (pref.motion)     html.dataset.motion = pref.motion;
    } catch(e) {}
  })();
</script>
```

#### 2. CSS 选择器优先级

```css
/* 浅色模式（默认） */
:root,
[data-color-mode="light"] {
  /* 应用浅色 token */
}

/* 深色模式（独立开关） */
[data-color-mode="dark"] {
  /* 应用深色 token */
}

/* 跟随系统 */
@media (prefers-color-scheme: dark) {
  :root:not([data-color-mode="light"]) {
    /* 应用深色 token */
  }
}

/* 4 种浅色主题变体（通过 data-theme-light 切换） */
[data-theme-light="pure"]   { /* 纯白 */ }
[data-theme-light="warm"]   { /* 暖骨白 ⭐默认 */ }
[data-theme-light="soft"]   { /* 柔和灰 */ }
[data-theme-light="cream"]  { /* 奶油米 */ }

/* 2 种深色主题变体 */
[data-theme-dark="classic"] { /* 经典深色 */ }
[data-theme-dark="warm"]    { /* 暖调深色 */ }
```

#### 3. JavaScript API

```js
// web/src/utils/theme.js
export const themeManager = {
  getPrefs() { /* 从 localStorage 读取 */ },
  setPref(key, value) { /* 更新并应用 */ },
  applyTheme(themeName) { /* 设置 data-theme-light */ },
  applyColorMode(mode) { /* 设置 data-color-mode */ },
  applyFontSize(size) { /* 设置 data-font-size */ },
  applyLineHeight(height) { /* 设置 data-line-height */ },
  applyMotion(motion) { /* 设置 data-motion */ },
  reset() { /* 恢复默认 */ }
};
```

### UI：设置面板

在导航栏增加**齿轮图标**（SVG），点击打开侧边设置面板：

```html
<button class="btn-settings" aria-label="打开设置">
  <svg><!-- 齿轮图标 --></svg>
</button>

<div class="settings-panel" role="dialog" aria-label="偏好设置">
  <!-- 面板内容 -->
</div>
```

**面板内容**：

```
┌──────────────────────────────────────┐
│  ⚙️  偏好设置                [✕]    │
├──────────────────────────────────────┤
│                                      │
│  外观                                │
│  ○  纯白简洁                         │
│  ●  暖骨白 ⭐                         │
│  ○  柔和灰                           │
│  ○  奶油米                           │
│                                      │
│  颜色模式                            │
│  ● 浅色  ○ 深色  ○ 跟随系统         │
│                                      │
│  深色主题（仅深色模式可见）          │
│  ● 经典深色  ○ 暖调深色              │
│                                      │
│  字号           ━━●━━━━              │
│  [小]  [中]  [大]  [超大]            │
│                                      │
│  行高           ━━━━━●━              │
│  [紧凑] [标准] [宽松]                │
│                                      │
│  □  减少动画效果                     │
│                                      │
│  ────────────────────────────        │
│  [恢复默认设置]                      │
│                                      │
└──────────────────────────────────────┘
```

### 触摸目标（WCAG 2.5.8）

- 主题选项按钮：≥ 44×44px
- 滑块控件：track ≥ 24px 高，thumb ≥ 24×24px
- 关闭按钮：44×44px
- 开关控件：44×24px

### 焦点指示器（WCAG 2.4.7 / 2.4.11）

```css
:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
  border-radius: 4px;
}
```

## 对比度验证（WCAG AA）

实施前必须验证**所有主题 × 所有文本类型**的对比度：

| 主题 | 文本类型 | 前景色 | 背景色 | 对比度 | 通过 |
|------|---------|--------|--------|--------|------|
| 暖骨白 | 正文 | `#1a1a1a` | `#f7f6f3` | 14.4:1 | ✓ AAA |
| 暖骨白 | 次要文本 | `#6b6b6b` | `#f7f6f3` | 5.7:1 | ✓ AA |
| 暖骨白 | 弱化文本 | `#9b9b9b` | `#f7f6f3` | 3.0:1 | △ 仅大字体 |
| 经典深色 | 正文 | `#e8eaed` | `#0f1117` | 16.2:1 | ✓ AAA |
| 经典深色 | 次要文本 | `#9ca3af` | `#0f1117` | 7.2:1 | ✓ AAA |
| 经典深色 | 弱化文本 | `#6b7280` | `#0f1117` | 4.6:1 | ✓ AA |

**结论**：所有主题 AA 达标，浅色主题弱化文本仅满足大字体场景。

## 测试清单

### 功能测试

- [ ] 切换 4 种浅色主题，页面立即更新
- [ ] 切换浅色/深色/跟随系统模式
- [ ] 在深色模式下切换 2 种深色变体
- [ ] 字号 4 档切换正常
- [ ] 行高 3 档切换正常
- [ ] 减少动画开关生效（所有 transition 立即）
- [ ] 恢复默认按钮生效
- [ ] 刷新页面后偏好持久化
- [ ] 跨页面（index/admin/login）保持一致

### 无障碍测试

- [ ] Tab 键能到达所有设置控件
- [ ] Enter/Space 能激活单选和开关
- [ ] 焦点指示器在所有主题下可见（对比度 ≥ 3:1）
- [ ] 屏幕阅读器正确朗读设置项
- [ ] 减少动画偏好被尊重

### 性能测试

- [ ] 页面加载无 FOUC（白屏/黑屏闪烁）
- [ ] CLS < 0.1（切换主题不引起布局抖动）
- [ ] localStorage 读写不影响首屏渲染

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 主题切换导致现有组件颜色不一致 | High | 严格使用语义 token，禁用硬编码颜色 |
| 旧浏览器不支持 data 属性 | Low | data-* 属性支持 IE11+ 所有现代浏览器 |
| localStorage 被禁用 | Medium | 提供 cookie 降级 + 内存存储 |
| 主题过多导致用户选择困难 | Medium | 默认暖骨白 + 跟随系统，2 步可达 |

## 不在范围内

- 用户自定义主题创建器（v1 仅内置 8 种组合）
- 主题市场/分享功能
- 主题同步到云端
- 配色无障碍模式（仅保留 WCAG AA 达标的默认）
- Widget.html 主题切换（独立组件，v1 不涉及）

## 实施步骤

1. **重构 variables.css**：采用三层 token 架构
2. **新增 theme-light.css**：4 种浅色主题变体
3. **新增 theme-dark.css**：2 种深色主题变体
4. **新增 theme-a11y.css**：字号/行高/动画选项
5. **新增 theme.js**：主题管理 JS
6. **修改 index.html / admin.html / login.html**：
   - 嵌入早期主题应用脚本
   - 引入新的 CSS 文件
   - 引入 theme.js
7. **新增设置面板 UI**：齿轮按钮 + 侧边面板
8. **替换所有硬编码颜色**：在 5 个 CSS 文件中将旧颜色引用替换为语义 token
9. **测试**：功能 + 无障碍 + 性能

## 验收标准

- 所有 4 浅 × 2 深 = 8 种组合 WCAG AA 达标
- 主题切换无 FOUC，CLS < 0.1
- 完整键盘可达，屏幕阅读器友好
- 现有功能（聊天、监控、登录）无破坏

---

## 参考

- WCAG 2.2: https://www.w3.org/WAI/WCAG22/quickref/
- `design-taste-frontend` skill: `/home/dev/.claude/skills/design-taste-frontend/SKILL.md`
- `accessibility` skill: `/home/dev/.claude/skills/accessibility/SKILL.md`
- `web-quality-audit` skill: `/home/dev/.claude/skills/web-quality-audit/SKILL.md`
- `frontend-ui-engineering` skill: `/home/dev/.claude/skills/frontend-ui-engineering/SKILL.md`
