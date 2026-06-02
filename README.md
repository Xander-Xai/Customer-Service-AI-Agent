# 多智能体客服系统 (Customer Service AI Agent v3.6)

面向化妆品生产企业的基于 **LangGraph** 多 Agent 协作问答系统，实现四层状态机动态路由：缓存检查 → 意图路由 → 专家 Agent → 响应处理。

> **v3.5** 新增 RAG 知识库检索、Function Calling 工具调用、ReAct 推理模式；**v3.6** 完成前端修复、安全加固、并发超时保护与代码瘦身（151 tests passed, 4 skipped）。

## 核心特性

### 🏗️ 多 Agent 架构

| 组件 | 说明 |
|------|------|
| **Router Agent** | LLM Router + Rule Classifier 双层意图识别，**并行执行**，复杂度多维评分（阈值 50 分流） |
| **ProductAgent** | 产品专家：成分分析、功效查询、价格对比、库存查询（对接金蝶 ERP + RAG 知识库） |
| **TechAgent** | 技术支持：使用指导、过敏处理、产品搭配、保质期（RAG 知识库检索） |
| **BillingAgent** | 账单专家：退款处理、订单查询、发票管理、物流追踪（对接金蝶 ERP） |
| **ComplaintAgent** | 投诉处理：情绪安抚、问题解决、补偿方案、升级处理 |
| **GeneralAgent** | 通用咨询：FAQ、产品概览、服务介绍、协调调度 |
| **ResponseAgent** | 响应处理：解决状态评估、缓存写入、SLA 监控、事件广播 |
| **ReActAgent** | v3.5 推理 Agent：多步推理 + RAG 检索 + Function Calling 工具调用循环 |

### 🔄 5 种协作模式（LangGraph Conditional Edge 动态选择）

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| **Sequential** | 快速通道（complexity < 50） | 单 Agent 处理 |
| **Parallel** | 多领域意图（≥2 domain hints） | 多 Agent 并发（Semaphore 限流）+ 结果聚合 |
| **Consultation** | 高复杂度（≥60）+ 需要补充信息 | 主 Agent + 顾问 Agent 补充信息 |
| **Hierarchical** | 投诉/升级场景 | 协调者分配子任务 + 汇总输出 |
| **ReAct** | 高复杂度（≥60）+ 多领域（≥2） | RAG 检索 + Function Calling 多轮工具调用自主推理 |

### 📡 双层通信（发布/订阅拓扑）

- **MessageBus**：异步 pub/sub 事件总线，支持 topic 订阅（消息日志 deque 限制 1000 条，防止内存泄漏）
- **SharedBlackboard**：TTL 过期的共享键值存储，跨 Agent 状态共享

### 🎯 意图识别与分类

- **双层机制**：LLM Router（语义分析）+ Rule Classifier（预编译正则模式匹配），并行执行
- **复杂度评分**：基于查询长度/意图数/技术术语/金额/标点等多维评分，阈值 50 实现快速/专家通道分流
- **二级缓存**：
  - L1 精确缓存：MD5 哈希 O(1) 匹配 + OrderedDict LRU 淘汰（容量 500）
  - L2 语义缓存：jieba 中文分词 + Jaccard 相似度 + 倒排索引（容量 2000）
  - 动态阈值：短文本（≤20 字）0.7 / 长文本 0.5

### 💬 会话状态管理

- **滑动窗口裁剪**：消息数 + token 数双重控制（tiktoken 精确计数，回退字符估算）
- **历史摘要**：裁剪旧消息通过 LLM 生成 2-3 句异步摘要，注入上下文
- **漂移检测**（4 类）：话题漂移（jieba Jaccard < 0.15）、意图漂移（7 类分类器）、矛盾检测（40+ 组反义词对）、重复提问（相似度 > 0.8）
- **漂移修复**：自动注入修复提示到 Agent 上下文（策略层 + Agent 层双层修复指引）
- **漂移升级**：累计 ≥5 次漂移建议转人工

