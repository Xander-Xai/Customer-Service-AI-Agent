# 事实驱动的工程执行闭环

> 日期：2026-09-29  
> 适用范围：本仓库的 Bug 修复、功能开发、RAG / Agent 优化、性能优化、部署与技术学习。

## 1. 基本工作方式

本项目采用：

```text
事实 / 现象
→ 定义问题
→ 拆解问题
→ 提出可证伪假设
→ 建立 Baseline
→ 最小实现或实验
→ 测试 / Benchmark / Trace 验证
→ 结果验收
→ 部署 / 监控
→ 复盘 / 沉淀
```

“学中干”意味着新知识尽快进入可运行实验；“干中学”意味着真实工程问题决定学习优先级。

## 2. 永久约束

```text
Opinion != Fact
Code Complete != Problem Solved
Test Passed != Production Outcome
Benchmark Changed != Improvement unless measurement is comparable
Learning Consumed != Learning Completed
```

因此：

- 直觉只能作为假设；
- merge、测试通过、覆盖率达标都不能单独证明用户问题已解决；
- Benchmark 必须说明环境、样本、配置和统计口径；
- 无法测量的结论应保留为 `UNKNOWN / NOT_MEASURED`；
- 学习至少产生代码、实验、Benchmark、Demo、故障复现、文档或决策之一。

## 3. 标准工程问题卡

中等以上复杂度任务应尽量回答：

```yaml
problem:
objective:
observed:
expected:
known_facts: []
unknowns: []
baseline:
hypothesis:
falsification_signal:
minimal_reproduction_or_test:
change_scope:
success_metrics: []
regression_risks: []
verification:
result:
remaining_unknowns: []
rollback_or_fallback:
reusable_asset:
```

不要求单独创建 YAML 文件；这些字段可以写在 Issue、PR、设计文档、Benchmark 报告或故障复盘中。

## 4. Bug 修复闭环

```text
症状
→ 最小复现
→ 根因假设
→ 证明 / 推翻
→ 最小修复
→ 回归测试
→ 原复现路径验证
→ 相关路径检查
```

最低完成条件：

- 说明实际观察与预期行为；
- 能复现，或明确说明为什么当前不可稳定复现；
- 修复针对根因而不只是隐藏症状；
- 至少增加或更新一个能覆盖该失败模式的测试；
- 修复后重新执行原复现步骤。

## 5. RAG 优化闭环

禁止只写“RAG 效果提升”。至少拆解：

```text
数据 / Chunk
→ Query / Rewrite
→ Recall
→ Fusion
→ Rerank
→ Context
→ Generation
→ Evidence use
```

固定评测集后，按任务选择并保持同口径指标，例如：

- Recall@K；
- MRR / nDCG；
- rerank 命中；
- answer correctness / faithfulness；
- latency；
- token cost；
- failure buckets。

如果只改善质量却显著增加时延或 Token，应显式记录权衡，而不是只报告改善项。

## 6. Agent / Tool Calling 优化闭环

Agent 问题至少区分：

- routing；
- tool selection；
- tool arguments；
- tool execution；
- Tool Result context；
- state / memory；
- loop / retry；
- final response；
- timeout / fallback。

例如 Tool Result Token 优化必须比较：

```text
原始 token / estimated token
→ 优化后 token
→ 信息保留情况
→ 任务成功率 / 相关质量指标
→ latency
→ fallback / rollback
```

外部 provider 延迟未实测时必须保持 `NOT_MEASURED`，不能用本地处理时间替代。

## 7. 性能优化闭环

先做 Profile，再优化最大瓶颈。至少记录：

```text
环境
+ 数据规模
+ 并发
+ warm / cold 状态
+ 样本数
+ P50 / P95 / P99（如适用）
+ 错误率
```

优化前后必须尽量保持同样测量条件。无法同口径比较时，只能报告观察值，不能宣称提升比例。

## 8. 安全与可靠性修改

安全相关改动除正常路径外，还应验证：

- invalid input；
- auth failure；
- timeout；
- dependency unavailable；
- retry / circuit breaker；
- degraded mode；
- secret / privacy boundary；
- rollback。

“系统还能启动”不等于安全修复完成。

## 9. Definition of Done

工程任务完成时优先回答：

1. 最初的问题是什么？
2. Baseline 是什么？
3. 哪个假设被验证或推翻？
4. 改了什么，为什么是最小必要范围？
5. 哪些自动化测试通过？
6. 哪些真实/集成路径被验证？
7. 指标发生了什么变化？
8. 有什么副作用和剩余未知？
9. 如何回滚或降级？
10. 哪个资产让下一次处理同类问题更便宜？

## 10. 学习输出

研究 LangGraph、RAG、模型、向量库、推理框架或新优化方法时，至少产生一项：

- 可运行最小 Demo；
- 本项目中的受控实验；
- Benchmark；
- 架构对比和采用/不采用决定；
- 测试 fixture；
- Debug / Incident 记录；
- SOP；
- 面试或教学用的可复现解释。

没有任何输出、没有验证、也没有改变下一步决策的“阅读完成”，不计为工程学习完成。

## 11. PR 建议结构

```text
Problem / Fact
Baseline
Hypothesis
Change
Verification
Result
Risk / Rollback
Remaining UNKNOWN
```

对于纯文案、拼写或机械重构，可以降低记录强度；对于 RAG、Agent、性能、安全、部署和生产故障，应尽量完整。

## 12. 一句话原则

> 从事实出发，把模糊问题拆成可以测的部分，用最小实验验证假设，以结果而不是提交动作验收，并把有效经验变成可复用工程资产。
