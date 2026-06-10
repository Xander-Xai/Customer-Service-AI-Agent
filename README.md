# 药妆智多星 — 多智能体客服系统 (Customer Service AI Agent v5.2)

面向化妆品生产/销售企业的基于 **LangGraph** 多 Agent 协作问答系统，实现四层状态机动态路由：缓存检查 → 意图路由 → 专家 Agent 协作 → 响应后处理。

> **v5.2** 无障碍 WCAG AA/AAA 全量达标 + TTS 语音选择器 + 会话详情侧面板 + 死代码清理 + Ruff lint 346→73
>
> **v5.1** 全量清理 320 临时文件 + 文档同步 + 隐私检查通过
>
> **v5.0** 前端 Vite 8 重构 + 1151 测试用例全覆盖 + Ruff 工具链 + 覆盖率门槛 80% + RAG 增强（查询改写/重排/RRF 融合/CLIP 多模态）+ 前后端 15 项匹配修复
>
> 核心能力：SiliconFlow/DeepSeek/OpenAI 兼容 LLM · 依赖注入容器 · SSE 真流式 · PostgreSQL + Alembic · Redis JWT 黑名单 · 反馈系统 · 多模态 · RAG 知识库 · Function Calling · ReAct 推理 · 查询改写 · BM25/交叉编码器重排 · RRF 融合 · CLIP 图片检索 · Token 用量追踪 · Prompt 版本管理

---

## ⚡ 3 分钟验证入口

> 面试官快速验证项目的 4 个入口：

| 验证项 | 入口 | 预期结果 |
|--------|------|---------|
| **代码能跑** | `make dev` → http://localhost:8000 | 聊天界面可用，发送"你好"得到回复 |
| **测试能过** | `make test` | 1151 passed, 覆盖率 ≥80% |
| **RAG 有数据** | `python scripts/evaluate_rag.py` | Hit Rate@3 = 80%, MRR = 0.778 |
| **CI 能过** | `.github/workflows/ci.yml` | 4 Job 流水线（测试→安全→构建→部署） |

**详细证据文档**：
- [Prompt Engineering 设计](docs/active/prompt-engineering.md) — Prompt 架构、策略选型、迭代演进
- [模型选型与 Token 成本](docs/active/model-comparison.md) — 模型对比、Embedding 效果、月度成本估算
- [RAG 评估报告](docs/active/rag-evaluation.md) — 基线 vs 改进后数据对比
- [安全设计文档](docs/active/SECURITY.md) — 安全措施清单 + 已知限制 + 改进计划
- [架构设计](docs/active/architecture-design.md) — 四层状态机 + 五种协作模式

---

## 🏗️ 架构设计

### 系统架构图

```mermaid
graph TB
    subgraph Input["接入层"]
        WS[WebSocket /ws/chat]
        REST[REST API /api/chat]
        SSE[SSE /api/chat/stream]
        IMG[多模态 /api/chat/image]
    end

    subgraph Middleware["中间件层（5 层）"]
        TRACE["1. trace_id 追踪"]
        CSRF["2. CSRF 防护 — 双重 Cookie 提交"]
        AUTH["3. API Key / JWT — 分级 RBAC 认证"]
        CSP["4. 安全头 CSP/HSTS"]
        RATE["5. 限流 60req/min/IP"]
    end

    subgraph Graph["LangGraph StateGraph"]
        C0["Layer 0: check_cache — 缓存检查"]
        C1["Layer 1: classify_query — 双层路由"]
        C2["Layer 2: 协作模式 — 5 种动态选择"]
        C3["Layer 3: final_response — 响应后处理"]
    end

    subgraph Cache["缓存层"]
        L1["L1 精确缓存 — MD5 + LRU OrderedDict"]
        L2["L2 语义缓存 — Jaccard + jieba 分词 + 倒排索引"]
        RD2["Redis 持久化 — 可选预热"]
    end

    subgraph Router["路由层"]
        LLM["LLM Router — JSON 分类"]
        RULE["Rule Classifier — 正则 + 复杂度评分"]
    end

    subgraph Agents["5 种协作模式"]
        SEQ["Sequential — 单 Agent"]
        PARA["Parallel — 并发 + 聚合"]
        CONS["Consultation — 主 + 顾问"]
        HIERS["Hierarchical — 协调者 + 子任务"]
        REACT["ReAct — RAG + FC 推理链"]
    end

    subgraph Expert["7 个专家 Agent"]
        PA["ProductAgent — 产品 + RAG + ERP"]
        TA["TechAgent — 技术支持 + RAG"]
        BA["BillingAgent — 账单 + ERP"]
        CA["ComplaintAgent — 投诉 + RAG"]
        GA["GeneralAgent — 通用 + 黑板桥接"]
        RA["ReActAgent — 多步推理 + FC"]
        RPA["ResponseAgent — 后处理 + 评估"]
    end

    subgraph Infra["基础设施"]
        MB["MessageBus — async pub/sub"]
        BB["SharedBlackboard — TTL KV"]
        MET[MetricsCollector]
        CB["CircuitBreaker — 三态熔断"]
        SLA["SLAAlertManager — 滑动窗口"]
        DI["ServiceContainer — 依赖注入"]
    end

    subgraph External["外部集成"]
        RAG["ChromaDB — RAG 知识库 4+1 collection"]
        ERP["金蝶 ERP — Mock / Real"]
        FC["Function Calling — 4 个 ERP 工具"]
        RWR["查询改写 — 同义词扩展"]
        RRK["重排器 — BM25 + CrossEncoder"]
    end

    subgraph Infra2["生产基础设施"]
        NG[Nginx 反向代理 + TLS]
        RD["Redis 7 — Session/Cache/JWT"]
        PG[PostgreSQL 15]
        PROM[Prometheus]
        GRAF[Grafana 仪表盘]
        AM[Alertmanager]
    end

    WS --> TRACE
    REST --> TRACE
    SSE --> TRACE
    IMG --> TRACE
    TRACE --> CSRF
    CSRF --> AUTH
    AUTH --> CSP
    CSP --> RATE
    RATE --> C0

    C0 --> L1
    L1 -. hit .-> C3
    C0 --> C1
    C1 --> LLM
    C1 --> RULE

    LLM --> C2
    RULE --> C2

    C2 --> SEQ & PARA & CONS & HIERS & REACT

    SEQ --> PA
    PARA --> PA & TA & BA
    CONS --> PA
    HIERS --> GA
    REACT --> RA

    PA --> RAG & ERP
    TA --> RAG
    BA --> ERP
    CA --> RAG
    RA --> FC
    FC --> ERP

    PA & TA & BA & CA & GA --> MB
    PA & BA & GA --> BB

    MB --> MET
    MET --> CB
    MET --> SLA

    SEQ & PARA & CONS & HIERS & REACT --> C3
    C3 --> RPA

    RD2 -.-> L1
    RD -.-> RD2

    style C0 fill:#e1f5fe
    style C1 fill:#fff3e0
    style C2 fill:#e8f5e9
    style C3 fill:#fce4ec
```

