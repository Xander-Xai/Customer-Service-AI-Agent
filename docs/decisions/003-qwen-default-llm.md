# ADR-003: Qwen2.5-7B 作为默认 LLM

**日期**：2026-06-02
**状态**：已采纳
**决策者**：项目负责人

## 背景

需要选择一个 LLM 作为系统的默认模型。候选有 Qwen2.5-7B、DeepSeek-V3、GPT-4o-mini。考虑因素：中文能力、Function Calling 支持、成本、延迟、国内合规。

## 决策

默认使用 Qwen2.5-7B-Instruct（通过 SiliconFlow API），通过 `LLM_PROVIDER` 环境变量支持切换。

## 理由

- **成本**：¥0.35/百万 token（输入），日均 1 万次对话的月成本约 ¥500-800。GPT-4o-mini 约 ¥15,000/月
- **中文能力**：Qwen2.5 中文理解优于 GPT-4o-mini，客服场景足够
- **Function Calling**：稳定支持 OpenAI FC 格式，ReAct 工具调用可靠
- **合规**：国内部署，无跨境数据传输问题
- **延迟**：SiliconFlow 国内节点，首 token 延迟 ~0.8s

## 影响

**正面**：
- 成本可控，适合中小企业的客服场景
- 多 Agent 协作模式补偿小模型不足（多个 7B Agent 协作 ≈ 单个大模型）
- 熔断器 + 规则引擎降级，LLM 故障时零延迟响应

**负面**：
- 小模型对 Prompt 注入防御指令遵从不足（已在输出层正则兜底）
- 复杂推理场景需要 ReAct 多步调用补偿
- 切换到其他模型需要验证 FC 兼容性

## 来源文档

[model-comparison.md](../reference/model-comparison.md)
