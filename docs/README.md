# 文档索引

> Current entry point (2026-09-29): [code/doc alignment matrix](reports/plans/2026-09-29-code-doc-alignment.md). Runtime version remains `6.3`; document dates are not release versions.

> 本索引按「读者场景 → 文档功能 → 生命周期」三维分类组织。找不到想要的文档？先看左侧的「按读者查找」。

---

## 📍 快速定位

| 你在找什么？ | 去这里 |
|---|---|
| 系统架构、技术设计 | → [design/](#designdesign) |
| API 接口、模型参数、配置速查 | → [reference/](#referencereference) |
| 为什么选这个方案（ADR） | → [decisions/](#decisionsdecisions) |
| 编码规范、项目约定、AI 助手指令 | → [standards/](#standardsstandards) |
| 部署、切换、运维排障 | → [operations/](#operationsoperations) |
| 发布前/部署前逐项检查 | → [checklists/](#checklistschecklists) |
| 阶段完成报告、版本发布说明 | → [reports/](#reportsreports) |
| 安全审计、代码审查、多维度分析 | → [reports/audit/](#reportsaudit) |
| 旧版设计、已完成的历史文档 | → [archive/](#archivearchive) |

---

## 按读者查找

### 👨‍💻 开发者
- 系统架构 → [design/architecture-design.md](design/architecture-design.md)
- API 参考 → [reference/api-reference.md](reference/api-reference.md)
- 安全设计 → [design/security.md](design/security.md)
- Prompt 策略 → [design/prompt-engineering.md](design/prompt-engineering.md)
- 编码规范 → [standards/conventions.md](standards/conventions.md)
- 架构决策 → [decisions/](decisions/)

### 🛠️ 运维者
- 生产运维指南 → [operations/production-operations-guide.md](operations/production-operations-guide.md)
- LLM 提供商切换 → [operations/llm-provider-switch.md](operations/llm-provider-switch.md)
- E2E 验证 → [operations/e2e-verification-guide.md](operations/e2e-verification-guide.md)
- 生产就绪检查 → [checklists/production-readiness-checklist.md](checklists/production-readiness-checklist.md)
- 当前代码/文档对齐记录 → [reports/plans/2026-09-29-code-doc-alignment.md](reports/plans/2026-09-29-code-doc-alignment.md)
- 事实驱动工程标准 → [standards/evidence-driven-engineering-loop.md](standards/evidence-driven-engineering-loop.md)
- Agent Context Engineering → [design/context-engineering.md](design/context-engineering.md)
- 生产证据边界 → [evaluation/production-evidence.md](evaluation/production-evidence.md)
- 公共仓库密钥策略 → [security/public-repository-secret-policy.md](security/public-repository-secret-policy.md)
- 快速启动检查 → [checklists/quick-launch-checklist.md](checklists/quick-launch-checklist.md)

### 👑 管理者
- 版本变更日志 → [reports/releases/changelog.md](reports/releases/changelog.md)
- 历史发布说明 → [reports/releases/release-notes-v6.0.md](reports/releases/release-notes-v6.0.md)（不是当前状态入口）
- 阶段改进报告 → [reports/milestone/](reports/milestone/)
- 评分分析 → [reports/milestone/score-improvement-analysis.md](reports/milestone/score-improvement-analysis.md)
- 技术债务清理 → [reports/milestone/tech-debt-fix-summary.md](reports/milestone/tech-debt-fix-summary.md)

### 🔒 审计者
- 安全审计报告 → [reports/audit/](reports/audit/)
- 治理审计 → [design/governance-audit.md](design/governance-audit.md)
- 模型对比分析 → [reference/model-comparison.md](reference/model-comparison.md)
- RAG 评估 → [reference/rag-evaluation.md](reference/rag-evaluation.md)

### 🆕 新人
- 项目总览 → [../README.md](../README.md)
- 架构设计 → [design/architecture-design.md](design/architecture-design.md)
- 编码规范 → [standards/conventions.md](standards/conventions.md)
- 项目介绍 → [design/interview-intro.md](design/interview-intro.md)
- 深入 Q&A → [design/interview-deep-dive.md](design/interview-deep-dive.md)

---

## 按目录详解

### `design/` — 系统设计文档

> 🟢 Active — 活的、随代码同步更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [architecture-design.md](design/architecture-design.md) | 四层状态机、9 个 Agent、5 种协作模式 | 开发者、新人 |
| [security.md](design/security.md) | 安全架构、威胁模型、认证方案 | 开发者、审计者 |
| [prompt-engineering.md](design/prompt-engineering.md) | Prompt 策略和模式 | 开发者 |
| [governance-audit.md](design/governance-audit.md) | 治理审计报告 | 审计者 |
| [interview-intro.md](design/interview-intro.md) | 项目介绍（面试用） | 新人 |
| [interview-deep-dive.md](design/interview-deep-dive.md) | 深入 Q&A（面试用） | 新人 |

---

### `standards/` — 规范与约定

> 🟢 Active — 规则变更时更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [conventions.md](standards/conventions.md) | 编码规范、包组织、Ruff 规则 | 开发者 |
| [CLAUDE.md](../CLAUDE.md) | AI 助手项目指令（根目录保留） | 开发者 |

---

### `decisions/` — 架构决策记录 (ADR)

> 🔵 Stable — 一旦采纳永不修改

| 文件 | 说明 |
|---|---|
| [001-langgraph-multi-agent.md](decisions/001-langgraph-multi-agent.md) | 选择 LangGraph 作为多 Agent 编排框架 |
| [002-vanilla-js-frontend.md](decisions/002-vanilla-js-frontend.md) | 前端使用原生 JavaScript |
| [003-qwen-default-llm.md](decisions/003-qwen-default-llm.md) | 默认 LLM 选型 |
| [004-rag-embedding-selection.md](decisions/004-rag-embedding-selection.md) | RAG 向量库与 Embedding 选型 |
| [005-dual-layer-cache.md](decisions/005-dual-layer-cache.md) | 双层缓存策略 |

---

### `reference/` — 参考手册

> 🔵 Stable — 接口变更时更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [api-reference.md](reference/api-reference.md) | API 端点速查 | 开发者 |
| [model-comparison.md](reference/model-comparison.md) | 模型对比、成本估算 | 开发者、审计者 |
| [rag-evaluation.md](reference/rag-evaluation.md) | RAG 检索质量评估 | 开发者 |

---

### `operations/` — 运维与操作指南

> 🟢 Active — 环境变更时更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [production-operations-guide.md](operations/production-operations-guide.md) | 生产运维手册 | 运维者 |
| [llm-provider-switch.md](operations/llm-provider-switch.md) | LLM 提供商切换指南 | 运维者 |
| [e2e-verification-guide.md](operations/e2e-verification-guide.md) | E2E 验证方法论 | 开发者、运维者 |

---

### `checklists/` — 检查清单

> 🟠 Disposable — 使用后即过时

| 文件 | 说明 | 场景 |
|---|---|---|
| [production-readiness-checklist.md](checklists/production-readiness-checklist.md) | 生产就绪检查 | 发布前 |
| [quick-launch-checklist.md](checklists/quick-launch-checklist.md) | 快速启动检查 | 新环境部署前 |
| [audit-execution-plan.md](checklists/audit-execution-plan.md) | 审计执行计划 | 审计前 |
| [acceptance-checklist.md](checklists/acceptance-checklist.md) | 验收条件清单 | 验收前 |
| [step-by-step-plan.md](checklists/step-by-step-plan.md) | 分步实施计划 | 实施前 |

---

### `reports/` — 报告与版本

> 🟡 Snapshot — 写完后永不更新

#### `reports/milestone/` — 阶段完成报告

| 文件 | 说明 |
|---|---|
| [phase2-improvements-completed.md](reports/milestone/phase2-improvements-completed.md) | Phase 2 改进完成报告 |
| [phase3-improvements-completed.md](reports/milestone/phase3-improvements-completed.md) | Phase 3 改进完成报告 |
| [quick-improvements-completed.md](reports/milestone/quick-improvements-completed.md) | 快速改进完成报告 |
| [tech-debt-fix-summary.md](reports/milestone/tech-debt-fix-summary.md) | 技术债务清理报告 |
| [score-improvement-analysis.md](reports/milestone/score-improvement-analysis.md) | 评分改进分析 |
| [final-acceptance-report.md](reports/milestone/final-acceptance-report.md) | 最终验收报告 |

#### `reports/releases/` — 版本发布说明

| 文件 | 说明 |
|---|---|
| [changelog.md](reports/releases/changelog.md) | 项目变更日志 |
| [release-notes-v6.0.md](reports/releases/release-notes-v6.0.md) | v6.0 发布说明（Qdrant 迁移） |
| [release-notes-v5.5.md](reports/releases/release-notes-v5.5.md) | v5.5 发布说明 |
| [release-notes-v5.4.1.md](reports/releases/release-notes-v5.4.1.md) | v5.4.1 发布说明 |
| [release-notes-v5.4.md](reports/releases/release-notes-v5.4.md) | v5.4 发布说明 |
| [fix-verification-report.md](reports/releases/fix-verification-report.md) | 修复验证报告 |

#### `reports/audit/` — 安全/质量审计报告

| 文件 | 说明 |
|---|---|
| [project-audit-report.md](reports/audit/project-audit-report.md) | 项目审计报告 v1 |
| [project-audit-report-v2.md](reports/audit/project-audit-report-v2.md) | 项目审计报告 v2 |
| [role-ai-safety.md](reports/audit/role-ai-safety.md) | AI 安全角色审计 |
| [role-attacker.md](reports/audit/role-attacker.md) | 攻击者角色审计 |
| [role-code-reviewer.md](reports/audit/role-code-reviewer.md) | 代码审查角色审计 |
| [role-consumer.md](reports/audit/role-consumer.md) | 消费者角色审计 |
| [role-data-guardian.md](reports/audit/role-data-guardian.md) | 数据守护角色审计 |
| [role-observability.md](reports/audit/role-observability.md) | 可观测性角色审计 |
| [business-alignment.md](reports/audit/business-alignment.md) | 业务对齐评估 |
| [cross-cutting-chains.md](reports/audit/cross-cutting-chains.md) | 跨链分析 |
| [dynamic-validation.md](reports/audit/dynamic-validation.md) | 动态验证测试 |
| [multi-dimensional-analysis.md](reports/audit/multi-dimensional-analysis.md) | 多维度质量分析 |
| [audit-remediation-report.md](reports/audit/audit-remediation-report.md) | 审计整改报告 |

#### `reports/plans/` — 实施计划

| 文件 | 说明 |
|---|---|
| [2026-06-25-code-doc-alignment.md](reports/plans/2026-06-25-code-doc-alignment.md) | 2026-06-25 第二轮全量前后端联调对齐复核 |
| [2026-06-25-v6.2-code-doc-alignment.md](reports/plans/2026-06-25-v6.2-code-doc-alignment.md) | 2026-06-25 v6.2 全量前后端联调 + 生产就绪修复 + 文档同步 |
| [2026-06-23-code-doc-alignment.md](reports/plans/2026-06-23-code-doc-alignment.md) | 2026-06-23 代码与文档对齐记录（历史） |
| [2026-06-22-code-doc-alignment.md](reports/plans/2026-06-22-code-doc-alignment.md) | 2026-06-22 代码与文档对齐记录（历史） |

---

### `archive/` — 历史归档

> 🔴 Archive — 不再维护

| 文件 | 说明 |
|---|---|
| [version-doc-sync-record.md](archive/version-doc-sync-record.md) | 版本文档同步记录 |
| [2026-06-07-v4.3-audit-and-ci-fix-summary.md](archive/2026-06-07-v4.3-audit-and-ci-fix-summary.md) | v4.3 审计修复总结 |
| [2026-06-09-theme-switcher-acceptance.md](archive/2026-06-09-theme-switcher-acceptance.md) | 主题切换验收 |
| [2026-06-09-theme-switcher-design.md](archive/2026-06-09-theme-switcher-design.md) | 主题切换设计 |
| [2026-06-09-theme-switcher-implementation.md](archive/2026-06-09-theme-switcher-implementation.md) | 主题切换实现 |
| [2026-06-10-frontend-backend-alignment-fix.md](archive/2026-06-10-frontend-backend-alignment-fix.md) | 前后端对齐修复 |
| [2026-06-10-frontend-contrast-fix.md](archive/2026-06-10-frontend-contrast-fix.md) | 前端对比度修复 |
| [audit-cleanup-design.md](archive/audit-cleanup-design.md) | 审计清理设计 |
| [comprehensive-optimization.md](archive/comprehensive-optimization.md) | 综合优化记录 |
| [frontend-architecture.md](archive/frontend-architecture.md) | 前端架构说明 |
| [full-code-quality-improvement.md](archive/full-code-quality-improvement.md) | 代码质量改进 |
| [prod-hotfix-design.md](archive/prod-hotfix-design.md) | 生产热修复设计 |
| [security-audit-summary.md](archive/security-audit-summary.md) | 安全审计摘要 |

---

## 生命周期流转规则

文档在生命周期中会在不同分类间流转：

```
创建期 → 保留期 → 归档期 → 删除期
  ↓         ↓          ↓
draft   design/   archive/
        standards/
        decisions/
        reference/
        operations/
        checklists/
        reports/
```

**关键规则**：
- Active → Archive：设计已过时 / 新设计已替代旧设计
- Checklists → Archive：已使用过的清单，确认执行完毕
- Reports → Archive：报告超过 3 个版本迭代后
- Archive → Delete：归档超过 1 年且无人查阅
- **禁止逆向流转**：一旦归档，不重新激活——需要时创建新文档

---

## 文档治理框架

完整的文档价值评估与分类体系定义在：

> [design/governance-audit.md](design/governance-audit.md) — 文档治理审计与分类报告

---

*最后更新：2026-06-25（v6.3 前后端联调修复 + 生产就绪加固 + 文档全面同步）*
