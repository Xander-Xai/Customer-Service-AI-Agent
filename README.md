# 药妆智多星 — 多智能体客服系统 (Customer Service AI Agent v4.5)

面向化妆品生产/销售企业的基于 **LangGraph** 多 Agent 协作问答系统，实现四层状态机动态路由：缓存检查 → 意图路由 → 专家 Agent 协作 → 响应后处理。

> **v4.5** 图构建统一：消除 3 处图拓扑重复定义，`build_graph(container)` 作为唯一入口，运行时模式升级重试
>
> **v4.4** 全面优化：安全加固 + 代码质量重构 + 测试覆盖率门槛 + 文档完善
>
> **v4.3** 生产验收通过：**383 tests passed** | 安全审计 7.5/10 | 代码质量 8.0/10 | 生产就绪性 7.0/10
>
> 核心能力：SiliconFlow/DeepSeek/OpenAI 兼容 LLM · 依赖注入容器 · SSE 真流式 · PostgreSQL + Alembic · Redis JWT 黑名单 · 反馈系统 · 多模态 · 生产安全加固 · 密钥自动生成

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

    subgraph Middleware["中间件层"]
        TRACE[trace_id 追踪]
        RATE[限流 60req/min/IP]
        AUTH[API Key / JWT 双认证]
        CSP[安全头 CSP/HSTS]
        SANITIZE[输入净化]
    end

    subgraph Graph["LangGraph StateGraph"]
        C0[Layer 0: check_cache<br/>缓存检查]
        C1[Layer 1: classify_query<br/>双层路由]
        C2[Layer 2: 协作模式<br/>5 种动态选择]
        C3[Layer 3: final_response<br/>响应后处理]
    end

    subgraph Cache["缓存层"]
        L1[L1 精确缓存<br/>MD5 + LRU OrderedDict]
        L2[L2 语义缓存<br/>Jaccard + jieba 分词 + 倒排索引]
        RD2[Redis 持久化<br/>可选预热]
    end

    subgraph Router["路由层"]
        LLM[LLM Router<br/>JSON 分类]
        RULE[Rule Classifier<br/>正则 + 复杂度评分]
    end

    subgraph Agents["5 种协作模式"]
        SEQ[Sequential<br/>单 Agent]
        PARA[Parallel<br/>并发 + 聚合]
        CONS[Consultation<br/>主 + 顾问]
        HIERS[Hierarchical<br/>协调者 + 子任务]
        REACT[ReAct<br/>RAG + FC 推理链]
    end

    subgraph Expert["7 个专家 Agent"]
        PA[ProductAgent<br/>产品 + RAG + ERP]
        TA[TechAgent<br/>技术支持 + RAG]
        BA[BillingAgent<br/>账单 + ERP]
        CA[ComplaintAgent<br/>投诉 + RAG]
        GA[GeneralAgent<br/>通用 + 黑板桥接]
        RA[ReActAgent<br/>多步推理 + FC]
        RPA[ResponseAgent<br/>后处理 + 评估]
    end

    subgraph Infra["基础设施"]
        MB[MessageBus<br/>async pub/sub]
        BB[SharedBlackboard<br/>TTL KV]
        MET[MetricsCollector]
        CB[CircuitBreaker<br/>三态熔断]
        SLA[SLAAlertManager<br/>滑动窗口]
        DI[ServiceContainer<br/>依赖注入]
    end

    subgraph External["外部集成"]
        RAG[ChromaDB<br/>RAG 知识库 4 collection]
        ERP[金蝶 ERP<br/>Mock / Real]
        FC[Function Calling<br/>4 个 ERP 工具]
    end

    subgraph Infra2["生产基础设施"]
        NG[Nginx 反向代理 + TLS]
        RD[Redis 7<br/>Session/Cache/JWT]
        PG[PostgreSQL 15]
        PROM[Prometheus]
        GRAF[Grafana 仪表盘]
        AM[Alertmanager]
    end

    WS --> TRACE
    REST --> TRACE
    SSE --> TRACE
    IMG --> TRACE
    TRACE --> RATE
    RATE --> AUTH
    AUTH --> CSP
    CSP --> SANITIZE
    SANITIZE --> C0

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
| **Layer 0** | `check_cache` | L1 MD5 精确匹配 + L2 Jaccard 语义匹配，命中直接返回（<10ms） | `cache/response_cache.py`：L1 OrderedDict + L2 倒排索引 + Redis 持久化 |
| **Layer 1** | `classify_query` | LLM Router ∥ Rule Classifier 并行（`asyncio.gather`）+ 复杂度评分（阈值 50） | `router/query_router.py`：7 种意图分类 + 熔断器保护 |
| **Layer 2** | `sequential/parallel/consultation/hierarchical/react` | 5 种协作模式动态选择 | `collaboration/orchestrator.py` + `collaboration/modes.py` |
| **Layer 3** | `final_response` | 质量评估 + 模式升级重试 + 缓存写入 + SLA 监控 + 事件广播 | `agents/response_agent.py` + `agents/evaluator.py` |

