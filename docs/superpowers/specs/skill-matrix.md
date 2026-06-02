---
name: skill-matrix
description: 多智能体客服系统 v3.7 技能矩阵 - 72 个可用技能盘点与任务推荐
metadata:
  type: reference
  created: 2026-06-03
---

# 多智能体客服系统 v3.7 技能矩阵

## 一、统计摘要

| 分类 | 数量 | 说明 |
|------|------|------|
| 🔴 核心技能 | 10 | 直接适用于项目的关键技能 |
| 🟡 有用技能 | 21 | 对项目有帮助的技能 |
| 🟢 可选技能 | 8 | 特定场景下有价值 |
| ⚪ 无关技能 | 33 | 与本后端项目无关 |
| **总计** | **72** | 实际可用技能 |

> 本项目是 Python + FastAPI + LangGraph 的多智能体客服系统，包含 agents、api、cache、collaboration、core、erp、rag、router、tools 等 9 个核心模块。

---

## 二、技能总览表

### 🔴 核心技能（10 个）— 直接用于三个任务

| 技能名称 | 路径 | 主要用途 | 适用场景 |
|---------|------|---------|---------|
| `security-and-hardening` | `/skills/security-and-hardening/` | 安全加固、OWASP Top 10 防护、输入验证 | 处理用户输入、认证授权、敏感数据、外部 API 集成 |
| `code-review-and-quality` | `/skills/code-review-and-quality/` | 多维度代码审查（正确性、可读性、架构、安全、性能） | PR 合并前、特性实现后、Bug 修复后、代码重构前 |
| `test-driven-development` | `/skills/test-driven-development/` | TDD 开发流程、测试金字塔、Prove-It 模式 | 实现新逻辑、修复 Bug、修改现有功能 |
| `systematic-debugging` | `/skills/systematic-debugging/` | 四阶段调试法、根因追踪、防御性编程 | 任何 Bug、测试失败、意外行为 |
| `verification-before-completion` | `/skills/verification-before-completion/` | 完成前验证、证据优先断言 | 声称工作完成、提交/PR 前 |
| `documentation-and-adrs` | `/skills/documentation-and-adrs/` | 架构决策记录 (ADR)、技术文档编写 | 架构决策、API 设计变更、功能发布 |
| `doc-coauthoring` | `/skills/doc-coauthoring/` | 文档协写、三阶段工作流 | 写文档、提案、技术规范、README 重构 |
| `code-simplification` | `/skills/code-simplification/` | 代码简化、复杂度降低、行为保持 | 重构后简化、复杂性积累、代码审查后 |
| `api-and-interface-design` | `/skills/api-and-interface-design/` | 稳定 API 设计、契约优先、边界验证 | 设计新端点、定义模块边界、FastAPI 接口 |
| `performance-optimization` | `/skills/performance-optimization/` | 性能测量、优化策略、Web Vitals | 性能要求存在、怀疑性能回归、负载优化 |

---

### 🟡 有用技能（21 个）— 辅助提升质量

