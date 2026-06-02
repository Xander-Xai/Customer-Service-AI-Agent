# 三任务并行实施计划 — 技能筛选 + README对齐 + 全量审查瘦身

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 并行执行三个任务——筛选可用技能、对齐README、全量审查+保守瘦身——提升项目文档质量和代码质量

**Architecture:** 三个独立 Agent 并行执行，任务1只读搜索无冲突，任务2和任务3各自在 worktree 中工作避免文件冲突。任务3内部使用流水线：先并行审查再统一修复。

**Tech Stack:** Python 3, LangGraph, FastAPI, ChromaDB, pytest

---

## 文件结构

### 任务1 产出（只读，不修改代码）
- Create: `docs/superpowers/specs/skill-filtering-report.md` — 技能筛选报告

### 任务2 产出（worktree 中修改）
- Modify: `README.md` — 全面对齐到 v3.8 代码现状

### 任务3 产出（worktree 中修改）
- Modify: 多个源文件（根据审查结果确定）
- Create: `docs/superpowers/specs/audit-report.md` — 审查报告

---

## 代码事实基准（计划编写时采集）

| 项目 | 实际值 |
|------|--------|
| 版本号 | v3.7（config.py 无显式版本号，README 声称 v3.7，最近提交含 v3.8） |
| 测试数量 | 199 tests collected |
| Agent 数量 | 8 个类（含 BaseAgent 抽象类） |
| 协作模式 | 5 种（Sequential, Parallel, Consultation, Hierarchical, ReAct） |
| ERP 工具 | 4 个（query_product, query_inventory, query_order, query_customer） |
| API 端点 | GET /, WS /ws/chat, POST /api/chat, GET /api/health, GET /api/metrics, GET /api/kpi |
| 源文件总行数 | 5,611 行（不含测试） |
| 技能目录 | 102 个技能包（/home/dev/.claude/skills/ 下） |

---

## Task 1: 技能筛选报告

**Agent 类型:** `Explore`（只读搜索，无 worktree 隔离）
**预计耗时:** 2-3 分钟
**冲突风险:** 无（只读操作）

### Step 1: 启动 Explore Agent 搜索技能目录

Agent 提示词：

```
你需要为一个 Python 客服 AI Agent 项目（基于 LangGraph + FastAPI + ChromaDB）筛选可用技能。

项目模块结构：
- agents/ — 8个多Agent专家系统（产品/技术/账务/投诉/通用/响应/ReAct）
- api/ — FastAPI 服务层（WebSocket + REST）
- cache/ — L1/L2 双层缓存（MD5+LRU / Jaccard+jieba）
- collaboration/ — 5种协作模式编排
- core/ — 消息总线、共享黑板、监控/CircuitBreaker
- erp/ — 金蝶ERP集成
- rag/ — ChromaDB RAG知识库
- router/ — 双层查询路由（LLM + 规则）
- tools/ — Function Calling 工具注册

技能目录位置：/home/dev/.claude/skills/
包含 102 个技能包（agent-skills/ 和 superpowers/ 下有子技能）

请按以下6个维度搜索并筛选技能：

1. Agent/LLM 维度 — 匹配 agent, LLM, RAG, function-calling, context, chat 等关键词
2. 安全加固维度 — 匹配 security, hardening, injection, auth, vulnerability 等关键词
3. 代码质量维度 — 匹配 code-review, simplification, refactoring, quality 等关键词
4. 测试维度 — 匹配 test, debugging, verification, tdd 等关键词
5. 文档/部署维度 — 匹配 documentation, deployment, CI/CD, shipping 等关键词
6. 性能/架构维度 — 匹配 performance, caching, architecture, optimization 等关键词

搜索方法：
- 用 find 搜索 /home/dev/.claude/skills/ 下所有 SKILL.md 文件
- 用 grep 搜索关键词匹配
- 读取匹配到的 SKILL.md 前20行了解技能描述

产出格式（Markdown）：
# 技能筛选报告 — 客服 AI Agent 项目

## ⭐ 直接可用（与项目高度相关）
| 技能名称 | 路径 | 适用场景 | 推荐度 |
|----------|------|----------|--------|
| ... | ... | ... | ⭐⭐⭐ |

## 🟡 间接相关（部分场景可用）
| 技能名称 | 路径 | 适用场景 | 推荐度 |
|----------|------|----------|--------|
| ... | ... | ... | ⭐⭐ |

## 📋 参考价值（了解即可）
| 技能名称 | 路径 | 简述 |
|----------|------|------|
| ... | ... | ... |

将报告写入：/home/dev/projects/customer-service-ai-agent/docs/superpowers/specs/skill-filtering-report.md
```