### 请求处理流程

```mermaid
sequenceDiagram
    participant C as 客户端
    participant MW as 中间件层
    participant G as LangGraph
    participant L0 as Layer 0 缓存
    participant L1 as Layer 1 路由
    participant L2 as Layer 2 协作
    participant RA as ResponseAgent
    participant LLM as LLM API

    C->>MW: WebSocket / REST / SSE 请求
    MW->>MW: trace_id → 限流 → 认证 → 安全头 → 输入净化
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

**BaseAgent 核心能力：**
- 会话上下文检索（滑动窗口最近 6 条）
- 漂移检测（话题/意图/矛盾/重复 4 种类型）+ 自动注入修复提示
- RAG 知识库检索（按 Agent 分配 collection）
- Function Calling 多轮工具调用循环（`_process_with_tools`，最多 `TOOL_MAX_ROUNDS` 轮）
- A/B 测试 prompt 变体分配（SHA-256 确定性分流）
- SSE 真流式输出（检测 `stream_callback` 自动切换）
- 指数退避重试（仅瞬态错误：`ConnectionError`/`TimeoutError`/`OSError`）
- 对话历史 `<untrusted-data>` 标签隔离（防 prompt 注入）

### 5 种协作模式

| 模式 | 触发条件 | 行为 | SLA 超时 |
|------|----------|------|----------|
| **Sequential** | `fast_path=True` 或 `complexity < 50` | 单 Agent 顺序处理 | 15s |
| **Parallel** | 多领域意图（≥2 domain hints）+ `complexity < 60` | 多 Agent 并发（Semaphore≤5） + 结果聚合 | 20s |
| **Consultation** | 单领域 + `complexity ≥ 60` + 有顾问映射 | 主 Agent + 辅助 Agent 补充信息注入 | 25s |
| **Hierarchical** | 投诉/升级场景 | GeneralAgent 协调者分配子任务 + 汇总（动态添加 product/billing 子任务） | 30s |
| **ReAct** | 多领域（≥2）+ `complexity ≥ 60` | RAG 检索 + Function Calling 多轮推理链 | 30s |

**模式选择算法**（`collaboration/orchestrator.py`）：

```
1. fast_path=True → Sequential（快速通道）
2. query_type == "complaint" → Hierarchical（动态添加 product/billing 子任务）
3. 多领域(≥2) + complexity ≥ 60 → ReAct
4. 多领域(≥2) → Parallel
5. 单领域 + complexity ≥ 60 + 有顾问 → Consultation
6. 默认 → Sequential
```

**模式升级重试**（v4.3）：当 ResponseAgent 质量评分低于阈值时，自动升级：Sequential → Consultation → Parallel → ReAct

**顾问映射关系：**
- `product_agent` → 顾问 `tech_agent`
- `billing_agent` → 顾问 `product_agent`
- `tech_agent` → 顾问 `product_agent`

### 缓存系统

```mermaid
flowchart LR
    Query[用户查询] --> L1[L1 精确缓存<br/>MD5 哈希 O(1)]
    L1 -- hit --> Response[直接响应 <10ms]
    L1 -- miss --> L2[L2 语义缓存<br/>Jaccard + jieba]
    L2 -- hit --> Response
    L2 -- miss --> Router[→ Layer 1 路由]

    subgraph L1Detail["L1 实现"]
        direction TB
        OD[OrderedDict LRU]
        EVICT1[满时淘汰 5%]
        TTL1[TTL 3600s]
    end

    subgraph L2Detail["L2 实现"]
        direction TB
        FROZEN[frozenset 存储分词]
        INV[倒排索引快速候选]
        DYN[动态阈值：短文本 0.7 / 长文本 0.5]
        EVICT2[deque FIFO 淘汰 5%]
    end

    L1Detail --> L1
    L2Detail --> L2
    L1 -.-> Redis[(Redis SETEX<br/>预热 + 持久化)]
