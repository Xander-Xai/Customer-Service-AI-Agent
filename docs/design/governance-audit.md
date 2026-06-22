# 文档治理评估报告

> 评估日期：2026-06-17（原 2026-06-10，v5.4 后复审更新）
> 评估依据：`/home/dev/projects/governance/document-evaluation-framework.md`
> 评估范围：项目 `docs/` 目录 + 根目录 `.md` 文件（共 25 份文档）
>
> **⚠️ 2026-06-21 更新说明**：本报告中评估的 `active/` 目录（位于 `docs/active/`）后续已被移除，其内容已重新组织到 `docs/design/`、`docs/reference/`、`docs/operations/` 和 `docs/checklists/` 目录中。以下评估中提到的 `active/` 目录下的文档现在位于以下路径：
> - SECURITY.md → `docs/design/security.md`
> - architecture-design.md → `docs/design/architecture-design.md`
> - interview-intro.md → `docs/design/interview-intro.md`
> - interview-deep-dive.md → `docs/design/interview-deep-dive.md`
> - prompt-engineering.md → `docs/design/prompt-engineering.md`
> - model-comparison.md → `docs/reference/model-comparison.md`
> - rag-evaluation.md → `docs/reference/rag-evaluation.md`
> - e2e-verification-guide.md → `docs/operations/e2e-verification-guide.md`

---

## 评估方法

对每份文档执行「五个问题」：

| # | 问题 | 回答「否」意味着 |
|---|------|----------------|
| 1 | 还能指导今天的决策吗？ | → 已过时效 |
| 2 | 未来还会被引用吗？ | → 低复用 |
| 3 | 结论已被代码/产品取代了吗？ | → 文档使命完成 |
| 4 | 同类文档是否重复？ | → 保留定稿，删除草稿 |
| 5 | 能否用一句话概括它的价值？ | → 过程噪音 |

**判定规则**：4+「否」→ 可清理；3+「否」→ 压缩归档；2+「是」→ 保留维护。

---

## 一、active/ 目录评估（8 份文档）

### ✅ 核心资产（保留维护）

| 文档 | Q1 时效 | Q2 复用 | Q3 已落地 | Q4 重复 | Q5 价值 | 结论 |
|------|---------|---------|-----------|---------|---------|------|
| **SECURITY.md** | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 安全权威文档 |
| **architecture-design.md** | ✅ | ✅ | ⚠️ 部分 | ✅ | ✅ | **核心资产** — 架构全景图 |
| **interview-intro.md** | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 面试脚本 |
| **interview-deep-dive.md** | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 深挖 Q&A |
| **prompt-engineering.md** | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — Prompt 设计 |
| **model-comparison.md** | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 模型/成本 |
| **rag-evaluation.md** | ✅ | ✅ | ⚠️ 部分 | ✅ | ✅ | **核心资产** — RAG 评估 |

### ⚠️ 需要审查

| 文档 | Q1 时效 | Q2 复用 | Q3 已落地 | Q4 重复 | Q5 价值 | 结论 |
|------|---------|---------|-----------|---------|---------|------|
| **e2e-verification-guide.md** | ⚠️ | ⚠️ | ⚠️ | ✅ | ⚠️ | **待定** — 见下方详细分析 |

**e2e-verification-guide.md 详细分析**：
- Q1：内容描述的验证步骤与当前代码基本一致（2026-06-17 复审确认）
- Q2：未来 E2E 测试维护时可能参考，但频率低
- Q3：验证步骤已落地为 `tests/e2e/` 中的实际测试代码
- Q5：一句话价值 — "E2E 测试的手动验证指南"
- **结论**：保留维护，已添加 v5.4 兼容性复审说明

---

## 二、archive/ 目录评估（9 份文档）

### 📦 历史参考（保留现状）

| 文档 | 大小 | 一句话价值 | 状态 |
|------|------|-----------|------|
| 2026-06-07-v4.3-audit-and-ci-fix-summary.md | 7KB | v4.3 审计修复记录 | ✅ 已归档，保留 |
| audit-cleanup-design.md | 6KB | 代码清理设计 | ✅ 已归档，保留 |
| full-code-quality-improvement.md | 12KB | 代码质量改进记录 | ✅ 已归档，保留 |
| prod-hotfix-design.md | 2KB | 生产热修复设计 | ✅ 已归档，保留 |
| comprehensive-optimization.md | 3KB | 综合优化记录 | ✅ 已归档，保留 |
| frontend-architecture.md | 3KB | 前端架构说明 | ✅ 已归档，保留 |
| security-audit-summary.md | 1KB | 安全审计摘要 | ✅ 已归档，保留 |

