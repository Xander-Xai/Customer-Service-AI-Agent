# 多智能体客服系统 — 架构设计文档

> 本文档面向技术面试场景，系统阐述项目的核心设计决策、技术选型理由与权衡取舍。

---

## 1. 问题定义

### 业务场景
面向化妆品生产企业的智能客服系统，需要处理 5 类核心查询：
- **产品咨询**：成分分析、功效查询、价格对比、肤质匹配
- **技术支持**：使用指导、过敏处理、产品搭配、保质期
- **账单服务**：退款处理、订单查询、发票管理、物流追踪
- **投诉处理**：情绪安抚、问题解决、补偿方案、升级处理
- **通用咨询**：FAQ、产品概览、服务介绍

### 核心挑战
1. **查询复杂度差异大**："你好" vs "我用了精华液过敏了，同时查一下订单1001的物流，如果没到就退款" — 后者需要跨领域多步骤处理
2. **需要企业系统集成**：ERP（金蝶）数据查询、产品知识库检索
3. **生产级要求**：高并发、安全防护、监控告警、优雅降级

---

## 2. 架构设计

### 2.1 总体架构：四层状态机

```
Layer 0: 缓存检查 ──→ 命中则直接返回（<10ms）
    │ miss
Layer 1: 双层路由 ──→ LLM 分类 ∥ 规则分类（并行）+ 复杂度评分
    │
Layer 2: 协作模式 ──→ 根据路由结果动态选择 5 种模式之一
    │
Layer 3: 响应处理 ──→ 解决状态评估 + 缓存写入 + SLA 监控
```

**设计决策：为什么用 LangGraph 而不是自己写状态机？**
- LangGraph 的 `StateGraph` 提供声明式的节点和条件边定义，代码可读性高
- 内置状态序列化支持，方便未来做检查点（checkpoint）和时间回溯
- 社区生态好，面试官认知度高

**设计决策：为什么缓存前置到 Layer 0？**
- 化妆品客服中 60-70% 的查询是高频重复问题（"你们有什么产品？""精华液多少钱？"）
- 缓存命中时跳过整个 LLM 路由 + Agent 处理链路，延迟从 5-15s 降到 <10ms
- 成本考量：每次 LLM 调用都有 token 成本，缓存前置直接减少 60%+ 的 LLM 调用

### 2.2 双层路由（Layer 1）

```python
# 并行执行两个分类器
rule_task = asyncio.create_task(rule_classify(query))
llm_task  = asyncio.create_task(llm_classify(query))
rule_result, llm_result = await asyncio.gather(rule_task, llm_task)

# 复杂度评分决定后续协作模式
complexity = score_complexity(query, intent_count, context_length)
fast_path = complexity < threshold  # 低复杂度走快速通道
```

**设计决策：为什么两个路由器并行而不是只用 LLM？**
- **可靠性**：LLM 服务不稳定（网络超时、API 限流），规则分类器是零延迟零故障的 fallback
- **性能**：规则分类器提前完成时，如果 LLM 置信度低（< 0.7），直接用规则结果
- **成本**：简单查询（"你好"）用规则分类器就能搞定，不需要调 LLM

**复杂度评分维度**：
- 查询长度、意图数量、技术术语密度、价格相关度、标点符号（问号多 = 复杂）、对话上下文长度

### 2.3 五种协作模式（Layer 2）

这是整个系统最核心的设计。不同复杂度的查询需要不同的处理策略：

| 模式 | 适用场景 | 实现 |
|------|---------|------|
| **Sequential** | 单领域、低复杂度 | 单 Agent 处理，指数退避重试 |
| **Parallel** | 多领域、中等复杂度 | `asyncio.gather` + Semaphore 限流 + 结果聚合 |
| **Consultation** | 需要补充信息 | 主 Agent + 顾问 Agent 并行，结果合并 |
| **Hierarchical** | 投诉/升级场景 | 协调者分配子任务，汇总各 Agent 结果 |
| **ReAct** | 高复杂度多领域 | Thought → Action → Observation 推理循环 |