### 四层状态机

| 层级 | 节点 | 职责 | 关键实现 |
|------|------|------|----------|
| **Layer 0** | `check_cache` | L1 MD5 精确匹配 + L2 Jaccard 语义匹配，命中直接返回（<10ms） | [response_cache.py](cache/response_cache.py)：L1 OrderedDict + L2 倒排索引 + Redis 持久化 |
| **Layer 1** | `classify_query` | LLM Router ∥ Rule Classifier 并行（`asyncio.gather`）+ 复杂度评分（阈值 50） | [query_router.py](router/query_router.py)：7 种意图分类 + 熔断器降级 |
| **Layer 2** | `sequential/parallel/consultation/hierarchical/react` | 5 种协作模式动态选择 | [orchestrator.py](collaboration/orchestrator.py) + [modes.py](collaboration/modes.py) |
| **Layer 3** | `final_response` | 质量评估 + 模式升级重试 + 缓存写入 + SLA 监控 + 事件广播 | [response_agent.py](agents/response_agent.py) + [evaluator.py](agents/evaluator.py) |

### 请求处理流程

```mermaid
sequenceDiagram
    participant C as 客户端
    participant MW as 中间件层（5 层）
    participant G as LangGraph
    participant L0 as Layer 0 缓存
    participant L1 as Layer 1 路由
    participant L2 as Layer 2 协作
    participant RA as ResponseAgent
    participant LLM as LLM API

    C->>MW: WebSocket / REST / SSE 请求
    MW->>MW: trace_id → CSRF → 认证(RBAC) → 安全头 → 限流
    MW->>G: invoke(state)

    G->>L0: check_cache(query)
    alt 缓存命中 (<10ms)
        L0-->>G: cached=true, response
        G->>RA: final_response
    else 缓存未命中
        L0-->>G: cached=false
        G->>L1: classify_query(query, context)
        par 双层路由并行
            L1->>LLM: LLM Router 分类（timeout 4s）
            L1->>L1: Rule Classifier 正则匹配 + 复杂度评分
        end
        L1-->>G: RoutingResult(agent, complexity, fast_path)

        G->>L2: select_collaboration_mode
        alt 简单查询 (complexity < 50)
            L2->>L2: Sequential → 单 Agent 处理
        else 多领域查询
            L2->>L2: Parallel → 多 Agent 并发 + 结果聚合
        else 投诉场景
            L2->>L2: Hierarchical → 协调者分配子任务
        else 高复杂度单领域
            L2->>L2: Consultation → 主 Agent + 顾问补充
        else 高复杂度多领域
            L2->>L2: ReAct → RAG 检索 + FC 工具调用推理链
        end
        L2-->>G: response, agents_used

        G->>RA: final_response(state)
        RA->>RA: 响应消毒 → 注入防护 → 质量评估
        alt 低分 (score < 30) 且未重试
            RA->>RA: 模式升级重试（sequential→consultation→parallel→react）
        end
        RA->>RA: 缓存写入 + 会话记录 + SLA 监控
    end

    G-->>MW: result
    MW-->>C: response + session_token + trace_id
```

---

## 🤖 功能模块详解

### Agent 系统（8 个 Agent）

| Agent | 职责 | 数据源 | 协作方式 |
|-------|------|--------|----------|
| **BaseAgent** | 抽象基类：会话管理 + 漂移检测 + RAG + FC + A/B 测试 + 重试 + 流式 | Bus/BB/Session | 模板方法 |
| **ProductAgent** | 产品成分分析、功效查询、价格对比、库存查询、肤质匹配 | ERP + RAG (`product_knowledge`, `faq`) | Sequential/Parallel/Consultation |
| **TechAgent** | 使用指导、过敏处理、产品搭配、保质期、储存方法 | RAG (`tech_support`, `product_knowledge`) | Sequential/Parallel/Consultation |
| **BillingAgent** | 退款处理、订单查询（`ORD\d+`/`C\d{3}`）、发票、物流追踪 | ERP（订单 + 客户） | Sequential/Parallel/Consultation |
| **ComplaintAgent** | 情绪安抚、问题解决、补偿方案、升级处理、投诉协议 | RAG (`complaint_knowledge`, `faq`) + BB 标记 | Hierarchical |
| **GeneralAgent** | FAQ、产品概览、服务介绍、协调调度、跨 Agent 黑板桥接 | ERP（客户） + BB (`erp.*`) | Sequential/Consultation |
| **ReActAgent** | 多步推理：Thought → Action → Observation → Answer | RAG（3 个 collection） + FC（4 个 ERP 工具） | ReAct |
| **ResponseAgent** | 响应消毒 + 注入防护 + 解决状态评估 + 质量评分 + 模式升级重试 + 缓存写入 + SLA + 事件广播 | Cache + Evaluator + Bus | 最终环节 |

**BaseAgent 核心能力（681 行）：**
- 会话上下文检索（滑动窗口最近 6 条）
- 漂移检测（话题/意图/矛盾/重复 4 种类型）+ 自动注入修复提示
- RAG 知识库检索（按 Agent 分配 collection）+ 查询改写（同义词扩展）+ 多 collection RRF 融合 + BM25/CrossEncoder 重排
- Function Calling 多轮工具调用循环（`_process_with_tools`，最多 `TOOL_MAX_ROUNDS` 轮）+ 自反思质量检查（v5.2）
- A/B 测试 prompt 变体分配（SHA-256 确定性分流）+ Prompt 版本管理（DB 持久化）
- SSE 真流式输出（检测 `stream_callback` 自动切换）
- 指数退避重试（仅瞬态错误：`ConnectionError`/`TimeoutError`/`OSError`）
- 对话历史 `<untrusted-data>` 标签隔离（防 prompt 注入）
- 协议化依赖注入（`core/protocols.py`：LLMProtocol、ERPProtocol、KnowledgeBaseProtocol 等）
- 多模态 Vision LLM 自动选择（`_get_effective_llm` 根据 state.has_multimodal 切换）