### 🔍 RAG 知识库（v3.5）

- **ChromaDB 向量检索**：支持内存模式和持久化模式
- **三类知识文档**：产品成分知识（25 条）、常见 FAQ（18 条）、技术支持（15 条）
- **Agent 集成**：ProductAgent、TechAgent 通过 `_retrieve_knowledge()` 自动检索相关知识注入上下文
- **多 collection 查询**：支持跨集合检索（如 `["tech_support", "product_knowledge"]`）

### 🔧 Function Calling 工具调用（v3.5）

- **工具注册中心**：OpenAI Function Calling 格式的工具定义、注册和执行调度
- **4 类 ERP 工具**：`query_product`、`query_inventory`、`query_order`、`query_customer`
- **多轮调用循环**：LLM → tool_calls → 执行工具 → 结果回传 → LLM → 最终回答（最多 3 轮）
- **自动降级**：模型不支持 tools 参数时自动降级为普通调用
- **安全执行**：工具错误返回通用消息，详细异常仅写服务端日志

### 🧠 ReAct 推理模式（v3.5）

- **适用场景**：高复杂度（≥60）+ 多领域交叉查询
- **推理链**：Thought → Action（RAG 检索 / ERP 工具调用）→ Observation → 最终回答
- **最多迭代**：5 步推理（可通过 `REACT_MAX_ITERATIONS` 配置）
- **无 Agent 回退**：ReActAgent 未注册时自动回退到 primary_agent 的 sequential 处理

### ⚡ 高性能异步架构

- **FastAPI + WebSocket** 异步服务层
- **渐进式轮询**：0.3s → 0.5s → 1.0s → 1.5s 进度推送
- **httpx 连接池**：20 keepalive / 100 max connections（FastAPI、LLM 客户端、ERP 适配器统一配置）
- **指数退避重试**：区分瞬时错误（ConnectionError/TimeoutError）和永久错误，最多重试 3 次
- **SLA 监控**：响应时间 5-20 秒目标范围，滑动窗口违约率告警（阈值 30%）
- **CircuitBreaker 熔断器**：三态（CLOSED→OPEN→HALF_OPEN），连续失败 5 次触发熔断，60s 恢复探测
- **缓存前置**：缓存命中时跳过 LLM 路由调用，响应延迟从 2-8s 降至 <10ms

### 🔒 安全加固（v3.4 + v3.6）

| 类别 | 措施 |
|------|------|
| **认证** | API Key 认证默认开启 + `hmac.compare_digest` 防时序攻击 |
| **限流** | 请求限流中间件（60 req/min/IP） |
| **输入验证** | Pydantic 请求模型 + 字段长度约束 + 请求体大小限制 |
| **注入防护** | ERP 输入消毒（SQL LIKE 通配符转义）+ Prompt 注入 XML 标签隔离 |
| **错误脱敏** | 工具执行错误返回通用消息，详细异常仅写服务端日志 |
| **安全头** | HSTS / CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy |
| **会话安全** | session_id UUID 格式校验，防路径遍历 |
| **CORS** | 默认 `http://localhost:8000`，不再使用通配符 `*` |

### 🏭 金蝶 ERP 集成

- 抽象适配器接口 + Mock/Real 工厂模式（配置不完整自动降级到 Mock）
- 真实适配器对接金蝶 Cloud REST API（Token 认证，表单 ID 映射）
- 支持查询：商品信息（BD_MATERIAL）、库存余量（STK_INVENTORY）、订单状态（SAL_ORDER）、客户资料（BD_CUSTOMER）
- Function Calling 集成：ERP 查询可通过工具注册中心暴露为 LLM 可调用工具

### 🐳 Docker 部署

```bash
# 一键启动（含 Redis 7）
docker-compose up -d

# 或手动构建
docker build -t customer-service-ai .
docker run -p 8000:8000 -e OPENAI_API_KEY=sk-xxx customer-service-ai
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 等配置
# 注意：API_KEY_ENABLED 默认为 true，需设置 API_KEY
```

