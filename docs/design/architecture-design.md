# 多智能体客服系统 — 架构设计文档

> 本文档面向技术面试场景，系统阐述项目的核心设计决策、技术选型理由与权衡取舍。

> **Current HEAD addendum (2026-09-29)**: runtime version remains 6.3. Historical
> percentages/P99 and production outcomes are not current facts without a
> provenance-bearing artifact.

## Current Tool Result and RAG flows

```text
tool call → cache policy/key → scoped exact cache hit OR real execution
→ raw structured result → specialized compression → token budget
→ compact result OR external reference → ToolMessage
→ history compaction → next LLM round
```

```text
query → rewrite/filter → vector + BM25 → retrieval contract
→ fusion → rerank → context
```

Tool Result Cache ≠ Response Cache; Tool Result Store ≠ Session Memory; compression
≠ pagination. Local estimated token reduction ≠ provider billing/token saving.
BM25 has an explicit lifecycle; Qdrant point IDs are deterministic with a
migration path, whose target-environment safety still requires a dry run.

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

9 个 Agent：7 个领域专家（Product/Tech/Billing/Complaint/General/Sales/Aftersales）+ ResponseAgent 响应后处理 + ReAct 推理 Agent；另设 ResponseEvaluator 质量评估器负责输出的 5 维评分。其中 SalesAgent 负责售前推荐（产品对比、肤质匹配），AftersalesAgent 负责售后处理（退换货、物流跟踪）。共享相同的 LLM 交互流程，差异仅在 `system_prompt` 和 `extra_context` 的构建方式。模板方法消除了 ~200 行重复代码。

---

## 3. 关键技术实现

### 3.1 三级语义缓存

**为什么需要三级缓存？**
- L1（Redis MD5 精确匹配）：Redis SETEX + MD5 标准化，O(1) 查找，适合完全相同的问题。命中率约 30%
- L2（Qdrant 向量语义检索）：BGE 嵌入模型（bge-large-zh-v1.5）+ Qdrant 向量搜索，支持 payload 过滤。"精华液多少钱" ≈ "这款精华价格是多少" 命中率约 40%
- L3（Jaccard 回退层）：jieba 分词 + 倒排索引 + 动态阈值，FIFO 淘汰 5%（最大 500 条），向后兼容 v5.x 语义缓存格式
- 三级组合命中率约 70%，将 LLM 调用量降低到 30% 以下

**Embedding API 优化（v6.3）**：将本地 sentence-transformers 替换为异步 HTTP API 调用（SiliconFlow bge-large-zh-v1.5），使用 httpx.AsyncClient 连接池复用，超时从 30s 降至 10s。

**BM25 混合检索（v6.3）**：新增 `BM25Retriever` 内存倒排索引，与 Qdrant 向量检索并行，通过 RRF 融合（k=60）合并结果。BM25 通道专门处理精确关键词匹配，弥补向量检索在专有名词（如"烟酰胺"）上的不足。

**防缓存雪崩**：淘汰时只清除 5% 的低热度条目，避免一次性清除大量缓存导致大量查询同时穿透到 LLM。

**跨用户隔离 / CachePolicy（P0-02）**：所有响应在写入缓存前先经统一 `CachePolicy`（`cache/cache_policy.py`）决定 `cacheable / scope / ttl / sensitivity / version`。个性化回答（订单状态、退款、投诉、售后、消费信息）默认 **不可进入共享缓存**：存在可信 `user_id`（P0-04 在请求边界写入 `state["user_id"]`）时写入用户作用域（`scope_key = u:hash(user_id)`），仅本人可命中；无身份时 fail closed（不写、不读共享槽）。公开回答（FAQ、成分功效、政策、产品信息）写入 `shared` 作用域，所有用户共享。三层（L1 Redis key / L2 Qdrant payload+filter / L3 Jaccard 元组）使用同一 `scope_key` 与 `version`，任一层不得绕过。读取发生在路由分类之前（intent 未知），因此读取端同时探测 `shared` 与调用方身份作用域——因个性化数据永不落入 `shared`，OR 探测不会泄漏。`CACHE_CONTENT_VERSION` 提升即可整体失效旧条目（版本不匹配）。

**Reranker 二次重排（v6.3）**：检索结果进入 Agent 前经重排序器（Reranker）优化。`ApiReranker` 调用 SiliconFlow / OpenAI 兼容的 CrossEncoder API（`BAAI/bge-reranker-v2-m3`），按相关性分数降序排列，提升 Top-K 精度。API 不可用时自动降级到 `BM25Reranker`（关键词重叠 BM25 公式 + jieba 分词，零外部依赖），确保检索质量不依赖外部服务。

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
- 最大迭代次数可配置（`REACT_MAX_ITERATIONS=3`，v4.3 从 5 降至 3 控制延迟），防止死循环
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