### 5 种协作模式

| 模式 | 触发条件 | 行为 | SLA 超时 |
|------|----------|------|----------|
| **Sequential** | `fast_path=True` 或 `complexity < 50` | 单 Agent 顺序处理 | 15s |
| **Parallel** | 多领域意图（≥2 domain hints）+ `complexity < 60` | 多 Agent 并发（Semaphore≤5） + 结果聚合 | 20s |
| **Consultation** | 单领域 + `complexity ≥ 60` + 有顾问映射 | 主 Agent + 辅助 Agent 补充信息注入 | 25s |
| **Hierarchical** | 投诉/升级场景 | GeneralAgent 协调者分配子任务 + 汇总（动态添加 product/billing 子任务） | 30s |
| **ReAct** | 多领域（≥2）+ `complexity ≥ 60` | RAG 检索 + Function Calling 多轮推理链 | 30s |

**模式选择算法**（[orchestrator.py](collaboration/orchestrator.py)）：

```
1. fast_path=True → Sequential（快速通道）
2. query_type == "complaint" → Hierarchical（动态添加 product/billing 子任务）
3. 多领域(≥2) + complexity ≥ 60 → ReAct
4. 多领域(≥2) → Parallel
5. 单领域 + complexity ≥ 60 + 有顾问 → Consultation
6. 默认 → Sequential
```

**模式升级重试**（v4.3+）：当 ResponseAgent 质量评分低于阈值时，自动升级：Sequential → Consultation → Parallel → ReAct

**顾问映射关系：**
- `product_agent` → 顾问 `tech_agent`
- `billing_agent` → 顾问 `product_agent`
- `tech_agent` → 顾问 `product_agent`

### 缓存系统

```mermaid
flowchart LR
    Query["用户查询"] --> L1["L1 精确缓存 — MD5 哈希 O(1)"]
    L1 -->|"hit"| Response["直接响应 &lt;10ms"]
    L1 -->|"miss"| L2["L2 语义缓存 — Jaccard + jieba"]
    L2 -->|"hit"| Response
    L2 -->|"miss"| Router["→ Layer 1 路由"]

    subgraph L1Detail["L1 实现"]
        direction TB
        OD["OrderedDict LRU"]
        EVICT1["满时淘汰 5%"]
        TTL1["TTL 3600s"]
    end

    subgraph L2Detail["L2 实现"]
        direction TB
        FROZEN["frozenset 存储分词"]
        INV["倒排索引快速候选"]
        DYN["动态阈值 — 短文本 0.7 / 长文本 0.5"]
        EVICT2["deque FIFO 淘汰 5%"]
    end

    L1Detail --> L1
    L2Detail --> L2
    L1 -.-> Redis[("Redis SETEX — 预热 + 持久化")]
```

### 会话管理（[session_manager.py](core/session/session_manager.py)）

| 功能 | 实现 | 配置 |
|------|------|------|
| 滑动窗口裁剪 | 消息数（`SESSION_WINDOW_SIZE`）+ token 数（tiktoken `cl100k_base`）双重控制 | 默认 10 条 / 4000 tokens |
| 历史摘要 | LLM 异步生成 2-3 句摘要注入上下文 | `SESSION_SUMMARY_MAX_CHARS=500` |
| 中文分词 | jieba 分词（lazy import，fallback 正则） | - |
| 漂移检测（[drift_detector.py](core/session/drift_detector.py)） | 4 种类型：话题漂移（jieba Jaccard < 0.15）、意图漂移（7 类意图）、矛盾检测（40+ 否定/矛盾词对）、重复检测（0.8 相似度） | `DRIFT_*` 阈值 |
| 漂移修复 | 自动注入修复提示到 Agent 上下文 | - |
| 漂移升级 | 累计 ≥5 次漂移建议转人工 | `DRIFT_ESCALATION_THRESHOLD=5` |
| 存储后端 | memory / file / Redis 三种后端 | `SESSION_STORAGE_BACKEND` |
| 会话安全 | HMAC-SHA256 会话所有权令牌，防劫持 | `SESSION_TOKEN_SECRET` |
| 空闲过期 | 超过 `SESSION_IDLE_TTL` 秒无活动自动清理 | 默认 3600s |

### RAG 知识库（ChromaDB）

| Collection | 文档数 | 来源 | 用途 | 使用 Agent |
|------------|--------|------|------|------------|
| `product_knowledge` | 50 条 | `data/seed/product_knowledge.json` | 产品成分、功效、价格、适用肤质 | Product, Tech, ReAct |
| `faq` | 45 条 | `data/seed/faq.json` | 常见问题解答 | Product, Complaint, ReAct |
| `tech_support` | 35 条 | `data/seed/tech_support.json` | 使用方法、过敏处理、储存知识 | Tech, ReAct |
| `complaint_knowledge` | 38 条 | `data/seed/complaint_knowledge.json` | 投诉处理流程、补偿方案 | Complaint |
| `image_knowledge` | 可选 | `data/seed/image_knowledge.json` | CLIP 多模态图片检索 | 全部（CLIP 模式） |

**嵌入模型选择**（自动降级）：
1. `BAAI/bge-small-zh-v1.5`（中文优化轻量模型）
2. `shibing624/text2vec-base-chinese`（通用中文向量模型）
3. ChromaDB 默认 `all-MiniLM-L6-v2`（英文兜底）

**RAG 检索增强管线（[knowledge_base.py](rag/knowledge_base.py)）：**
```
用户查询 → 查询改写（query_rewriter.py：同义词扩展 + 多问题拆分）
         → 多 collection 并行检索（run_in_executor 异步包装）
         → RRF 融合（Reciprocal Rank Fusion, k=60）合并文本 + 图片结果
         → BM25 / CrossEncoder 重排（reranker.py：BM25 关键词密度 + 向量距离加权）
         → 截断返回（每条 500 字符）
```

**关键操作：**
- `query()`：单 collection 异步检索（`run_in_executor` 包装同步调用）
- `query_multiple()`：多 collection 并行检索 + 距离合并 + 去重 + 重排
- `query_multimodal()`：文本 + CLIP 图片 RRF 融合检索
- `seed_if_empty()`：幂等种子数据加载（从 `data/seed/*.json` 文件）

### Function Calling 工具（OpenAI 格式）