**设计决策：为什么需要 5 种模式而不是一个通用 Agent？**
- **成本效率**：简单查询用 Sequential（单次 LLM 调用），复杂查询才用多 Agent（多次调用）
- **响应速度**：Parallel 模式多 Agent 并发，总耗时 = max(单个 Agent 耗时)，而不是 sum
- **结果质量**：Consultation 模式的顾问补充机制，让产品专家的回答有技术支持背书
- **业务匹配**：投诉场景天然需要 Hierarchical — 先安抚、再调查、再补偿，分层处理

### 2.4 Agent 设计：模板方法模式

```python
class BaseAgent(ABC):
    async def _process_with_llm(self, state, system_prompt, extra_context, fallback):
        """模板方法：子类只需提供 prompt 和 context"""
        # 1. 获取会话上下文
        # 2. 检测漂移
        # 3. 构建消息
        # 4. 调用 LLM
        # 5. 写入会话
        # 6. 发布事件
```

8 个 Agent：6 个领域专家（Product/Tech/Billing/Complaint/General + ResponseAgent）+ ReAct 推理 Agent + ResponseEvaluator 质量评估器。共享相同的 LLM 交互流程，差异仅在 `system_prompt` 和 `extra_context` 的构建方式。模板方法消除了 ~200 行重复代码。

---

## 3. 关键技术实现

### 3.1 二级语义缓存

**为什么需要两级而不是一级？**
- L1（MD5 精确匹配）：O(1) 查找，适合完全相同的问题。命中率约 30%
- L2（Jaccard 语义匹配）：用 jieba 分词后计算集合相似度。"精华液多少钱" ≈ "这款精华价格是多少" 命中率约 40%
- 两级组合命中率约 70%，将 LLM 调用量降低到 30% 以下

**倒排索引优化**：L2 语义匹配不做全量扫描，而是通过倒排索引（token → 候选集）缩小搜索范围，将 O(n) 降到 O(k)（k << n）。

**防缓存雪崩**：淘汰时只清除 5% 的低热度条目，避免一次性清除大量缓存导致大量查询同时穿透到 LLM。

### 3.2 ReAct 推理引擎

```
用户: "帮我查订单 1001 的物流，如果没到就推荐替代产品"
                         ↓
Thought: 用户需要查询订单物流状态，需要调用 ERP 工具
Action:  query_order(order_id="1001")
Observation: 订单已发货，预计明天到达
Thought: 订单已发货，用户还要求推荐替代产品，需要查询产品库
Action:  query_product(query="替代产品推荐")
Observation: 找到 3 款相关产品...
Final Answer: 您的订单 1001 已发货，预计明天到达。同时为您推荐以下替代产品...
```

**实现要点**：
- 最大迭代次数可配置（`REACT_MAX_ITERATIONS=5`），防止死循环
- Function Calling 支持：注册 ERP 查询工具，LLM 自主决定调用哪个工具
- 优雅降级：模型不支持 tools 时自动回退到纯文本模式

### 3.3 会话漂移检测

4 种漂移类型覆盖了真实客服场景中的常见问题：

| 类型 | 检测方法 | 修复策略 |
|------|---------|---------|
| **话题漂移** | jieba 分词 + Jaccard 相似度 < 0.15 | 注入修复提示，引导 Agent 确认新需求 |
| **意图漂移** | 7 类意图关键词评分突变 | 调整响应策略，告知用户服务模式切换 |
| **矛盾检测** | 40+ 否定词对匹配 | 温和指出矛盾，请求确认真实需求 |
| **重复检测** | 相似度 > 0.8 | 参考前次回答，提供更精炼回复 |

**升级机制**：累计漂移 ≥ 5 次时，建议转人工客服，避免用户在 AI 服务中反复受挫。

### 3.4 Circuit Breaker 熔断器

```
CLOSED ──(连续5次失败)──→ OPEN ──(60秒后)──→ HALF_OPEN
  ↑                        │                    │
  │                        │              (探测成功)
  │                        │                    │
  └────────────────────────←────────────────────┘
                             │
                        (探测失败) → OPEN
```