### 3.5 多模态处理

统一多模态入口 `/api/chat/multimodal`（v6.1）自动识别并路由 5 种媒体类型：

| 处理器 | 文件类型 | 处理流程 |
|--------|---------|---------|
| **ImageProcessor** | png/jpg/webp | 压缩至 ≤4MB → Base64 → 多模态 LLM 理解 |
| **AudioProcessor** | wav/mp3/ogg | 语音转写（Whisper）→ 文本输入路由 |
| **VideoProcessor** | mp4/webm | 首帧提取 → ImageProcessor 处理 |
| **DocumentProcessor** | pdf/docx/txt | 文本提取（PyMuPDF/python-docx）→ 分段注入上下文 |
| **TTSProcessor** | — | Edge TTS 文本转语音（zh-CN-XiaoxiaoNeural 等）|

图片保留 PNG Alpha 通道（v6.1 修复），语音支持 Widget 麦克风输入。

---

## 4. 安全设计

### 4.1 认证与授权
- **双层认证**：API Key（系统间调用）+ JWT（终端用户），Admin Token（监控端点）
- **Argon2id 密码哈希**（v5.4 升级）：OWASP 2023 推荐，64MB 内存硬度，向后兼容 PBKDF2
- **时序攻击防护**：`hmac.compare_digest` 替代 `==` 比较
- **会话令牌签名**：HMAC 签名防会话劫持
- **会话数据加密**：AES-256-Fernet 可选加密（v5.3 新增）

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
- **管理后台**：`admin.html` + 8 个 admin 模块 — 用户管理/知识库统计/告警配置/Prompt 管理/Token 用量/系统健康/监控仪表盘
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

## 6. 生产部署架构

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
         │  Redis  │     │ Qdrant   │     │ Prometheus│ ← 指标采集
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
- **缓存指标**：L1/L2/L3 命中率、缓存大小、Qdrant/Redis 操作延迟
- **SLA 指标**：响应时间达标率、首次解决率、AI 接管率
- **熔断器指标**：当前状态、失败计数、恢复时间

---

## 7. 技术选型与权衡

| 选型 | 选择 | 备选 | 权衡 |
|------|------|------|------|
| LLM 框架 | LangGraph | LangChain Agent / AutoGen | LangGraph 状态机更清晰，可控性更强 |
| Web 框架 | FastAPI | Flask / Django | 原生 async + WebSocket + 自动文档 |
| 向量库 | Qdrant（v6.0 从 ChromaDB 迁移，v6.3 起完全替代 ChromaDB） | FAISS / Pinecone | Rust 原生，Docker 部署，生产就绪，高并发 |
| 缓存 | 自研三层（L1 Redis + L2 Qdrant + L3 Jaccard） | Redis 单层 | 三级缓存（精确+向量+分词），Redis 无法实现语义缓存 |
| 中文分词 | jieba | HanLP / LAC | 轻量、成熟、社区大 |
| 部署 | Docker Compose | K8s | 项目规模适中，K8s 过重 |
| 监控 | Prometheus + Grafana | DataDog / ELK | 开源免费、行业标准 |

### 已知限制与改进方向
1. **ERP Mock 模式**：生产 ERP 集成仅完成接口抽象，真实适配器未完整实现（`ERP_MODE=real` 开关已预留但未充分验证），当前默认运行在 Mock 模式
2. **Widget DOMPurify CDN 依赖**：widget 的 Markdown XSS 防护依赖 CDN 加载 DOMPurify，若 CDN 不可用则降级为纯文本渲染
3. **RAG Embedding 优化**：已从本地 sentence-transformers 替换为异步 API 嵌入（api_embedding.py bge-large-zh-v1.5），添加 BM25 混合检索（v6.3），多项优化并行以提升 Hit Rate
4. **前端内联样式**：CSP 的 style-src 仍使用 `unsafe-inline`（部分主题切换和动态样式无法避免），未来可考虑迁移到 CSS 自定义属性方案
5. **小模型注入防御**：Qwen3-8B 对"不泄露系统提示"的指令遵从不足 → 已在输出层增加正则检测兜底（v4.2 修复）