| 技能名称 | 路径 | 主要用途 | 适用场景 |
|---------|------|---------|---------|
| `planning-and-task-breakdown` | `/skills/planning-and-task-breakdown/` | 任务分解、垂直切片、依赖图 | 有 spec 需要分解、任务太大、并行化需求 |
| `brainstorming` | `/skills/brainstorming/` | 创意探索、设计验证、澄清问题 | 任何创意工作、功能设计、需求澄清前 |
| `spec-driven-development` | `/skills/spec-driven-development/` | 规范优先开发、六区域规范模板 | 新项目/特性开始、需求不明确 |
| `idea-refine` | `/skills/idea-refine/` | 想法精炼、发散-收敛思维 | 想法模糊、需要压力测试、扩展选项 |
| `deprecation-and-migration` | `/skills/deprecation-and-migration/` | 废弃管理、渐进迁移、Strangler 模式 | 移除旧系统、API 迁移、Zombie 代码 |
| `incremental-implementation` | `/skills/incremental-implementation/` | 增量实现、薄切片、增量周期 | 多文件变更、大特性构建、风险优先 |
| `git-workflow-and-versioning` | `/skills/git-workflow-and-versioning/` | Git 工作流、原子提交、变更管理 | 任何代码变更、提交、分支管理 |
| `ci-cd-and-automation` | `/skills/ci-cd-and-automation/` | CI/CD 流水线、质量门禁、自动化测试 | 搭建构建/部署流水线、自动化检查 |
| `requesting-code-review` | `/skills/requesting-code-review/` | 请求代码审查、审查流程 | 任务完成、大特性实现、PR 前 |
| `receiving-code-review` | `/skills/receiving-code-review/` | 接收审查反馈、技术验证 | 收到审查意见、处理反馈、实施建议 |
| `finishing-a-development-branch` | `/skills/finishing-a-development-branch/` | 分支完成、合并策略、工作树清理 | 实现完成、决定如何集成 |
| `context-engineering` | `/skills/context-engineering/` | Agent 上下文优化、上下文层次 | 新会话开始、质量下降、任务切换 |
| `subagent-driven-development` | `/skills/subagent-driven-development/` | 子 Agent 驱动开发、两阶段审查 | 执行独立任务计划、并行开发 |
| `dispatching-parallel-agents` | `/skills/dispatching-parallel-agents/` | 并行 Agent 分发、独立问题域 | 多个独立失败、并行调查 |
| `using-agent-skills` | `/skills/using-agent-skills/` | 技能发现、生命周期序列 | 会话开始、发现适用技能 |
| `find-skills` | `/skills/find-skills/` | 探索遗漏的技能、发现新技能 | 扩展能力、寻找特定用途的技能 |
| `debugging-and-error-recovery` | `/skills/debugging-and-error-recovery/` | 系统化调试、根因分析、故障恢复 | 测试失败、构建中断、行为异常 |
| `writing-plans` | `/skills/writing-plans/` | 实现计划编写、任务粒度 | 有 spec 需要转换为可执行计划 |
| `source-driven-development` | `/skills/source-driven-development/` | 官方文档驱动、引用权威来源 | 构建框架/库集成、确保正确性 |
| `doubt-driven-development` | `/skills/doubt-driven-development/` | 质疑每个决策、审查每个选择 | 正确性关键、不熟悉的代码、高风险场景 |
| `shipping-and-launch` | `/skills/shipping-and-launch/` | 发布和启动检查、灰度发布、回滚策略 | 生产部署、版本发布、数据迁移 |

---

### 🟢 可选技能（8 个）— 特定场景有价值

| 技能名称 | 路径 | 主要用途 | 适用场景 |
|---------|------|---------|---------|
| `webapp-testing` | `/skills/webapp-testing/` | Web 应用测试、Playwright | Web 界面验证、截图、浏览器日志 |
| `browser-testing-with-devtools` | `/skills/browser-testing-with-devtools/` | Chrome DevTools 测试 | 浏览器运行时验证 |
| `full-output-enforcement` | `/skills/full-output-enforcement/` | 输出强制执行、禁止截断 | 大量代码生成、完整输出要求 |
| `frontend-design` | `/skills/frontend-design/` | 前端设计、反 AI slop | 前端 UI 改进、web 组件 |
| `frontend-ui-engineering` | `/skills/frontend-ui-engineering/` | 前端 UI 工程、生产质量 | UI 实现、布局、状态管理 |
| `using-git-worktrees` | `/skills/using-git-worktrees/` | Git Worktree 使用、隔离工作空间 | 需要隔离工作、特征工作 |
| `composition-patterns` | `/skills/composition-patterns/` | React 组合模式 | 组件重构、灵活 API 设计 |
| `devops-engineer` | `/skills/devops-engineer/` | DevOps 工程、Dockerfile、K8s | 容器化、基础设施、GitOps |

---

### ⚪ 无关技能（33 个）— 与本项目无关