### 3. 启动服务

```bash
# 方式一：直接运行
uvicorn api.app_factory:app --host 0.0.0.0 --port 8000

# 方式二：LangGraph CLI
langgraph up

# 方式三：Docker
docker-compose up -d
```

### 4. 访问

- **前端界面**：http://localhost:8000（暗色主题，含对话 + 监控仪表盘）
- **API 文档**：http://localhost:8000/docs

## 项目结构

```
├── agents/                          # 多 Agent 专家体系
│   ├── base_agent.py                # Agent 抽象基类（漂移检测/重试/Bus/BB/ERP/RAG/FC 集成）
│   ├── product_agent.py             # 产品专家 Agent（RAG 知识库 + ERP）
│   ├── tech_agent.py                # 技术支持 Agent（RAG 知识库检索）
│   ├── billing_agent.py             # 账单专家 Agent（ERP 工具）
│   ├── complaint_agent.py           # 投诉处理 Agent
│   ├── general_agent.py             # 通用咨询 Agent
│   ├── response_agent.py            # 响应处理 Agent（解决状态评估/缓存/SLA/事件）
│   └── react_agent.py               # ReAct 推理 Agent（多步推理 + RAG + FC）
├── router/                          # 双层查询路由器
│   └── query_router.py              # LLM Router ∥ Rule Classifier（并行）+ 复杂度评分
├── collaboration/                   # 协作模式编排
│   ├── modes.py                     # 5 种模式：Sequential/Parallel/Consultation/Hierarchical/ReAct
│   └── orchestrator.py              # 统一模式选择编排器（单一来源）
├── core/                            # 通信与监控基础设施
│   ├── message_bus.py               # 异步 pub/sub 消息总线
│   ├── shared_blackboard.py         # TTL 共享黑板（跨 Agent 状态存储）
│   └── monitoring.py                # MetricsCollector + CircuitBreaker + SLAAlertManager + LLM Client
├── cache/                           # 二级缓存系统
│   └── response_cache.py            # L1 MD5 精确 + L2 jieba Jaccard 语义缓存 + 倒排索引
├── rag/                             # RAG 知识库（v3.5）
│   ├── knowledge_base.py            # ChromaDB 向量存储（内存/持久化）
│   └── seed_data.py                 # 种子数据（产品知识 25 条 + FAQ 18 条 + 技术支持 15 条）
├── tools/                           # Function Calling 工具（v3.5）
│   ├── tool_registry.py             # 工具注册中心（OpenAI FC 格式）
│   └── erp_tools.py                 # ERP 工具封装（query_product/inventory/order/customer）
├── erp/                             # 金蝶 ERP 集成
│   ├── __init__.py                  # KingdeeAdapterBase 抽象接口 + 输入消毒
│   ├── factory.py                   # 适配器工厂（mock/real 自动切换 + 配置校验）
│   ├── kingdee_adapter.py           # Mock 适配器（化妆品行业测试数据）
│   ├── kingdee_real_adapter.py      # 真实金蝶 Cloud API 适配器
│   └── test_kingdee_real.py         # ERP 连接验证工具
├── api/                             # FastAPI 服务层
│   ├── app.py                       # FastAPI 应用（WebSocket/REST/限流/认证/安全头/前端挂载）
│   └── app_factory.py               # uvicorn 入口
├── templates/                       # 前端页面（暗色主题 / WebSocket 实时对话）
│   └── index.html                   # SPA 主页（对话 + 监控双页面）
├── static/                          # 前端静态资源
│   ├── css/style.css                # 设计系统（CSS 变量 / 暗色主题 / 响应式）
│   └── js/
│       ├── api.js                   # API 对接层（WebSocket 管理 / REST 封装）
│       └── chat.js                  # 交互逻辑（消息渲染 / Agent 可视化 / 监控面板）
├── session_manager.py               # 增强会话管理（滑动窗口/摘要/漂移检测 4 类/升级机制）
├── multi_agent_customer_service.py  # LangGraph 图构建 + 节点定义 + 全局实例
├── config.py                        # 统一配置（路由/缓存/会话/漂移/连接池/SLA/ERP/RAG/FC/ReAct/安全）
├── logger.py                        # 结构化日志
├── Dockerfile                       # Docker 构建文件（非 root 用户 + 健康检查）
├── docker-compose.yml               # Docker Compose（应用 + Redis 7）
├── langgraph.json                   # LangGraph CLI 部署配置
└── test_*.py                        # 测试套件（151 passed, 4 skipped）
```

