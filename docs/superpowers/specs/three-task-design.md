---
name: three-task-design
description: 技能驱动流水线 + 多 Agent 并行审查三任务设计方案
metadata:
  type: project
  created: 2026-06-03
---

# 三任务流水线设计方案

## 概述

本设计涵盖三个串行任务的完整执行方案：
- **任务 1**：技能盘点与矩阵建立
- **任务 2**：README 全面重构与文档体系建设
- **任务 3**：全面质量审查 + 结构性重构

执行策略：严格串行（A+B 组合方案），任务 3 内部采用 Workflow 编排 4 个并行 Agent。

---

## 执行策略

| 维度 | 决策 |
|------|------|
| 总体执行 | 严格串行：任务 1 → 2 → 3 |
| 任务 3 内部 | Workflow 编排 4 个并行审查 Agent |
| 审查深度 | 全面深度：151 测试 + 逐模块对照 README |
| 瘦身程度 | 结构性重构：可重组模块、合并/拆分文件 |

---

## 任务 1：技能盘点与技能矩阵

### 目标

盘点 72 个可用技能，建立与本项目的关联矩阵，识别可组合使用的技能链。

### 执行步骤

#### Step 1.1 — 技能分类与标注

将 72 个技能按与本项目的**相关度**分为四档：

| 相关度 | 含义 | 示例 |
|--------|------|------|
| 🔴 核心 | 直接用于本次三个任务 | `security-and-hardening`、`code-review-and-quality`、`doc-coauthoring` |
| 🟡 有用 | 可辅助提升质量 | `test-driven-development`、`systematic-debugging`、`performance-optimization` |
| 🟢 可选 | 特定场景下有价值 | `shipping-and-launch`、`ci-cd-and-automation` |
| ⚪ 无关 | 与本项目无关 | `llm-intern-skill`、`slack-gif-creator`、`brandkit` |

#### Step 1.2 — 技能组合链路

识别可串联使用的技能组合：

| 组合名称 | 技能链 | 用途 |
|----------|--------|------|
| 审查链 | `security-and-hardening` → `code-review-and-quality` → `code-simplification` | 任务 3 审查与瘦身 |
| 文档链 | `doc-coauthoring` → `documentation-and-adrs` | 任务 2 文档建设 |
| 质量门禁 | `verification-before-completion` + `test-driven-development` | 每个任务的质量验证 |
| 调试链 | `systematic-debugging` + `doubt-driven-development` | 修复时的根因分析 |

#### Step 1.3 — 核心技能清单（72 个中）

**开发方法论**（11 个）：
- brainstorming, spec-driven-development, planning-and-task-breakdown, executing-plans, subagent-driven-development, incremental-implementation, writing-plans, finishing-a-development-branch, using-git-worktrees, full-output-enforcement, verification-before-completion

**编码质量**（8 个）：
- test-driven-development, code-review-and-quality, requesting-code-review, receiving-code-review, code-simplification, doubt-driven-development, source-driven-development, systematic-debugging, debugging-and-error-recovery

**上下文配置**（7 个）：
- context-engineering, using-agent-skills, using-superpowers, find-skills, writing-skills, template-skill

**设计与视觉**（13 个）：
- frontend-design, frontend-ui-engineering, design-taste-frontend, design-taste-frontend-v1, gpt-taste, high-end-visual-design, industrial-brutalist-ui, minimalist-ui, stitch-design-taste, redesign-existing-projects, theme-factory

**艺术与图像**（5 个）：
- algorithmic-art, canvas-design, image-to-code, imagegen-frontend-web, imagegen-frontend-mobile

**文档处理**（4 个）：
- pdf, docx, pptx, xlsx

**API 与集成**（5 个）：
- api-and-interface-design, claude-api, mcp-builder, notebooklm, notebooklm-skill

**Git 与 CI/CD**（3 个）：
- git-workflow-and-versioning, ci-cd-and-automation, github-actions-docs

**安全与性能**（4 个）：
- security-and-hardening, deprecation-and-migration, performance-optimization, devops-engineer

**文档与通信**（3 个）：
- documentation-and-adrs, doc-coauthoring, internal-comms

**需求与创意**（2 个）：
- idea-refine, interview-me

**测试**（2 个）：
- browser-testing-with-devtools, webapp-testing

**发布**（1 个）：
- shipping-and-launch