```

### 会话管理（`session_manager.py`）

| 功能 | 实现 | 配置 |
|------|------|------|
| 滑动窗口裁剪 | 消息数（`SESSION_WINDOW_SIZE`）+ token 数（tiktoken `cl100k_base`）双重控制 | 默认 10 条 / 4000 tokens |
| 历史摘要 | LLM 异步生成 2-3 句摘要注入上下文 | `SESSION_SUMMARY_MAX_CHARS=500` |
| 中文分词 | jieba 分词（lazy import，fallback 正则） | - |
| 漂移检测（`drift_detector.py`） | 4 种类型：话题漂移（jieba Jaccard < 0.15）、意图漂移（7 类意图）、矛盾检测（40+ 否定/矛盾词对）、重复检测（0.8 相似度） | `DRIFT_*` 阈值 |
| 漂移修复 | 自动注入修复提示到 Agent 上下文 | - |
| 漂移升级 | 累计 ≥5 次漂移建议转人工 | `DRIFT_ESCALATION_THRESHOLD=5` |
| 存储后端 | memory / file / Redis 三种后端 | `SESSION_STORAGE_BACKEND` |
| 会话安全 | HMAC-SHA256 会话所有权令牌，防劫持 | `SESSION_TOKEN_SECRET` |
| 空闲过期 | 超过 `SESSION_IDLE_TTL` 秒无活动自动清理 | 默认 3600s |

### RAG 知识库（ChromaDB）

| Collection | 文档数 | 用途 | 使用 Agent |
|------------|--------|------|------------|
| `product_knowledge` | 78 条 | 产品成分、功效、价格、适用肤质 | Product, Tech, ReAct |
| `faq` | 84 条 | 常见问题解答 | Product, Complaint, ReAct |
| `tech_support` | 72 条 | 使用方法、过敏处理、储存知识 | Tech, ReAct |
| `complaint_knowledge` | 66 条 | 投诉处理流程、补偿方案 | Complaint |

**嵌入模型选择**（自动降级）：
1. `BAAI/bge-small-zh-v1.5`（中文优化轻量模型）
2. `shibing624/text2vec-base-chinese`（通用中文向量模型）
3. ChromaDB 默认 `all-MiniLM-L6-v2`（英文兜底）

**关键操作：**
- `query()`：单 collection 异步检索（`run_in_executor` 包装同步调用）
- `query_multiple()`：多 collection 并行检索 + 距离合并 + 去重
- `seed_if_empty()`：幂等种子数据加载

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

### 质量评估系统（`agents/evaluator.py`）

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
| **密码哈希** | PBKDF2-SHA256 + 600K 迭代 + 随机 salt（OWASP 推荐） |
| **JWT** | PyJWT 库 + HS256 算法白名单 + jti 吊销 + Redis 黑名单 + Refresh Token（access 2h + refresh 7d） |
| **限流** | 通用 60 req/min/IP + 登录 5次/5min + 注册 3次/h + Redis 滑动窗口优先，内存回退 |
| **输入验证** | Pydantic 请求模型 + `MAX_QUERY_LENGTH=2000` + 控制字符 + HTML 标签净化（HTML 实体解码防绕过） |
| **注入防护** | ERP 白名单消毒 + 对话历史 `<untrusted-data>` 隔离 + 输出层系统提示泄露检测 |
| **错误脱敏** | 工具执行错误返回通用消息，详细异常仅写服务端日志 |
| **安全头** | HSTS / CSP（nonce，无 `unsafe-inline`）/ X-Frame-Options / X-Content-Type-Options / Referrer-Policy / Permissions-Policy |
| **会话安全** | UUID 格式校验 + HMAC 会话令牌签名 + 用户级会话所有权隔离 |
| **WebSocket** | 首条消息 JWT 认证（非 URL 参数）+ 每 IP 连接限制 + 消息限流 + 空闲超时 + 定期清理 |
| **CORS** | 环境变量配置，默认 `http://localhost:8000`，生产必须配置真实域名 |
| **监控保护** | `/metrics/prometheus` + `/api/metrics` 等 Admin Token 认证 |
| **启动校验** | 生产环境强制校验 `JWT_SECRET` / `SESSION_TOKEN_SECRET` / `API_KEY`，缺失则拒绝启动 |
| **密钥管理** | `scripts/generate_prod_env.py` 使用 `secrets` 模块生成密码学安全随机密钥 |
| **ERP 安全** | `sanitize_erp_input()` 白名单消毒 + 订单查询拒绝空过滤条件（防全量泄露） |

---

## 🚀 快速开始

### 环境要求

- Python 3.10+（CI 测试 3.10 / 3.11 / 3.12 三版本兼容）
- Docker & Docker Compose（生产必填）
- Redis 7（生产必填，用于 Session / Cache / JWT 黑名单持久化）
- PostgreSQL 15（生产推荐，开发可使用 SQLite）
- Nginx（生产推荐，或使用内置 Nginx 容器）

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
OPENAI_MODEL=Qwen/Qwen3-8B                         # 模型名称
# LLM_PROVIDER=siliconflow                          # siliconflow | deepseek | openai | custom