## 架构图

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI + WebSocket 异步服务层                                │
│  (限流 60req/min / 认证 hmac / 安全头 / 渐进式轮询 0.3s→1.5s) │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph（四层状态机）                            │
│                                                              │
│  Layer 0: 缓存检查（入口）                                     │
│  ┌─────────────────────────────┐                             │
│  │ 二级缓存检查                  │                             │
│  │ L1: MD5+LRU / L2: Jaccard   │──[命中]──→ ResponseAgent    │
│  │ (jieba 分词 + 倒排索引)       │          (<10ms 直接返回)    │
│  └────────────┬────────────────┘                             │
│               │[未命中]                                      │
│               ▼                                              │
│  Layer 1: Router                                             │
│  ┌─────────────────────────────────────┐                     │
│  │ LLM Router ∥ Rule Classifier        │  ← 并行执行         │
│  │ (预编译正则 + 单次遍历评分)            │                     │
│  │ + 复杂度评分（阈值 50 分流）          │                     │
│  └────────────────┬────────────────────┘                     │
│                   │                                          │
│                   ▼                                          │
│  Layer 2: Expert Agent（5 种协作模式动态选择）                  │
│  ┌──────┐ ┌──────┐ ┌───────────┐ ┌────────────┐ ┌────────┐ │
│  │Seq.  │ │Para. │ │Consult.   │ │Hierarch.   │ │ ReAct  │ │
│  │<50分 │ │多领域│ │≥60+需补充 │ │投诉/升级    │ │RAG+FC  │ │
│  └──┬───┘ └──┬───┘ └─────┬─────┘ └─────┬──────┘ └───┬────┘ │
│     │        │           │             │             │       │
│     ▼        ▼           ▼             ▼             ▼       │
│  ┌──────────────────────────────────────────────────────────┐│
│  │ 5 类垂直 Agent + ReActAgent                              ││
│  │ Product │ Tech │ Billing │ Complaint │ General │ ReAct   ││
│  │                                                         ││
│  │ RAG: ChromaDB 知识库检索（产品/FAQ/技术支持 58 条文档）    ││
│  │ FC:   Function Calling 工具调用（ERP 查询 4 类工具）       ││
│  │ ERP:  金蝶 Cloud API（商品/库存/订单/客户）               ││
│  └──────────────────────────┬───────────────────────────────┘│
│                             │                                │
│                             ▼                                │
│  Layer 3: ResponseAgent                                      │
│  ┌──────────────────────────────────────────┐                │
│  │ 解决状态评估 → 缓存写入 → SLA 监控 → 事件广播│               │
│  └──────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
                  ┌─────────────┐
                  │  响应输出     │
                  └─────────────┘