**其他**（4 个）：
- slack-gif-creator, web-artifacts-builder, brand-guidelines, brandkit, llm-intern-skill, mmx-cli, skill-creator

#### Step 1.4 — 本项目推荐技能调用顺序

**任务 1 阶段**：
1. `using-agent-skills` — 技能发现
2. `find-skills` — 探索遗漏

**任务 2 阶段**：
1. `doc-coauthoring` — 文档共创（三阶段）
2. `documentation-and-adrs` — 架构决策记录
3. `verification-before-completion` — 文档验证

**任务 3 阶段**：
1. Phase 1（4 并行 Agent）：`security-and-hardening` + `code-review-and-quality` + `verification-before-completion` + `code-simplification`
2. Phase 3 修复：`systematic-debugging` + `test-driven-development`
3. Phase 4：`verification-before-completion`

### 输出物

文件：`docs/superpowers/specs/skill-matrix.md`
- 技能分类表（72 个技能 × 4 档相关度）
- 技能组合链路图
- 每个任务推荐的技能调用顺序

---

## 任务 2：README 全面重构与文档体系建设

### 目标

将 README 从 427 行的"功能概述"升级为**完整、专业、对齐 v3.7 的项目文档**，同时建立 `docs/` 目录。

### 执行步骤

#### Step 2.1 — 现状审计

逐节对照当前 README 与实际代码，标记：
- 过时描述（版本号、测试数量、功能状态）
- 缺失内容（新增的 v3.7 安全加固项）
- 模糊/不准确的表述

#### Step 2.2 — 新 README 结构

```
# 多智能体客服系统 v3.7

## 项目概述（精简，3-5 句话）

## 🏗️ 架构设计
  ├── 系统架构图（Mermaid 格式）
  ├── 四层状态机流程图
  ├── 模块依赖关系图
  └── 数据流图

## 🤖 功能模块详解
  ├── Agent 系统（8 个 Agent）
  ├── 协作模式（5 种）
  ├── 路由系统（双层意图分类）
  ├── RAG 知识库（ChromaDB）
  ├── Function Calling
  ├── ReAct 推理
  ├── 缓存系统（L1/L2）
  ├── 会话管理
  ├── 监控系统
  └── ERP 集成

## 🚀 快速开始

## 📡 API 参考

## 🔒 安全设计

## 🧪 测试指南

## 🐳 部署

## 📁 项目结构

## 📋 变更日志

## 🤝 贡献指南
```

#### Step 2.3 — docs/ 目录建立

```
docs/
├── architecture.md
├── api-reference.md
├── deployment-guide.md
├── security-model.md
└── superpowers/
    └── specs/
        ├── skill-matrix.md
        └── three-task-design.md
```

#### Step 2.4 — 版本同步清单

- config.py 版本号 → v3.7.0
- 测试数量 → `pytest --collect-only`
- 功能清单 → 逐模块确认
- 安全加固项 → 对照 memory 记录

### 使用的技能

- `doc-coauthoring` — 三阶段文档共创流程
- `documentation-and-adrs` — 架构决策记录

---

## 任务 3：全面质量审查 + 结构性重构

### 目标

以新 README 为参照基准，对每个功能模块进行审查，最后进行结构性重构。

### 总体流程

```
Phase 1: 并行审查（4 Agent 并行）
  ├── Agent 1: 安全审计（security-and-hardening）
  ├── Agent 2: 代码审查（code-review-and-quality）
  ├── Agent 3: 功能验证（verification-before-completion）
  └── Agent 4: 瘦身评估（code-simplification）
       ↓
Phase 2: 汇总与去重
       ↓
Phase 3: 修复与重构（串行）
  ├── 3.1 安全漏洞修复（最高优先级）
  ├── 3.2 Bug 修复
  ├── 3.3 代码质量改进
  └── 3.4 结构性重构
       ↓
Phase 4: 二次验证
       ↓
  最终交付
```

### Phase 1 详细设计

#### Agent 1 — 安全审计员

**技能指导**：`security-and-hardening`
**Agent 类型**：`agent-skills:security-auditor`

**审查维度**：
- 输入校验（SQL 注入、XSS、命令注入）
- 认证/授权漏洞（API Key 泄露、权限越界）
- 敏感数据处理（日志中是否打印密钥、PII）
- 依赖安全（requirements.txt 已知漏洞）
- WebSocket 安全（连接劫持、消息注入）
- SSRF 风险（外部 API 调用）

