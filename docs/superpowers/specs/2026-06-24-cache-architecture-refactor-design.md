# 缓存架构重构设计文档

> **HISTORICAL DESIGN SNAPSHOT (2026-06-24)**
> 这是带日期的设计文档，记录作者当时的设计意图，**不是当前架构权威**。
> Current Truth: `docs/reference/current-state.md`；仅作历史留档。

> 日期：2026-06-24
> 状态：设计阶段
> 影响范围：cache/response_cache.py, core/config.py, core/container.py, core/graph_builder.py, agents/response_agent.py, api/routes/monitoring.py, rag/qdrant_knowledge_base.py

## 1. 背景与动机

### 1.1 当前问题

现有 `ResponseCache` 将 L2 标记为"语义缓存"，但实际使用 Jaccard 相似度（纯词法匹配），存在根本性的概念错误：

| 问题 | 表现 |
|------|------|
| **假阴性** | 「烟酰胺能美白吗」vs「烟酰胺提亮肤色效果」→ 语义一致但 Jaccard 极低（~0.14），漏召回 |
| **假阳性** | 「含有酒精吗」vs「酒精含量多少」→ 语义不同但高频词重合，误命中 |
| **无同义词处理** | Jaccard 基于字符串交集，无法理解"美白"="提亮肤色" |
| **内存存储** | L1/L2 均在 `OrderedDict` 中，重启丢失、多实例不一致 |

### 1.2 设计目标

将缓存系统重构为三层漏斗模型：

```
Query → L1(Redis) → HIT → 返回
                ↓ MISS
         L2(Qdrant + BGE) → HIT → 返回
                ↓ MISS
         L3(Jaccard Fallback) → HIT → 返回
                ↓ MISS
         LLM → 写入 L1+L2 → 返回
```

## 2. 整体架构

```
                    ┌─────────────────────────────────────┐
                    │         ResponseCache                │
                    │  ┌─────────┐  ┌──────────┐  ┌────┐  │
                    │  │  L1:    │  │  L2:     │  │L3: │  │
                    │  │  Redis  │→ │  Qdrant  │→ │Jac │  │
                    │  │  Exact  │  │  Vector  │  │card│  │
                    │  └─────────┘  └──────────┘  └────┘  │
                    └──────┬──────────┬───────────────┬────┘
                           │          │               │
                    ┌──────┴─┐  ┌─────┴──────┐  ┌────┴─────┐
                    │ Redis  │  │  Qdrant    │  │ 内存     │
                    │ Server │  │  Server    │  │ Dict     │
                    └────────┘  └────────────┘  └──────────┘
```

## 3. L1：精确缓存（纯 Redis）

### 3.1 设计决策

**纯 Redis，放弃"内存 + Redis 双写"**。理由：

1. 业务峰值 ~40 QPS，LLM 推理 1~3s，L1 的 0.5ms vs 1ns 差异无实际影响
2. 多实例部署时，本地内存会导致缓存一致性问题（需引入 Redis Pub/Sub 广播失效）
3. 纯 Redis 天然无状态，重启/扩缩容无缝衔接

### 3.2 实现细节

| 项目 | 值 |
|------|-----|
| Key | `MD5(normalize(query))`，标准化规则：去首尾空格、全角→半角、小写 |
| Value | `JSON({"response": "...", "intent_type": "...", "created_at": ..., "expires_at": ...})` |
| TTL | Redis 原生 `EXPIRE`，按 intent_type 从 TTL 映射表读取（见 §5） |
| 客户端 | 复用 `core/config.py` 中 `REDIS_URL` 配置 |

### 3.3 接口

```python
def get(self, query: str, user_role: str = "default",
        intent_type: str = "default") -> str | None: ...
def set(self, query: str, response: str, *, metadata: dict) -> None: ...
```

`get()` 接收 metadata（从 graph state 传入）供 L2/L3 使用；L1 只需要 query 本身做 MD5。

## 4. L2：语义缓存（Qdrant + BGE）

### 4.1 核心组件关系

```
            ┌──────────────────────────────┐
            │  Container.embedding_model    │  ← 单例 SentenceTransformer
            │  BAAI/bge-small-zh-v1.5       │     (BGE, 768维)
            └─────────┬────────────────────┘
                      │ 注入
          ┌───────────┴───────────┐
          ↓                       ↓
  QdrantKnowledgeBase       ResponseCache
  (RAG 知识库)               (L2 语义缓存层)
```

两系统共享同一个 embedding 模型实例，避免重复加载（~500MB 显存/内存）。

### 4.2 Qdrant 集合配置

| 项目 | 值 |
|------|-----|
| Collection 名 | `response_cache` |
| 向量维度 | 768 |
| 距离度量 | Cosine |
| HNSW | m=16, ef_construct=100 |
| 最大点数 | `CACHE_QDRANT_MAX_POINTS`（默认 10000） |

### 4.3 写入 Payload

