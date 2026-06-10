# ADR-001: 选择 LangGraph 作为多 Agent 编排框架

**日期**：2026-06-02
**状态**：已采纳
**决策者**：项目负责人

## 背景

需要一个多 Agent 协作框架来编排 8 个 AI Agent 的执行流程。候选方案有 LangChain Agent、AutoGen、LangGraph、自研状态机。

## 决策

使用 LangGraph 的 `StateGraph` 作为多 Agent 编排框架。

## 理由

- **可控性**：LangGraph 的声明式节点 + 条件边，比 LangChain Agent 的黑盒执行更透明。可以精确控制"什么时候用哪种协作模式"
- **状态管理**：内置 `TypedDict` 状态定义和序列化，天然支持 checkpoint 和时间回溯
- **调试性**：状态流转可视化，出问题时能清晰看到"卡在哪个节点"
- **社区认知**：LangGraph 是 LangChain 生态的最新框架，面试官认知度高
- **AutoGen 排除**：偏对话式多 Agent，不适合确定性的状态机流程
- **自研排除**：状态机 + 条件边 + 事件发布的工作量大，且缺乏社区支持

## 影响

**正面**：
- 四层状态机实现清晰，代码可读性高
- 条件边实现协作模式选择，逻辑集中在 `graph_builder.py`
- 支持运行时模式升级（低质量 → 自动提升协作模式）

**负面**：
- 依赖 LangGraph 版本升级（当前 `>=0.2.0`）
- StateGraph 的类型约束较松（`TypedDict(total=False)`），需要自行保证状态完整性

## 来源文档

[architecture-design.md](../active/architecture-design.md) 第 2.1 节