### 🗑️ 需要清理

| 文档 | 大小 | 问题 | 建议 |
|------|------|------|------|
| **rag-evaluation-report.json** | 13KB | **已过时** — 数据为 63.3%（改进前），`docs/rag-evaluation-report.json` 有最新数据 80% | **删除** — 历史数据已无参考价值，git 历史可追溯 |
| **README.md** | 1KB | 归档目录的索引，内容简略 | **保留** — 作为归档目录的入口说明 |

---

## 三、decisions/ 目录评估（1 份文档）

### 🚨 重大缺失：无实际 ADR

**现状**：`decisions/README.md` 只有模板，没有任何实际的 ADR 记录。

**治理框架要求**：关键决策应写 ADR 存入 `decisions/`。

**项目中已有的关键决策（应写 ADR 但未写）**：

| # | 决策 | 来源 | 建议 ADR 标题 |
|---|------|------|--------------|
| 1 | 选择 LangGraph 而非 LangChain Agent | architecture-design.md | ADR-001: 选择 LangGraph 作为多 Agent 编排框架 |
| 2 | 原生 JS 而非 React/Vue | README.md 前端选型 | ADR-002: 前端使用原生 JavaScript |
| 3 | Qwen2.5-7B 作为默认模型 | model-comparison.md | ADR-003: 默认 LLM 选型 |
| 4 | ChromaDB + 中文 Embedding 降级链 | rag-evaluation.md | ADR-004: RAG 向量库与 Embedding 选型 |
| 5 | 双层缓存（MD5 + Jaccard） | architecture-design.md | ADR-005: 双层缓存策略 |
| 6 | ~~PBKDF2 而非 Argon2id~~ | ~~SECURITY.md~~ | ~~ADR-006: 密码哈希算法选型~~ | ✅ v5.4 已升级到 Argon2id |

**建议**：从 `architecture-design.md` 和 `model-comparison.md` 中提取关键决策，写 3-5 份 ADR。

---

## 四、根目录文档评估

| 文档 | 大小 | Q1 | Q2 | Q3 | Q4 | Q5 | 结论 |
|------|------|----|----|----|----|----|------|
| **README.md** | 58KB | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 但过长（见建议） |
| **CHANGELOG.md** | 6KB | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 版本历史 |
| **CLAUDE.md** | 3KB | ✅ | ✅ | ❌ | ✅ | ✅ | **核心资产** — 项目规则 |
| **plan.md** | 18KB | ❌ | ❌ | ✅ | ❌ | ❌ | 🗑️ **可清理** — 开发计划已全部完成 |
| **plan-phase2.md** | 2KB | ❌ | ❌ | ✅ | ❌ | ❌ | 🗑️ **可清理** — Phase 2 计划已落地 |

### plan.md / plan-phase2.md 详细分析

- Q1：计划中的所有任务已完成（v5.0 已发布）→ **已过时效**
- Q2：不会再被引用（不是架构文档，是过程计划）→ **低复用**
- Q3：结论已全部落地为代码 → **使命完成**
- Q4：CHANGELOG.md 已记录所有变更 → **有替代**
- Q5：一句话价值 = "开发过程的计划清单" → **过程噪音**
- **判定**：5 个「否」→ **删除**（git 历史可追溯）

---

## 五、其他目录评估

### docs/release/

| 文档 | 结论 | 理由 |
|------|------|------|
| miniapp-release-checklist-template.md | 🗑️ **删除或移走** | 与当前项目无关（微信小程序提审），属于另一个项目的模板 |

### docs/superpowers/specs/

| 文档 | 大小 | 结论 | 理由 |
|------|------|------|------|
| theme-switcher-acceptance.md | 11KB | 📦 **归档** | 功能已实现，验收报告使命完成 |
| theme-switcher-design.md | 16KB | 📦 **归档** | 设计已落地为代码 |
| theme-switcher-implementation.md | 18KB | 📦 **归档** | 实施已完成 |