# ===== 安全配置（生产必改） =====
API_KEY_ENABLED=true                                # 是否启用 API Key 认证
API_KEY=your-secure-api-key-here                    # API Key 值
MONITORING_ADMIN_TOKEN=your-admin-token             # 监控端点管理令牌
JWT_SECRET=your-jwt-secret-here                     # JWT 签名密钥
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
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 方式三：开发环境（热重载）
make dev

# 方式四：直接运行（仅开发，需已安装依赖）
uvicorn api.app_factory:app --host 0.0.0.0 --port 8000 --reload

# 方式五：仅运行测试（无需 API Key）
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

### 数据备份

```bash
# 手动备份（ChromaDB + Redis + 配置）
make backup

# 定时备份（每天凌晨 2 点，保留 7 份）
crontab -e
0 2 * * * /path/to/scripts/backup.sh /data/backups
```

---

## 📡 API 参考

### WebSocket 实时对话

```javascript
// 连接（JWT 通过首条消息认证，非 URL 参数）
const ws = new WebSocket('ws://localhost:8000/ws/chat');

// 首条消息：认证 + 查询
ws.send(JSON.stringify({
    token: "your-jwt-token",      // JWT 认证令牌
    query: "这款产品的成分是什么？",
    session_id: "optional-session-id"
}));

// 接收消息（渐进式推送）
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    switch (data.type) {
        case 'status':     // 处理状态更新
        case 'progress':   // Agent 处理进度
        case 'response':   // 最终响应
        case 'error':      // 错误信息
    }
};
```

### REST API 端点

#### 聊天接口

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `POST` | `/api/chat` | 同步对话接口 | JWT / API Key |
| `POST` | `/api/chat/stream` | SSE 真流式输出 | JWT / API Key |
| `POST` | `/api/chat/image` | 多模态图片 + 文本 | JWT / API Key |
| `WS` | `/ws/chat` | WebSocket 实时对话 | 首条消息 JWT |

#### 认证接口

| 方法 | 路径 | 说明 | 限流 |
|------|------|------|------|
| `POST` | `/api/auth/register` | 用户注册 | 3 次/h |
| `POST` | `/api/auth/login` | 用户登录（返回 access_token + refresh_token） | 5 次/5min |
| `POST` | `/api/auth/refresh` | 刷新 access_token | - |
| `POST` | `/api/auth/logout` | 登出（吊销 JWT） | - |
| `GET` | `/api/auth/me` | 当前用户信息 | JWT |
| `GET` | `/api/auth/users` | 用户列表 | JWT (admin) |
| `GET` | `/api/auth/audit` | 审计日志 | JWT (admin) |

#### 知识库管理

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/knowledge/stats` | 知识库统计 | JWT / API Key |
| `POST` | `/api/knowledge/seed` | 重新种子数据 | JWT (admin) |
| `POST` | `/api/knowledge/{collection}/add` | 添加文档 | JWT (admin) |
| `POST` | `/api/knowledge/sync` | 从 ERP 同步 | JWT (admin) |

#### 告警管理

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/alerts/config` | 告警配置 | JWT (admin) |
| `POST` | `/api/alerts/test` | 测试告警通知 | JWT (admin) |
| `GET` | `/api/alerts/history` | 告警历史 | JWT (admin) |

#### 监控与运维

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| `GET` | `/api/health` | 健康检查（DB/Redis/LLM/ChromaDB/熔断器） | 无 |
| `GET` | `/api/metrics` | 性能监控指标 + 缓存统计 | Admin Token |
| `GET` | `/api/kpi` | 业务 KPI（首解率/AI 处理率/人力节省） | Admin Token |
| `GET` | `/api/cache/stats` | 缓存统计（L1/L2 命中率） | Admin Token |
| `GET` | `/api/sessions` | 会话列表（用户级隔离） | JWT |
| `GET` | `/api/sessions/{id}` | 会话详情（含所有权校验） | JWT |
| `DELETE` | `/api/sessions/{id}` | 删除会话 | JWT |
| `GET` | `/api/history` | 历史会话列表 | JWT |
| `GET` | `/api/history/{id}/messages` | 会话消息历史 | JWT |
| `POST` | `/api/feedback` | 客户满意度反馈（👍/👎） | JWT |
| `GET` | `/api/feedback/stats` | 反馈统计 | Admin Token |
| `GET` | `/api/alerts` | SLA 告警记录 | Admin Token |
| `GET` | `/api/circuit-breaker` | LLM 熔断器状态 | Admin Token |
| `GET` | `/metrics/prometheus` | Prometheus 文本格式指标 | Admin Token |

#### 前端页面

