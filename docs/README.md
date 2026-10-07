# 文档索引

> Current entry point: [reference/current-state.md](reference/current-state.md)（当前事实 + 验证命令，不硬编码 HEAD）。
> Runtime version 由 `core/config.py::VERSION` 决定；带日期的对齐报告（`reports/plans/**`）是历史审计快照，不是 Current Truth。

> 本索引按「读者场景 → 文档功能 → 生命周期」三维分类组织。找不到想要的文档？先看左侧的「按读者查找」。

---

## 📍 快速定位

| 你在找什么？ | 去这里 |
|---|---|
| 系统架构、技术设计 | → [design/](#designdesign) |
| **分布式 Agent Runtime（Celery worker / checkpoint / 幂等 / DLQ）** | → [design/agent-runtime.md](design/agent-runtime.md)、[decisions/009](decisions/009-distributed-agent-runtime.md)、[operations/distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md) |
| **高风险工具为什么要人工审批（退款/改单）** | → [design/human-in-the-loop.md](design/human-in-the-loop.md) |
| API 接口、模型参数、配置速查 | → [reference/](#referencereference) |
| 为什么选这个方案（ADR） | → [decisions/](#decisionsdecisions) |
| 编码规范、项目约定、AI 助手指令 | → [standards/](#standardsstandards) |
| 部署、切换、运维排障 | → [operations/](#operationsoperations) |
| 发布前/部署前逐项检查 | → [checklists/](#checklistschecklists) |
| provider/生产/Runtime 证据边界 | → [evaluation/](#evaluationevaluation)、[evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md) |
| 面试介绍、深入问答、题集 | → [design/interview-intro.md](design/interview-intro.md)、[design/interview-deep-dive.md](design/interview-deep-dive.md)、[interview-questions-final.md](interview-questions-final.md) |
| 整改规格、完成报告（历史） | → [audit/](#auditaudit) |
| 历史设计与实施计划（2026-06） | → [superpowers/](#superpowerssuperpowers) |
| 阶段完成报告、版本发布说明 | → [reports/](#reportsreports) |
| 安全审计、代码审查、多维度分析 | → [reports/audit/](#reportsaudit) |
| 旧版设计、已完成的历史文档 | → [archive/](#archivearchive) |

---

## 按读者查找

### 👨‍💻 开发者
- 系统架构 → [design/architecture-design.md](design/architecture-design.md)
- 分布式 Agent Runtime 设计 → [design/agent-runtime.md](design/agent-runtime.md)
- 状态归属（checkpoint / session / cache / tool store） → [design/runtime-state-ownership.md](design/runtime-state-ownership.md)
- API 参考 → [reference/api-reference.md](reference/api-reference.md)
- 安全设计 → [design/security.md](design/security.md)
- Prompt 策略 → [design/prompt-engineering.md](design/prompt-engineering.md)
- 编码规范 → [standards/conventions.md](standards/conventions.md)
- 架构决策 → [decisions/](decisions/)

### 🛠️ 运维者
- 生产运维指南 → [operations/production-operations-guide.md](operations/production-operations-guide.md)
- **分布式 Runtime runbook** → [operations/distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md)
- LLM 提供商切换 → [operations/llm-provider-switch.md](operations/llm-provider-switch.md)
- E2E 验证 → [operations/e2e-verification-guide.md](operations/e2e-verification-guide.md)
- 生产就绪检查 → [checklists/production-readiness-checklist.md](checklists/production-readiness-checklist.md)
- 当前事实入口 → [reference/current-state.md](reference/current-state.md)
- 事实驱动工程标准 → [standards/evidence-driven-engineering-loop.md](standards/evidence-driven-engineering-loop.md)
- Agent Context Engineering → [design/context-engineering.md](design/context-engineering.md)
- 生产证据边界 → [evaluation/production-evidence.md](evaluation/production-evidence.md)
- 分布式 Runtime 证据边界 → [evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md)
- 一键离线可复现 demo（Mock LLM / 无 API Key / 无出网 / 证据卡） → [guides/offline-demo.md](guides/offline-demo.md)
- 公共仓库密钥策略 → [security/public-repository-secret-policy.md](security/public-repository-secret-policy.md)
- 快速启动检查 → [checklists/quick-launch-checklist.md](checklists/quick-launch-checklist.md)

### 👑 管理者
- 版本变更日志 → [reports/releases/changelog.md](reports/releases/changelog.md)
- 历史发布说明 → [reports/releases/release-notes-v6.0.md](reports/releases/release-notes-v6.0.md)（不是当前状态入口）
- 阶段改进报告 → [reports/milestone/](reports/milestone/)
- 评分分析 → [reports/milestone/score-improvement-analysis.md](reports/milestone/score-improvement-analysis.md)
- 技术债务清理 → [reports/milestone/tech-debt-fix-summary.md](reports/milestone/tech-debt-fix-summary.md)

### 🔒 审计者
- 当前事实入口 → [reference/current-state.md](reference/current-state.md)
- 安全审计报告 → [reports/audit/](reports/audit/)
- 治理审计 → [reports/audit/governance-audit.md](reports/audit/governance-audit.md)（历史快照）
- 模型对比分析 → [reference/model-comparison.md](reference/model-comparison.md)
- RAG 评估（canonical） → [reference/rag-evaluation.md](reference/rag-evaluation.md)
- RAG Gold 标注契约与离线校验（#119） → [reference/rag-gold-label-contract.md](reference/rag-gold-label-contract.md)
- RAG Gold 静态 provenance 审计（#99） → [reference/rag-gold-label-provenance.md](reference/rag-gold-label-provenance.md)
- 生产证据边界 → [evaluation/production-evidence.md](evaluation/production-evidence.md)

### 真相层级（-current truth 速查）

| 想知道 | 看 | 生命周期 |
|---|---|---|
| 当前 runtime 事实 + 验证命令 | [reference/current-state.md](reference/current-state.md) | 🟢 CURRENT（随代码/命令同步） |
| RAG 评估方法/口径/当前评测状态 | [reference/rag-evaluation.md](reference/rag-evaluation.md) | 🟢 CURRENT（评测实现变更时更新；历史小节单独标注） |
| provider/生产证据语义 | [evaluation/production-evidence.md](evaluation/production-evidence.md) | 🟣 EVIDENCE |
| 分布式 Runtime 为什么这么设计 | [design/agent-runtime.md](design/agent-runtime.md)、[decisions/009-distributed-agent-runtime.md](decisions/009-distributed-agent-runtime.md) | 🔵 DESIGN / ADR |
| 高风险副作用的审批治理与证据边界 | [design/human-in-the-loop.md](design/human-in-the-loop.md) | 🔵 DESIGN |
| 分布式 Runtime 运维操作 | [operations/distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md) | 🟠 RUNBOOK |
| 分布式 Runtime 能宣称到什么程度 | [evaluation/distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md) | 🟣 EVIDENCE |
| 历史审计输出 | [reports/audit/](reports/audit/)、[reports/plans/](reports/plans/) | 🟡 HISTORICAL SNAPSHOT（按日期，仅执行时点有效） |
| 版本历史 | [reports/releases/](reports/releases/) | 🟡 SNAPSHOT（旧版本章节永不重写） |

### 生命周期图例（canonical，唯一一套）

每个**文档**必须有且只有一个 lifecycle；同一目录内不同文件可以是不同
lifecycle（例如 `reference/` 里 `current-state.md` 是 CURRENT，而带日期的
模型对比快照是 HISTORICAL AUDIT）。不允许"有大量文档但不知道是否还有效"。

| 标记 | 生命周期 | 含义 |
|---|---|---|
| 🟢 | CURRENT | 当前事实/规范，随代码与配置同步更新 |
| 🔵 | DESIGN / ADR | 设计说明或已采纳决策（决策正文一旦采纳不重写；被取代只更新 Status） |
| 🟠 | RUNBOOK | 操作/运维/清单，执行后可能过时但仍是操作入口 |
| 🟣 | EVIDENCE | 证据边界与评估口径（CURRENT，但结论可能是 NOT_VERIFIED） |
| 🟡 | HISTORICAL AUDIT | 带日期的审计/对齐/迁移快照，仅执行时点有效 |
| ⚪ | SUPERSEDED | 已被后续文档取代，保留历史身份，不再具权威 |
| 🔴 | ARCHIVE | 归档，不再维护 |

> 已废弃的旧标签（Active / Stable / Disposable / Snapshot）不再是独立
> lifecycle；它们只能是上表某一项的别名。机器 guard 见
> `scripts/audit_doc_consistency.py::check_lifecycle_vocabulary`。

### 🆕 新人
- 项目总览 → [../README.md](../README.md)
- 架构设计 → [design/architecture-design.md](design/architecture-design.md)
- 编码规范 → [standards/conventions.md](standards/conventions.md)
- 项目介绍 → [design/interview-intro.md](design/interview-intro.md)
- 深入 Q&A → [design/interview-deep-dive.md](design/interview-deep-dive.md)
- 面试题集 → [interview-questions-final.md](interview-questions-final.md)
- **五个可复现演示场景（场景/执行链/源码锚点/测试锚点/命令/实际结果/讲解/追问/边界/证据等级）** → [interview/demo-scenarios.md](interview/demo-scenarios.md)
- **源码溯源地图（问题 → 源码 → 测试 → 证据）** → [interview/source-map.md](interview/source-map.md)
- 架构逐问（15 问 + 证据分级 + 不宣称清单） → [interview/architecture-walkthrough.md](interview/architecture-walkthrough.md)
- Runtime 深入（Celery/ACK/租约/崩溃恢复 + flaky 根因） → [interview/runtime-deep-dive.md](interview/runtime-deep-dive.md)
- RAG 深入（检索链路、评测口径、失败分析；指标 NOT_VERIFIED） → [interview/rag-deep-dive.md](interview/rag-deep-dive.md)
- HITL 深入（风险分级、执行前拦截、TTL、职责分离、approval ≠ idempotency） → [interview/hitl-deep-dive.md](interview/hitl-deep-dive.md)
- 失败模式与取舍（为什么不做 / 代价 / 我们犯过的错） → [interview/failure-and-tradeoffs.md](interview/failure-and-tradeoffs.md)
- Tool Result 面试材料 → [interview/context-engineering-interview.md](interview/context-engineering-interview.md)
- 简历描述（证据冻结） → [reports/resume-description.md](reports/resume-description.md)

---

## 按目录详解

### `design/` — 系统设计文档

> 🔵 DESIGN / ADR — 活的设计文档，随代码同步更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [architecture-design.md](design/architecture-design.md) | 四层状态机、9 个 Agent、5 种协作模式 | 开发者、新人 |
| [agent-runtime.md](design/agent-runtime.md) | 分布式 Agent Runtime 完整设计（状态机 / 锁 / 幂等 / DLQ） | 开发者、面试者 |
| [distributed-agent-runtime.md](design/distributed-agent-runtime.md) | 分布式 Runtime 架构与可靠性边界（能力/非能力） | 开发者、审计者 |
| [human-in-the-loop.md](design/human-in-the-loop.md) | 高风险工具副作用的人工审批治理（风险模型 / 职责分离 / TTL / 幂等双防线 / 证据边界） | 开发者、审计者 |
| [runtime-state-ownership.md](design/runtime-state-ownership.md) | 四类状态归属表（checkpoint / session / cache / tool store） | 开发者、审计者 |
| [async-agent-worker-architecture.md](design/async-agent-worker-architecture.md) | Worker pool 深化设计（已实现部分 vs 纯设计部分） | 开发者 |
| [context-engineering.md](design/context-engineering.md) | Tool Result / Session Context Engineering | 开发者 |
| [security.md](design/security.md) | 安全架构、威胁模型、认证方案 | 开发者、审计者 |
| [prompt-engineering.md](design/prompt-engineering.md) | Prompt 策略和模式 | 开发者 |
| [interview-intro.md](design/interview-intro.md) | 项目介绍（面试用，60s / 2min / 3min 三档） | 新人 |
| [interview-deep-dive.md](design/interview-deep-dive.md) | 深入 Q&A（面试用） | 新人 |

---

### `standards/` — 规范与约定

> 🟢 CURRENT — 规则变更时更新

| 文件 | 说明 | 读者 |
|---|---|---|
| [conventions.md](standards/conventions.md) | 编码规范、包组织、Ruff 规则 | 开发者 |
| [CLAUDE.md](../CLAUDE.md) | AI 助手项目指令（根目录保留） | 开发者 |

---

### `decisions/` — 架构决策记录 (ADR)

> 🔵 DESIGN / ADR — 一旦采纳永不修改（被取代只更新 Status）

| 文件 | 说明 |
|---|---|
| [001-langgraph-multi-agent.md](decisions/001-langgraph-multi-agent.md) | 选择 LangGraph 作为多 Agent 编排框架 |
| [002-vanilla-js-frontend.md](decisions/002-vanilla-js-frontend.md) | 前端使用原生 JavaScript |
| [003-qwen-default-llm.md](decisions/003-qwen-default-llm.md) | 默认 LLM 选型（Superseded by ADR-007） |
| [004-rag-embedding-selection.md](decisions/004-rag-embedding-selection.md) | RAG 向量库与 Embedding 选型（Partially Superseded by ADR-008） |
| [005-dual-layer-cache.md](decisions/005-dual-layer-cache.md) | 双层缓存策略（Superseded by ADR-006） |
| [006-cache-and-tool-result-context-architecture.md](decisions/006-cache-and-tool-result-context-architecture.md) | Cache 与 Tool Result Context 架构分离 |
| [007-current-default-llm.md](decisions/007-current-default-llm.md) | 当前默认 LLM（Qwen/Qwen3-8B） |
| [008-current-rag-retrieval-architecture.md](decisions/008-current-rag-retrieval-architecture.md) | 当前 RAG 检索与 Embedding 架构 |
| [009-distributed-agent-runtime.md](decisions/009-distributed-agent-runtime.md) | 分布式 Agent Runtime（Postgres checkpoint / Redis session+lock / Celery worker / AgentRun 真相源） |

---

### `reference/` — 参考手册

> 逐文件 lifecycle（`reference/` 不是单一 lifecycle）：

| 文件 | 说明 | 读者 | Lifecycle |
|---|---|---|---|
| [api-reference.md](reference/api-reference.md) | API 端点速查（数量由生成工具校验，不手工维护） | 开发者 | 🟢 CURRENT / REFERENCE |
| [configuration.md](reference/configuration.md) | 配置项全表（默认值的真相源是 `core/config.py`） | 开发者、运维 | 🟢 CURRENT / REFERENCE |
| [current-state.md](reference/current-state.md) | 当前事实入口（版本/模型/验证命令） | 开发者、审计者 | 🟢 CURRENT |
| [distributed-runtime-evidence.md](evaluation/distributed-runtime-evidence.md) | 分布式 Runtime 证据边界（Level 1/2/3 与不宣称项） | 开发者、审计者 | 🟣 EVIDENCE |
| [model-comparison.md](reference/model-comparison.md) | 模型配置 + 历史估算口径 | 开发者、审计者 | 🟡 HISTORICAL AUDIT（含 CURRENT 配置小结） |
| [project-structure.md](reference/project-structure.md) | 仓库目录与职责 | 开发者 | 🟢 CURRENT / REFERENCE |
| [rag-evaluation.md](reference/rag-evaluation.md) | RAG 检索质量评估 | 开发者 | 🟢 CURRENT（历史小节单独标注） |
| [rag-gold-label-contract.md](reference/rag-gold-label-contract.md) | RAG gold 标注契约（`rag-gold-label/v1`）+ 离线校验器（#119） | 开发者、标注者 | 🟢 CURRENT / CONTRACT |
| [rag-gold-label-provenance.md](reference/rag-gold-label-provenance.md) | 旧 gold 静态 provenance 审计（#99） | 开发者、审计者 | 🟣 EVIDENCE |
| [testing-guide.md](reference/testing-guide.md) | 测试分层、运行方式与新增测试落位约定 | 开发者 | 🟢 CURRENT / REFERENCE |

---

### `operations/` — 运维与操作指南

> 🟠 RUNBOOK — 操作/运维入口

| 文件 | 说明 | 读者 |
|---|---|---|
| [production-operations-guide.md](operations/production-operations-guide.md) | 生产运维手册 | 运维者 |
| [distributed-runtime-runbook.md](operations/distributed-runtime-runbook.md) | 分布式 Runtime 运维 runbook（生产 gate / DLQ 重放 / 排障） | 运维者 |
| [llm-provider-switch.md](operations/llm-provider-switch.md) | LLM 提供商切换指南 | 运维者 |
| [e2e-verification-guide.md](operations/e2e-verification-guide.md) | E2E 验证方法论（开发者 E2E / 生产式分布式 Runtime E2E 分轨） | 开发者、运维者 |

---

### `evaluation/` — 证据与评估口径

> 🟣 EVIDENCE — CURRENT：定义"什么是证据、什么还没有证据"

| 文件 | 说明 | 读者 |
|---|---|---|
| [production-evidence.md](evaluation/production-evidence.md) | provider/生产证据边界与状态语义（VERIFIED / NOT_VERIFIED / NOT_MEASURED / UNKNOWN） | 开发者、审计者 |

---

### `checklists/` — 检查清单

> 🟠 RUNBOOK — 检查清单（执行后可能过时，仍是操作入口）

| 文件 | 说明 | 场景 |
|---|---|---|
| [production-readiness-checklist.md](checklists/production-readiness-checklist.md) | 生产就绪检查 | 发布前 |
| [quick-launch-checklist.md](checklists/quick-launch-checklist.md) | 快速启动检查 | 新环境部署前 |
| [audit-execution-plan.md](checklists/audit-execution-plan.md) | 审计执行计划 | 审计前 |
| [acceptance-checklist.md](checklists/acceptance-checklist.md) | 验收条件清单 | 验收前 |
| [step-by-step-plan.md](checklists/step-by-step-plan.md) | 分步实施计划 | 实施前 |

---

### `reports/` — 报告与版本

> 🟡 HISTORICAL AUDIT — 阶段/发布快照，写完后永不更新

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
| [governance-audit.md](reports/audit/governance-audit.md) | 文档治理评估（2026-06-17 历史快照；2026-10-01 由 design/ 迁入） |
| [2026-10-02-DISTRIBUTED_RUNTIME_AUDIT.md](reports/audit/2026-10-02-DISTRIBUTED_RUNTIME_AUDIT.md) | 分布式 Runtime 状态归属审计（HEAD `912ad49` 快照；2026-10-02 由仓库根迁入） |
| [2026-10-02-FINAL_RUNTIME_FACT_CHECK.md](reports/audit/2026-10-02-FINAL_RUNTIME_FACT_CHECK.md) | 分布式 Runtime 事实核对（HEAD `aa8d082` 快照，部分结论已被后续 commit 推翻；2026-10-02 由仓库根迁入） |
| [tool-result-cache-reuse-audit.md](reports/audit/tool-result-cache-reuse-audit.md) | Tool Result cache reuse 审计快照（2026-10-01 由仓库根迁入） |
| [tool-result-v2-audit-report.md](reports/audit/tool-result-v2-audit-report.md) | Tool Result Context Engineering V2 审计快照（2026-10-01 由仓库根迁入） |

#### `reports/plans/` — 实施计划（均为 HISTORICAL AUDIT SNAPSHOT）

| 文件 | 说明 |
|---|---|
| [2026-09-29-code-doc-alignment.md](reports/plans/2026-09-29-code-doc-alignment.md) | 2026-09-29 全仓对齐审计（baseline 18c927d，仅该时点有效） |
| [2026-06-25-code-doc-alignment.md](reports/plans/2026-06-25-code-doc-alignment.md) | 2026-06-25 第二轮全量前后端联调对齐复核 |
| [2026-06-25-v6.2-code-doc-alignment.md](reports/plans/2026-06-25-v6.2-code-doc-alignment.md) | 2026-06-25 v6.2 全量前后端联调 + 生产就绪修复 + 文档同步 |
| [2026-06-23-code-doc-alignment.md](reports/plans/2026-06-23-code-doc-alignment.md) | 2026-06-23 代码与文档对齐记录（历史） |
| [2026-06-22-code-doc-alignment.md](reports/plans/2026-06-22-code-doc-alignment.md) | 2026-06-22 代码与文档对齐记录（历史） |
| [frontend-repair-plan.md](reports/plans/frontend-repair-plan.md) | 前端修复计划（历史） |

---

### `audit/` — 整改规格与完成报告

> 🟡 HISTORICAL AUDIT / ⚪ SUPERSEDED — 带日期的整改规格与验收报告，仅执行时点有效。
> `CODEX_PROJECT_REMEDIATION_SPEC.md` 的"唯一执行规格"权威身份已于 2026-10-01 撤销，
> 不再与 [reference/current-state.md](reference/current-state.md) 竞争 Current Truth。

| 文件 | 说明 | 生命周期 |
|---|---|---|
| [CODEX_PROJECT_REMEDIATION_SPEC.md](audit/CODEX_PROJECT_REMEDIATION_SPEC.md) | 2026-08-13 整改规格 | ⚪ SUPERSEDED |
| [CODEX_REMEDIATION_PLAN.md](audit/CODEX_REMEDIATION_PLAN.md) | 整改规划（历史） | 🟡 HISTORICAL |
| [P0_02_COMPLETION_REPORT.md](audit/P0_02_COMPLETION_REPORT.md) | 缓存跨用户泄漏修复报告 | 🟡 HISTORICAL |
| [P0_03_COMPLETION_REPORT.md](audit/P0_03_COMPLETION_REPORT.md) | ERP 授权修复报告 | 🟡 HISTORICAL |

---

### `superpowers/` — 历史设计与实施计划（2026-06）

> 🟡 HISTORICAL AUDIT — 带日期的设计/实施工作单，已加 historical banner。
> 均不是当前架构权威；Current Truth 只认
> [reference/current-state.md](reference/current-state.md)。

`plans/`（迁移/SSE/缓存/证据缺口）与 `specs/`（设计文档 + 审查报告）均为
2026-06 快照，保留原版本与当时结论，不再逐一列出。

---

### `archive/` — 历史归档

> 🔴 ARCHIVE — 不再维护

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
CURRENT  DESIGN/ADR  ARCHIVE
         RUNBOOK
         EVIDENCE
         HISTORICAL AUDIT
         SUPERSEDED
```

**关键规则**：
- CURRENT / DESIGN / RUNBOOK → ARCHIVE：设计已过时 / 新设计已替代旧设计
- RUNBOOK → ARCHIVE：已使用过的清单，确认执行完毕
- HISTORICAL AUDIT → ARCHIVE：报告超过 3 个版本迭代后
- ARCHIVE → Delete：归档超过 1 年且无人查阅
- **禁止逆向流转**：一旦归档，不重新激活——需要时创建新文档

---

## 文档治理框架

完整的文档价值评估与分类体系定义在：

> [reports/audit/governance-audit.md](reports/audit/governance-audit.md) — 文档治理审计与分类报告（2026-06-17 历史快照）

---

*最后更新：2026-10-02（Repository Truth Convergence：新增分布式 Agent Runtime 文档索引 — ADR-009 / agent-runtime / runtime-state-ownership / async-agent-worker-architecture / distributed-runtime-runbook / distributed-runtime-interview-evidence，并修正 lifecycle 标注）*