- Q1：功能已实现，不再指导决策 → 已过时效
- Q2：除非主题系统有大改动，否则不会被引用 → 低复用
- Q3：结论已完全落地为代码 → 使命完成
- **建议**：将 3 份文件压缩为 1 份 ADR 存入 `decisions/`，原文移入 `archive/`

### docs/fix-plan.md

| 文档 | 结论 | 理由 |
|------|------|------|
| fix-plan.md | 🗑️ **删除** | 所有修复项已完成，过程计划无持续价值 |

### docs/rag-evaluation-report.json（根目录）

| 文档 | 结论 | 理由 |
|------|------|------|
| docs/rag-evaluation-report.json | ⚠️ **移动** | 最新评估数据（80%），应移入 `docs/active/` 或保留在根目录但删除 archive 中的旧版本 |

---

## 六、治理总览

### 按象限分类

```
                    高复用                    低复用
              ┌─────────────────┬─────────────────┐
  仍有时效     │ ✅ 核心资产      │ ⚡ 临时产物       │
              │                 │                 │
              │ SECURITY.md     │ fix-plan.md     │
              │ architecture.md │ plan.md         │
              │ interview-*.md  │ plan-phase2.md  │
              │ prompt-eng.md   │ superpowers/    │
              │ model-comp.md   │ miniapp模板     │
              │ rag-eval.md     │                 │
              │ README.md       │                 │
              │ CHANGELOG.md    │                 │
              │ CLAUDE.md       │                 │
              ├─────────────────┼─────────────────┤
  已过时效     │ 📦 历史参考      │ 🗑️ 可清理        │
              │                 │                 │
              │ archive/*.md    │ archive/rag旧JSON│
              │ decisions/模板   │ e2e-guide (如过时)│
              │                 │                 │
              └─────────────────┴─────────────────┘
```

### 统计

| 分类 | 数量 | 总大小 | 处理方式 |
|------|------|--------|---------|
| ✅ 核心资产（active/） | 8 | ~83KB | 持续维护 |
| ✅ 核心资产（根目录） | 3 | ~67KB | 持续维护 |
| 📦 历史参考（archive/） | 8 | ~46KB | 保留现状 |
| 🗑️ 可清理 | 5 | ~40KB | 删除（git 可追溯） |
| ⚠️ 需治理 | 6 | ~63KB | 移动/压缩/写 ADR |

---

## 七、执行建议（按优先级）

### P0：立即执行（30 分钟内）

| # | 操作 | 文件 | 说明 |
|---|------|------|------|
| 1 | 删除 | `plan.md` | 开发计划已全部完成，5 问全否 |
| 2 | 删除 | `plan-phase2.md` | 同上 |
| 3 | 删除 | `docs/fix-plan.md` | 修复计划已全部完成 |
| 4 | 删除 | `docs/archive/rag-evaluation-report.json` | 旧版评估数据（63.3%），新版在 `docs/rag-evaluation-report.json` |
| 5 | 删除/移走 | `docs/release/miniapp-release-checklist-template.md` | 与当前项目无关 |

### P1：短期执行（1-2 小时）

| # | 操作 | 文件 | 说明 |
|---|------|------|------|
| 6 | 归档 | `docs/superpowers/specs/` 3 份文件 → `docs/archive/` | 功能已实现，设计文档使命完成 |
| 7 | 移动 | `docs/rag-evaluation-report.json` → `docs/active/rag-evaluation-report.json` | 最新评估数据应归入 active |
| 8 | 审查 | `docs/active/e2e-verification-guide.md` | 检查内容是否与当前代码一致，不一致则更新或归档 |

### P2：补充执行（半天）

| # | 操作 | 说明 |
|---|------|------|
| 9 | 写 ADR | 从 architecture-design.md 提取 3-5 份关键决策 ADR 存入 `decisions/` |
| 10 | 压缩 superpowers | 3 份主题切换文档压缩为 1 份 ADR（ADR-007: 主题切换系统设计） |

### P3：README 精简（可选）

README 当前 58KB / 1092 行，建议：
- 将"架构设计"章节的 mermaid 图保留，详细说明移入 `architecture-design.md`
- 将"安全设计"章节保留摘要，详细内容引用 `SECURITY.md`
- 将"API 文档"章节移入独立文件 `docs/active/api-reference.md`
- 目标：README 精简到 30KB 以内
