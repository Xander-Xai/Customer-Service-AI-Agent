---
name: skill-filtering-report
description: 技能筛选报告 - 102个技能包中筛选出与客服AI Agent项目相关的可用技能
metadata:
  type: reference
  created: 2026-06-03
  based-on: skill-matrix.md
---

# 技能筛选报告

## 筛选方法

1. 扫描 `/home/dev/.claude/skills/` 目录下 102 个技能包
2. 对每个 SKILL.md 执行关键词搜索（agent, LLM, RAG, function.calling, security, code.review, test, documentation, performance 等）
3. 读取匹配技能的描述信息（前 20 行）
4. 按项目匹配度分为三级：直接可用、间接相关、参考价值

---

## 项目概况

本项目是 **Python + FastAPI + LangGraph 多智能体客服系统**，包含 9 个核心模块：

| 模块 | 职责 | 关键技术 |
|------|------|---------|
| agents/ | 8 个多 Agent 专家系统 | LangGraph, LLM, ReAct |
| api/ | FastAPI HTTP + WebSocket | FastAPI, Pydantic |
| cache/ | L1/L2 双层缓存 | TTL, LRU |
| collaboration/ | 5 种协作模式 | 并行, 串行, Workflow |
| core/ | MessageBus + Monitoring | 事件驱动, Prometheus |
| erp/ | 金蝶 ERP 集成 | REST API, Mock |
| rag/ | ChromaDB RAG 知识库 | ChromaDB, 向量检索 |
| router/ | 双层查询路由 | 关键词 + 语义 |
| tools/ | Function Calling 工具 | OpenAI Tools 格式 |

---

## ⭐ 直接可用（10 个）— 高度相关，立即使用

### 1. security-and-hardening
- **路径**: `/skills/security-and-hardening/`
- **匹配原因**: 项目涉及用户输入处理、认证授权、外部 API 集成（金蝶 ERP）、WebSocket 连接
- **适用场景**: OWASP Top 10 防护、输入验证、SQL 注入防护、XSS 防护、密钥管理
- **项目关联模块**: api/, erp/, tools/, agents/

### 2. code-review-and-quality
- **路径**: `/skills/code-review-and-quality/`
- **匹配原因**: 多维度代码审查（正确性、可读性、架构、安全、性能）直接适用于代码质量提升
- **适用场景**: PR 合并前、特性实现后、Bug 修复后、代码重构前
- **项目关联模块**: 全部模块

### 3. test-driven-development
- **路径**: `/skills/test-driven-development/`
- **匹配原因**: 项目已有 182 个测试，TDD 流程确保修复不破坏现有功能
- **适用场景**: 实现新逻辑、修复 Bug、修改现有功能
- **项目关联模块**: 全部模块

### 4. systematic-debugging
- **路径**: `/skills/systematic-debugging/`
- **匹配原因**: 四阶段调试法、根因追踪适用于复杂多 Agent 系统的 Bug 排查
- **适用场景**: 任何 Bug、测试失败、意外行为
- **项目关联模块**: 全部模块

### 5. verification-before-completion
- **路径**: `/skills/verification-before-completion/`
- **匹配原因**: 完成前验证、证据优先断言确保每次变更都经过充分验证
- **适用场景**: 声称工作完成、提交/PR 前、发布前
- **项目关联模块**: 全部模块

### 6. documentation-and-adrs
- **路径**: `/skills/documentation-and-adrs/`
- **匹配原因**: 架构决策记录（ADR）适用于记录 LangGraph 工作流设计、协作模式选择等决策
- **适用场景**: 架构决策、API 设计变更、功能发布
- **项目关联模块**: core/, collaboration/, agents/

### 7. doc-coauthoring
- **路径**: `/skills/doc-coauthoring/`
- **匹配原因**: 文档协写三阶段工作流适用于重构 README、编写 API 参考文档
- **适用场景**: 写文档、提案、技术规范、README 重构
- **项目关联模块**: 全部文档产出

### 8. code-simplification
- **路径**: `/skills/code-simplification/`
- **匹配原因**: 代码简化、复杂度降低适用于重构后简化、消除重复代码
- **适用场景**: 重构后简化、复杂性积累、代码审查后
- **项目关联模块**: 全部模块

### 9. api-and-interface-design
- **路径**: `/skills/api-and-interface-design/`
- **匹配原因**: 稳定 API 设计、契约优先直接适用于 FastAPI 端点设计
- **适用场景**: 设计新端点、定义模块边界、FastAPI 接口
- **项目关联模块**: api/, tools/

### 10. performance-optimization
- **路径**: `/skills/performance-optimization/`
- **匹配原因**: 性能测量、优化策略适用于缓存命中率优化、并发处理优化
- **适用场景**: 性能要求存在、怀疑性能回归、负载优化
- **项目关联模块**: cache/, core/, api/

---

## 🟡 间接相关（21 个）— 部分场景可用

### 任务规划类

| # | 技能 | 路径 | 适用场景 |
|---|------|------|---------|
| 1 | planning-and-task-breakdown | `/skills/planning-and-task-breakdown/` | 有 spec 需要分解、任务太大、并行化需求 |
| 2 | brainstorming | `/skills/brainstorming/` | 功能设计、需求澄清前 |
| 3 | spec-driven-development | `/skills/spec-driven-development/` | 新项目/特性开始、需求不明确 |
| 4 | idea-refine | `/skills/idea-refine/` | 想法模糊、需要压力测试 |
| 5 | writing-plans | `/skills/writing-plans/` | 有 spec 需要转换为可执行计划 |

### 开发流程类