| 工具 | 功能 | ERP 表 | 参数 |
|------|------|--------|------|
| `query_product` | 产品信息查询 | BD_MATERIAL | `keyword` |
| `query_inventory` | 库存余量查询 | STK_INVENTORY | `product_id` / `keyword` |
| `query_order` | 订单状态查询 | SAL_ORDER | `order_id` / `customer_id`（限 5 条） |
| `query_customer` | 客户资料查询 | BD_CUSTOMER | `customer_id` |

工具注册中心（`ToolRegistry`）以 OpenAI Function Calling 格式管理，`ReActAgent` 通过 `_process_with_tools()` 多轮调用，最多 `TOOL_MAX_ROUNDS=3` 轮。

### ReAct 推理链

```
Thought（推理当前需要什么信息）
  → Action（调用 RAG 检索 或 ERP 工具）
    → Observation（获取工具返回结果）
      → Loop（直到足够信息）
        → Final Answer（综合回答）
```

- **最大迭代**：`REACT_MAX_ITERATIONS=3`（v4.3 从 5 降至 3，控制延迟在 20s 内）
- **触发阈值**：`REACT_COMPLEXITY_THRESHOLD=60`（复杂度评分 ≥ 60 且多领域意图）
- **RAG 检索范围**：同时查询 `product_knowledge` + `faq` + `tech_support`（5 条结果，多于普通 Agent 的 3 条）

### 质量评估系统（[evaluator.py](agents/evaluator.py)）

| 维度 | 权重 | 评分逻辑 |
|------|------|----------|
| **完整性** | 25% | 惩罚过短响应，奖励结构化内容（编号列表），检查关键词覆盖 |
| **准确性** | 25% | 奖励知识库引用、数值数据、确定性措辞；惩罚不确定性措辞 |
| **简洁性** | 15% | 理想范围 50-500 字；惩罚填充短语和句子级重复 |
| **礼貌性** | 10% | 奖励礼貌中文用语；最低分 40（除非粗鲁） |
| **相关性** | 25% | 中文字符重叠 + 关键词短语命中率 |

- **LLM-as-Judge**（可选）：发送 query+response 到 LLM，请求 JSON 格式五维评分
- **反馈聚合**：`aggregate_feedback()` 计算满意度率和趋势（improving/declining/stable）
- **低分自动重试**：评分 < `EVAL_RETRY_THRESHOLD`(30) 且协作模式为 Sequential 时，触发模式升级重试

### 安全设计

| 类别 | 措施 |
|------|------|
| **认证** | API Key（系统间）+ JWT Bearer（终端用户）双认证模式 + `hmac.compare_digest` 防时序攻击 |
| **RBAC 角色** | 4 级角色：customer / agent / supervisor / admin，分级权限控制（[middleware.py](api/middleware.py) `ROLE_PERMISSIONS`） |
| **密码哈希** | PBKDF2-SHA256 + 600K 迭代 + 随机 salt（OWASP 推荐） |
| **JWT** | PyJWT 库 + HS256 算法白名单 + jti 吊销 + Redis 黑名单 + Refresh Token（access 2h + refresh 7d） |
| **CSRF** | 双重 Cookie 提交模式（`csrf_token` cookie + `X-CSRF-Token` header），`hmac.compare_digest` 比较 |
| **限流** | 通用 60 req/min/IP + 登录 5次/5min + 注册 3次/h + Redis 滑动窗口优先，内存回退 |
| **输入验证** | Pydantic 请求模型 + `MAX_QUERY_LENGTH=2000` + 控制字符 + HTML 标签净化（HTML 实体解码防绕过） |
| **注入防护** | ERP 白名单消毒 + 对话历史 `<untrusted-data>` 隔离 + 输出层系统提示泄露检测 |
| **错误脱敏** | 工具执行错误返回通用消息，详细异常仅写服务端日志 |
| **安全头** | HSTS / CSP（script-src 使用 nonce 无 `unsafe-inline`，style-src 暂用 `unsafe-inline` 已知限制）/ X-Frame-Options / X-Content-Type-Options / Referrer-Policy / Permissions-Policy |
| **会话安全** | UUID 格式校验 + HMAC 会话令牌签名（可绑定客户端指纹）+ 用户级会话所有权隔离 |
| **WebSocket** | 首条消息 JWT 认证（非 URL 参数）+ 每 IP 连接限制 + 消息限流 + 空闲超时 + 定期清理 |
| **CORS** | 环境变量配置，默认 `http://localhost:8000`，生产必须配置真实域名 |
| **监控保护** | `/metrics/prometheus` + `/api/metrics` 等 Admin Token 认证 |
| **启动校验** | 生产环境强制校验 `JWT_SECRET`(≥32字符) / `SESSION_TOKEN_SECRET` / `API_KEY`，缺失则抛出 `ConfigurationError` |
| **密钥管理** | `scripts/generate_prod_env.py` 使用 `secrets` 模块生成密码学安全随机密钥 |
| **ERP 安全** | `sanitize_erp_input()` 白名单消毒 + 订单查询拒绝空过滤条件（防全量泄露） |
| **SSRF 防护** | 告警 Webhook URL 验证：阻止私有 IP / 回环 / 链路本地 / 元数据端点 |

---

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Docker & Docker Compose（生产必填）
- Redis 7（生产必填，用于 Session / Cache / JWT 黑名单持久化）
- PostgreSQL 15（生产推荐，开发可使用 SQLite）

### 前端技术选型说明

前端使用**原生 JavaScript**（非 React/Vue），技术选型理由：

| 考量 | 决策 |
|------|------|
| **可嵌入性** | 原生 JS 可直接作为 `<script>` 标签嵌入任意页面（已实现 `widget.html`），无框架运行时依赖 |
| **构建管线** | Vite 8 提供模块化 + Tree-shaking + Hashed 产物，开发体验接近框架项目 |
| **安全** | DOMPurify 净化 LLM 输出 + escapeHtml 净化动态数据 + 全量 innerHTML 审计 |
| **体积** | 无框架运行时，首屏 JS 体积更小，适合嵌入场景 |
| **测试** | Vitest + jsdom 提供单元测试能力 |

**前端已实现功能：**

