---
name: version-doc-sync-record
description: 2026-06-16 v5.2.3 版本对齐与文档同步 — 修复 config.py VERSION 5.2.0→5.2.2 + admin.html v5.0→v5.2.2 + README/CHANGELOG/api-ref/架构文档全量更新
metadata:
  type: project
---

# v5.2.3 版本对齐与文档同步

## 版本修复
- `core/config.py` VERSION 5.2.0 → 5.2.2（实际代码行为领先，仅版本字符串未同步）
- `web/admin.html` 标题栏 v5.0 → v5.2.2（主聊天页已是 v5.2.2）

## README.md 更新点
- 测试数：1191 → 1341（满校准）
- 前端功能表新增 6 项
- BaseAgent 能力新增 3 项
- 核心能力栏新增 5 项
- 项目结构目录同步（JS 模块 22→38, CSS 12→13）
- 版本表格更新

## docs/active/ 更新（⚠️ 注意：此目录后续已移除，文件已移至 docs/design/ 和 docs/reference/）
- architecture-design.md（当前在 docs/design/）：测试数、文件数同步
- api-reference.md（当前在 docs/reference/）：TOKEN 端点描述规范化

## 如何应用
- 每次版本更新后检查 `core/config.py` 的 `VERSION` 与 README 声明一致
- 前端页面版本号应与 config.py 统一维护
- 测试用例数自 v5.2.2 起采用`grep -c 'def test_' **/*.py` 自动化统计