| 路径 | 说明 | 认证 |
|------|------|------|
| `GET` `/` | 主页面（对话 + 监控仪表盘） | 无 |
| `GET` `/login.html` | 登录/注册页 | 无 |
| `GET` `/admin.html` | 管理后台 | JWT (admin) |

### 错误码

| 错误码 | 含义 | 处理建议 |
|--------|------|----------|
| `400` | 请求参数错误 | 检查 query 字段长度和格式 |
| `401` | 认证失败 | 检查 API Key 或 JWT 是否正确/过期 |
| `403` | 权限不足 | 使用 Admin Token 或 admin 角色 JWT |
| `429` | 请求过于频繁 | 降低请求频率，等待限流窗口重置 |
| `500` | 服务内部错误 | 检查日志（`logs/app.log`），联系管理员 |
| `503` | LLM 服务不可用 | 检查 `/api/circuit-breaker` 状态，等待恢复 |

---

## 📁 项目结构

```
customer-service-ai-agent/
├── agents/                              # 多 Agent 专家体系（8 个 Agent）
│   ├── __init__.py
│   ├── base_agent.py                    # Agent 抽象基类（会话 + RAG + FC + 漂移 + A/B + 流式）
│   ├── product_agent.py                 # 产品专家（ERP + RAG）
│   ├── tech_agent.py                    # 技术支持（RAG）
│   ├── billing_agent.py                 # 账单专家（ERP）
│   ├── complaint_agent.py               # 投诉处理（RAG + 黑板标记）
│   ├── general_agent.py                 # 通用咨询（ERP + 黑板桥接）
│   ├── react_agent.py                   # ReAct 推理（RAG + FC 工具调用）
│   ├── response_agent.py                # 响应后处理（消毒 + 评估 + 模式升级 + 缓存）
│   └── evaluator.py                     # 响应质量评估器（5 维规则 + LLM-as-Judge）
├── router/                              # 双层查询路由器
│   └── query_router.py                  # LLM Router ∥ Rule Classifier + 复杂度评分
├── collaboration/                       # 协作模式编排
│   ├── modes.py                         # 5 种模式实现（Sequential/Parallel/Consultation/Hierarchical/ReAct）
│   └── orchestrator.py                  # 统一模式选择 + 运行时模式升级
├── core/                                # 通信与监控基础设施
│   ├── state.py                         # AgentState TypedDict（LangGraph 状态定义）
│   ├── message_bus.py                   # 异步 pub/sub 消息总线
│   ├── shared_blackboard.py             # TTL 共享黑板（跨 Agent 数据共享）
│   ├── container.py                     # ServiceContainer 依赖注入容器（两阶段初始化）
│   ├── monitoring.py                    # MetricsCollector + CircuitBreaker + SLAAlertManager + LLM Client
│   ├── tracing.py                       # OpenTelemetry 分布式追踪（可选）
│   └── ab_testing.py                    # A/B 测试框架（SHA-256 确定性分流）
├── cache/                               # 二级缓存系统
│   └── response_cache.py                # L1 MD5 精确 + L2 Jaccard 语义 + Redis 持久化
├── db/                                  # 数据库层
│   ├── models.py                        # User / ChatHistory / AuditLog / Feedback / PromptVersion
│   └── database.py                      # 连接管理（SQLite + PostgreSQL）+ Alembic 迁移
├── alembic/                             # 数据库迁移（Alembic）
│   ├── env.py
│   └── versions/                        # 迁移版本脚本（001/002/003）
├── auth/                                # 用户认证
│   ├── service.py                       # PBKDF2-SHA256 密码哈希 + JWT（PyJWT + Redis 黑名单）
│   └── router.py                        # 认证 API（register/login/refresh/logout/me/users/audit）
├── knowledge/                           # 知识库管理
│   └── router.py                        # 知识库 API（stats/seed/sync/add）
├── alerts/                              # 告警通知
│   ├── notifier.py                      # Webhook + SMTP 邮件通知（含 SSRF 防护）
│   └── router.py                        # 告警 API（config/test/history）
├── rag/                                 # RAG 知识库
│   ├── knowledge_base.py                # ChromaDB 向量检索（嵌入模型自动降级）
│   └── seed_data.py                     # 种子数据（4 个 collection，300+ 文档）
├── tools/                               # Function Calling 工具
│   ├── tool_registry.py                 # 工具注册中心（OpenAI FC 格式）
│   └── erp_tools.py                     # ERP 工具封装（4 个查询工具）
├── erp/                                 # 金蝶 ERP 集成
│   ├── __init__.py                      # 抽象接口 + 输入消毒（白名单 + SQL 转义）
│   ├── factory.py                       # 适配器工厂（Mock / Real 自动切换）
│   ├── kingdee_adapter.py               # Mock 适配器（5 产品 + 3 订单 + 2 客户）
│   └── kingdee_real_adapter.py          # 真实金蝶 API（Token 管理 + 重试 + 分页）
├── llm/                                 # LLM 实现
│   └── rule_based_llm.py                # 规则引擎 LLM 回退（无 API Key 时使用）
├── session_manager.py                   # 会话管理（滑动窗口 + token 裁剪 + 摘要 + 存储后端）
├── drift_detector.py                    # 漂移检测（jieba + 4 种类型 + 升级机制）
├── token_counter.py                     # Token 计数 + 中文分词工具（tiktoken + jieba lazy）
├── multi_agent_customer_service.py      # LangGraph 图构建（build_graph 唯一入口 + make_graph 兼容包装）
├── config.py                            # 统一配置（80+ 参数 + 生产启动校验）
├── logger.py                            # 结构化日志（gzip 轮转 + trace_id 注入 + JSON 格式）
├── gunicorn.conf.py                     # Gunicorn 生产配置（UvicornWorker + 自动扩 Worker）
├── api/                                 # FastAPI 服务层
│   ├── app.py                           # 应用 + 4 层中间件 + REST/WebSocket/SSE 端点 + Prometheus
│   └── app_factory.py                   # 入口 + ServiceContainer + 数据库 + 安全检查 + lifespan
├── nginx/                               # Nginx 反向代理
│   ├── nginx.conf                       # TLS + WebSocket + 负载均衡 + canary 路由
│   └── Dockerfile
├── monitoring/                          # 监控配置
│   ├── prometheus.yml                   # Prometheus 抓取配置
│   ├── alert_rules.yml                  # Prometheus 告警规则
│   ├── alertmanager.yml                 # Alertmanager 路由配置
│   └── grafana/                         # Grafana 仪表盘 + 数据源自动配置
├── loki/                                # 日志聚合
│   ├── loki-config.yaml                 # Loki 服务端配置
│   └── promtail-config.yaml             # Promtail 日志采集配置
├── scripts/                             # 运维脚本
│   ├── deploy.sh                        # 一键部署
│   ├── backup.sh                        # 数据备份（ChromaDB + Redis + 配置）
│   ├── generate_prod_env.py             # 生产密钥自动生成（secrets 模块）
│   └── evaluate_rag.py                  # RAG 检索质量评估
├── static/                              # 前端静态资源
│   ├── css/style.css                    # 暗色主题样式
│   └── js/
│       ├── api.js                       # API 客户端（WebSocket + REST）
│       ├── chat.js                      # 对话 + 监控仪表盘 UI
│       └── admin.js                     # 管理后台 JS
├── templates/                           # HTML 模板
│   ├── index.html                       # 主页面（对话 + 监控）
│   ├── login.html                       # 登录/注册页
│   └── admin.html                       # 管理后台
├── tests/                               # 测试套件（383 离线 + 5 真实 LLM E2E）
│   ├── test_all.py                      # 端到端集成测试（图构建/API/会话/熔断器/SLA/并发/性能）
│   ├── test_modules.py                  # 模块级单元测试（16 个测试类，覆盖全部模块）
│   ├── test_integration.py              # Mock LLM 集成测试（图调用/缓存/5 种协作模式/降级）
│   ├── test_stress.py                   # 压力/性能测试（缓存/总线/黑板/会话/路由高并发）
│   ├── test_v4_production.py            # v4 生产功能（DI 容器/并发安全/配置校验/Refresh Token）
│   ├── test_production_features.py      # 生产特性（数据库/认证/告警/知识库 API）
│   ├── test_erp_integration.py            # ERP 集成测试（Mock/Real 适配器/Token 管理/重试）
│   ├── test_e2e_real_llm.py             # 真实 LLM E2E（需 OPENAI_API_KEY，默认跳过）
│   └── performance/
│       └── locustfile.py                # Locust 压测脚本（3 种用户类型）
├── docs/                                # 文档
│   ├── architecture-design.md           # 架构设计文档（面试用）
│   ├── interview-intro.md               # 3 分钟项目介绍脚本
│   ├── interview-deep-dive.md           # 8 个面试深挖 Q&A
│   ├── e2e-verification-guide.md        # E2E 验证指南
│   ├── rag-evaluation.md               # RAG 检索质量评估方法论
│   ├── rag-evaluation-report.json       # RAG 评估数据（30 条查询）
│   ├── 2026-06-07-v4.3-audit-and-ci-fix-summary.md  # v4.3 验收总结
│   ├── superpowers/specs/               # 设计规格文档
│   └── archive/                         # 归档文档（历史记录，不再维护）
├── alembic.ini                          # Alembic 迁移配置
├── Dockerfile                           # 多阶段构建（builder + runtime，非 root）
├── docker-compose.yml                   # 核心栈：Nginx + App + PostgreSQL + Redis + Prometheus + Grafana
├── docker-compose.prod.yml              # 生产覆盖（Gunicorn + 资源限制 + Loki + Promtail）
├── docker-compose.override.yml          # 开发覆盖（uvicorn --reload，自动加载）
├── docker-compose.canary.yml            # 灰度发布（canary 服务 + Nginx 90/10 流量分割）
├── docker-compose.monitoring.yml        # 独立日志聚合（Loki + Promtail）
├── docker-compose.scale.yml             # 水平扩展（多实例 + Nginx 负载均衡）
├── requirements.txt                     # Python 依赖（18 个直接依赖）
├── requirements-dev.txt                 # 开发依赖（pytest + pytest-asyncio + pytest-cov）
├── requirements-lock.txt                # 依赖锁定（200+ 传递依赖）
├── pytest.ini                           # pytest 配置（asyncio_mode=auto + 覆盖率）
├── .coveragerc                          # 覆盖率配置（最低 60%）
├── .github/workflows/ci.yml             # CI/CD（Python 3.10/3.11/3.12 + 安全扫描 + Docker 构建）
├── .env.example                         # 环境变量模板（180+ 行完整注释）
├── .env.dev                             # 开发环境配置
├── .env.prod                            # 生产环境配置（CHANGE_ME_* 占位符）
├── .env.test                            # 测试环境配置
├── CHANGELOG.md                         # 变更日志（从 README 独立）
├── SECURITY.md                          # 安全策略文档
├── LICENSE                              # Apache 2.0 许可证
└── langgraph.json                       # LangGraph Cloud 部署清单
```

