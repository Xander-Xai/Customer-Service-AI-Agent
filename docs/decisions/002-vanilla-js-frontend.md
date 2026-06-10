# ADR-002: 前端使用原生 JavaScript

**日期**：2026-06-08
**状态**：已采纳
**决策者**：项目负责人

## 背景

需要为客服系统构建前端界面（聊天页 + 管理后台 + 嵌入式 widget）。候选方案有 React、Vue、原生 JavaScript。

## 决策

使用原生 JavaScript + Vite 构建管线，不引入前端框架。

## 理由

- **可嵌入性**：原生 JS 可直接作为 `<script>` 标签嵌入任意页面（`widget.html`），无框架运行时依赖。这是核心需求——客服 widget 需要嵌入客户的企业网站
- **体积**：无框架运行时，首屏 JS 体积更小，适合嵌入场景
- **构建管线**：Vite 8 提供模块化 + Tree-shashing + Hashed 产物，开发体验接近框架项目
- **测试**：Vitest + jsdom 提供单元测试能力
- **安全**：DOMPurify 净化 LLM 输出 + escapeHtml 净化动态数据，无框架自动转义的依赖

## 影响

**正面**：
- widget 可无缝嵌入任意页面，无 React/Vue 版本冲突
- 首屏加载快，无框架运行时开销
- 54 处 innerHTML 全量审计安全，模式一致

**负面**：
- 无组件化抽象，大型页面（admin.js 700+ 行）维护成本高
- 无虚拟 DOM，大量 innerHTML 操作的性能不如框架优化
- 面试官可能质疑"为什么不用 React"

## 迁移路径

如需组件化开发，可渐进迁移到 Web Components 或轻量框架（Lit/Preact），当前架构已为迁移预留了模块边界（`web/src/` 按功能分目录）。

## 来源文档

[README.md](../../README.md) 前端技术选型说明