### Step 2: 验证产出

- [ ] 检查 `docs/superpowers/specs/skill-filtering-report.md` 是否存在
- [ ] 确认报告包含三个分级（⭐/🟡/📋）
- [ ] 确认每个维度至少有 2-3 个推荐技能

### Step 3: 提交

```bash
git add docs/superpowers/specs/skill-filtering-report.md
git commit -m "docs(task1): 技能筛选报告 — 为客服AI Agent项目筛选可用技能"
```

---

## Task 2: README 全面对齐

**Agent 类型:** `general-purpose`（worktree 隔离）
**预计耗时:** 3-5 分钟
**冲突风险:** 仅修改 README.md，与任务3无文件交集

### Step 1: 启动 general-purpose Agent

Agent 提示词：

```
你需要将 README.md 全面对齐到当前代码实际状态。项目路径：/home/dev/projects/customer-service-ai-agent/

## 代码事实（已采集，直接使用）

版本号：README 声称 v3.7，但最近提交信息包含 "v3.8"。config.py 无显式版本号。
测试数量：199 tests collected（README 声称 182 passed, 4 skipped）
Agent 数量：8 个类（BaseAgent抽象 + Product/Tech/Billing/Complaint/General/Response/ReAct）
协作模式：5 种（Sequential/Parallel/Consultation/Hierarchical/ReAct）
ERP 工具：4 个（query_product/query_inventory/query_order/query_customer）
API 端点：GET /, WS /ws/chat, POST /api/chat, GET /api/health, GET /api/metrics, GET /api/kpi
源文件总行数：5,611 行

## 各模块实际行数

agents/base_agent.py: 363行, agents/product_agent.py: 68行, agents/tech_agent.py: 37行
agents/billing_agent.py: 72行, agents/complaint_agent.py: 35行, agents/general_agent.py: 63行
agents/response_agent.py: 139行, agents/react_agent.py: 74行
api/app.py: 639行, api/app_factory.py: 29行
cache/response_cache.py: 207行
collaboration/modes.py: 391行, collaboration/orchestrator.py: 108行
core/message_bus.py: 68行, core/shared_blackboard.py: 36行, core/monitoring.py: 507行
erp/__init__.py: 40行, erp/factory.py: 69行, erp/kingdee_adapter.py: 74行, erp/kingdee_real_adapter.py: 188行
rag/knowledge_base.py: 159行, rag/seed_data.py: 290行
router/query_router.py: 181行
tools/tool_registry.py: 70行, tools/erp_tools.py: 147行
config.py: 120行, logger.py: 22行
multi_agent_customer_service.py: 444行, session_manager.py: 607行

## 执行步骤

1. 先读取当前 README.md 全文
2. 逐节比对，找出所有不一致：
   - 标题/版本号是否需要更新
   - 架构图 Mermaid 节点是否与实际模块匹配
   - Agent 列表描述是否与代码一致
   - 协作模式描述是否正确
   - 缓存系统描述是否准确
   - RAG 知识库集合数量和种子数据数量是否正确
   - Function Calling 工具描述是否匹配
   - 安全特性列表是否与实际实现一致
   - API 参考端点是否完整（补充 /api/kpi 等可能遗漏的端点）
   - 配置参考是否与 config.py 一致
   - 变更日志是否需要补充 v3.8 条目
   - 模块行数描述是否与上面的实际行数一致
3. 修改 README.md，确保每个描述与代码完全一致
4. 补充 v3.8 变更日志（安全审计修复 + 测试修复）

## 约束
- 仅更新文档描述，不改变任何功能逻辑代码
- 保持现有 README 的中英文风格和 Markdown 格式
- 保持 Mermaid 图的风格一致性
- 版本号统一更新为 v3.8
- 测试数量更新为 199 tests
```