```json
{
  "response": "烟酰胺可以抑制黑色素转移..."
  "query_text": "烟酰胺能美白吗？",
  "intent_type": "knowledge_qa",
  "product_id": null,
  "user_role": "vip",
  "created_at": 1719200000.0,
  "expires_at": 1719804800.0
}
```

写入时机：LLM 推理完成后，仅 `resolution_status == RESOLUTION_RESOLVED`（当前逻辑保持不变）。

### 4.4 检索 Filter

```python
Filter(must=[
    # 时效性：只返回未过期的
    FieldCondition(key="expires_at", range=Range(gte=current_timestamp)),
    # 权限隔离：当前角色可见的缓存
    FieldCondition(key="user_role", match=MatchValue(value=current_role)),
    # product_id 为可选过滤（§7.1 中主动失效的逆操作）
])
score_threshold = 0.85
limit = 1
```

`product_id` 在检索时**不强制**过滤——没有 product_id 的查询（如"人工客服"）不应被排除。但在主动失效删除时，product_id + intent_type 联合作为精确删除条件。

## 5. TTL 策略

### 5.1 静态 TTL 映射表

| `intent_type` | TTL | 示例查询 |
|---|---|---|
| `knowledge_qa` | 7 天（604800s） | "烟酰胺的作用"、"玻尿酸保湿原理" |
| `pricing_stock` | 5 分钟（300s） | "这个多少钱？"、"还有货吗" |
| `policy_rule` | 24 小时（86400s） | "退换货规则"、"保修政策" |
| `order_status` | 5 分钟（300s） | "我的订单到哪了" |
| `after_sales` | 1 小时（3600s） | "我要投诉"、"怎么退款" |
| `chitchat` | 10 分钟（600s） | "你好"、"谢谢" |
| `default` | 1 小时（3600s） | 未分类或未知类型 |

### 5.2 实现方式

`expires_at = created_at + TTL(intent_type)` 在写入时计算。TTL 映射表定义在 `core/config.py` 中，写入阶段固化，不考虑运行中热加载（v6.x 无此需求）。

### 5.3 过期数据清理

**定时间隔**：每 3600 秒（1 小时）执行一次。

**清理方法**：
```python
client.delete(
    collection_name="response_cache",
    points_selector=Filter(
        must=[FieldCondition(key="expires_at", range=Range(lt=current_timestamp))]
    ),
)
```

**限速**：每次最多清理 1000 条，避免单次任务过重。

**注册位置**：`container.py` 的 `initialize()` 中用 `asyncio.create_task` 启动后台循环。

## 6. Jaccard 降级层（Lexical Fallback）

### 6.1 定位

**Jaccard 被降级为第三层（纯兜底）**，仅在 L2 Qdrant 抛出异常时激活（网络断开、Qdrant 宕机、超时等）。

### 6.2 改动

- 保留现有 `_jaccard`、`_inverted_index`、`_semantic_search` 代码
- 方法重命名为 `_search_jaccard`，并包裹在 try-except 块中
- 所有注释去掉"语义"字样，标记为"词法模糊降级"
- 通过配置 `CACHE_FALLBACK_ENABLED` 可开关
- 降级发生时记录告警日志，累加 Prometheus 指标 `cache_qdrant_fallback_total`

### 6.3 L2 → L3 切换逻辑

```python
async def _search_l2(self, query, query_embedding, metadata):
    try:
        result = await self._search_qdrant(query_embedding, metadata)
        if result:
            return result
    except Exception as e:
        logger.error(f"Qdrant search failed, falling back to Jaccard: {e}")
        self._metrics.fallback_count.inc()

    if config.CACHE_FALLBACK_ENABLED:
        return self._search_jaccard(query)
    return None
```

## 7. 主动失效（Active Invalidation）

### 7.1 事件驱动失效

通过 `MessageBus` 订阅 `product.updated` 事件：

```
ERP 价格变更 → MessageBus → ResponseCache.on_product_updated(event)
                                          ↓
                          client.delete(points_selector=Filter(
                            must=[
                              MatchValue(key="product_id", value="SKU_123"),
                              MatchValue(key="intent_type", value="pricing_stock"),
                            ]
                          ))
```

**关键设计**：删除时必须带 `intent_type` 限制。价格变更不应删除成分知识类的缓存。

### 7.2 管理 API

```

POST /api/cache/invalidate
Body: {"product_id": "SKU_123"}        # 删除该商品所有缓存
Body: {"intent_type": "pricing_stock"} # 删除所有价格类缓存（谨慎）
Body: {"product_id": "SKU_123", "intent_type": "pricing_stock"} # 联合条件
```

## 8. 配置新增