---

## 🧪 测试指南

### 测试套件

| 测试文件 | 覆盖范围 | 测试数 |
|---------|---------|--------|
| `test_all.py` | 端到端集成：图构建 / API 端点 / 会话令牌 / 熔断器 / SLA / 并发安全 / ReAct / RAG / 性能 / 工具注册 | ~130 |
| `test_modules.py` | 模块级单元测试：Session / Cache / Router / Agent / ERP / 协作 / RAG / Tools / Config / API / Logger / 流式 | ~85 |
| `test_integration.py` | Mock LLM 集成：图调用 / 缓存命中跳过 / 5 种协作模式 / Agent process() / 多轮上下文 / 漂移 / 错误降级 | ~20 |
| `test_stress.py` | 压力 / 性能：缓存 2000 读写 / 总线 200 并发 / 黑板 400 写入 / 会话 1000 创建 / 路由 800 调用 | ~13 |
| `test_v4_production.py` | v4 生产功能：DI 容器 / 并发安全 / 配置校验 / Refresh Token / LLM-as-Judge / OpenTelemetry | ~17 |
| `test_production_features.py` | 生产特性：数据库模型 / 认证服务 / 告警通知 / 知识库 API / 认证 API 集成 | ~25 |
| `test_erp_integration.py` | ERP 集成：Mock/Real 数据格式兼容 / Token 刷新 / 重试逻辑 / 分页合并 / 查询流程 | ~30 |
| `test_e2e_real_llm.py` | **真实 LLM E2E**（需 OPENAI_API_KEY）：产品咨询 / 退货路由 / RAG / 多轮 / 注入防御 | 5 |