### Step 2: 验证产出

- [ ] 检查 README.md 版本号已更新为 v3.8
- [ ] 检查测试数量已更新为 199
- [ ] 检查变更日志包含 v3.8 条目
- [ ] 检查 API 端点列表完整（含 /api/kpi）
- [ ] 检查模块行数与实际一致

### Step 3: 提交

```bash
git add README.md
git commit -m "docs(task2): README 全面对齐到 v3.8 代码现状"
```

---

## Task 3: 全量审查 + 保守瘦身

**Agent 类型:** `code-reviewer` + `security-auditor` + `test-engineer`（worktree 隔离）
**预计耗时:** 8-12 分钟
**冲突风险:** worktree 隔离，与任务2无冲突

### Phase 1: 并行审查（3 个 Agent 同时启动）

#### Step 1a: 安全审查 Agent

Agent 提示词：

```
你是安全审计工程师。请对以下 Python 项目进行全面安全审查。

项目路径：/home/dev/projects/customer-service-ai-agent/
项目描述：基于 LangGraph 的多Agent客服系统，FastAPI服务层，ChromaDB RAG知识库

审查维度：

1. 注入攻击
   - 检查所有用户输入点（api/app.py 的 REST/WebSocket 端点）
   - 检查 SQL/NoSQL 注入风险
   - 检查命令注入风险（os.system, subprocess 等）
   - 检查 Prompt 注入风险（用户输入直接拼接到 LLM prompt）
   - 检查 XSS 风险（HTML 模板渲染）

2. 认证授权
   - 检查 API Key 验证逻辑（api/app.py）
   - 检查 Session Token 签名和验证（session_manager.py）
   - 检查权限边界（是否有可能越权访问）

3. 敏感数据
   - 检查密钥硬编码（所有 .py 文件）
   - 检查日志中是否泄露敏感信息（logger.py + 各模块的日志调用）
   - 检查环境变量处理（config.py, .env）

4. 输入验证
   - 检查 API 参数校验（Pydantic models）
   - 检查类型检查和边界值处理
   - 检查输入净化函数的有效性

5. 依赖安全
   - 检查 requirements.txt 中的依赖版本
   - 是否有已知漏洞的包

6. 并发安全
   - 检查 asyncio 锁使用（cache/, core/, session_manager.py）
   - 检查竞态条件
   - 检查资源泄露（未关闭的连接等）

读取每个相关文件，输出结构化的安全审查报告。

产出格式：
# 安全审查报告

## 🔴 Critical 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟠 High 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟡 Medium 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟢 Low 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## ✅ 安全通过项
- [列出检查通过的安全项]

将报告写入：/home/dev/projects/customer-service-ai-agent/docs/superpowers/specs/security-audit-report.md
```

#### Step 1b: 代码审查 Agent

Agent 提示词：