```python
# ===== v6.1: 缓存架构重构 =====
# L1: Redis
CACHE_REDIS_ENABLED = True

# L2: Qdrant 语义缓存
CACHE_QDRANT_COLLECTION = "response_cache"
CACHE_QDRANT_MAX_POINTS = 10_000       # 最大点数，超限触发全量清理
CACHE_VECTOR_SCORE_THRESHOLD = 0.85    # 余弦相似度阈值

# TTL 策略: intent_type → TTL(秒)
CACHE_TTL_POLICY = {
    "knowledge_qa": 604800,
    "pricing_stock": 300,
    "policy_rule": 86400,
    "order_status": 300,
    "after_sales": 3600,
    "chitchat": 600,
    "default": 3600,
}

# L3: Jaccard 降级
CACHE_FALLBACK_ENABLED = True
CACHE_FALLBACK_THRESHOLD = 0.6         # Jaccard 匹配阈值

# 后台清理
CACHE_CLEANUP_INTERVAL = 3600          # 过期数据清理间隔（秒）
```

## 9. 兼容性

### 9.1 接口兼容

`ResponseCache.get(query)` / `.set(query, response)` 签名保持不变，但：

- `get()` 新增可选参数 `metadata: dict | None = None`（向下兼容）
- `set()` 新增可选参数 `metadata: dict | None = None`（向下兼容）

### 9.2 初始化变更

`container.py` 中：

```python
# 之前
self.cache = ResponseCache(l1_max=..., l2_max=...)

# 之后
self.cache = ResponseCache(
    redis_client=self._get_redis_client(),
    qdrant_client=self.knowledge_base.client,   # QdrantKnowledgeBase.client
    embedding_model=self.embedding_model,
    # L1 配置
    l1_ttl_policy=config.CACHE_TTL_POLICY,
    # L2 配置
    l2_collection=config.CACHE_QDRANT_COLLECTION,
    l2_threshold=config.CACHE_VECTOR_SCORE_THRESHOLD,
    l2_max_points=config.CACHE_QDRANT_MAX_POINTS,
    # L3 配置
    fallback_enabled=config.CACHE_FALLBACK_ENABLED,
    fallback_threshold=config.CACHE_FALLBACK_THRESHOLD,
)
```

### 9.3 测试兼容

现有测试路径：

- L1 `test_modules.py` 中 7 个测试：需改为 Mock Redis（但接口断言不变）
- L2 `test_modules.py` 中 3 个测试：需重写为向量检索测试
- E2E `test_all.py` 中 10 个测试：需调整 Mock 模式

## 10. 监控指标

Prometheus 指标（复用现有 `cache_*` 指标家族）：

| 指标名 | 类型 | 说明 |
|--------|------|------|
| `cache_l1_hits_total` | Counter | L1 Redis 命中 |
| `cache_l2_hits_total` | Counter | L2 Qdrant 命中 |
| `cache_misses_total` | Counter | 全层未命中 |
| `cache_l1_size` | Gauge | L1 条目数 |
| `cache_l2_size` | Gauge | L2 条目数 |
| `cache_hit_rate` | Gauge | 总命中率 |
| `cache_qdrant_fallback_total` | Counter | Qdrant 降级到 Jaccard 次数 |
| `cache_qdrant_latency_seconds` | Histogram | Qdrant 检索延迟 |
| `cache_redis_latency_seconds` | Histogram | Redis 检索延迟 |

## 11. 数据流全景

```
用户 Query
    │
    ├─→ normalize(query) → MD5 → Redis GET
    │     │ HIT → 返回
    │     │ MISS
    │     ↓
    ├─→ embedding_model.encode(query)
    │     │
    │     ↓
    │   Qdrant search(query_vector, filter)
    │     │ HIT → 返回
    │     │ FAIL (异常)
    │     ↓
    ├─→ [Fallback] jieba tokenize → inverted_index → Jaccard
    │     │ HIT → 返回
    │     │ MISS
    │     ↓
    ├─→ LLM 推理 → 评估 → 写入 L1(Redis) + L2(Qdrant)
    │
    └─→ (后台) 定时清理过期 Qdrant 点
```

## 12. 变更文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `cache/response_cache.py` | **重写** | 三层架构：L1 Redis + L2 Qdrant(含 BGE) + L3 Jaccard 降级 |
| `core/config.py` | **修改** | 新增 §8 所有配置项，保留并重定向旧配置到新值 |
| `core/container.py` | **修改** | 注入 Redis client / Qdrant client / embedding_model 到 Cache |
| `rag/qdrant_knowledge_base.py` | **修改** | 接受外部 embedding_model 注入，容器级单例 |
| `rag/__init__.py` | **无操作** | 保持兼容 |
| `agents/response_agent.py` | **微调** | `cache.put()` 调用补上 metadata 参数 |
| `core/graph_builder.py` | **微调** | `c.cache.get()` 调用补上 metadata 参数 |
| `api/routes/monitoring.py` | **无操作** | 接口兼容，自动获取新 stats |
| `scripts/` | **可选** | 新增主动失效测试脚本 |
| `tests/unit/test_modules.py` | **修改** | L2 测试从 Jaccard → 向量检索 |
| `tests/e2e/test_all.py` | **修改** | Mock 兼容新接口 |
| `tests/stress/test_stress.py` | **修改** | 兼容新初始化参数 |