**输出**：漏洞清单，每个漏洞含：
- `严重度(Critical/High/Medium/Low)`
- `位置（文件:行号）`
- `描述`
- `修复建议`

#### Agent 2 — 代码审查员

**技能指导**：`code-review-and-quality`
**Agent 类型**：`agent-skills:code-reviewer`

**审查维度（5 轴）**：
- 正确性：逻辑错误、边界条件、异常处理
- 可读性：命名、注释、函数长度、复杂度
- 架构：模块耦合、职责清晰度、接口设计
- 安全：与 Agent 1 交叉验证
- 性能：异步正确性、内存泄漏、N+1 查询

**输出**：代码问题清单，每个问题含：
- `维度`
- `位置`
- `描述`
- `严重度`
- `建议`

#### Agent 3 — 功能验证员

**技能指导**：`verification-before-completion` + `test-driven-development`
**Agent 类型**：`agent-skills:test-engineer`

**验证内容**：
- 运行全部 151 个测试，记录通过/失败/跳过
- 对照新 README 的"功能模块详解"，逐模块验证
- 识别"文档有但代码无"和"代码有但文档无"的差异

**输出**：
- 测试报告
- 模块一致性验证报告

#### Agent 4 — 瘦身评估员

**技能指导**：`code-simplification`
**Agent 类型**：`agent-skills:code-reviewer`

**评估内容**：
- 重复代码检测（跨文件的相似逻辑）
- 死代码识别（未使用的函数、类、导入）
- 过长函数（>50 行的函数拆分建议）
- 模块耦合度分析
- 依赖冗余（requirements.txt 中未使用的包）

**输出**：瘦身机会清单，每个机会含：
- `类型(重复/死代码/过长/耦合)`
- `位置`
- `当前行数`
- `预估精简后行数`
- `重构建议`

### Phase 3 修复优先级

| 步骤 | 内容 | 依据 | 验证方式 |
|------|------|------|----------|
| 3.1 安全修复 | 修复 Critical + High 漏洞 | Agent 1 报告 | 安全测试 |
| 3.2 Bug 修复 | 修复功能正确性问题 | Agent 2 + 3 报告 | 相关测试 |
| 3.3 质量改进 | 改善可读性、架构、性能 | Agent 2 报告 | 全量测试 |
| 3.4 结构重构 | 模块重组、文件合并/拆分 | Agent 4 报告 | 全量测试 |

### 重构约束

- 每次重构后立即运行测试，确保零回归
- 模块重组时更新所有 import 路径
- 文件拆分/合并时同步更新 README 和 docs/
- 保留 git 原子提交，每步可回滚

### Phase 4 二次验证

- 运行全量测试，确认 151 个测试全部通过
- 对照最终 README 再做一轮快速一致性检查
- 生成最终审查报告

---

## 技能使用汇总表

| 任务 | 阶段 | 使用的技能 |
|------|------|-----------|
| 1 | 全阶段 | using-agent-skills, find-skills |
| 2 | 全阶段 | doc-coauthoring, documentation-and-adrs, verification-before-completion |
| 3 | Phase 1 | security-and-hardening, code-review-and-quality, verification-before-completion, code-simplification |
| 3 | Phase 3 | systematic-debugging, test-driven-development |
| 3 | Phase 4 | verification-before-completion |

---

## 验收标准

### 任务 1 验收
- [ ] 72 个技能全部盘点完成
- [ ] 技能分为 4 档，相关度准确
- [ ] 技能组合链路清晰
- [ ] 每个任务推荐了正确的技能调用顺序
- [ ] `docs/superpowers/specs/skill-matrix.md` 已生成并 commit

### 任务 2 验收
- [ ] README 对齐 v3.7（版本号、测试数量、功能清单）
- [ ] 包含架构图（Mermaid）
- [ ] 所有 10 个功能模块有详解
- [ ] API 参考完整
- [ ] docs/ 目录建立，包含 4 个深度文档
- [ ] 快速开始指南可用

### 任务 3 验收
- [ ] 4 个审查 Agent 全部完成，报告齐全
- [ ] Critical + High 安全漏洞全部修复
- [ ] 所有 Bug 修复完成
- [ ] 代码质量改进完成
- [ ] 结构性重构完成，无回归
- [ ] 151 个测试全部通过
- [ ] README 与代码一致性 100%

---

*本设计于 2026-06-03 完成，等待用户确认后开始执行*