通信层: MessageBus (pub/sub) + SharedBlackboard (TTL KV)
监控层: MetricsCollector (async Lock) + CircuitBreaker (三态) + SLAAlertManager
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `WS` | `/ws/chat` | WebSocket 实时对话（Header 认证 + 渐进式状态推送） |
| `POST` | `/api/chat` | REST 对话接口（Pydantic 请求验证） |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/metrics` | 性能监控（含 SLA 详情 + 持久化快照） |
| `GET` | `/api/kpi` | 业务 KPI（首次解决率 / AI 处理率 / 解决率） |
| `GET` | `/api/cache/stats` | 缓存统计（L1/L2 命中率） |
| `GET` | `/api/sessions` | 会话列表 |
| `GET` | `/api/sessions/{id}` | 会话详情 |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |
| `POST` | `/api/feedback` | 客户满意度反馈（Pydantic 验证） |
| `GET` | `/api/alerts` | SLA 告警记录 |
| `GET` | `/api/circuit-breaker` | LLM 熔断器状态 |

## 配置说明

### 核心配置（`.env`）

```bash
# ===== LLM 配置 =====
OPENAI_API_KEY=sk-xxx              # API Key（必填）
OPENAI_BASE_URL=https://api.siliconflow.cn/v1   # 兼容 OpenAI 的 API 地址
OPENAI_MODEL=Qwen/Qwen3-8B        # 模型名称

# ===== 路由配置 =====
ROUTING_COMPLEXITY_THRESHOLD=50    # 复杂度阈值（<50 快速通道，≥50 专家通道）

# ===== 缓存配置 =====
CACHE_L1_MAX=500                   # L1 精确缓存容量
CACHE_L2_MAX=2000                  # L2 语义缓存容量
CACHE_TTL=3600                     # 缓存过期时间（秒）
CACHE_SEMANTIC_THRESHOLD_SHORT=0.7 # 短文本语义相似度阈值（≤20 字）
CACHE_SEMANTIC_THRESHOLD_LONG=0.5  # 长文本语义相似度阈值

# ===== 会话配置 =====
SESSION_WINDOW_SIZE=10             # 滑动窗口大小（消息对数）
SESSION_MAX_TOKENS=4000            # 上下文最大 token 数
SESSION_STORAGE_BACKEND=memory     # 存储后端：memory / file / redis

# ===== 漂移检测 =====
DRIFT_TOPIC_JACCARD_THRESHOLD=0.15 # 话题漂移阈值
DRIFT_REPETITION_THRESHOLD=0.8     # 重复提问阈值
DRIFT_ESCALATION_THRESHOLD=5       # 漂移升级阈值（累计次数）

# ===== 连接池（httpx）=====
HTTPX_MAX_CONNECTIONS=100          # 最大连接数
HTTPX_KEEPALIVE_CONNECTIONS=20     # 保活连接数

# ===== SLA 目标 =====
RESPONSE_TIME_TARGET_MIN=5.0       # 最小响应时间（秒）
RESPONSE_TIME_TARGET_MAX=20.0      # 最大响应时间（秒）

# ===== SLA 告警 =====
SLA_ALERT_WINDOW=50                # 滑动窗口大小
SLA_ALERT_THRESHOLD=30.0           # 违约率告警阈值（%）
SLA_ALERT_COOLDOWN=300             # 告警冷却时间（秒）

# ===== 熔断器 =====
CIRCUIT_BREAKER_FAIL_THRESHOLD=5   # 连续失败次数触发熔断
CIRCUIT_BREAKER_RECOVERY_TIME=60   # 熔断恢复时间（秒）
LLM_ROUTER_TIMEOUT=8.0             # 路由 LLM 调用超时（秒）

# ===== 重试配置 =====
RETRY_MAX_ATTEMPTS=3               # 最大重试次数
RETRY_BASE_DELAY=1.0               # 基础退避延迟（秒）

# ===== 安全配置 =====
API_KEY_ENABLED=true               # 是否启用 API Key 认证（默认开启）
API_KEY=your-secure-api-key-here   # API Key 值
CORS_ORIGINS=http://localhost:8000 # CORS 允许源（逗号分隔）
MAX_QUERY_LENGTH=2000              # 用户查询最大字符数
MAX_SESSIONS=10000                 # 最大内存会话数
SESSION_IDLE_TTL=3600              # 会话空闲过期时间（秒）

# ===== 金蝶 ERP =====
ERP_MODE=mock                      # mock（模拟数据）| real（真实金蝶 API）
ERP_BASE_URL=                      # 金蝶 Cloud API 地址（real 模式必填）
ERP_APP_ID=                        # 应用 ID（real 模式必填）
ERP_APP_SECRET=                    # 应用密钥（real 模式必填）
ERP_DB_ID=                         # 账套 ID（real 模式必填）

