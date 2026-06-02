# 多智能体客服系统 (Customer Service AI Agent v3.3)

面向化妆品生产企业的基于 **LangGraph** 多 Agent 协作问答系统，实现 Router → 专家 Agent → ResponseAgent 三层状态机动态路由。

> **v3.3 性能优化版**：缓存检查前置、路由并行化、正则预编译、内存泄漏修复等 7 项优化，详见[优化记录](#v33-性能优化记录)。

## 核心特性

### 🏗️ 多 Agent 架构

| 组件 | 说明 |
|------|------|
| **Router Agent** | LLM Router + Query Classifier 双层意图识别 + 复杂度评分（阈值 50） |
| **ProductAgent** | 产品专家：成分分析、功效查询、价格对比、库存查询（对接金蝶 ERP） |
| **TechAgent** | 技术支持：使用指导、过敏处理、产品搭配、保质期 |
| **BillingAgent** | 账单专家：退款处理、订单查询、发票管理、物流追踪（对接金蝶 ERP） |
| **ComplaintAgent** | 投诉处理：情绪安抚、问题解决、补偿方案、升级处理 |
| **GeneralAgent** | 通用咨询：FAQ、产品概览、服务介绍、协调调度 |
| **ResponseAgent** | 响应处理：缓存写入、会话记录、SLA 监控、事件广播 |

### 🔄 4 种协作模式（LangGraph Conditional Edge 动态选择）

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| **Sequential** | 快速通道（complexity < 50） | 单 Agent 处理 |
| **Parallel** | 多领域意图（≥2 domain hints） | 多 Agent 并发 + 结果聚合 |
| **Consultation** | 高复杂度（≥60）+ 需要补充信息 | 主 Agent + 顾问 Agent |
| **Hierarchical** | 投诉/升级场景 | 协调者分配子任务 + 汇总 |

### 📡 双层通信（Mesh 拓扑）

- **MessageBus**：异步 pub/sub 事件总线，支持 topic 订阅和 request/response 模式（v3.3: 消息日志 deque 限制 1000 条，防止内存泄漏）
- **SharedBlackboard**：TTL 过期的共享键值存储，跨 Agent 状态共享

### 🎯 意图识别与分类

- **双层机制**：LLM Router（语义分析）+ Query Classifier（规则模式匹配）
- **复杂度评分**：基于查询长度/意图数/技术术语/金额/标点等多维评分，阈值 50 实现快速/专家通道分流
- **二级缓存**：
  - L1 精确缓存：MD5 哈希 O(1) 匹配 + OrderedDict LRU 淘汰（容量 500）
  - L2 语义缓存：Jaccard 相似度 + 倒排索引（容量 2000）
  - 动态阈值：短文本 0.7 / 长文本 0.5

### 💬 会话状态管理

- **滑动窗口裁剪**：消息数 + token 数双重控制（tiktoken 精确计数）
- **历史摘要**：裁剪旧消息通过 LLM 生成 2-3 句摘要
- **漂移检测**（4 类）：话题漂移、意图漂移、矛盾检测、重复提问
- **漂移修复**：自动注入修复提示到 Agent 上下文
- **漂移升级**：累计 ≥5 次漂移建议转人工

### ⚡ 高性能异步架构

- **FastAPI + WebSocket** 异步服务层
- **渐进式轮询**：0.3s → 0.5s → 1.0s → 1.5s 进度推送
- **httpx 连接池**：20 keepalive / 100 max connections（FastAPI、LLM 客户端、ERP 适配器统一配置）
- **指数退避重试**：1s → 2s → 4s（Agent 层、LLM 客户端层、通用装饰器层）
- **SLA 监控**：响应时间 5-20 秒目标范围，违约自动告警
- **v3.3 缓存前置**：缓存命中时跳过 LLM 路由调用，响应延迟从 2-8s 降至 <10ms
- **v3.3 路由并行化**：LLM 分类与规则分类 `asyncio.gather` 并行执行
- **v3.3 正则预编译**：路由规则在模块加载时编译，避免每次请求重复编译

### 🏭 金蝶 ERP 集成

- 抽象适配器接口 + Mock/Real 工厂模式
- 真实适配器对接金蝶 Cloud REST API（HMAC 认证）
- 支持查询：商品信息、库存余量、订单状态、客户资料

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
```

### 3. 启动服务

```bash
# 方式一：直接运行（v3.0 多 Agent 模式）
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
│   ├── base_agent.py                # Agent 抽象基类（漂移检测/重试/Bus/BB/ERP 集成）
│   ├── product_agent.py             # 产品专家 Agent
│   ├── tech_agent.py                # 技术支持 Agent
│   ├── billing_agent.py             # 账单专家 Agent
│   ├── complaint_agent.py           # 投诉处理 Agent
│   ├── general_agent.py             # 通用咨询 Agent
│   └── response_agent.py            # 响应处理 Agent（Router→Expert→Response 第三层）
├── router/                          # 双层查询路由器
│   └── query_router.py              # LLM Router + Rule Classifier + 复杂度评分
├── collaboration/                   # 协作模式编排
│   ├── modes.py                     # 4 种模式：Sequential/Parallel/Consultation/Hierarchical
│   └── orchestrator.py              # 统一模式选择编排器
├── core/                            # 通信基础设施
│   ├── message_bus.py               # 异步 pub/sub 消息总线（Mesh 拓扑）
│   └── shared_blackboard.py         # TTL 共享黑板（跨 Agent 状态存储）
├── cache/                           # 二级缓存系统
│   └── response_cache.py            # L1 MD5 精确 + L2 Jaccard 语义缓存 + 倒排索引
├── session_manager.py               # 增强会话管理（滑动窗口/摘要/漂移检测 4 类）
├── erp/                             # 金蝶 ERP 集成
│   ├── __init__.py                  # KingdeeAdapterBase 抽象接口
│   ├── factory.py                   # 适配器工厂（mock/real 自动切换 + 配置校验）
│   ├── kingdee_adapter.py           # Mock 适配器（化妆品行业测试数据）
│   ├── kingdee_real_adapter.py      # 真实金蝶 Cloud API 适配器
│   └── test_kingdee_real.py         # ERP 连接验证工具
├── api/                             # FastAPI 服务层
│   ├── app.py                       # FastAPI 应用（WebSocket/REST/CORS/Auth/前端挂载）
│   └── app_factory.py               # uvicorn 入口
├── templates/                       # 前端页面（暗色主题 / WebSocket 实时对话）
│   └── index.html                   # SPA 主页（对话 + 监控双页面）
├── static/                          # 前端静态资源
│   ├── css/style.css                # 设计系统（CSS 变量 / 暗色主题 / 响应式）
│   └── js/
│       ├── api.js                   # API 对接层（WebSocket 管理 / REST 封装）
│       └── chat.js                  # 交互逻辑（消息渲染 / Agent 可视化 / 监控面板）
├── multi_agent_customer_service.py  # LangGraph 图构建 + 节点定义 + 全局实例
├── config.py                        # 统一配置（路由/缓存/会话/漂移/连接池/SLA/ERP/Redis）
├── logger.py                        # 结构化日志
├── Dockerfile                       # Docker 构建文件
├── docker-compose.yml               # Docker Compose（应用 + Redis）
├── langgraph.json                   # LangGraph CLI 部署配置
└── test_*.py                        # 测试套件（e2e/stress/v31/v32）
```

## API 端点

### v3.0 多 Agent 模式（核心）

| 方法 | 路径 | 说明 |
|------|------|------|
| `WS` | `/ws/chat` | WebSocket 实时对话（含渐进式状态推送） |
| `POST` | `/api/chat` | REST 对话接口 |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/metrics` | 性能监控（含 SLA 详情） |
| `GET` | `/api/kpi` | 业务 KPI（首次解决率/AI 处理率） |
| `GET` | `/api/cache/stats` | 缓存统计（L1/L2 命中率） |
| `GET` | `/api/sessions` | 会话列表 |
| `GET` | `/api/sessions/{id}` | 会话详情 |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |
| `POST` | `/api/feedback` | 客户满意度反馈 |
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
CACHE_SEMANTIC_THRESHOLD_SHORT=0.7 # 短文本语义相似度阈值
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

# ===== 重试配置 =====
RETRY_MAX_ATTEMPTS=3               # 最大重试次数
RETRY_BASE_DELAY=1.0               # 基础退避延迟（秒）

# ===== 金蝶 ERP =====
ERP_MODE=mock                      # mock（模拟数据）| real（真实金蝶 API）
ERP_BASE_URL=                      # 金蝶 Cloud API 地址（real 模式必填）
ERP_APP_ID=                        # 应用 ID（real 模式必填）
ERP_APP_SECRET=                    # 应用密钥（real 模式必填）
ERP_DB_ID=                         # 账套 ID（real 模式必填）

# ===== Redis =====
REDIS_URL=redis://localhost:6379   # Redis 地址（可选，用于 Session/Cache 持久化）

# ===== 安全 =====
API_KEY_ENABLED=false              # 是否启用 API Key 认证
API_KEY=                           # API Key 值
CORS_ORIGINS=*                     # CORS 允许源（逗号分隔）
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

配置不完整时系统会自动降级到 Mock 模式并输出诊断日志。

## 架构图

```
用户消息
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI + WebSocket 异步服务层                                │
│  (渐进式轮询 0.3s→1.5s / httpx 连接池 20+100)                 │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph (三层状态机 v3.3 缓存前置)               │
│                                                              │
│  Layer 0: 缓存检查（入口）                                     │
│  ┌─────────────────────────────┐                             │
│  │ 二级缓存检查                  │                             │
│  │ L1: MD5+LRU / L2: Jaccard   │──[命中]──→ ResponseAgent    │
│  └────────────┬────────────────┘          (<10ms 直接返回)    │
│               │[未命中]                                      │
│               ▼                                              │
│  Layer 1: Router                                             │
│  ┌─────────────────────────────────────┐                     │
│  │ LLM Router ∥ Rule Classifier        │  ← 并行执行         │
│  │ (正则预编译 + 单次遍历评分)           │                     │
│  │ + 复杂度评分（阈值 50 分流）          │                     │
│  └────────────────┬────────────────────┘                     │
│                   │                                          │
│                   ▼                                          │
│  Layer 2: Expert Agent (动态选择协作模式)                       │
│  ┌──────┐ ┌──────┐ ┌───────────┐ ┌────────────┐             │
│  │Seq.  │ │Para. │ │Consult.   │ │Hierarch.   │             │
│  │<50分 │ │多领域│ │≥60+需补充 │ │投诉/升级    │             │
│  └──┬───┘ └──┬───┘ └─────┬─────┘ └─────┬──────┘             │
│     │        │           │             │                     │
│     ▼        ▼           ▼             ▼                     │
│  ┌──────────────────────────────────────────────┐            │
│  │ 5 类垂直 Agent                                │            │
│  │ Product │ Tech │ Billing │ Complaint │ General│            │
│  │ (对接金蝶 ERP: 商品/库存/订单/客户)            │            │
│  └──────────────────┬───────────────────────────┘            │
│                     │                                        │
│                     ▼                                        │
│  Layer 3: ResponseAgent                                      │
│  ┌──────────────────────────────────────────┐                │
│  │ 缓存写入 → 会话记录 → SLA 监控 → 事件广播  │                │
│  └──────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
                  ┌─────────────┐
                  │  响应输出     │
                  └─────────────┘

通信层: MessageBus (Mesh pub/sub, deque≤1000) + SharedBlackboard (TTL KV)
监控层: MetricsCollector (TTL 会话清理) + CircuitBreaker + SLAAlertManager
```

## 测试

```bash
# 端到端测试（12 类 ~80 方法）
pytest test_e2e.py -v

# 压力测试
pytest test_stress.py -v

# v3.0 集成冒烟测试
pytest test_v3_integration.py -v

# v3.1 改进验证
pytest test_v31_improvements.py -v

# v1.0 子系统测试
pytest tests/ -v
```

## v3.3 性能优化记录

### 🔴 高优先级 — 直接降低响应延迟

| # | 优化项 | 修改文件 | 方案 | 预期效果 |
|---|--------|---------|------|---------|
| 1 | **缓存检查前置** | `multi_agent_customer_service.py` | LangGraph 图入口从 `classify_query` 改为 `check_cache`，缓存命中直接跳到 `final_response`，跳过 LLM 路由调用 | 缓存命中响应 **2-8s → <10ms** |
| 2 | **移除重复消息写入** | `multi_agent_customer_service.py` | 删除 `classify_query_node` 中的 `add_message` 调用，由各 Agent 的 `_process_with_llm` 统一写入 | 消除会话窗口无效膨胀 |
| 3 | **路由正则预编译 + 并行** | `router/query_router.py` | 正则模式在模块加载时 `re.compile`；`_rule_classify` 与 `_score_complexity` 合并为 `_rule_classify_and_score` 单次遍历；LLM 分类与规则分类用 `asyncio.gather` 并行执行 | 路由 CPU 开销 **↓40%**，延迟 **↓** |

### 🟡 中优先级 — 防止内存泄漏

| # | 优化项 | 修改文件 | 方案 | 预期效果 |
|---|--------|---------|------|---------|
| 4 | **消息日志限制** | `core/message_bus.py` | `_message_log` 从 `list` 改为 `deque(maxlen=1000)` | 长期运行内存不再无限增长 |
| 5 | **会话统计过期清理** | `core/monitoring.py` | 新增 `session_last_activity` 时间戳追踪 + `_cleanup_expired_sessions` 方法，每 100 次请求清理 1 小时过期的会话统计 | 防止 `session_turn_counts` 字典无限膨胀 |

### 🟢 低优先级 — 小幅优化

| # | 优化项 | 修改文件 | 方案 | 预期效果 |
|---|--------|---------|------|---------|
| 6 | **缓存 L1 LRU 淘汰** | `cache/response_cache.py` | L1 从 `dict` 改为 `OrderedDict`，`get` 时 `move_to_end`，淘汰时 `popitem(last=False)` | 淘汰从 O(n log n) 排序降至 O(1) |
| 7 | **去掉并行 peer 注入** | `collaboration/modes.py` | 移除并行模式中每个 Agent 完成后读取所有 peer Blackboard 结果并拼接到响应的逻辑 | 避免输出膨胀和重复计算 |

### 请求流程对比

**v3.0（优化前）：**
```
请求 → LLM路由分类(2-8s) → 缓存检查 → [命中] → 响应（前面的LLM调用浪费）
                                → [未命中] → 协作模式 → 响应
```

**v3.3（优化后）：**
```
请求 → 缓存检查 → [命中] → 响应（<10ms，跳过LLM）
                 → [未命中] → LLM路由 ∥ 规则路由(并行) → 协作模式 → 响应
```

## License

Apache 2.0
