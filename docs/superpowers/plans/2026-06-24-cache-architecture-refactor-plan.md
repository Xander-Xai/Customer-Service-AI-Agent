# 缓存架构重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将响应缓存从「内存 MD5 + 伪语义 Jaccard」重构为「Redis 精确缓存 + Qdrant(BGE) 语义缓存 + Jaccard 降级」三层漏斗模型

**Architecture:**
```
L1(MD5+Redis) → L2(BGE+Qdrant+PayloadFilter) → L3(Jaccard Fallback) → LLM
```
复用容器级单例 BGE embedding 模型，Payload 携带 intent_type/product_id/user_role/expires_at 做业务过滤，后台定时任务清理过期数据，MessageBus 驱动主动失效

**Tech Stack:** Python 3.10+, Redis 5.2.0, Qdrant 1.18.0, sentence-transformers(BAAI/bge-small-zh-v1.5)

---

### Task 1: 新增缓存配置常量

**Files:**
- Modify: `core/config.py:67-71` — 替换旧的缓存配置块；新增 `# ===== v6.1: 缓存架构重构 =====` 块

- [ ] **Step 1: 确认当前配置项位置**

Read `core/config.py` lines 67-71 to see the exact block we're replacing.

- [ ] **Step 2: 替换缓存配置块**

Replace lines 67-71 with new config block:

```python
# ===== v6.1: 缓存架构重构 =====
# L1: Redis 精确缓存
# （复用 REDIS_URL，不用新增变量）

# L2: Qdrant 语义缓存
CACHE_QDRANT_COLLECTION = os.getenv("CACHE_QDRANT_COLLECTION", "response_cache")
CACHE_QDRANT_MAX_POINTS = _int_env("CACHE_QDRANT_MAX_POINTS", 10000)
CACHE_VECTOR_SCORE_THRESHOLD = _float_env("CACHE_VECTOR_SCORE_THRESHOLD", 0.85)

# TTL 策略: intent_type → TTL(秒)
CACHE_TTL_POLICY = {
    "knowledge_qa": 604800,      # 7 天
    "pricing_stock": 300,        # 5 分钟
    "policy_rule": 86400,        # 24 小时
    "order_status": 300,         # 5 分钟
    "after_sales": 3600,         # 1 小时
    "chitchat": 600,             # 10 分钟
    "default": 3600,             # 1 小时
}

# L3: Jaccard 降级
CACHE_FALLBACK_ENABLED = os.getenv("CACHE_FALLBACK_ENABLED", "true").lower() == "true"
CACHE_FALLBACK_THRESHOLD = _float_env("CACHE_FALLBACK_THRESHOLD", 0.6)

# 后台清理
CACHE_CLEANUP_INTERVAL = _int_env("CACHE_CLEANUP_INTERVAL", 3600)  # 秒
```

- [ ] **Step 3: 保留旧常量兼容**

Add backward-compatible aliases right after the new block:

```python
# 向后兼容别名（v6.0 及之前的配置项）
CACHE_L1_MAX = _int_env("CACHE_L1_MAX", 500)       # 不再用于内存 L1，保留避免 ImportError
CACHE_L2_MAX = _int_env("CACHE_L2_MAX", 2000)       # 同上
CACHE_TTL = _int_env("CACHE_TTL", 3600)             # 仅作为 default_ttl 兼容
CACHE_SEMANTIC_THRESHOLD_SHORT = CACHE_VECTOR_SCORE_THRESHOLD  # 重定向
CACHE_SEMANTIC_THRESHOLD_LONG = CACHE_VECTOR_SCORE_THRESHOLD   # 重定向
```

- [ ] **Step 4: 验证没有其他文件直接 import 旧配置**

Run: `grep -rn "CACHE_SEMANTIC_THRESHOLD\|CACHE_L1_MAX\|CACHE_L2_MAX" --include="*.py" | grep -v ".venv" | grep -v "__pycache__"`
Expected: only `core/config.py` and `core/container.py` reference these

- [ ] **Step 5: Commit**

```bash
git add core/config.py
git commit -m "config(v6.1): add cache refactoring configuration constants"
```

---

### Task 2: Embedding 模型容器级单例

**Files:**
- Modify: `rag/qdrant_knowledge_base.py:29-84`
- Test: 现有测试不受影响（兼容旧接口）
- The new `SentenceTransformer` singleton is created in `container.py` (Task 4), not here

- [ ] **Step 1: Read current QdrantKnowledgeBase.__init__**

Read `rag/qdrant_knowledge_base.py` lines 29-84.

- [ ] **Step 2: Add optional `embedding_model` parameter to `__init__`**

Modify the `__init__` signature (line 29) and body to accept an external model:

```python
def __init__(
    self,
    host: str = "localhost",
    port: int = 6333,
    grpc_port: int = 6334,
    prefer_grpc: bool = False,
    api_key: str = "",
    clip_enabled: bool = False,
    embedding_model=None,       # v6.1: 容器级单例
):
    self._clip_enabled = clip_enabled
    self._clip_embed_fn = None
    self._reranker = None

    if embedding_model is not None:
        # v6.1: 使用容器注入的单例模型
        self._embed_fn = embedding_model
        QdrantKnowledgeBase._embed_fn_name = "container-injected"
        logger.info("Embedding 模型使用容器单例")
    else:
        # 保留旧逻辑：自己加载
        self._embed_fn = self._create_embedding_function()
```

Add this import-like guard: after `self._embed_fn` is set, wrap the old loading code path with `if self._embed_fn is None: self._embed_fn = self._create_embedding_function()`. Actually, the cleanest approach is to just add the early branch and shift the existing code:

Insert right after `self._reranker = None` (after line 42):

```python
    if embedding_model is not None:
        self._embed_fn = embedding_model
        QdrantKnowledgeBase._embed_fn_name = "container-injected"
        self._collection_cache: dict[str, bool] = {}
        logger.info("Embedding 模型使用容器单例（共享 bge-small-zh-v1.5）")
    else:
        self._collection_cache: dict[str, bool] = {}
        self._embed_fn = self._create_embedding_function()
```

