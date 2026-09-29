# 客户服务 AI Agent 审查修复计划

> **HISTORICAL AUDIT SNAPSHOT**
> This is a dated 2026-06 execution plan/record. It does not describe the current file layout;
> several referenced files have since been renamed or removed.


> **生成日期**: 2026-06-15
> **依据文档**: `docs/reports/audit/project-audit-report.md`

依据项目审查报告，我制定了以下分为三个里程碑（Milestone）的执行计划。我们将依次完成这些任务。

## Milestone 1：工程基线、A11y 与美学修复 (Engineering & Aesthetics)
**目标**：消除现存的显性代码规范错误，修复无障碍体验，提升整体 UI 质感。
- [x] **Task 1.1: 修复现有 Lint 与 A11y 错误**
  - 修复 `theme-comparison.html` 中所有 `<button>` 缺失 `type` 属性的问题。
  - 修复 `theme-a11y.css` 中的 `noDescendingSpecificity` CSS 权重覆盖警告。
  - 格式化 `admin.js`, `sessions.js`, `voice.js`, `monitor/index.js` 修复缩进规范。
- [x] **Task 1.2: 修复“破窗效应”与基础响应式**
  - 清理 `components.css` 中破坏主题的写死 Hex 色值（如 `#14161e`），统一替换为 CSS 变量。
  - 在 `responsive.css` 中补全对 `1024px` 及 `1440px` 的大屏/平板断点适配。
- [x] **Task 1.3: 引入现代高端美学 (Premium UX)**
  - 为卡片、模态框引入毛玻璃 (Glassmorphism) 和高级光影。
  - 引入极低饱和度的 HSL 渐变与动态光晕。
  - 增加元素的点击回弹与悬浮扩散等微交互动画。
  - 在主要 HTML 入口补充 `lang="zh-CN"` 声明。

## Milestone 2：安全加固与“上帝模块”重构 (Security & Debt)
**目标**：拆解巨石文件，消除 XSS 隐患，收敛网络请求。
- [ ] **Task 2.1: 消除 `innerHTML` XSS 隐患**
  - 排查并重构前端所有滥用 `innerHTML` 的地方，改为现代化的 DOM 构建模式。
- [ ] **Task 2.2: 重构 `admin.js`**
  - 将近 800 行的 `admin.js` 拆解为多个子模块（如 users, analytics, sessions 等）。
  - 提取出与 `monitor/index.js` 重复的图表渲染逻辑至公共工具模块。
- [ ] **Task 2.3: 网络请求中央管控**
  - 审计所有的 `fetch` 调用，确保其全部统一经过 `rest.js` 的 `fetchWithAuth` 拦截器。

## Milestone 3：质量防线与防刷单机制 (Quality & Control)
**目标**：建立缺失的 E2E 测试，保护大模型不被恶意消耗。
- [ ] **Task 3.1: 前端 E2E 测试补全**
  - 引入 Playwright 框架。
  - 编写涵盖登录、核心对话流、管理后台渲染的基础 E2E 自动化测试用例。
- [ ] **Task 3.2: 完善 LLM Token 预算限制**
  - 设计并在应用层（如中间件或业务逻辑）增强针对单用户的 Token 消耗限流（Quota 控制）。
- [ ] **Task 3.3: 评估并移除 CSP unsafe-inline**
  - 将内联样式迁移到外部 CSS 或使用 Nonce，彻底消除 CSS 注入风险。

---
*注：我们将按照此清单严格执行，保证每一个步骤的验证与代码隔离。*