| # | 技能 | 路径 | 适用场景 |
|---|------|------|---------|
| 6 | incremental-implementation | `/skills/incremental-implementation/` | 多文件变更、大特性构建 |
| 7 | git-workflow-and-versioning | `/skills/git-workflow-and-versioning/` | 代码变更、提交、分支管理 |
| 8 | finishing-a-development-branch | `/skills/finishing-a-development-branch/` | 分支合并、工作树清理 |
| 9 | deprecation-and-migration | `/skills/deprecation-and-migration/` | 移除旧系统、API 迁移 |
| 10 | source-driven-development | `/skills/source-driven-development/` | 构建框架/库集成时引用权威来源 |
| 11 | doubt-driven-development | `/skills/doubt-driven-development/` | 正确性关键、不熟悉的代码 |

### 调试与审查类

| # | 技能 | 路径 | 适用场景 |
|---|------|------|---------|
| 12 | debugging-and-error-recovery | `/skills/debugging-and-error-recovery/` | 测试失败、构建中断、行为异常 |
| 13 | requesting-code-review | `/skills/requesting-code-review/` | 任务完成、大特性实现、PR 前 |
| 14 | receiving-code-review | `/skills/receiving-code-review/` | 收到审查意见、处理反馈 |

### CI/CD 与发布类

| # | 技能 | 路径 | 适用场景 |
|---|------|------|---------|
| 15 | ci-cd-and-automation | `/skills/ci-cd-and-automation/` | 搭建构建/部署流水线 |
| 16 | shipping-and-launch | `/skills/shipping-and-launch/` | 生产部署、版本发布、数据迁移 |

### Agent 协作类

| # | 技能 | 路径 | 适用场景 |
|---|------|------|---------|
| 17 | context-engineering | `/skills/context-engineering/` | 新会话开始、质量下降、任务切换 |
| 18 | subagent-driven-development | `/skills/subagent-driven-development/` | 执行独立任务计划、并行开发 |
| 19 | dispatching-parallel-agents | `/skills/dispatching-parallel-agents/` | 多个独立失败、并行调查 |
| 20 | using-agent-skills | `/skills/using-agent-skills/` | 会话开始、发现适用技能 |
| 21 | find-skills | `/skills/find-skills/` | 扩展能力、寻找特定用途的技能 |

---

## 📋 参考价值（8 个）— 特定场景有价值

| # | 技能 | 路径 | 说明 |
|---|------|------|------|
| 1 | webapp-testing | `/skills/webapp-testing/` | Web 界面验证（如有前端需求） |
| 2 | browser-testing-with-devtools | `/skills/browser-testing-with-devtools/` | Chrome DevTools 测试 |
| 3 | full-output-enforcement | `/skills/full-output-enforcement/` | 大量代码生成时防止截断 |
| 4 | frontend-design | `/skills/frontend-design/` | 前端 UI 改进（如需管理后台） |
| 5 | frontend-ui-engineering | `/skills/frontend-ui-engineering/` | UI 实现、布局、状态管理 |
| 6 | using-git-worktrees | `/skills/using-git-worktrees/` | 隔离工作空间（复杂重构时） |
| 7 | composition-patterns | `/skills/composition-patterns/` | React 组件模式（如有前端） |
| 8 | devops-engineer | `/skills/devops-engineer/` | Dockerfile、K8s 容器化部署 |

---

## 技能组合链路

### 审查链（任务 3 核心）

```
systematic-debugging → debugging-and-error-recovery → verification-before-completion
         ↓                      ↓                           ↓
 code-review-and-quality ← security-and-hardening ← performance-optimization
```

### 文档链（任务 2 核心）

```
brainstorming → documentation-and-adrs → doc-coauthoring
       ↓               ↓                      ↓
spec-driven-development → writing-plans → verification-before-completion
```

### 测试链（质量门禁）

```
test-driven-development → verification-before-completion
         ↓                        ↓
code-review-and-quality → systematic-debugging
```

### 实现链（结构性重构）

```
planning-and-task-breakdown → incremental-implementation
           ↓                         ↓
  context-engineering → subagent-driven-development
           ↓                         ↓
  git-workflow-and-versioning → finishing-a-development-branch
```

### 质量门禁链（最终验证）

```
requesting-code-review → receiving-code-review
         ↓                    ↓
 code-simplification → code-review-and-quality
         ↓                    ↓
  verification-before-completion
```

---

## 统计摘要

| 级别 | 数量 | 占比 |
|------|------|------|
| ⭐ 直接可用 | 10 | 9.8% |
| 🟡 间接相关 | 21 | 20.6% |
| 📋 参考价值 | 8 | 7.8% |
| 无关技能 | 63 | 61.8% |
| **总计** | **102** | **100%** |

---

## 推荐掌握顺序

### 第一优先级（立即掌握）
1. `security-and-hardening` — 安全是客服系统的第一要务
2. `code-review-and-quality` — 代码质量的基础
3. `test-driven-development` — 测试是质量保证的核心
4. `verification-before-completion` — 验证是完成的标准

### 第二优先级（一周内掌握）
5. `systematic-debugging` — 调试复杂系统必备
6. `api-and-interface-design` — FastAPI 接口设计
7. `performance-optimization` — 缓存和并发优化
8. `code-simplification` — 代码简化和重构

### 第三优先级（按需掌握）
9. `documentation-and-adrs` — 文档和架构决策
10. `doc-coauthoring` — 文档编写协作
11. Agent 协作类技能（context-engineering, subagent-driven-development 等）

---

*本报告由技能筛选流水线产出，基于 102 个技能包的关键词搜索和内容分析，2026-06-03 完成*