```
你是高级代码审查工程师。请对以下 Python 项目进行全面代码审查。

项目路径：/home/dev/projects/customer-service-ai-agent/

审查维度：

1. 正确性
   - 逻辑错误（条件判断、循环边界）
   - 异常处理（是否吞没异常、是否有未处理的异常路径）
   - 类型一致性（参数类型、返回值类型）

2. 可读性
   - 命名规范（函数名、变量名是否清晰）
   - 注释密度（是否过多或过少）
   - 函数长度（超过50行的函数标记）
   - 文件长度（超过300行的文件标记）

3. 架构
   - 模块耦合（是否有循环依赖）
   - 职责划分（单责原则）
   - 接口设计（是否一致、是否过度设计）

4. 性能
   - 不必要的计算（重复计算、N+1查询）
   - 缓存使用是否合理
   - 内存泄漏风险（全局变量、闭包）

5. 错误处理
   - 异常吞没（bare except、except Exception: pass）
   - 错误恢复策略
   - 降级策略

需要读取的关键文件：
- multi_agent_customer_service.py (444行)
- session_manager.py (607行)
- api/app.py (639行)
- agents/base_agent.py (363行)
- core/monitoring.py (507行)
- collaboration/modes.py (391行)
- cache/response_cache.py (207行)
- rag/knowledge_base.py (159行)
- rag/seed_data.py (290行)
- router/query_router.py (181行)
- config.py (120行)
- 所有 agents/ 下的 Agent 实现文件
- 所有 erp/ 下的文件
- tools/ 下的文件

产出格式：
# 代码审查报告

## 🔴 Critical 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟠 High 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟡 Medium 发现
| # | 文件 | 行号 | 问题描述 | 建议修复 |
|---|------|------|----------|----------|

## 🟢 Low / 保守瘦身候选
| # | 文件 | 行号 | 问题描述 | 操作建议 |
|---|------|------|----------|----------|
（仅列出：未使用import、空函数、重复代码、过度注释、未使用变量）

## 📊 代码统计
| 模块 | 文件数 | 总行数 | 最大文件 | 评分 |
|------|--------|--------|----------|------|

将报告写入：/home/dev/projects/customer-service-ai-agent/docs/superpowers/specs/code-review-report.md
```

#### Step 1c: 功能验证 Agent

Agent 提示词：

```
你是 QA 工程师。请对以下项目进行功能验证——逐模块对照 README 声明，检查代码是否真正实现了这些功能。

项目路径：/home/dev/projects/customer-service-ai-agent/

验证清单（逐项检查，每项给出 ✅ 已实现 / ⚠️ 部分实现 / ❌ 未实现 / 📝 README描述不准确）：

1. 四层状态机架构
   - 检查 multi_agent_customer_service.py 中的 StateGraph 定义
   - 是否真的有 4 层（缓存检查→意图路由→专家Agent→响应处理）
   - 各层节点是否与 README 描述一致

2. 8 个 Agent
   - 检查 agents/ 目录，列出所有 Agent 类
   - 每个 Agent 的 invoke() 方法是否真正有实现（不是空壳）
   - BaseAgent 的抽象接口是否被正确继承

3. 5 种协作模式
   - 检查 collaboration/modes.py
   - Sequential/Parallel/Consultation/Hierarchical/ReAct 是否都有完整实现
   - 各模式的核心逻辑是否与 README 描述一致

4. L1/L2 双层缓存
   - 检查 cache/response_cache.py
   - L1 是否使用 MD5 + LRU
   - L2 是否使用 Jaccard + jieba
   - 容量配置是否与 README 一致（L1: 500, L2: 2000）

5. 会话管理
   - 检查 session_manager.py
   - 滑动窗口是否实现
   - 历史摘要是否实现
   - 话题漂移检测/修复是否实现

6. RAG 知识库
   - 检查 rag/knowledge_base.py 和 rag/seed_data.py
   - 是否有 3 个集合（product_knowledge, faq, tech_support）
   - 种子数据数量是否与 README 一致（25/18/15）

7. Function Calling
   - 检查 tools/erp_tools.py 和 tools/tool_registry.py
   - 是否有 4 个工具（query_product, query_inventory, query_order, query_customer）
   - 工具定义格式是否符合 OpenAI Function Calling 规范

8. ERP 集成
   - 检查 erp/ 目录
   - 抽象接口 + 工厂模式是否实现
   - Mock adapter 和 Real adapter 是否都存在

9. 安全特性
   - API Key 认证
   - 速率限制
   - 输入验证/净化
   - XSS/注入防护
   - CSP 安全头
   - Session Token 签名
   逐项检查是否在 api/app.py 中实现

10. API 端点
    - 检查 api/app.py 的路由定义
    - 列出所有端点及其实现状态
    - 与 README API 参考章节对比

11. 监控/CircuitBreaker
    - 检查 core/monitoring.py
    - Metrics 收集是否实现
    - CircuitBreaker 状态机是否实现
    - SLA 告警是否实现

产出格式：
# 功能验证报告

## 验证总览
| 模块 | 状态 | 详情 |
|------|------|------|
| ... | ✅/⚠️/❌/📝 | ... |

## 详细发现
（每项验证的详细检查结果）

## README 与代码不一致项
| # | README 声称 | 代码实际 | 建议 |
|---|------------|---------|------|

将报告写入：/home/dev/projects/customer-service-ai-agent/docs/superpowers/specs/functional-verification-report.md
```

