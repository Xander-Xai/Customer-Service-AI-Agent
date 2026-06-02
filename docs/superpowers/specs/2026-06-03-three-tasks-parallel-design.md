# 三任务并行执行设计 — 技能筛选 + README对齐 + 全量审查

**日期：** 2026-06-03
**项目：** Multi-Agent Customer Service System (v3.8)
**执行方式：** Agent 并行 + Skill 驱动（方案 A）

---

## 概述

三个独立任务并行执行，各自使用专业 Agent 类型：

| 任务 | Agent 类型 | 隔离方式 | 产出 |
|------|-----------|---------|------|
| 任务1：技能筛选 | Explore（只读搜索） | 无（只读） | 技能筛选报告 |
| 任务2：README 对齐 | general-purpose | worktree | 更新后的 README.md |
| 任务3：全量审查+瘦身 | code-reviewer + security-auditor + test-engineer | worktree | 审查报告 + 修复后代码 |

---

## 任务1：技能筛选

### 目标
从 875 个技能文件中筛选出与本项目（客服 AI Agent）相关的技能。

### 筛选维度（6 个）

| 维度 | 匹配关键词 | 对应本项目模块 |
|------|-----------|--------------|
| Agent/LLM | agent, LLM, RAG, function-calling, context | agents/, rag/, tools/ |
| 安全加固 | security, hardening, injection, auth | 全局安全 |
| 代码质量 | code-review, simplification, refactoring | 全局代码 |
| 测试 | test, debugging, verification | test_*.py |
| 文档/部署 | documentation, deployment, CI/CD | README, docs/ |
| 性能/架构 | performance, caching, architecture | cache/, core/ |

### 产出格式
按三级分类输出：
- ⭐ 直接可用 — 与本项目高度相关，优先推荐使用
- 🟡 间接相关 — 部分场景可用
- 📋 参考价值 — 了解即可

---

## 任务2：README 全面对齐

### 目标
将 README 从 v3.7 状态全面对齐到当前 v3.8 代码实际状态。

### 执行流程

**Phase 1：代码事实采集**
- 统计每个模块的实际行数、类数、函数数
- 提取所有 API 端点（api/app.py）
- 提取所有配置项（config.py）
- 统计实际测试数量（pytest --collect-only）
- 检查当前版本号（代码中的实际版本）

**Phase 2：README 逐节比对**

| 检查项 | README 当前值 | 需核实 |
|--------|-------------|--------|
| 版本号 | v3.7 | 代码中是否已是 v3.8 |
| 测试数量 | 182 passed, 4 skipped | 实际 pytest 收集结果 |
| Agent 数量 | 8 个 | agents/ 目录实际数量 |
| 安全特性 | 6 项 | 实际实现的安全功能 |
| RAG 集合 | 3 个（25/18/15 条） | seed_data.py 实际数据 |
| 变更日志 | 到 v3.7 | 是否需补 v3.8 |
| 模块行数 | README 声称的行数 | 实际 wc -l 结果 |

**Phase 3：差异修复**
- 输出不一致清单
- 逐一修复 README，确保每个描述与代码一致
- 补充缺失的 v3.8 变更日志

### 约束
- 仅更新文档描述，不改变任何功能逻辑
- 保持现有 README 结构和中英文风格

---

## 任务3：全量审查 + 保守瘦身

### 执行架构

```
Phase 1: 并行审查（3 个 Agent 同时，只读）
  ├─ 🔒 security-auditor → 漏洞审查 + 安全审查
  ├─ 🔍 code-reviewer   → 代码审查 + 质量审查
  └─ 🧪 test-engineer   → 功能验证（基于 README）
  │
Phase 2: 合并审查结果 → 去重 → 统一修复清单
  │
Phase 3: 执行修复 + 保守瘦身
```

### Phase 1a：安全审查（security-auditor）

| 维度 | 检查内容 | 涉及模块 |
|------|---------|---------|
| 注入攻击 | SQL注入、命令注入、Prompt注入、XSS | 全局 |
| 认证授权 | API Key验证、Session Token签名、权限边界 | api/, session_manager.py |
| 敏感数据 | 密钥硬编码、日志泄露、环境变量处理 | config.py, .env |
| 输入验证 | 参数校验、类型检查、边界值 | api/app.py, router/ |
| 依赖安全 | 已知漏洞的依赖包 | requirements.txt |
| 并发安全 | 竞态条件、死锁、资源泄露 | cache/, core/, session_manager.py |

### Phase 1b：代码审查（code-reviewer）

| 维度 | 检查内容 |
|------|---------|
| 正确性 | 逻辑错误、边界条件、异常处理 |
| 可读性 | 命名、注释密度、函数长度、文件长度 |
| 架构 | 模块耦合、职责划分、接口设计 |
| 性能 | 不必要的计算、缓存失效、内存泄漏 |
| 错误处理 | 异常吞没、错误恢复、降级策略 |

### Phase 1c：功能验证（test-engineer）

逐模块对照 README 验证：

| README 声称的功能 | 验证方法 |
|------------------|---------|
| 四层状态机架构 | 检查 multi_agent_customer_service.py 图定义 |
| 8 个 Agent | 检查 agents/ 目录，每个 Agent 的 invoke 实现 |
| 5 种协作模式 | 检查 collaboration/modes.py 实际实现 |
| L1/L2 双层缓存 | 检查 cache/response_cache.py 实现 |
| 会话管理 | 检查 session_manager.py |
| RAG 知识库（3 集合） | 检查 rag/ 实现 |
| Function Calling（4 工具） | 检查 tools/ 实现 |
| ERP 集成（金蝶） | 检查 erp/ 实现 |
| 安全特性 | 检查实际实现 vs README 列表 |
| API 端点 | 检查 api/app.py 路由定义 |
| 监控/CircuitBreaker | 检查 core/monitoring.py |

### Phase 2：合并与去重

严重度分级：
- 🔴 Critical — 必须立即修复（漏洞、数据丢失风险）
- 🟠 High — 强烈建议修复（逻辑错误、安全弱点）
- 🟡 Medium — 建议修复（代码质量、可维护性）
- 🟢 Low — 可选优化（命名、注释、风格）
- 📋 Info — README 与代码不一致（交给任务2）

### Phase 3：修复 + 保守瘦身

**修复原则：** 🔴 Critical + 🟠 High 必须修复，🟡 Medium 尽量修复

**保守瘦身范围（严格限定）：**

| 允许操作 | 禁止操作 |
|---------|---------|
| ✅ 删除未使用的 import | ❌ 不合并文件 |
| ✅ 删除空函数/占位符 | ❌ 不重构类层次 |
| ✅ 删除完全重复的代码 | ❌ 不改变公共接口 |
| ✅ 删除过度注释 | ❌ 不改变包结构 |
| ✅ 删除未使用的变量/常量 | |

---

## 三任务协调规则

1. **文件冲突避免：** 任务2和任务3各自在独立 worktree 中工作，不产生文件冲突
2. **数据流向：** 任务1的技能筛选报告供后续使用；任务3的 📋 Info 级发现与任务2的 README 更新互补
3. **测试验证：** 任务2和任务3完成后，统一运行全量测试确保无回归
4. **Git 提交：** 每个任务独立提交，commit message 标注任务编号