Then remove lines 42 (`self._collection_cache...`) and the existing `self._embed_fn = self._create_embedding_function()` (currently line 41).

- [ ] **Step 3: Run existing tests to verify backward compatibility**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -m pytest tests/unit/test_qdrant_knowledge_base.py::test_qdrant_init -x -v 2>&1 | tail -20`

Expected: PASS (the test uses default constructor, `embedding_model=None`, falls through to old self-loading logic)

- [ ] **Step 4: Commit**

```bash
git add rag/qdrant_knowledge_base.py
git commit -m "refactor(rag): QdrantKnowledgeBase accepts external embedding model singleton"
```

---

### Task 3: ResponseCache 核心重写

**Files:**
- Rewrite: `cache/response_cache.py`
- Modify: `cache/__init__.py` — 确保 `ResponseCache` 仍导出

- [ ] **Step 1: Read current full file**

Read `cache/response_cache.py` completely (399 lines) to understand all methods.

- [ ] **Step 2: Rewrite the file with new three-layer architecture**

Write `cache/response_cache.py` with the following complete implementation:

<details>
<summary>Click to see full file</summary>

```python
"""
三层缓存系统 + Jaccard 降级（v6.1）
L1: Redis 精确缓存（Hash 索引，原生 TTL）
L2: Qdrant 语义缓存（BGE embedding + Cosine 相似度 + Payload 过滤）
L3: Jaccard 词法模糊降级（仅 Qdrant 不可用时激活）

v6.1 重构：
- 废弃内存 OrderedDict L1，改为纯 Redis
- L2 从 Jaccard 伪语义改为真正的 BGE 向量检索
- Jaccard 降级为 L3 兜底（仅 Qdrant 异常时触发）
- 支持 Payload 过滤（intent_type / user_role / expires_at）
- 后台定时清理过期 Qdrant 点
"""

import hashlib
import json
import time
from collections import defaultdict, deque
from typing import Any

from core import config
from core.logger import get_logger

logger = get_logger("cache")

# v5.4: Prometheus 监控指标
try:
    from prometheus_client import Counter, Gauge, Histogram

    cache_l1_hits = Counter("cache_l1_hits_total", "L1 Redis cache hits")
    cache_l2_hits = Counter("cache_l2_hits_total", "L2 Qdrant cache hits")
    cache_misses = Counter("cache_misses_total", "Cache misses (no match found)")
    cache_fallback_hits = Counter("cache_fallback_hits_total", "L3 Jaccard fallback hits")
    cache_l1_size = Gauge("cache_l1_size", "Number of entries in L1 cache")
    cache_l2_size = Gauge("cache_l2_size", "Number of entries in L2 cache")
    cache_hit_rate = Gauge("cache_hit_rate", "Overall cache hit rate (0-1)")
    cache_operation_duration = Histogram(
        "cache_operation_seconds", "Time spent on cache operations",
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    )
    cache_qdrant_fallback = Counter("cache_qdrant_fallback_total", "Qdrant → Jaccard fallback count")
    cache_qdrant_latency = Histogram(
        "cache_qdrant_latency_seconds", "Qdrant search latency",
        buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
    )
    cache_redis_latency = Histogram(
        "cache_redis_latency_seconds", "Redis search latency",
        buckets=[0.0005, 0.001, 0.002, 0.005, 0.01, 0.025],
    )
    PROMETHEUS_ENABLED = True
except ImportError:
    class _NoopMetric:
        def inc(self, *args, **kwargs): pass
        def set(self, *args, **kwargs): pass
        def observe(self, *args, **kwargs): pass

    cache_l1_hits = cache_l2_hits = cache_misses = cache_fallback_hits = _NoopMetric()
    cache_l1_size = cache_l2_size = cache_hit_rate = _NoopMetric()
    cache_operation_duration = _NoopMetric()
    cache_qdrant_fallback = _NoopMetric()
    cache_qdrant_latency = _NoopMetric()
    cache_redis_latency = _NoopMetric()
    PROMETHEUS_ENABLED = False