### Phase 2: 合并审查结果

#### Step 2: 合并与去重

- [x] 读取三份审查报告：
  - `docs/superpowers/specs/security-audit-report.md`
  - `docs/superpowers/specs/code-review-report.md`
  - `docs/superpowers/specs/functional-verification-report.md`
- [x] 合并所有发现，按文件和行号去重
- [x] 统一严重度分级（取最高严重度）
- [x] 生成统一修复清单，按优先级排序

### Phase 3: 修复 + 保守瘦身

#### Step 3: 执行 Critical + High 修复

- [x] 逐一修复所有 🔴 Critical 发现（4 项并发安全修复）
- [x] 逐一修复所有 🟠 High 发现（静默异常/ReActMode 导出/环境变量安全/文件句柄）
- [x] 每个修复后运行相关测试确认无回归

#### Step 4: 执行 Medium 修复

- [x] 逐一修复所有 🟡 Medium 发现（MessageBus锁/RAG get_running_loop/alerts限制/hasattr清理/安全头）
- [x] 每个修复后运行相关测试确认无回归

#### Step 5: 保守瘦身

仅执行以下操作（严格限定范围）：
- [ ] 删除所有未使用的 `import` 语句
- [ ] 删除空函数/占位符（仅含 `pass` 且无文档字符串）
- [ ] 删除完全重复的代码块
- [ ] 删除对显而易见代码的过度注释
- [ ] 删除未使用的变量和常量

**禁止操作：**
- ❌ 不合并文件
- ❌ 不重构类层次
- ❌ 不改变公共接口（函数签名）
- ❌ 不改变包结构

#### Step 6: 全量测试验证

```bash
python3 -m pytest test_e2e.py test_rag_tools_react.py test_security_hardening.py test_v32_optimizations.py test_v34_optimizations.py test_stress.py test_v31_improvements.py -v
```

- [x] 确认所有测试通过（194 passed, 4 skipped, 1 pre-existing failure）
- [x] 确认无新增失败

#### Step 7: 提交审查报告和修复

```bash
git add docs/superpowers/specs/security-audit-report.md docs/superpowers/specs/code-review-report.md docs/superpowers/specs/functional-verification-report.md
git commit -m "docs(task3): 三份审查报告 — 安全/代码/功能验证"

git add -A  # 添加所有代码修复
git commit -m "fix(task3): 全量审查修复 + 保守瘦身"
```

- [x] Step 7 已完成

---

## 三任务协调

### 并行执行策略

使用 `Workflow` 工具一次性启动三个任务：

```
Task 1 (Explore Agent)     ─── 只读，无 worktree
Task 2 (general-purpose)   ─── worktree 隔离
Task 3 (3 Agent 流水线)    ─── worktree 隔离
```

### 最终验证

三个任务全部完成后：
1. 合并所有 worktree 变更到主分支
2. 运行全量测试确认无冲突
3. 检查所有产出文件完整

---

## 自审检查

| 检查项 | 结果 |
|--------|------|
| Spec 覆盖 | ✅ 三个任务全部覆盖，每个任务有明确步骤 |
| 占位符扫描 | ✅ 无 TBD/TODO，所有步骤含具体操作 |
| 类型一致性 | ✅ Agent 类型、文件路径、产出格式在各处一致 |
| 可执行性 | ✅ 每个 Agent 提示词完整，可直接复制执行 |