| 功能 | 实现 | 说明 |
|------|------|------|
| **聊天界面** | `web/index.html` + `chat/` 模块 | 消息渲染（marked.js Markdown）+ 历史会话侧栏 + 快捷短语 |
| **SSE 流式** | `api/sse.js` | 逐 token 推送 + Agent 流转轨迹 + 进度条 |
| **WebSocket** | `api/websocket.js` | 指数退避重连（2s~30s）+ 心跳 + 消息队列 |
| **文件上传** | `chat/input.js` | 支持图片/视频/PDF/DOCX/文本，REST `/api/chat/file` |
| **语音输入** | `chat/voice.js` | Web Speech API + 🎤 按钮 |
| **TTS 语音** | `chat/voice.js` | Edge TTS（zh-CN-XiaoxiaoNeural 等）+ 声音选择器 `<select>` |
| **会话侧面板** | `chat/sessions.js` | 点击会话项弹出侧面板（Agent/模式/时间），Esc 关闭 |
| **主题系统** | `utils/theme.js` + 8 个 CSS | 亮色 4 种 + 暗色 2 种 + 字号/行高/减弱动效/系统偏好 |
| **认证** | `auth/index.js` + `login.js` | JWT 登录/注册 + Token 自动刷新（过期前 5 分钟） |
| **管理后台** | `admin.html` + `admin.js` | 用户管理 / 知识库统计 / 告警配置 / ChromaDB+DB 健康状态 |
| **可嵌入 Widget** | `widget.html` | 轻量聊天组件，可嵌入任意网页 |
| **消息搜索** | `chat/search.js` | 会话内消息关键词搜索 |
| **反馈** | `chat/messages.js` | 👍/👎 反馈 + message_index 精确定位 |
| **无障碍** | ARIA 标签 + 焦点环 + 对比度 + 跳转链接 + 键盘快捷键 |
| **移动端** | 响应式布局 + 抽屉式导航（`responsive.css`） |
| **Toast 通知** | `utils/toast.js` | 操作反馈通知 |
| **剪贴板** | `utils/copy.js` | 一键复制消息内容 |

> 如需组件化开发，可渐进迁移到 Web Components 或轻量框架（Lit/Preact），当前架构已为迁移预留了模块边界。

### 安装

```bash
# 克隆项目
git clone <repository>
cd customer-service-ai-agent

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入必要的配置项（见下方说明）
```

### 配置说明

```bash
# ===== LLM 配置（必填） =====
OPENAI_API_KEY=sk-xxx                              # API Key
OPENAI_BASE_URL=https://api.siliconflow.cn/v1      # 兼容 OpenAI 的 API 地址
OPENAI_MODEL=Qwen/Qwen2.5-7B-Instruct              # 模型名称
# LLM_PROVIDER=siliconflow                          # siliconflow | deepseek | openai | custom

# ===== 安全配置（生产必改） =====
API_KEY_ENABLED=true                                # 是否启用 API Key 认证
API_KEY=your-secure-api-key-here                    # API Key 值
MONITORING_ADMIN_TOKEN=your-admin-token             # 监控端点管理令牌
JWT_SECRET=your-jwt-secret-here                     # JWT 签名密钥（≥32 字符）
SESSION_TOKEN_SECRET=your-session-secret            # 会话令牌签名密钥
CORS_ORIGINS=["https://your-domain.com"]            # CORS 允许来源

# ===== ERP 配置 =====
ERP_MODE=mock                                       # mock（模拟数据）| real（真实金蝶 API）
# real 模式必填：ERP_BASE_URL, ERP_APP_ID, ERP_APP_SECRET, ERP_DB_ID

# ===== Redis（生产必填） =====
REDIS_URL=redis://redis:6379                        # 用于 Session / Cache / JWT 黑名单持久化

# ===== 数据库 =====
# DATABASE_URL=postgresql://user:pass@host:5432/db  # 留空则使用 SQLite
```

### 启动服务

```bash
# 方式一：生产部署（推荐）
# 1. 生成安全配置（自动替换 CHANGE_ME_* 占位符）
python3 scripts/generate_prod_env.py
# 2. 编辑 .env.prod.generated，填写 OPENAI_API_KEY 和 CORS_ORIGINS
# 3. 部署
cp .env.prod.generated .env.prod
cp .env.prod .env
make prod

# 方式二：Docker Compose 直接启动
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml up -d

# 方式三：开发环境（热重载）
make dev

# 方式四：HTTPS 开发环境（支持麦克风等安全上下文功能）
make dev-https

# 方式五：直接运行（仅开发，需已安装依赖）
uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --reload

# 方式六：仅运行测试（无需 API Key）
make test
```

### 验证

| 地址 | 说明 |
|------|------|
| http://localhost:8000 | 前端界面（暗色主题，含对话 + 监控仪表盘） |
| http://localhost:8000/docs | FastAPI 自动生成的 API 文档（Swagger UI） |
| `curl http://localhost:8000/api/health` | 健康检查（DB / Redis / LLM / ChromaDB / 熔断器状态） |
| http://localhost:3000 | Grafana 仪表盘（admin / `<GRAFANA_PASSWORD>`） |
| http://localhost:9090 | Prometheus UI |

### 环境切换

```bash
make env-dev     # 切换到开发环境（宽松认证，DEBUG 日志，内存存储）
make env-prod    # 切换到生产环境（严格认证，JSON 日志，Redis 会话）
make env-test    # 切换到测试环境（最小化依赖，Mock 一切）
make env-check   # 查看当前环境配置摘要
```

---

## 📡 API 参考

| 类别 | 端点数 | 说明 |
|------|--------|------|
| 聊天 | 8 | REST + SSE 流式 + 多模态图片 + 图片流式 + 语音 + 文件上传 + TTS + TTS 声音列表 |
| 会话 | 6 | 列表 / 详情 / 删除 / Checkpoint / 历史 / 消息 |
| 认证 | 8 | 注册 / 登录 / 刷新 / 登出 / 当前用户 / 用户列表 / 审计 / 角色更新 |
| 知识库 | 4 | 统计 / 种子 / 添加 / 同步 |
| 告警 | 3 | 配置 / 测试 / 历史 |
| 监控 | 12 | 健康 / 指标 / KPI / 缓存 / 告警 / 熔断器 / Prometheus / 质量趋势 / 热门问题 / 满意度 / Token 追踪 |
| Prompt | 5 | Agent 列表 / 版本列表 / 创建版本 / 激活版本 / 查询当前版本 |
| 反馈 | 2 | 提交 / 统计 |
| 前端 | 5 | 聊天页 / 登录页 / 管理后台 / Widget / 主题预览 |
| WebSocket | 1 | 实时双向聊天 `/ws/chat` |

**合计：44 个端点 + 5 个页面**