**总计：383 离线测试 + 5 真实 LLM E2E = 388 tests**

### 运行测试

```bash
# 全量测试（离线，无需 API Key）— 383 passed
make test

# 或直接使用 pytest
python3 -m pytest tests/ -v --ignore=tests/test_e2e_real_llm.py

# 真实 LLM E2E 测试（需配置 OPENAI_API_KEY）
python3 -m pytest tests/test_e2e_real_llm.py -v -m real_llm

# 带覆盖率报告
make test-cov

# 快速测试（跳过 stress 标记的慢测试）
make test-fast

# 仅 Mock LLM 集成测试（推荐演示）
python3 -m pytest tests/test_integration.py -v

# 单个测试
python3 -m pytest tests/test_all.py -v -k "test_router"

# RAG 检索质量评估
make eval-rag

# 代码语法检查
make lint
```

### Locust 压测

```bash
# 启动 Locust 压测
locust -f tests/performance/locustfile.py --host=http://localhost:8000
# 访问 http://localhost:8089 配置并发用户数
```

---

## 📊 架构质量评估

> 基于工业化标准的七维度评估（满分 10 分），定期审查更新。

| 维度 | v4.0 | v4.1 | v4.3 | v4.4 | 说明 |
|------|------|------|------|------|------|
| **可扩展性** | 4.0 | 5.5 | 7.0 | 7.0 | ServiceContainer DI + Redis 限流 + 双数据库 + Alembic |
| **可靠性** | 6.0 | 7.5 | 8.0 | 8.0 | 三态熔断器 + 重试 + 优雅关闭 + 全链路健康检查 |
| **安全性** | 5.5 | 8.0 | 8.5 | 9.0 | PyJWT + CSP nonce + WebSocket 安全 + 占位符检测 |
| **性能** | 5.0 | 7.0 | 7.5 | 7.5 | SSE 真流式 + SQLite WAL + 二级缓存 + 连接池 |
| **可观测性** | 7.0 | 8.5 | 9.0 | 9.0 | trace_id + Prometheus + Grafana + Loki + Alertmanager |
| **部署运维** | 6.5 | 8.0 | 8.5 | 8.5 | 多阶段 Docker + 非 root + 灰度发布 + 密钥自动生成 + CI/CD |
| **代码质量** | 6.0 | 7.5 | 8.0 | 8.5 | DI 容器 + 383 tests + 覆盖率门槛 60% + 模块拆分 + 类型注解 |

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
| `JWT_SECRET` | - | JWT 签名密钥（生产必改） |
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
| **v4.4** | 2026-06-08 | 安全加固（PyJWT/CSP/WS）+ 代码重构（状态拆分/DI）+ 测试覆盖率门槛 + 文档完善 |
| **v4.3** | 2026-06-07 | 生产验收 8.1/10 + 密钥自动生成 + 低分重试 + 漂移检测拆分 |
| **v4.2** | 2026-06-06 | SSE 真流式 + ServiceContainer 完全迁移 + 真实 LLM E2E 5/5 |
| **v4.1** | 2026-06-05 | DeepSeek LLM + 依赖注入 + PostgreSQL + Alembic + 反馈 + 多模态 |
| **v4.0** | 2026-06-05 | 用户认证 + 知识库管理 + 告警通知 + 审计日志 |
| **v3.9** | 2026-06-03 | Nginx + Gunicorn + Prometheus/Grafana + CI/CD |