| 技能名称 | 说明 |
|---------|------|
| `design-taste-frontend` | 前端视觉设计，本项目为后端 API |
| `design-taste-frontend-v1` | 同上 |
| `high-end-visual-design` | 高端视觉设计 |
| `minimalist-ui` | 极简 UI 设计 |
| `industrial-brutalist-ui` | 工业粗野主义 UI |
| `gpt-taste` | GSAP 动画设计 |
| `stitch-design-taste` | Google Stitch 设计 |
| `redesign-existing-projects` | 网站重设计 |
| `theme-factory` | 主题创建 |
| `image-to-code` | 图片转代码 |
| `imagegen-frontend-web` | Web 图片生成 |
| `imagegen-frontend-mobile` | 移动端图片生成 |
| `algorithmic-art` | 算法艺术 |
| `canvas-design` | Canvas 设计 |
| `brand-guidelines` | Anthropic 品牌指南 |
| `brandkit` | 品牌工具包 |
| `mcp-builder` | MCP 服务器构建（非本项目范围） |
| `slack-gif-creator` | Slack GIF 创建 |
| `pptx` | PowerPoint 文档 |
| `pdf` | PDF 文档处理 |
| `docx` | Word 文档处理 |
| `xlsx` | Excel 文档处理 |
| `notebooklm` | NotebookLM 集成 |
| `notebooklm-skill` | NotebookLM 技能 |
| `llm-intern-skill` | LLM 简历优化 |
| `internal-comms` | 内部沟通 |
| `deep-research` | 深度研究 |
| `agent-browser` | 浏览器自动化（非本项目需要） |
| `github-actions-docs` | GitHub Actions 文档 |
| `mmx-cli` | MiniMax CLI |
| `skill-creator` | 技能创建 |
| `react-best-practices` | React 最佳实践 |
| `react-native-skills` | React Native 技能 |
| `react-view-transitions` | React 视图过渡 |
| `web-artifacts-builder` | Web 工件构建 |
| `web-design-guidelines` | Web 设计指南 |
| `deploy-to-vercel` | Vercel 部署 |
| `vercel-cli-with-tokens` | Vercel CLI |
| `vercel-optimize` | Vercel 优化 |

---

## 三、技能组合链路

### 1. 审查链 (Review Chain) — 任务 3 核心

```
systematic-debugging → debugging-and-error-recovery → verification-before-completion
     ↓                      ↓                           ↓
 code-review-and-quality ← security-and-hardening ← performance-optimization
```

**用途**：对代码进行全面的安全、质量、性能审查。

### 2. 文档链 (Documentation Chain) — 任务 2 核心

```
brainstorming → documentation-and-adrs → doc-coauthoring
     ↓               ↓                      ↓
spec-driven-development → writing-plans → verification-before-completion
```

**用途**：重构 README 和建立 docs/ 目录。

### 3. 测试链 (Testing Chain) — 质量门禁

```
test-driven-development → verification-before-completion
         ↓                        ↓
code-review-and-quality → systematic-debugging
```

**用途**：确保修复不破坏现有测试，验证功能正确性。

### 4. 实现链 (Implementation Chain) — 结构性重构

```
planning-and-task-breakdown → incremental-implementation
           ↓                         ↓
  context-engineering → subagent-driven-development
           ↓                         ↓
  git-workflow-and-versioning → finishing-a-development-branch
```

**用途**：将审查发现的问题转换为可执行的重构任务。

### 5. 质量门禁链 (Quality Gate Chain) — 最终验证

```
requesting-code-review → receiving-code-review
         ↓                    ↓
 code-simplification → code-review-and-quality
         ↓                    ↓
  verification-before-completion
```

**用途**：重构完成后进行最终质量检查。

---

## 四、任务推荐技能调用顺序

### 任务 1：技能盘点与矩阵建立

```
1. planning-and-task-breakdown
   → 分解：文件扫描 → 内容分析 → 分类评估 → Markdown 生成

2. documentation-and-adrs
   → 参考 ADR 模板格式输出 skill-matrix.md

3. doc-coauthoring
   → 协写文档，确保结构清晰

4. verification-before-completion
   → 验证文档完整性和格式正确性

输出：docs/superpowers/specs/skill-matrix.md（已产出）
```

