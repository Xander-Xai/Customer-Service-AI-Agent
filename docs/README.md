# Documentation

文档入口按读者任务组织。当前实现、生产验证与历史快照各有独立事实来源，避免把规划或旧报告当作主线现状。

## 当前事实来源

| 读者问题 | 唯一入口 | 职责 |
|---|---|---|
| 项目是什么、如何开始 | [README](../README.md) | 对外简介、架构概览、Quickstart 和导航 |
| 当前主线实现是什么 | [Current State](reference/current-state.md) | 可从代码/配置生成的运行事实与验证入口；不硬编码 HEAD |
| 系统如何组织 | [Architecture](architecture.md) | 当前架构、职责边界和设计裁决 |
| 生产还未验收什么 | [Production Readiness](production-readiness.md) | 当前生产/企业环境验收状态，区分代码与证据 |
| 版本发生了什么变化 | [Changelog](reports/releases/changelog.md) | Unreleased 与已发布版本历史 |
| 下一版本发布准备 | [Next release notes — DRAFT](reports/releases/next-release-notes-draft.md) | 尚未定版本/发布；发布前重新核验门禁 |
| 还需要什么工作 | [GitHub Issues](https://github.com/Xander-Xai/Customer-Service-AI-Agent/issues) | 可跟踪任务与外部验收阻塞 |
| 为什么采用某项设计 | [ADRs](decisions/README.md) | 已采纳架构决策及被取代状态 |
| 某次评测结论的证据是什么 | [Evaluation](evaluation.md) · [Evidence](evaluation/production-evidence.md) | 方法、分母、环境及边界 |

## 按任务阅读

### 入门与学习

- [5 / 15 / 30 分钟工程阅读路线](../README.md#文档与工程阅读路线)
- [离线演示与 proof card](guides/offline-demo.md)
- [配置参考](reference/configuration.md)
- [API 参考](reference/api-reference.md)

### 架构与运行时

- [Architecture](architecture.md) · [Agent 设计](agent-design.md)
- [Durable Runtime](design/agent-runtime.md) · [状态归属](design/runtime-state-ownership.md)
- [Runtime reliability boundaries](design/distributed-agent-runtime.md) · [Worker architecture](design/async-agent-worker-architecture.md)
- [ADR-009: Distributed Agent Runtime](decisions/009-distributed-agent-runtime.md)
- [Memory 工程定位](reference/memory-engineering-positioning-20261124.md)
- [工具执行边界](design/tool-execution-boundaries.md)
- [HITL 审批](design/human-in-the-loop.md) · [人工工单运维](human-handoff-operations.md)
- [MCP ProductDocs](operations/mcp-product-documents-20261125.md) · [A2A after-sales](architecture/a2a-after-sales-business-20261125.md)

### 配置、部署与运维

- [Deployment](deployment.md) · [Quick launch checklist](checklists/quick-launch-checklist.md)
- [生产运维指南](operations/production-operations-guide.md)
- [Runtime runbook](operations/distributed-runtime-runbook.md)
- [故障排查](operations/e2e-verification-guide.md)
- [工业准入契约](architecture/industrial-runtime-admission.md)：候选 profile 的代码边界与外部 NO-GO 条件

### 安全、数据与评测

- [Security design](design/security.md) · [Public secret policy](security/public-repository-secret-policy.md)
- [Data provenance](../DATA_PROVENANCE.md) · [Limitations](limitations.md)
- [Agent Evaluation](reference/agent-evaluation.md) · [RAG evaluation](reference/rag-evaluation.md)
- [Distributed Runtime evidence](evaluation/distributed-runtime-evidence.md)
- [Production evidence semantics](evaluation/production-evidence.md)

### 开发与贡献

- [CONTRIBUTING](../CONTRIBUTING.md) · [SECURITY](../.github/SECURITY.md)
- [Agent project comparison (2026-11-26)](reference/agent-project-comparison-20261126.md)
- [Testing guide](reference/testing-guide.md) · [Coding conventions](standards/conventions.md)
- [Document consistency audit](../scripts/audit_doc_consistency.py)
- [Architecture decision records](decisions/README.md)

## 历史报告与归档

- [Audit reports](reports/audit/)、[dated plans](reports/plans/) 是各自标注日期的历史快照；不能覆盖 Current State。
- [Release reports](reports/releases/) 保存发布时点记录；release/tag 的实际状态以 [GitHub Releases](https://github.com/Xander-Xai/Customer-Service-AI-Agent/releases) 为准。
- [Archive](archive/)、[historical design work](superpowers/) 保留审计脉络，不作为当前操作指南。

文档修改后执行 `make audit-docs`；涉及配置、API 或用户接口时同步更新对应规范参考与生成契约。