---

## 🏗️ 基础设施组件

### 依赖注入容器（ServiceContainer）

```
Phase 1（sync __init__）：
  MessageBus / SharedBlackboard / MetricsCollector / CircuitBreaker / SLAAlertManager / ResponseCache / SessionManager

Phase 2（async initialize()，幂等）：
  1. LLM Client（API Key 无效 + DEV_MODE → RuleBasedLLM 回退）
  2. ERP Adapter（Factory：mock / real）
  3. RAG KnowledgeBase + ToolRegistry
  4. 7 个 Agent（注入 llm / session / bus / blackboard / erp / knowledge / tools）
  5. QueryRouter
  6. CollaborationOrchestrator
  7. LangGraph StateGraph 构建

close()：逆序关闭（LLM 连接池 → ERP 适配器）
```

### 中间件栈（执行顺序）

```
1. TraceMiddleware    → 生成 12 字符 trace_id，注入 contextvars + X-Trace-ID 响应头
2. AuthMiddleware     → 分层认证：公开路径 / admin 端点 / 知识库端点 / 默认端点
3. SecurityHeaders    → CSP(nonce) + HSTS + X-Frame-Options + X-Content-Type-Options + ...
4. RateLimitMiddleware → 通用 60/min/IP + auth 端点专项限流 + Redis 滑动窗口优先
```

### LLM 客户端（OpenAICompatibleClient）

- `async_invoke()`：非流式调用，指数退避重试 + 熔断器 + Function Calling + 工具降级
- `async_invoke_stream()`：SSE 真流式，`httpx.stream()` 逐 chunk 推送
- 连接池隔离：按 `base_url` 分池，`asyncio.Lock` 保护
- 消息格式化：LangChain 消息类型 → OpenAI 格式（支持多模态 content）

### CI/CD 流水线

```
push/PR → test（Python 3.10/3.11/3.12 并行）
       → security（pip-audit + bandit + 硬编码密钥扫描）
       → build-and-push（main 分支，Docker → GHCR）
       → deploy（需配置 DEPLOY_HOST，SSH 部署 + 健康检查验证）
```

---

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'feat: add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

### 开发规范

- 异步优先：所有图节点和 API 端点使用 `async/await`
- 使用 `make test` 运行测试，确保全部通过
- 遵循 PEP 8 代码风格（`black --line-length 100`）
- 新功能需包含对应的测试用例
- 更新相关文档
- 结构化日志：使用 `from logger import get_logger` 获取 logger

---

## 📄 许可证

Apache 2.0 - see [LICENSE](LICENSE) for details.