**为什么需要熔断器？**
- LLM API 可能出现持续故障（服务端过载、网络中断）
- 没有熔断器时，每个请求都会等待 LLM 超时（30s），导致请求堆积
- 熔断器在连续 5 次失败后"跳闸"，后续请求直接降级到规则分类器（零延迟），60 秒后自动尝试恢复

**并发安全**：状态转换使用 `asyncio.Lock` 保护，防止多个协程同时从 HALF_OPEN → CLOSED。

---

## 4. 安全设计

### 4.1 认证与授权
- **双层认证**：API Key（普通端点）+ Admin Token（监控端点）
- **时序攻击防护**：`hmac.compare_digest` 替代 `==` 比较
- **会话令牌签名**：HMAC 签名防会话劫持

### 4.2 输入安全
- **输入净化**：控制字符移除 + HTML 标签剥离
- **字段约束**：Pydantic 模型验证 + 查询长度限制（2000 字符）
- **注入防护**：ERP 查询白名单 + LIKE 通配符转义 + Prompt XML 标签隔离

### 4.3 响应安全
- **错误脱敏**：异常详情仅写服务端日志，返回用户通用错误消息
- **安全头**：CSP / HSTS / X-Frame-Options / X-Content-Type-Options / Referrer-Policy

---

## 5. 前端架构

### 技术选型

| 考量 | 决策 | 理由 |
|------|------|------|
| **可嵌入性** | 原生 JS | `widget.html` 可直接嵌入任意网页，无框架运行时 |
| **构建工具** | Vite 8 | 模块化 + Tree-shaking + Hashed 产物 |
| **安全** | DOMPurify + marked.js | Markdown 渲染 + XSS 防护 |
| **体积** | 无框架运行时 | 首屏 JS 体积更小 |
| **测试** | Vitest + jsdom | 单元测试能力 |

### 功能实现

- **聊天界面**：`web/index.html` + `chat/` 模块（消息渲染/输入/会话管理/语音/TTS）
- **SSE 流式**：`api/sse.js` — 逐 token 推送 + Agent 流转轨迹 + 进度条
- **WebSocket**：`api/websocket.js` — 指数退避重连（2s~30s）+ 心跳 + 消息队列
- **文件上传**：支持图片/视频/PDF/DOCX/文本，`/api/chat/file`
- **语音输入**：Web Speech API + 🎤 按钮
- **TTS 语音**：`/api/tts` — Edge TTS（zh-CN-XiaoxiaoNeural 等）+ 声音选择器
- **会话侧面板**：点击会话项弹出侧面板（Agent/模式/时间），Esc 关闭
- **主题系统**：8 种主题（亮色 pure/warm/soft/cream + 暗色 classic/warm + 无障碍 + 面板）+ 字号/行高/动画控制
- **无障碍**：ARIA 标签 + 焦点环 + 对比度 + 跳转链接 + 键盘快捷键（WCAG AA/AAA）
- **移动端**：响应式布局 + 抽屉式导航
- **管理后台**：`admin.html` + `admin.js` — 用户管理/知识库统计/告警配置/ChromDB+DB 健康状态
- **可嵌入 Widget**：`widget.html` — 轻量聊天组件

### 模块结构

```
web/src/
├── api/          # REST/SSE/WebSocket 客户端 + 事件系统
├── auth/        # JWT 认证 + Token 自动刷新（过期前 5 分钟）
├── chat/        # 聊天模块（消息/输入/会话/语音/欢迎/搜索/快捷键）
├── state/       # 集中状态管理（chatState.js 单一数据源）
├── utils/       # 工具函数（主题/Toast/DOM/Markdown/格式化/监控图表）
├── admin-*.js   # 管理后台（分析/设置/用户）
├── main.js      # 主聊天页入口
└── login.js    # 登录页入口
```

---

## 7. 生产部署架构