> 完整 API 文档：Swagger UI http://localhost:8000/docs · 详细端点列表：[docs/active/api-reference.md](docs/active/api-reference.md)

---

## 📁 项目结构

```
customer-service-ai-agent/
├── agents/            # 8 个 AI Agent（BaseAgent + 5 领域 + ReAct + Response + Evaluator）
├── core/              # 核心基础设施（配置/DI容器/图构建/消息总线/监控/会话/漂移检测/Prompt管理/A/B测试）
├── api/               # FastAPI 服务层（工厂/中间件/路由/SSE/WebSocket/依赖注入）
├── auth/              # JWT 认证（PBKDF2 + Redis 黑名单 + Refresh Token + RBAC）
├── router/            # 双层查询路由（LLM + 规则并行 + 熔断器降级）
├── collaboration/     # 5 种协作模式 + 模式选择器 + 升级重试
├── rag/               # RAG 知识库（ChromaDB + 查询改写 + BM25/CrossEncoder 重排 + RRF 融合）
├── cache/             # 双层缓存（L1 MD5 + L2 Jaccard + Redis 持久化）
├── db/                # SQLAlchemy 模型 + Alembic 迁移（5 表：User/ChatHistory/AuditLog/Feedback/PromptVersion）
├── erp/               # 金蝶 ERP 适配器（Mock + Real API + HMAC 认证 + 重试 + 分页）
├── tools/             # Function Calling 工具注册（OpenAI 格式 + 4 个 ERP 工具）
├── llm/               # LLM 客户端（重试 + 熔断 + FC + SSE 流式 + 连接池）+ 规则兜底 LLM
├── media/             # 多模态处理（图片/音频/视频/文档/TTS 5 个处理器）
├── alerts/            # 告警通知（Webhook 钉钉/企微/飞书 + SMTP）
├── knowledge/         # 知识库管理路由
├── web/               # 前端（原生 JS + Vite 8 构建 + 22 模块 + 12 CSS + 5 页面）
├── deploy/compose/    # Docker Compose 变体（prod/canary/scale/monitoring）
├── tests/             # 测试套件（1151 Python + 4 Vitest：unit/integration/e2e/stress/performance）
├── docs/              # 文档（active/archive/decisions + ADR）
├── alembic/           # 数据库迁移脚本（3 个版本）
├── nginx/             # Nginx 反向代理（TLS + WebSocket + canary）
├── monitoring/        # Prometheus + Grafana + Alertmanager + Loki
├── loki/              # 日志聚合配置（Loki + Promtail）
└── scripts/           # 运维脚本（部署/备份/RAG 评估/密钥生成）
```

---

## 🧪 测试指南

### 测试套件

| 测试文件 | 目录 | 测试数 | 覆盖范围 |
|---------|------|--------|---------|
| `test_modules.py` | `unit/` | ~115 | 模块级单元测试：Session / Cache / Router / Agent / ERP / 协作 / RAG / Tools / Config / Evaluator / ReAct / Self-Reflection / ERP Factory |
| `test_core_modules.py` | `unit/` | ~120 | 核心模块：ABTest / Tracing / AlertNotifier / RuleBasedLLM / KnowledgeRouter / Exceptions / Monitoring / CircuitBreaker / SLA |
| `test_api_routes.py` | `unit/` | ~115 | API 路由：Sessions / Feedback / Chat(REST+SSE+文件上传) / WebSocket / Monitoring / Dependencies |
| `test_app_factory.py` | `unit/` | 16 | 应用工厂：create_app / lifespan / _run_graph / _persist_metrics_snapshot |
| `test_media.py` | `unit/` | ~55 | 多模态处理：Image / Audio / Video / Document / TTS 各处理器 + 边界用例 |
| `test_llm_rag_coverage.py` | `unit/` | ~105 | LLM 客户端 + RAG 知识库：格式化 / 重试 / 熔断器 / 流式 / CLIP / 查询改写 / 重排器 |
| `test_session_manager_coverage.py` | `unit/` | ~65 | 会话管理器 + 漂移检测：CRUD / Token / 摘要 / 漂移 / 存储后端 / 同步包装 / TokenCounter |
| `test_ws_coverage.py` | `unit/` | 28 | WebSocket：连接清理 / 认证 / 限流 / 模块导入 |
| `test_middleware.py` | `unit/` | 28 | 中间件：限流白名单 / 安全头 / 限流逻辑 / 认证白名单 / 输入保护 / CSRF / 角色权限 |
| `test_prompt_manager.py` | `unit/` | 25 | Prompt 管理器：CRUD / 缓存 / API 路由 / BaseAgent 集成 |
| `test_protocols_di.py` | `unit/` | 15 | 协议接口 + 依赖注入：Protocol conformance / Agent 构造注入 / FastAPI DI |
| `test_rag_reranker.py` | `unit/` | 18 | RAG 重排：BM25 / CrossEncoder / 工厂 / QueryRewriter / 知识库集成 |
| `test_token_tracker_db.py` | `unit/` | 13 | Token 追踪：记录 / 汇总 / 分位数 / DB 回滚保护 |
| `test_integration.py` | `integration/` | 28 | Mock LLM 集成：图调用 / 缓存命中 / 5 种协作模式 / Agent process() / 漂移 / 错误降级 |
| `test_erp_integration.py` | `integration/` | 40 | ERP 集成：Mock/Real 格式兼容 / Token 刷新 / 重试 / 分页 / 查询流程 |
| `test_multimodal.py` | `integration/` | 21 | 多模态集成：图片处理 / AgentState 字段 / BaseAgent 消息构建 / Vision LLM 选择 |
| `test_all.py` | `e2e/` | ~120 | 全链路 E2E：图构建 / API / 会话 / 熔断器 / SLA / 并发 / ReAct / RAG / 性能 / 上下文压力 |
| `test_v4_production.py` | `e2e/` | 27 | 生产功能：数据库 / Auth 服务 / 告警 / API 集成（登录/注册/me/users） |
| `test_production_features.py` | `e2e/` | 25 | 生产特性：DI 容器 / 并发安全 / 配置校验 / Refresh Token / LLM-as-Judge / OpenTelemetry |
| `test_e2e_real_llm.py` | `e2e/` | 5 | **真实 LLM E2E**（需 `OPENAI_API_KEY`）：产品 / 退货 / RAG / 多轮 / 注入防御 |
| `test_stress.py` | `stress/` | 12 | 压力测试（`@pytest.mark.stress`）：缓存 2000 读写 / 总线 200 并发 / 黑板 400 写入 / 会话 1000 创建 / 路由 800 调用 |