class ResponseCache:
    """
    三级响应缓存 + Jaccard 降级（v6.1）

    架构：
      L1 → Redis Hash 精确匹配（<1ms，QDPS 40000+）
      L2 → Qdrant 向量语义检索（BGE embedding + Cosine）
      L3 → Jaccard 词法模糊降级（仅 Qdrant 降级时触发）

    Metadata (写入时从 Graph State 收集):
      intent_type: str — 来自 classify_query 节点
      user_role: str — 来自 RBAC
      product_id: str | None — 来自 NER/实体抽取
    """

    def __init__(
        self,
        redis_client=None,                          # sync Redis client (redis.Redis)
        qdrant_client=None,                         # QdrantClient instance
        embedding_model=None,                       # SentenceTransformer instance
        l1_ttl_policy: dict | None = None,           # intent_type → TTL seconds
        l2_collection: str = "response_cache",
        l2_threshold: float = 0.85,
        l2_max_points: int = 10000,
        fallback_enabled: bool = True,
        fallback_threshold: float = 0.6,
    ):
        # ==== L1: Redis ====
        self._redis = redis_client
        self._l1_prefix = "cache:resp:"

        # ==== L2: Qdrant ====
        self._qdrant = qdrant_client
        self._embed_fn = embedding_model
        self._l2_collection = l2_collection
        self._l2_threshold = l2_threshold
        self._l2_max_points = l2_max_points

        # ==== TTL 策略 ====
        self._ttl_policy = dict(l1_ttl_policy or {})
        if "default" not in self._ttl_policy:
            self._ttl_policy["default"] = 3600

        # ==== L3: Jaccard fallback (always in-memory) ====
        self._fallback_enabled = fallback_enabled
        self._fallback_threshold = fallback_threshold
        self._l3_store: dict[str, tuple[frozenset, str, float]] = {}
        self._l3_order: deque = deque()
        self._l3_counter = 0
        self._l3_inverted: dict[str, set] = defaultdict(set)
        self._l3_max = 500  # Jaccard 降级容器上限

        # ==== 统计 ====
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0, "fallback_hits": 0}
        self._metrics_pending = 0
        self._metrics_last_flush = time.monotonic()
        self._metrics_flush_ops = 25
        self._metrics_flush_seconds = 1.0

        logger.info(
            f"ResponseCache 初始化: L1=Redis L2=Qdrant({l2_collection}, threshold={l2_threshold}) "
            f"L3=Jaccard(fallback={fallback_enabled}, threshold={fallback_threshold})"
        )

    # ============================================================
    # 公共接口（get/set/put/invalidate/clear/get_stats）
    # ============================================================

    def get(self, query: str, metadata: dict | None = None) -> str | None:
        """
        三级缓存查询：L1 Redis → L2 Qdrant → L3 Jaccard

        Args:
            query: 用户查询文本
            metadata: 可选上下文（user_role 等），用于 L2 Payload 过滤

        Returns:
            str | None: 缓存响应或 None
        """
        start = time.time()
        metadata = metadata or {}

        # L1: Redis 精确匹配
        if self._redis:
            redis_start = time.time()
            result = self._redis_get(query)
            cache_redis_latency.observe(time.time() - redis_start)
            if result is not None:
                self._stats["l1_hits"] += 1
                cache_l1_hits.inc()
                cache_operation_duration.observe(time.time() - start)
                self._schedule_metrics_flush()
                return result

        # L2: Qdrant 语义检索
        if self._qdrant and self._embed_fn:
            try:
                qdrant_start = time.time()
                result = self._qdrant_get(query, metadata)
                cache_qdrant_latency.observe(time.time() - qdrant_start)
                if result is not None:
                    self._stats["l2_hits"] += 1
                    cache_l2_hits.inc()
                    cache_operation_duration.observe(time.time() - start)
                    self._schedule_metrics_flush()
                    return result
            except Exception as e:
                logger.warning(f"Qdrant search 异常，降级到 Jaccard: {e}")
                cache_qdrant_fallback.inc()
                # 继续到 L3

        # L3: Jaccard 降级
        if self._fallback_enabled:
            result = self._jaccard_search(query)
            if result is not None:
                self._stats["fallback_hits"] += 1
                cache_fallback_hits.inc()
                cache_operation_duration.observe(time.time() - start)
                self._schedule_metrics_flush()
                return result

        # 全层未命中
        self._stats["misses"] += 1
        cache_misses.inc()
        cache_operation_duration.observe(time.time() - start)
        self._schedule_metrics_flush()
        return None

    def set(self, query: str, response: str, metadata: dict | None = None):
        """写入三级缓存"""
        self._set(query, response, metadata)

    def put(self, query: str, response: str, metadata: dict | None = None):
        """set 的别名，兼容旧 API"""
        self._set(query, response, metadata)

    def invalidate(self, query: str):
        """按查询文本删除 L1 缓存（L2/L3 不做单条删除，靠 TTL 和定时清理）"""
        if self._redis:
            key = self._l1_prefix + self._md5(self._normalize(query))
            self._redis.delete(key)

    def invalidate_by_filter(self, filter_dict: dict[str, Any]):
        """
        按业务条件删除 L2 Qdrant 缓存（主动失效）
        Args:
            filter_dict: {field_name: value}，例如 {"product_id": "SKU_123", "intent_type": "pricing_stock"}
        """
        if not self._qdrant or not filter_dict:
            return
        try:
            from qdrant_client.http import models as qmodels

            conditions = [
                qmodels.FieldCondition(
                    key=k,
                    match=qmodels.MatchValue(value=v),
                )
                for k, v in filter_dict.items()
            ]
            self._qdrant.delete(
                collection_name=self._l2_collection,
                points_selector=qmodels.Filter(must=conditions),
            )
            logger.info(f"主动失效: {filter_dict}")
        except Exception as e:
            logger.warning(f"主动失效失败 {filter_dict}: {e}")

    def clear(self):
        """清空所有缓存"""
        # L1: Redis
        if self._redis:
            try:
                keys = list(self._redis.scan_iter(match=f"{self._l1_prefix}*", count=1000))
                if keys:
                    self._redis.delete(*keys)
            except Exception as e:
                logger.warning(f"Redis 清空失败: {e}")

        # L2: Qdrant — 删除集合重建
        if self._qdrant:
            try:
                from qdrant_client.http import models as qmodels

                self._qdrant.recreate_collection(
                    collection_name=self._l2_collection,
                    vectors_config=qmodels.VectorParams(
                        size=768,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
            except Exception as e:
                logger.warning(f"Qdrant 清空失败: {e}")

        # L3: 内存
        self._l3_store.clear()
        self._l3_order.clear()
        self._l3_inverted.clear()

        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0, "fallback_hits": 0}
        self._update_metrics(force=True)
        logger.info("缓存已全部清空")

    def get_stats(self) -> dict[str, Any]:
        """返回缓存统计信息"""
        total = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["fallback_hits"]
            + self._stats["misses"]
        )
        hit_total = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["fallback_hits"]
        hit_rate = (hit_total / max(total, 1)) * 100

        l1_size = 0
        if self._redis:
            try:
                l1_size = len(list(self._redis.scan_iter(match=f"{self._l1_prefix}*", count=100)))
            except Exception:
                l1_size = -1

        l2_size = 0
        if self._qdrant:
            try:
                l2_size = self._qdrant.count(self._l2_collection).count
            except Exception:
                l2_size = -1

        return {
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "fallback_hits": self._stats["fallback_hits"],
            "misses": self._stats["misses"],
            "total": total,
            "hit_rate": f"{hit_rate:.1f}%",
            "l1_size": l1_size,
            "l2_size": l2_size,
            "l3_size": len(self._l3_store),
        }

    # ============================================================
    # 后台维护
    # ============================================================

    def cleanup_expired(self) -> int:
        """
        清理 L2 Qdrant 中所有 expires_at < now 的点
        Returns: 删除数量
        """
        if not self._qdrant:
            return 0
        try:
            from qdrant_client.http import models as qmodels

            now = time.time()
            result = self._qdrant.delete(
                collection_name=self._l2_collection,
                points_selector=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="expires_at",
                            range=qmodels.Range(lt=now),
                        )
                    ],
                ),
            )
            count = getattr(result, 'count', -1)
            if count > 0:
                logger.info(f"缓存清理: 过期数据 {count} 条已删除")
            return count
        except Exception as e:
            logger.warning(f"缓存清理失败: {e}")
            return 0

    # ============================================================
    # L1: Redis 操作
    # ============================================================

    @staticmethod
    def _normalize(text: str) -> str:
        """查询标准化：去首尾空格、全角→半角、小写"""
        result = text.strip()
        # 全角→半角
        full_to_half = str.maketrans(
            "１２３４５６７８９０ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｘｙｚＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＸＹＺ",
            "1234567890abcdefghijklmnopqrstuxyzABCDEFGHIJKLMNOPQRSTUXZY",
        )
        result = result.translate(full_to_half)
        return result.lower()

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _redis_get(self, query: str) -> str | None:
        """L1: Redis 精确查询"""
        try:
            key = self._l1_prefix + self._md5(self._normalize(query))
            data = self._redis.get(key)
            if data is None:
                return None
            entry = json.loads(data)
            return entry.get("response")
        except Exception as e:
            logger.debug(f"Redis 查询异常: {e}")
            return None

    def _redis_set(self, query: str, response: str, ttl: int):
        """L1: Redis 写入"""
        try:
            key = self._l1_prefix + self._md5(self._normalize(query))
            value = json.dumps({"response": response, "created_at": time.time()}, ensure_ascii=False)
            self._redis.setex(key, ttl, value)
        except Exception as e:
            logger.debug(f"Redis 写入异常: {e}")

    # ============================================================
    # L2: Qdrant 操作
    # ============================================================

    def _embed_query(self, query: str) -> list[float]:
        """使用单例 BGE 模型编码查询"""
        if self._embed_fn is None:
            # 极端降级：使用简单哈希作为向量（仅防止崩溃）
            import hashlib as _h
            seed = int(_h.md5(query.encode()).hexdigest()[:8], 16)
            rng = __import__("random").Random(seed)
            return [rng.random() for _ in range(768)]
        return self._embed_fn.encode([query]).tolist()[0]

    def _qdrant_get(self, query: str, metadata: dict) -> str | None:
        """L2: Qdrant 向量语义检索"""
        from qdrant_client.http import models as qmodels

        query_vector = self._embed_query(query)
        user_role = metadata.get("user_role", "default")
        now = time.time()

        # 构建 filter：永远过滤过期 + 用户角色
        must_conditions = [
            qmodels.FieldCondition(
                key="expires_at",
                range=qmodels.Range(gte=now),
            ),
            qmodels.FieldCondition(
                key="user_role",
                match=qmodels.MatchValue(value=user_role),
            ),
        ]

        search_result = self._qdrant.search(
            collection_name=self._l2_collection,
            query_vector=query_vector,
            query_filter=qmodels.Filter(must=must_conditions),
            limit=1,
            score_threshold=self._l2_threshold,
            with_payload=True,
        )

        if not search_result:
            return None

        best = search_result[0]
        payload = best.payload or {}
        return payload.get("response")

    def _qdrant_set(
        self,
        query: str,
        response: str,
        intent_type: str,
        user_role: str,
        product_id: str | None,
        expires_at: float,
    ):
        """L2: Qdrant 写入"""
        from qdrant_client.http import models as qmodels

        # 确保 collection 存在
        self._ensure_l2_collection()

        query_vector = self._embed_query(query)
        point_id = int(self._md5(query + user_role + intent_type)[:16], 16)

        payload = {
            "response": response,
            "query_text": query[:200],
            "intent_type": intent_type,
            "user_role": user_role,
            "product_id": product_id,
            "created_at": time.time(),
            "expires_at": expires_at,
        }

        self._qdrant.upsert(
            collection_name=self._l2_collection,
            points=[
                qmodels.PointStruct(
                    id=point_id,
                    vector=query_vector,
                    payload=payload,
                )
            ],
        )

    def _ensure_l2_collection(self):
        """确保 L2 Qdrant collection 存在"""
        if not self._qdrant:
            return
        try:
            collections = self._qdrant.get_collections().collections
            if not any(c.name == self._l2_collection for c in collections):
                from qdrant_client.http import models as qmodels

                self._qdrant.create_collection(
                    collection_name=self._l2_collection,
                    vectors_config=qmodels.VectorParams(
                        size=768,
                        distance=qmodels.Distance.COSINE,
                    ),
                    hnsw_config=qmodels.HnswConfigDiff(m=16, ef_construct=100),
                    optimizers_config=qmodels.OptimizersConfigDiff(default_segment_number=2),
                )
                logger.info(f"Qdrant collection '{self._l2_collection}' 已创建")
        except Exception as e:
            logger.warning(f"Qdrant collection 创建失败: {e}")

    # ============================================================
    # L3: Jaccard 词法降级
    # ============================================================

    def _jaccard_search(self, query: str) -> str | None:
        """L3: Jaccard 词法模糊匹配（仅 Qdrant 降级时使用）"""
        tokens = self._tokenize(query)
        if not tokens:
            return None

        candidate_keys: set = set()
        for t in tokens:
            candidate_keys.update(self._l3_inverted.get(t, set()))

        best_score = 0.0
        best_response = None
        now = time.time()

        for ck in candidate_keys:
            if ck not in self._l3_store:
                continue
            cached_tokens, response, ts = self._l3_store[ck]
            # TTL 检查：Jaccard 降级使用固定 1 小时 TTL
            if now - ts >= 3600:
                self._l3_evict_key(ck)
                continue
            score = self._jaccard(tokens, cached_tokens)
            if score >= self._fallback_threshold and score > best_score:
                best_score = score
                best_response = response

        return best_response

    def _jaccard_set(self, query: str, response: str, now: float):
        """写入 Jaccard 降级容器"""
        tokens = self._tokenize(query)
        if len(self._l3_store) >= self._l3_max:
            self._l3_evict()
        self._l3_counter += 1
        key = f"j{self._l3_counter}"
        self._l3_store[key] = (tokens, response, now)
        self._l3_order.append(key)
        for t in tokens:
            self._l3_inverted[t].add(key)

    def _l3_evict(self):
        """L3 淘汰 5% 最旧条目"""
        n = max(1, len(self._l3_order) // 20)
        for _ in range(n):
            if not self._l3_order:
                break
            old_key = self._l3_order.popleft()
            if old_key in self._l3_store:
                tokens = self._l3_store[old_key][0]
                for t in tokens:
                    self._l3_inverted[t].discard(old_key)
                    if not self._l3_inverted[t]:
                        del self._l3_inverted[t]
                del self._l3_store[old_key]

    def _l3_evict_key(self, key: str):
        """L3 删除指定 key（TTL 过期时调用）"""
        if key in self._l3_store:
            tokens = self._l3_store[key][0]
            for t in tokens:
                self._l3_inverted[t].discard(key)
                if not self._l3_inverted[t]:
                    del self._l3_inverted[t]
            del self._l3_store[key]

    def _tokenize(self, text: str) -> frozenset:
        """使用 jieba 分词（与 session_manager 共享）"""
        try:
            from core.session.session_manager import _tokenize_chinese
            return _tokenize_chinese(text)
        except ImportError:
            import re
            tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
            tokens.update(re.findall(r"[一-鿿]+", text))
            return frozenset(tokens)

    @staticmethod
    def _jaccard(set_a: frozenset, set_b: frozenset) -> float:
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union > 0 else 0.0

    # ============================================================
    # 内部方法
    # ============================================================

    def _set(self, query: str, response: str, metadata: dict | None):
        """内部写入方法"""
        metadata = metadata or {}
        intent_type = metadata.get("intent_type", "default")
        user_role = metadata.get("user_role", "default")
        product_id = metadata.get("product_id")

        ttl = self._ttl_policy.get(intent_type, self._ttl_policy["default"])
        now = time.time()
        expires_at = now + ttl

        # L1: Redis
        if self._redis:
            self._redis_set(query, response, ttl)

        # L2: Qdrant
        if self._qdrant and self._embed_fn:
            try:
                self._qdrant_set(query, response, intent_type, user_role, product_id, expires_at)
            except Exception as e:
                logger.debug(f"Qdrant 写入失败: {e}")

        # L3: Jaccard fallback
        self._jaccard_set(query, response, now)

        # 写操作后刷新大小指标
        self._schedule_metrics_flush(force=True)

    def _schedule_metrics_flush(self, force: bool = False):
        """按节奏刷新 Prometheus 指标"""
        self._metrics_pending += 1
        now = time.monotonic()
        if not force:
            pending_ops = self._metrics_pending < self._metrics_flush_ops
            not_due_yet = (now - self._metrics_last_flush) < self._metrics_flush_seconds
            if pending_ops and not_due_yet:
                return
        self._update_metrics(force=force)

    def _update_metrics(self, force: bool = False):
        """更新 Prometheus 监控指标"""
        hit_sum = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["fallback_hits"]
        total = hit_sum + self._stats["misses"]
        if total > 0:
            cache_hit_rate.set(hit_sum / total)

        # L1 大小
        if self._redis:
            try:
                count = len(list(self._redis.scan_iter(match=f"{self._l1_prefix}*", count=100)))
                cache_l1_size.set(count)
            except Exception:
                pass

        # L2 大小
        if self._qdrant:
            try:
                cache_l2_size.set(self._qdrant.count(self._l2_collection).count)
            except Exception:
                pass

        self._metrics_pending = 0
        self._metrics_last_flush = time.monotonic()
```
</details>

- [ ] **Step 3: Verify `cache/__init__.py` exports**

Read `cache/__init__.py` — it should still `from .response_cache import ResponseCache`. No change needed.

- [ ] **Step 4: Import test — basic instantiation**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -c "from cache.response_cache import ResponseCache; c = ResponseCache(); print('OK:', c.get_stats())"`

Expected: `OK: {'l1_hits': 0, 'l2_hits': 0, 'fallback_hits': 0, 'misses': 0, ...}`

- [ ] **Step 5: L3 Jaccard fallback functional test**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -c "
from cache.response_cache import ResponseCache
c = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
c.put('你好', '您好！欢迎咨询')
assert c.get('你好') == '您好！欢迎咨询', 'L3 get failed'
assert c.get('你好呀') == '您好！欢迎咨询', 'L3 Jaccard match failed'
print('L3 Jaccard fallback OK')
"`

Expected: `L3 Jaccard fallback OK`

- [ ] **Step 6: Commit**

```bash
git add cache/response_cache.py
git commit -m "feat(cache): rewrite ResponseCache with L1 Redis + L2 Qdrant + L3 Jaccard fallback"
```

---

### Task 4: 依赖注入容器 — 装配新缓存

**Files:**
- Modify: `core/container.py:64-70` (cache 初始化), 整个 `initialize()` 方法中新增 embedding 模型加载

- [ ] **Step 1: Read current container.py thoroughly**

Read `core/container.py` lines 1-215 to understand the full initialization flow.

- [ ] **Step 2: Add `embedding_model` slot to `__init__`**

After line 118 (`self._legacy_kb`), add:

```python
# v6.1: 容器级单例 Embedding 模型
self.embedding_model: Any = None
```

- [ ] **Step 3: Refactor cache initialization in `__init__`**

Replace the old ResponseCache init (lines 64-70):

```python
# v6.1: ResponseCache 延迟初始化（embedding 模型 / Redis 在 initialize() 中注入）
self.cache: ResponseCache | None = None
```

- [ ] **Step 4: Create Redis client accessor**

Add a helper method after `__init__` (before `initialize`):

```python
def _create_redis_client(self):
    """创建同步 Redis 客户端（可能失败返回 None）"""
    try:
        import redis
        from core.config import REDIS_URL
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        client.ping()
        logger.info("Redis 客户端初始化成功")
        return client
    except Exception as e:
        logger.warning(f"Redis 不可用，L1 缓存将降级: {e}")
        return None
```

- [ ] **Step 5: Load embedding model singleton in `_init_rag_and_tools`**

In `_init_rag_and_tools`, before creating the knowledge base, add:

```python
# v6.1: 加载容器级单例 Embedding 模型
if self.embedding_model is None:
    try:
        from sentence_transformers import SentenceTransformer
        self.embedding_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        logger.info("容器级 Embedding 模型加载完成 (BAAI/bge-small-zh-v1.5)")
    except Exception as e:
        logger.warning(f"Embedding 模型加载失败: {e}")
        self.embedding_model = None
```

Then when creating `QdrantKnowledgeBase`, pass `embedding_model=self.embedding_model` as extra argument.

- [ ] **Step 6: Wire up ResponseCache in `initialize()`**

In `initialize()`, right after Redis and before building the graph, add:

```python
# v6.1: 初始化 ResponseCache（注入 Redis / Qdrant / Embedding）
if self.cache is None:
    from cache.response_cache import ResponseCache
    from core.config import (
        CACHE_CLEANUP_INTERVAL,
        CACHE_FALLBACK_ENABLED,
        CACHE_FALLBACK_THRESHOLD,
        CACHE_QDRANT_COLLECTION,
        CACHE_QDRANT_MAX_POINTS,
        CACHE_TTL_POLICY,
        CACHE_VECTOR_SCORE_THRESHOLD,
    )

    redis_client = self._create_redis_client()
    qdrant_client = getattr(self.knowledge_base, '_client', None) if self.knowledge_base else None

    self.cache = ResponseCache(
        redis_client=redis_client,
        qdrant_client=qdrant_client,
        embedding_model=self.embedding_model,
        l1_ttl_policy=CACHE_TTL_POLICY,
        l2_collection=CACHE_QDRANT_COLLECTION,
        l2_threshold=CACHE_VECTOR_SCORE_THRESHOLD,
        l2_max_points=CACHE_QDRANT_MAX_POINTS,
        fallback_enabled=CACHE_FALLBACK_ENABLED,
        fallback_threshold=CACHE_FALLBACK_THRESHOLD,
    )

    # 后台缓存清理任务
    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(CACHE_CLEANUP_INTERVAL)
                self.cache.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"缓存清理循环异常: {e}")

    self._cache_cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("ResponseCache 初始化完成 (L1=Redis L2=Qdrant L3=Jaccard)")

# 将 cache 注入到 response_agent（如果已创建）
if self.response_agent and hasattr(self.response_agent, 'cache'):
    self.response_agent.cache = self.cache
```

- [ ] **Step 7: Close cleanup task in `close()`**

In `close()` method (line 487), before the main cleanup, add:

```python
# v6.1: 停止缓存清理任务
if hasattr(self, '_cache_cleanup_task') and self._cache_cleanup_task:
    self._cache_cleanup_task.cancel()
    logger.info("  ✅ 缓存清理任务已停止")
```

Then flush Redis connection pools:

```python
# v6.1: 关闭 Redis 连接
if self.cache and hasattr(self.cache, '_redis') and self.cache._redis:
    try:
        self.cache._redis.close()
        logger.info("  ✅ Redis 连接已关闭")
    except Exception as e:
        logger.warning(f"  ⚠️ Redis 关闭异常: {e}")
```

- [ ] **Step 8: Update imports at top of `__init__`**

Remove the old import of ResponseCache (line 64-66) — it's now imported inline in `initialize()`.

- [ ] **Step 9: Verify container starts**

Run: `cd /home/dev/projects/customer-service-ai-agent && make dev 2>&1 | head -30 & sleep 5; kill %1 2>/dev/null`

Expected: starts without ImportError on cache module

- [ ] **Step 10: Commit**

```bash
git add core/container.py
git commit -m "feat(core): wire Redis/Qdrant/Embedding into ResponseCache via DI container"
```

---

### Task 5: 集成点 — graph_builder + response_agent 传递 metadata

**Files:**
- Modify: `core/graph_builder.py:208` — `c.cache.get(query)` → `c.cache.get(query, metadata)`
- Modify: `agents/response_agent.py:239` — `self.cache.put(query, response)` → `self.cache.put(query, response, metadata)`

- [ ] **Step 1: Update `_check_cache_node` in graph_builder.py**

Read `core/graph_builder.py` around line 195-210. Change:

```python
cached = c.cache.get(query)
```

To:

```python
# v6.1: 传递 metadata 供 L2 Qdrant Payload 过滤
cache_metadata = {
    "user_role": state.get("user_role", "default"),
}
cached = c.cache.get(query, metadata=cache_metadata)
```

- [ ] **Step 2: Update `_final_response_node` in graph_builder.py**

Find the `_final_response_node` function (around line 290-340). Locate cache.put calls (lines 325 and 338). Change:

```python
c.cache.put(state["customer_query"], state["response"])
```

To:

```python
# v6.1: 写入缓存时携带完整 metadata
cache_meta = {
    "intent_type": state.get("query_type", "default"),
    "user_role": state.get("user_role", "default"),
    "product_id": state.get("extracted_entities", {}).get("product_id"),
}
c.cache.put(state["customer_query"], state["response"], metadata=cache_meta)
```

Both occurrences (mode upgrade retry line ~325 and regular post-process line ~338).

- [ ] **Step 3: Update response_agent.py cache.put**

Read `agents/response_agent.py` around line 234-240. Change:

```python
self.cache.put(query, response)
```

To:

```python
# v6.1: 写入缓存时携带 Graph State metadata
cache_meta = {
    "intent_type": state.get("query_type", "default"),
    "user_role": state.get("user_role", "default"),
    "product_id": state.get("extracted_entities", {}).get("product_id"),
}
self.cache.put(query, response, metadata=cache_meta)
```

- [ ] **Step 4: Commit**

```bash
git add core/graph_builder.py agents/response_agent.py
git commit -m "feat(cache): pass metadata (intent_type/user_role/product_id) to cache.get/put"
```

---

### Task 6: 增加主动失效 API 端点

**Files:**
- Modify: `api/routes/monitoring.py` — 新增 `POST /api/cache/invalidate` 端点

- [ ] **Step 1: Read monitoring.py to understand endpoint patterns**

Read `api/routes/monitoring.py` lines 1-50 and around 210-220 to see existing cache endpoint.

- [ ] **Step 2: Add invalidate endpoint**

After the `cache_stats` endpoint (~line 217), add:

```python
@router.post("/api/cache/invalidate")
async def invalidate_cache(request: Request, body: dict):
    """按条件删除缓存（主动失效）"""
    _require_monitoring_auth(request)
    cache = getattr(request.app.state, "response_cache", None)
    if not cache:
        return {"error": "cache not initialized"}
    
    # 支持 query/body 两种模式
    filter_dict = {}
    if body.get("product_id"):
        filter_dict["product_id"] = body["product_id"]
    if body.get("intent_type"):
        filter_dict["intent_type"] = body["intent_type"]
    
    if not filter_dict:
        return {"error": "至少提供一个过滤条件 (product_id / intent_type)"}
    
    try:
        cache.invalidate_by_filter(filter_dict)
        return {"status": "ok", "filter": filter_dict}
    except Exception as e:
        return {"error": str(e)}
```

Add the import for `Body` at the top if not already there — in FastAPI, `body: dict` receives the JSON body automatically.

- [ ] **Step 3: Add MessageBus subscriber for active invalidation**

Modify `cache/response_cache.py` to add a bus subscriber method:

At the end of `ResponseCache` class, add:

```python
async def subscribe_to_bus(self, bus):
    """订阅 MessageBus 事件以触发主动失效"""
    await bus.subscribe("product.updated", self._on_product_updated)

async def _on_product_updated(self, event):
    """处理商品更新事件：删除相关定价/库存缓存"""
    try:
        product_id = getattr(event, "data", {}).get("product_id")
        if not product_id:
            return
        self.invalidate_by_filter({
            "product_id": product_id,
            "intent_type": "pricing_stock",
        })
        self.invalidate_by_filter({
            "product_id": product_id,
            "intent_type": "order_status",
        })
        logger.info(f"主动失效: product_id={product_id} (定价/库存缓存已清除)")
    except Exception as e:
        logger.warning(f"主动失效 handler 异常: {e}")
```

Then in `core/container.py`, after creating the cache in `initialize()`:

```python
# v6.1: 订阅主动失效事件
if self.cache and self.bus:
    await self.cache.subscribe_to_bus(self.bus)
```

```bash
git add api/routes/monitoring.py
git commit -m "feat(api): add POST /api/cache/invalidate endpoint for active invalidation"
```

---

### Task 7: 更新测试套件

**Files:**
- Modify: `tests/unit/test_modules.py` — 重写所有 ResponseCache 测试
- Modify: `tests/e2e/test_all.py` — 更新 TestCache 类
- Modify: `tests/stress/test_stress.py` — 更新 TestCachePressure 类

- [ ] **Step 1: Rewrite unit tests for new ResponseCache**

Replace `TestResponseCacheModule` (lines 316-382) and `TestResponseCacheL2AndRedis` (lines 2580-2705) with:

```python
# ═══════════════════════════════════════════════════════════════════════════════
# 3. ResponseCache 模块（v6.1: 三层缓存架构）
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseCacheModule:
    """ResponseCache L1/L2/L3 完整验证（使用 mock Redis + mock Qdrant）"""

    def test_l1_put_get(self):
        """L1: 缓存写入与读取（纯 L3 fallback 模式）"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("你好", "您好！")
        assert cache.get("你好") == "您好！"

    def test_l1_miss(self):
        """L1: 不存在的查询返回 None"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
        assert cache.get("不存在的查询") is None

    def test_l1_normalize_matches_same_query(self):
        """L1: 标准化后相同文本应命中"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put(" 烟酰胺能美白吗 ", "可以")
        # 有空格版本应命中（标准化后一致）
        assert cache.get("烟酰胺能美白吗") == "可以"

    def test_cache_stats(self):
        """缓存统计包含所有三级"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("q1", "r1")
        cache.get("q1")
        cache.get("miss_query_xyz")
        stats = cache.get_stats()
        assert "l1_hits" in stats
        assert "fallback_hits" in stats
        assert "misses" in stats
        assert stats["misses"] >= 1

    def test_jaccard_fallback_match(self):
        """L3: Jaccard 降级语义匹配（词法级）"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("烟酰胺美白", "烟酰胺可以抑制黑色素")
        # 相似查询应命中 L3
        result = cache.get("烟酰胺美白效果")
        assert result == "烟酰胺可以抑制黑色素"

    def test_jaccard_fallback_miss_low_similarity(self):
        """L3: Jaccard 降级不命中（相似度低）"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.99)
        cache.put("red lipstick", "response1")
        result = cache.get("completely different query about skincare routine")
        assert result is None

    def test_clear(self):
        """清空所有缓存"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("c1", "v1")
        cache.put("c2", "v2")
        cache.clear()
        assert cache.get("c1") is None

    def test_invalidate(self):
        """invalidate 删除 L1 缓存"""
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("inv_key", "inv_val")
        cache.invalidate("inv_key")
        # L1 不可查，但 L3 还能找到（invalidate 只删 L1）
        # 此测试验证调用不抛异常
        assert True

    def test_jaccard_zero_sets(self):
        """_jaccard 空集合返回 0"""
        from cache.response_cache import ResponseCache

        assert ResponseCache._jaccard(frozenset(), frozenset()) == 0.0
```

Then replace `TestResponseCacheL2AndRedis` completely (no more Redis-specific tests for unit tests — Redis tests belong in integration):

```python
# ═══════════════════════════════════════════════════════════════════════════════
# ResponseCache — L3 Fallback Coverage
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseCacheFallback:
    """ResponseCache L3 Jaccard 降级 + get_stats + edge cases"""

    def test_jaccard_search_hit(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("red lipstick recommendation", "Product A is great")
        result = cache.get("red lipstick recommendation")
        assert result == "Product A is great"

    def test_l3_eviction(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        for i in range(600):  # L3 max = 500
            cache.put(f"unique_query_{i}_abc", f"response_{i}")
        stats = cache.get_stats()
        assert stats["l3_size"] <= 500

    def test_invalidate_l1_no_error(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        cache.put("inv_key", "inv_val")
        cache.invalidate("inv_key")  # should not raise

    def test_evict_l3_empty(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache()
        cache._l3_evict()  # should not raise

    def test_normalize(self):
        from cache.response_cache import ResponseCache

        assert ResponseCache._normalize("  Hello World  ") == "hello world"
        assert ResponseCache._normalize("Ｈｅｌｌｏ") == "hello"
```

- [ ] **Step 2: Run unit tests**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -m pytest tests/unit/test_modules.py::TestResponseCacheModule -x -v 2>&1 | tail -30`

Expected: All tests PASS

- [ ] **Step 3: Update e2e tests**

Replace `class TestCache` (tests/e2e/test_all.py lines 231-304) with updated version:

```python
class TestCache:
    def test_l3_fallback_hit(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        c.put("test", "result")
        assert c.get("test") == "result"

    def test_l3_fallback_miss(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        assert c.get("nonexistent_xyz_123") is None

    def test_cache_stats(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        stats = c.get_stats()
        assert "l1_size" in stats
        assert "l2_size" in stats
        assert "l3_size" in stats

    def test_cache_eviction(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)
        for i in range(25):
            c.put(f"key_{i}", f"value_{i}")
        # should not crash
        stats = c.get_stats()
        assert stats["l3_size"] <= 25

    def test_invalidate_by_filter_no_crash(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        # No Qdrant client — should not raise
        c.invalidate_by_filter({"product_id": "SKU_123"})
        assert True

    def test_cleanup_expired_no_crash(self):
        from cache.response_cache import ResponseCache

        c = ResponseCache()
        count = c.cleanup_expired()
        assert count == 0
```

- [ ] **Step 4: Update stress tests**

Replace `tests/stress/test_stress.py` `TestCachePressure` class (lines 15-59):

```python
class TestCachePressure:
    """缓存压力测试（使用 L3 Jaccard fallback 模式，无需外部依赖）"""

    def test_cache_high_frequency_read_write(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)

        start = time.time()
        for i in range(2000):
            cache.put(f"query_{i}", f"response_{i}")
        for i in range(2000):
            cache.get(f"query_{i}")
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Cache throughput too slow: {elapsed:.2f}s"

    def test_cache_eviction_under_pressure(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)

        for i in range(500):
            cache.put(f"query_{i}", f"response_{i}")

        stats = cache.get_stats()
        assert stats["l3_size"] <= 500

    def test_cache_concurrent_access(self):
        from cache.response_cache import ResponseCache

        cache = ResponseCache(fallback_enabled=True, fallback_threshold=0.1)

        for i in range(500):
            cache.put(f"q{i}", f"r{i}")
        for i in range(500):
            cache.get(f"q{i}")

        stats = cache.get_stats()
        assert stats["l3_size"] > 0
```

- [ ] **Step 5: Run all cache-related tests**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -m pytest tests/unit/test_modules.py::TestResponseCacheModule tests/unit/test_modules.py::TestResponseCacheFallback tests/e2e/test_all.py::TestCache -x -v 2>&1 | tail -30`

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_modules.py tests/e2e/test_all.py tests/stress/test_stress.py
git commit -m "test(cache): update tests for three-layer cache architecture"
```

---

### Task 8: 更新 `app.py` + `app_factory.py` 中的缓存引用

**Files:**
- Modify: `api/app.py` — `_response_cache` 全局变量兼容新引用
- Modify: `api/app_factory.py` — `app.state.response_cache` 赋值兼容

- [ ] **Step 1: Read api/app.py references**

Search for `_response_cache` in api/app.py:

```bash
grep -n "_response_cache" api/app.py
```

- [ ] **Step 2: Ensure compatibility**

The existing code references `app.state.response_cache` and `_response_cache` global. Since we changed `cache` from a direct `ResponseCache(...)` in `container.__init__` to `None` (set in `initialize()`), we need to ensure that `app_factory.py` accesses cache only after initialization.

Read `api/app_factory.py` lines 55-75 to see how cache is set. If the factory accesses `_container.cache` before `initialize()`, we need to move the assignment after.

- [ ] **Step 3: Commit (if changes needed)**

```bash
git add api/app.py api/app_factory.py
git commit -m "fix(api): ensure cache reference compatibility after lazy init"
```

---

### Task 9: 最终验证 — 全量测试

- [ ] **Step 1: Run unit tests**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -m pytest tests/unit/ -x --timeout=60 -q 2>&1 | tail -40`

Expected: At least 95% of existing tests PASS (intentional failures may come from tests that relied on old L2 semantic matching behavior)

- [ ] **Step 2: Run stress tests**

Run: `cd /home/dev/projects/customer-service-ai-agent && python -m pytest tests/stress/test_stress.py::TestCachePressure -x -v 2>&1 | tail -20`

Expected: All 3 cache stress tests PASS

- [ ] **Step 3: Run lint**

Run: `cd /home/dev/projects/customer-service-ai-agent && make lint 2>&1 | tail -20`

Expected: No new lint errors in cache/response_cache.py

- [ ] **Step 4: Full test suite with coverage (optional)**

Run: `cd /home/dev/projects/customer-service-ai-agent && make test-cov 2>&1 | tail -30`

Expected: Coverage >= 80%