```
                         ┌──────────────┐
                         │    Nginx     │ ← TLS 终止 + 反向代理
                         │  :443 → 8000 │
                         └──────┬───────┘
                                │
                    ┌───────────┴───────────┐
                    │    Gunicorn (4 Worker) │
                    │    + FastAPI App       │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                  │
         ┌────┴────┐     ┌─────┴─────┐     ┌─────┴─────┐
         │  Redis  │     │ ChromaDB  │     │ Prometheus│ ← 指标采集
         │ Session │     │  RAG 库   │     │  :9090    │
         │ + Cache │     └───────────┘     └─────┬─────┘
         └─────────┘                              │
                                           ┌─────┴─────┐
                                           │  Grafana  │ ← 可视化
                                           │  :3000    │
                                           └───────────┘
```

### 监控指标（Prometheus 格式）
- **请求指标**：总数、错误率、响应时间（P50/P95）
- **Agent 指标**：各 Agent 调用次数、协作模式分布
- **缓存指标**：L1/L2 命中率、缓存大小
- **SLA 指标**：响应时间达标率、首次解决率、AI 接管率
- **熔断器指标**：当前状态、失败计数、恢复时间

---

## 8. 技术选型与权衡

| 选型 | 选择 | 备选 | 权衡 |
|------|------|------|------|
| LLM 框架 | LangGraph | LangChain Agent / AutoGen | LangGraph 状态机更清晰，可控性更强 |
| Web 框架 | FastAPI | Flask / Django | 原生 async + WebSocket + 自动文档 |
| 向量库 | ChromaDB | FAISS / Pinecone | 轻量、嵌入式、开发友好 |
| 缓存 | 自研双层 | Redis 单层 | 语义缓存是核心差异点，Redis 无法实现 |
| 中文分词 | jieba | HanLP / LAC | 轻量、成熟、社区大 |
| 部署 | Docker Compose | K8s | 项目规模适中，K8s 过重 |
| 监控 | Prometheus + Grafana | DataDog / ELK | 开源免费、行业标准 |

### 已知限制与改进方向
1. **ERP Mock**：生产 ERP 集成仅完成接口抽象，真实适配器未完整实现 → 已预留 `ERP_MODE=real` 开关
2. **RAG Embedding 优化**：已从默认 all-MiniLM-L6-v2（英文）替换为中文 embedding 降级链（bge-small-zh-v1.5 → text2vec-base-chinese），Hit Rate@3 从 63% 提升到 80%
3. **前端 XSS**：style-src CSP 使用 `unsafe-inline`（65 处内联样式），但 script-src 已用 nonce 且 54 处 innerHTML 全量审计安全（DOMPurify + escapeHtml）
4. **小模型注入防御**：Qwen2.5-7B 对"不泄露系统提示"的指令遵从不足 → 已在输出层增加正则检测兜底（v4.2 修复）

### 真实 LLM 测试发现的问题（v4.2）
E2E 集成测试（硅基流动 Qwen2.5-7B-Instruct）暴露了两个 Mock 测试无法覆盖的 Bug：
1. **路由优先级缺陷**：多意图同分时 `max()` 按字典顺序取第一个，导致"退货退款"被路由到产品 Agent → 新增 `_INTENT_PRIORITY` 权重
2. **注入泄露**：小模型会在回复中讨论自己的系统设置，`[untrusted data]` 隔离不够 → 输出层正则检测 + 安全回复替换

---

## 7. 测试策略

| 层级 | 覆盖范围 | 数量 |
|------|---------|------|
| 单元测试 | API 路由 / 中间件 / Agent / Session / Cache / Router / RAG / LLM / 工具 等 20 个文件 | ~1,032 |
| 集成测试 | 端到端图调用 / ERP 适配器 / 多模态 | ~89 |
| E2E 测试 | 全图执行 / 生产特性 / 真实 LLM（需 API Key） | ~213 |
| 压力测试 | 缓存吞吐 / 总线并发 / 黑板并发 | ~12 |
| 前端测试 | Agent 映射 / 状态管理 / 主题 / 无障碍 / 对比度 | ~5 Vitest |
| **总计** | | **~1,338** |

所有核心测试 **无需 LLM API Key 或网络**，Mock 适配器 + 内存 ChromaDB + Mock LLM 实现 100% 离线测试。