**前端测试**（Vitest + jsdom）：

| 测试文件 | 目录 | 覆盖范围 |
|---------|------|---------|
| `theme.test.js` | `web/src/__tests__/` | 主题切换 / 暗色模式 / 系统偏好 |
| `chatState.test.js` | `web/src/__tests__/` | 聊天状态管理 |
| `copy.test.js` | `web/src/__tests__/` | 剪贴板复制 |
| `agents.test.js` | `web/src/__tests__/` | Agent 显示名称映射 |

**总计：1151 Python 测试用例 + 4 Vitest 前端测试**（含 5 个真实 LLM E2E 测试，需配置 `OPENAI_API_KEY`；12 个压力测试标记 `@pytest.mark.stress`）

### 运行测试

```bash
# 全量测试（离线，无需 API Key）
make test

# 或直接使用 pytest
python3 -m pytest tests/ -v --ignore=tests/e2e/test_e2e_real_llm.py

# 真实 LLM E2E 测试（需配置 OPENAI_API_KEY）
python3 -m pytest tests/e2e/test_e2e_real_llm.py -v -m real_llm

# 带覆盖率报告（最低门槛 80%）
make test-cov

# 快速测试（跳过 stress 标记的慢测试）
make test-fast

# 仅 Mock LLM 集成测试（推荐演示）
python3 -m pytest tests/integration/test_integration.py -v

# 单个测试
python3 -m pytest tests/e2e/test_all.py -v -k "test_router"

# RAG 检索质量评估
make eval-rag

# 代码检查（Ruff）
make lint

# 代码格式化（Ruff）
make format
```

### Locust 压测

```bash
# 启动 Locust 压测
locust -f tests/performance/locustfile.py --host=http://localhost:8000
# 访问 http://localhost:8089 配置并发用户数
```

---

## 🔧 配置参考

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| **路由** | | |
| `ROUTING_COMPLEXITY_THRESHOLD` | 50 | 复杂度阈值（<50 快速通道 Sequential） |
| `LLM_ROUTER_TIMEOUT` | 4.0 | 路由 LLM 调用超时（秒） |
| `REACT_COMPLEXITY_THRESHOLD` | 60 | ReAct 触发复杂度阈值 |
| **缓存** | | |
| `CACHE_L1_MAX` | 500 | L1 精确缓存容量（OrderedDict） |
| `CACHE_L2_MAX` | 2000 | L2 语义缓存容量（倒排索引） |
| `CACHE_TTL` | 3600 | 缓存过期时间（秒） |
| `CACHE_SEMANTIC_THRESHOLD_SHORT` | 0.7 | 短文本（≤20 字）语义相似度阈值 |
| `CACHE_SEMANTIC_THRESHOLD_LONG` | 0.5 | 长文本语义相似度阈值 |
| **会话** | | |
| `SESSION_WINDOW_SIZE` | 10 | 滑动窗口大小 |
| `SESSION_MAX_TOKENS` | 4000 | 上下文最大 token 数（tiktoken 计数） |
| `SESSION_STORAGE_BACKEND` | memory | 会话存储后端（memory / file / redis） |
| `SESSION_SUMMARY_MAX_CHARS` | 500 | 历史摘要最大字符数 |
| `SESSION_IDLE_TTL` | 3600 | 会话空闲过期时间（秒） |
| `MAX_SESSIONS` | 10000 | 最大内存会话数 |
| **漂移检测** | | |
| `DRIFT_TOPIC_JACCARD_THRESHOLD` | 0.15 | 话题漂移阈值（jieba Jaccard） |
| `DRIFT_REPETITION_THRESHOLD` | 0.8 | 重复提问阈值 |
| `DRIFT_ESCALATION_THRESHOLD` | 5 | 漂移升级阈值（累计次数建议转人工） |
| **熔断器** | | |
| `CIRCUIT_BREAKER_FAIL_THRESHOLD` | 5 | 熔断触发失败次数 |
| `CIRCUIT_BREAKER_RECOVERY_TIME` | 60 | 熔断恢复探测时间（秒） |
| **SLA** | | |
| `SLA_ALERT_WINDOW` | 50 | SLA 滑动窗口大小 |
| `SLA_ALERT_THRESHOLD` | 30.0 | SLA 违约率告警阈值（%） |
| `SLA_ALERT_COOLDOWN` | 300 | SLA 告警冷却时间（秒） |
| `RESPONSE_TIME_TARGET_MIN` | 5.0 | 最小响应时间 SLA（秒） |
| `RESPONSE_TIME_TARGET_MAX` | 20.0 | 最大响应时间 SLA（秒） |
| `SLA_SEQUENTIAL_MAX` | 15.0 | Sequential 模式 SLA 超时（秒） |
| `SLA_PARALLEL_MAX` | 20.0 | Parallel 模式 SLA 超时（秒） |
| `SLA_CONSULTATION_MAX` | 25.0 | Consultation 模式 SLA 超时（秒） |
| `SLA_HIERARCHICAL_MAX` | 30.0 | Hierarchical 模式 SLA 超时（秒） |
| `SLA_REACT_MAX` | 30.0 | ReAct 模式 SLA 超时（秒） |
| **重试** | | |
| `RETRY_MAX_ATTEMPTS` | 3 | 最大重试次数（仅瞬态错误） |
| `RETRY_BASE_DELAY` | 1.0 | 基础退避延迟（秒，指数退避 max 10s） |
| **连接池** | | |
| `HTTPX_MAX_CONNECTIONS` | 100 | httpx 最大连接数 |
| `HTTPX_KEEPALIVE_CONNECTIONS` | 20 | httpx 保活连接数 |
| **ReAct** | | |
| `REACT_MAX_ITERATIONS` | 3 | ReAct 最大推理步数 |
| `TOOL_MAX_ROUNDS` | 3 | Function Calling 最大轮数 |
| **安全** | | |
| `MAX_QUERY_LENGTH` | 2000 | 用户查询最大字符数 |
| `WS_MAX_CONNECTIONS_PER_IP` | 5 | 每 IP 最大 WebSocket 连接数 |
| `WS_MESSAGE_RATE_LIMIT` | 10 | 每分钟每连接最大消息数 |
| `WS_IDLE_TIMEOUT` | 300 | WebSocket 空闲超时（秒） |
| `JWT_SECRET` | - | JWT 签名密钥（生产必改，≥32 字符） |
| `SESSION_TOKEN_SECRET` | - | 会话令牌签名密钥（生产必改） |
| `JWT_EXPIRE_HOURS` | 72 | JWT token 有效期（小时） |
| `JWT_ACCESS_EXPIRE_HOURS` | 2 | access_token 有效期（小时） |
| `JWT_REFRESH_EXPIRE_HOURS` | 168 | refresh_token 有效期（小时，默认 7 天） |
| **评估** | | |
| `EVAL_RETRY_THRESHOLD` | 30 | 低分重试触发阈值 |
| `EVAL_LOW_SCORE_THRESHOLD` | 40 | 低分告警触发阈值 |
| `MODE_UPGRADE_ENABLED` | true | 低分自动升级协作模式 |
| **LLM** | | |
| `LLM_PROVIDER` | siliconflow | LLM 提供商（siliconflow / deepseek / openai / custom） |
| **SSE** | | |
| `SSE_ENABLED` | true | SSE 流式输出开关 |
| `SSE_CHUNK_SIZE` | 50 | 每次发送的字符数 |
| **多模态** | | |
| `MULTIMODAL_ENABLED` | false | 多模态图片识别开关 |
| `MAX_IMAGE_SIZE_MB` | 5 | 最大图片大小（MB） |
| `VISION_MODEL` | - | 多模态视觉模型（留空复用 OPENAI_MODEL） |
| `CLIP_ENABLED` | false | CLIP 多模态图片检索开关 |
| **A/B 测试** | | |
| `AB_TEST_ENABLED` | false | A/B 测试开关 |
| **数据库** | | |
| `DATABASE_URL` | - | 数据库 URL（空 = SQLite，生产建议 PostgreSQL） |
| `DB_DIR` | data | SQLite 数据库目录 |
| **ERP** | | |
| `ERP_MODE` | mock | ERP 模式（mock / real） |
| `ERP_BASE_URL` | - | 金蝶 API 地址（real 模式必填） |
| `ERP_APP_ID` | - | 金蝶应用 ID |
| `ERP_APP_SECRET` | - | 金蝶应用密钥 |
| `ERP_DB_ID` | - | 金蝶数据库 ID |
| **告警** | | |
| `ALERT_WEBHOOKS` | - | 告警 Webhook（JSON 数组，支持钉钉/企微/飞书） |
| `SMTP_HOST` | - | 邮件 SMTP 服务器 |
| `ALERT_EMAIL_TO` | - | 告警邮件收件人（逗号分隔） |
| **RAG** | | |
| `RAG_PERSIST_DIRECTORY` | - | RAG 持久化目录（空 = 内存模式） |
| `RAG_N_RESULTS` | 3 | RAG 检索返回文档数 |
| `RAG_QUERY_REWRITING` | false | 查询改写开关（同义词扩展 + 多问题拆分） |
| `REACT_SELF_REFLECTION` | false | ReAct 自反思开关（工具调用后 LLM 质量自检） |
| **Redis 键前缀** | | |
| `REDIS_JWT_PREFIX` | csai:jwt:blacklist: | JWT 黑名单键前缀 |
| `REDIS_RATE_PREFIX` | csai:rate: | 限流键前缀 |
| `REDIS_SESSION_PREFIX` | csai:session: | 会话键前缀 |