# ===== RAG 配置 =====
RAG_PERSIST_DIRECTORY=             # ChromaDB 持久化目录（空则内存模式）
RAG_N_RESULTS=3                    # 检索返回文档数

# ===== Function Calling =====
TOOL_MAX_ROUNDS=3                  # 工具调用最大轮数

# ===== ReAct 配置 =====
REACT_MAX_ITERATIONS=5             # ReAct 最大推理步数
REACT_COMPLEXITY_THRESHOLD=60      # ReAct 模式复杂度阈值

# ===== Redis =====
REDIS_URL=redis://localhost:6379   # Redis 地址（可选，用于 Session/Cache 持久化）
```

### 金蝶 ERP 对接指南

系统默认使用 Mock 适配器（含 5 个化妆品产品、3 个订单、2 个客户的测试数据）。对接真实金蝶 API：

1. 在 `.env` 中设置 `ERP_MODE=real`
2. 配置以下必填项：
   - `ERP_BASE_URL`：金蝶 Cloud API 地址（如 `https://xxx.kingdee.com`）
   - `ERP_APP_ID`：在金蝶开放平台申请的应用 ID
   - `ERP_APP_SECRET`：应用密钥
   - `ERP_DB_ID`：账套 ID
3. 重启服务，系统会自动校验配置完整性
4. 使用验证工具测试连接：
   ```bash
   python -m erp.test_kingdee_real
   ```

配置不完整时系统会自动降级到 Mock 模式并输出诊断日志。ERP 查询同时通过 Agent 直接调用和 Function Calling 工具两种方式暴露给 LLM。

## 测试

```bash
# 全量测试（151 passed, 4 skipped）
python3 -m pytest test_e2e.py test_rag_tools_react.py test_v32_optimizations.py test_v34_optimizations.py -v

# 压力测试（12 tests）
python3 -m pytest test_stress.py -v

# v3.1 改进验证（脚本模式，非 pytest）
python3 test_v31_improvements.py
```

| 测试文件 | 测试数 | 覆盖范围 |
|---------|--------|---------|
| `test_e2e.py` | 68 | 端到端：导入/图构建/路由/缓存/会话/漂移/通信/ERP/协作/API/性能/指标 |
| `test_rag_tools_react.py` | 43 | v3.5：工具注册/ERP 工具/知识库/种子数据/FC 格式/ReAct/图集成/RAG |
| `test_v32_optimizations.py` | 27 | v3.2：CircuitBreaker 状态机/SLA 告警/首次解决率 |
| `test_v34_optimizations.py` | 17 | v3.4：并发安全/安全加固/中文缓存/逻辑修复/安全头 |
| `test_stress.py` | 12 | 压力/性能：缓存高频/总线并发/黑板并发/会话扩展/Agent 顺序/API 压力 |
| `test_v31_improvements.py` | 9 | v3.1：jieba 回退/矛盾检测/意图漂移/token 计数/漂移升级/客户资料 |

所有测试**无需 LLM API Key 或网络**（Mock 适配器 + 内存 ChromaDB + Mock 图执行）。

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent 编排 | LangGraph (StateGraph + Conditional Edge) |
| LLM 调用 | OpenAI Compatible API (httpx.AsyncClient) |
| Web 框架 | FastAPI + WebSocket + Uvicorn |
| 向量数据库 | ChromaDB（RAG 知识库） |
| 中文分词 | jieba（漂移检测 + 语义缓存） |
| Token 计数 | tiktoken（滑动窗口裁剪） |
| 缓存 | 自实现 L1 MD5 + L2 Jaccard（支持 Redis 持久化） |
| ERP | 金蝶 Cloud REST API |
| 容器化 | Docker + Docker Compose + Redis 7 |

## License

Apache 2.0