### 任务 2：README 重构与 docs/ 目录建设

```
阶段 1：分析与规划
1. brainstorming
   → 澄清重构目标、受众、成功标准

2. idea-refine
   → 探索不同重构方案

阶段 2：文档编写
3. doc-coauthoring（Context Gathering）
   → 收集当前 README、config.py、所有模块接口

4. documentation-and-adrs
   → 创建架构决策记录

5. doc-coauthoring（Refinement + Reader Testing）
   → 迭代完善文档内容

阶段 3：验证
6. verification-before-completion
   → 验证 README 可读性和完整性

输出：
- 重构后的 README.md
- docs/architecture.md
- docs/api-reference.md
- docs/deployment-guide.md
- docs/security-model.md
```

### 任务 3：全面质量审查 + 结构性重构（Workflow 并行）

```
Phase 1：并行审查（4 个 Agent 并行）
├── Agent 1：安全审计
│   技能：security-and-hardening
│   输入：全部源码
│   输出：漏洞清单（严重度分级）
│
├── Agent 2：代码审查
│   技能：code-review-and-quality
│   输入：全部源码
│   输出：代码问题清单（5 维度）
│
├── Agent 3：功能验证
│   技能：verification-before-completion + test-driven-development
│   输入：README + 全部源码 + 151 测试
│   输出：测试报告 + 模块一致性报告
│
└── Agent 4：瘦身评估
    技能：code-simplification
    输入：全部源码
    输出：瘦身机会清单

Phase 2：汇总去重
- 合并四份报告
- 按优先级排序
- 识别冲突的修复建议

Phase 3：修复与重构（串行）
1. security-and-hardening → 修复 Critical + High 安全漏洞
2. systematic-debugging → 修复 Bug
3. code-simplification → 代码质量改进
4. code-review-and-quality → 结构性重构

Phase 4：二次验证
verification-before-completion → 运行全量测试

输出：
- 安全修复后的代码
- Bug 修复后的代码
- 瘦身后的代码
- 全量测试通过（151/151）
```

---

## 五、本项目模块结构参考

| 模块 | 代码行数 | 核心职责 | 暴露的公共接口 |
|------|---------|---------|---------------|
| agents/ | ~870 行 | 8 种专业 Agent（BaseAgent + 7 子类） | `process(state)`, `_process_with_llm()`, `_retrieve_knowledge()` |
| api/ | ~689 行 | FastAPI 服务层（HTTP + WebSocket） | REST 端点 + WebSocket `/ws/chat` |
| cache/ | ~210 行 | L1+L2 二级缓存 | `get()`, `put()`, `get_stats()` |
| collaboration/ | ~503 行 | 5 种协作模式编排 | `select_mode_name()`, `build_context()` |
| core/ | ~611 行 | MessageBus + SharedBlackboard + Monitoring | `subscribe()`, `publish()`, `write()`, `read()` |
| erp/ | ~372 行 | 金蝶 ERP 适配器（Mock/Real） | `query_product()`, `query_order()`, `sanitize_erp_input()` |
| rag/ | ~453 行 | ChromaDB 向量知识库 | `query()`, `query_multiple()` |
| router/ | ~184 行 | 双层查询路由器 | `route()` → `RoutingResult` |
| tools/ | ~221 行 | Function Calling 工具注册 | `register()`, `get_openai_tools()`, `execute()` |

**总代码行数：~4,113 行**（不含测试）

---

## 六、关键发现

### 1. 技能覆盖度
- 约 **37%** 的技能（31个）对本项目直接有用
- 审查链和实现链是最常用的技能组合

### 2. 推荐掌握顺序
建议优先掌握 10 个核心技能的完整工作流程，然后逐步扩展到有用技能。

### 3. 任务 3 并行化机会
任务 3 的 Phase 1 有天然的并行化机会：
- 安全审查与代码审查可以同时进行
- 功能验证与瘦身评估可以同时进行

这正是设计中使用 Workflow 编排 4 个并行 Agent 的原因。

---

*本文件由 `brainstorming` 技能驱动流水线产出，2026-06-03 完成*