---

## 📋 变更日志

> 完整变更日志见 [CHANGELOG.md](CHANGELOG.md)

### 最近版本

| 版本 | 日期 | 主题 |
|------|------|------|
| **v5.2** | 2026-06-10 | 无障碍 WCAG AA/AAA 达标 + TTS 语音选择器 + 会话侧面板 + 死代码清理 + Ruff 346→73 |
| **v5.1** | 2026-06-10 | 全量清理 320 临时文件 + 文档同步 + 隐私检查通过 |
| **v5.0** | 2026-06-08 | 前端 Vite 8 重构 + 1151 测试用例 + Ruff 工具链 + 覆盖率 80% + RAG 增强 + 前后端 15 项匹配修复 + 安全审查 7 项 |
| **v4.6** | 2026-06-08 | 文档扫描 20/20 项完成 + pre-commit + 覆盖率 80% + api/app.py 路由拆分 7 模块 |
| **v4.5** | 2026-06-08 | 图构建统一 + 测试断言加固 49 项 + mypy CI + 净减 317 行 |
| **v4.4** | 2026-06-08 | 安全加固（PyJWT/CSP/WS）+ 代码重构（状态拆分/DI）+ 测试覆盖率门槛 |
| **v4.3** | 2026-06-07 | 生产验收 8.1/10 + 密钥自动生成 + 低分重试 + 漂移检测拆分 |
| **v4.2** | 2026-06-06 | SSE 真流式 + ServiceContainer 完全迁移 + 真实 LLM E2E |
| **v4.1** | 2026-06-05 | DeepSeek LLM + 依赖注入 + PostgreSQL + Alembic + 反馈 + 多模态 |
| **v4.0** | 2026-06-05 | 用户认证 + 知识库管理 + 告警通知 + 审计日志 |

---

## 🏗️ 基础设施组件

### 依赖注入容器（[ServiceContainer](core/container.py)）

两阶段初始化：Phase 1 同步创建基础设施（MessageBus / Metrics / CircuitBreaker / Cache / Session），Phase 2 异步初始化 AI 组件（LLM / Agent / Router / RAG / Tools）。幂等保护 + asyncio.Lock + 逆序关闭。

### 中间件栈（[middleware.py](api/middleware.py)）

5 层执行顺序：Trace → CSRF → Auth（分层 RBAC）→ SecurityHeaders（CSP nonce）→ RateLimit（Redis 滑动窗口）

### LLM 客户端（[client.py](llm/client.py)）

OpenAI 兼容客户端：指数退避重试 + 熔断器 + Function Calling + 工具不支持时自动降级 + SSE 真流式 + 按 base_url 连接池隔离

---

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'feat: add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

### 开发规范

- 异步优先：所有图节点和 API 端点使用 `async/await`
- 使用 `make test` 运行测试，确保全部通过（覆盖率 ≥ 80%）
- 代码规范：Ruff（`make lint`）+ Ruff 格式化（`make format`）
- 新功能需包含对应的测试用例
- 更新相关文档
- 结构化日志：使用 `from logger import get_logger` 获取 logger

---

## 📄 许可证

Apache 2.0 - see [LICENSE](LICENSE) for details.