### 真实 LLM 测试发现的问题（v4.2）
E2E 集成测试（硅基流动 Qwen3-8B）暴露了两个 Mock 测试无法覆盖的 Bug：
1. **路由优先级缺陷**：多意图同分时 `max()` 按字典顺序取第一个，导致"退货退款"被路由到产品 Agent → 新增 `_INTENT_PRIORITY` 权重
2. **注入泄露**：小模型会在回复中讨论自己的系统设置，`[untrusted data]` 隔离不够 → 输出层正则检测 + 安全回复替换

---

## 8. 测试策略

| 层级 | 覆盖范围 | 数量 |
|------|---------|------|
| 单元测试 | API 路由 / 中间件 / Agent / Session / Cache / Router / RAG / LLM / 工具 / 协作模式 / 查询路由 / 告警通知 / 知识库 / 认证 / 漂移检测 等 26 个文件 | ~1,040 |
| 集成测试 | 端到端图调用 / ERP 适配器 / 多模态 / 音频管道 / 知识库生成 | ~86 |
| E2E 测试 | 全图执行 / 生产特性 / 真实 LLM（需 API Key）/ 场景路由 / Trace ID 传播 | ~200 |
| 压力测试 | 缓存吞吐 / 总线并发 / 黑板并发 | ~12 |
| 前端测试 | Agent 映射 / 状态管理 / SSE / 主题 / 对比度 / 管理后台 | ~60 Vitest（7 个文件） |
| **总计** | **26 个单元测试文件，39+ 个测试文件** | **~1,370+ 项** |

所有核心测试 **无需 LLM API Key 或网络**，Mock 适配器 + Mock Qdrant + Mock LLM 实现 100% 离线测试。

### v5.3+ 架构增强

| 版本 | 增强 | 影响 |
|------|------|------|
| **v5.3** | Token Quota 持久化（Redis Hash + 内存回退） | 用户级 Token 限额跨重启保留 |
| **v5.3** | 黑板 Session 隔离（ContextVar） | 多用户并发时 Agent 间数据不串扰 |
| **v5.3** | 会话数据加密（AES-256-Fernet） | 敏感会话数据落盘加密 |
| **v5.4** | Argon2id 密码哈希 | 抗 GPU/ASIC 攻击能力提升 100 倍+ |
| **v5.4** | 分级告警（warning/critical/emergency）+ 自动升级 | 7×24 无人值守运维 |
| **v5.4** | 8 个业务 Prometheus 指标 | 数据驱动决策（满意度/Agent/意图/升级率等） |
| **v5.5** | 账单 Agent 降级增强 + LLM 启动健康检查 | LLM 不可用时保留 ERP 上下文，启动阶段提前暴露供应商连通性问题 |
| **v5.5** | API Key 占位符校验加固 | 阻止 `test-` / `mock-` / `sk-placeholder` 等测试 Key 混入生产环境 |
| **v6.0** | Qdrant 向量数据库迁移 | 替代 ChromaDB，Docker 部署，高并发，余弦距离检索 |
| **v6.0** | 全链路 SSE 真流式（Tool-Calling + RAG 检索 + 图节点状态 + 缓存伪流式） | 实时显示 thinking/tool_call/tool_result/rag_status/agent_switch 等 10 种事件类型 |
| **v6.1** | 四大场景 Agent（Sales/Aftersales/Complaint/General）+知识库 5000+ 文档 | 场景化路由与专业回复 |
| **v6.1** | 统一多模态入口 `/api/chat/multimodal` | 自动文件类型路由（voice/image/document） |
| **v6.3** | Embedding API 异步化（api_embedding.py） | 连接池复用，超时从 30s 降至 10s |
| **v6.3** | BM25Retriever 混合检索 | 与 Qdrant 向量检索并行，RRF 融合，弥补专有名词匹配不足 |
| **v6.3** | Widget 会话连续性修复 | widget 刷新/重连后恢复已有会话，不再创建新会话 |
| **v6.3** | CSP frame-ancestors 支持 widget 嵌入 | 允许第三方页面通过 `frame-ancestors` 安全嵌入 widget |
| **v6.3** | Widget DOMPurify Markdown 净化 | widget 内 Markdown 渲染使用 DOMPurify 防 XSS |
| **v6.3** | Token refresh 竞态修复 | 并发请求 refresh token 时只触发一次刷新，避免 401 |
| **v6.3** | Redis 限流 DoS 修复 | 限流 key 设 TTL，Redis 不可用时不拒绝请求 |
| **v6.3** | file_type header 对齐 | 前端 widget 从 file_type（下划线）改为 file-type（连字符），对齐 FastAPI Header() 参数转换规则 |
