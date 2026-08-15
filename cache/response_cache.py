"""
三级缓存系统 (v6.0)
L1: Redis 精确缓存 (MD5 哈希) - 分布式持久化
L2: Qdrant 向量缓存 (BGE 嵌入) - 语义搜索
L3: Jaccard 相似度缓存 (jieba 分词) - 内存回退

架构设计：
- L1 Cache: Redis SETEX + MD5 精确匹配，O(1)查找，适合完全相同的查询
- L2 Cache: Qdrant 向量检索 + 多维度 payload 过滤，支持语义相似度搜索
- L3 Cache: Jaccard 相似度 + 倒排索引 + jieba 分词，纯内存回退层

v6.0 重写：
- L1: OrderedDict -> Redis (SETEX + MD5 标准化)
- L2: Jaccard 误标为语义 -> Qdrant 向量搜索 (BGE 嵌入 + Filter)
- L3: 保留 Jaccard 作为最终回退层，提升容错率

P0-02 (Cache Cross-User Leakage):
- 三层统一遵守 CachePolicy（cacheable/scope/ttl/sensitivity/version）。
- 个性化回答（订单/退款/投诉/售后）写入用户作用域，跨用户隔离；
  无身份时 fail closed（不写共享槽）。
- 读取端在路由分类前执行，同时探测 SHARED 与调用方身份作用域；
  因个性化数据永不落入 SHARED，OR 探测不会泄漏。

缓存策略：
- L1 过期：TTL 按意图类型配置（Redis EXPIRE 自动处理）
- L2 过期：expires_at 时间戳过滤（Qdrant Range 条件）
- L3 淘汰：FIFO 每次淘汰 5%（最大 500 条）
- 降级路径：Redis 失败 -> Qdrant -> Jaccard -> LLM 调用
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import random
import time
from collections import defaultdict, deque

from cache.cache_policy import (
    CACHE_PAYLOAD_VERSION,
    read_scope_keys,
    resolve_cache_policy,
)
from core.logger import get_logger
from core.session.token_counter import _tokenize_chinese as _tokenize

logger = get_logger("cache")

# ===== Prometheus 监控指标 (v5.4) =====
try:
    from prometheus_client import REGISTRY
    from prometheus_client import Counter as _PromCounter
    from prometheus_client import Gauge as _PromGauge
    from prometheus_client import Histogram as _PromHistogram

    _METRICS: dict[str, object] = {}

    def _counter(name, documentation, *args, **kwargs):
        """Create or retrieve a Counter metric (safe for re-import/test sessions)."""
        if name not in _METRICS:
            try:
                _METRICS[name] = _PromCounter(name, documentation, *args, **kwargs)
            except ValueError:
                with contextlib.suppress(KeyError, ValueError):
                    REGISTRY.unregister(name)
                _METRICS[name] = _PromCounter(name, documentation, *args, **kwargs)
        return _METRICS[name]

    def _gauge(name, documentation, *args, **kwargs):
        """Create or retrieve a Gauge metric (safe for re-import/test sessions)."""
        if name not in _METRICS:
            try:
                _METRICS[name] = _PromGauge(name, documentation, *args, **kwargs)
            except ValueError:
                with contextlib.suppress(KeyError, ValueError):
                    REGISTRY.unregister(name)
                _METRICS[name] = _PromGauge(name, documentation, *args, **kwargs)
        return _METRICS[name]

    def _histogram(name, documentation, *args, **kwargs):
        """Create or retrieve a Histogram metric (safe for re-import/test sessions)."""
        if name not in _METRICS:
            try:
                _METRICS[name] = _PromHistogram(name, documentation, *args, **kwargs)
            except ValueError:
                with contextlib.suppress(KeyError, ValueError):
                    REGISTRY.unregister(name)
                _METRICS[name] = _PromHistogram(name, documentation, *args, **kwargs)
        return _METRICS[name]

    # 原始缓存命中统计（保持向后兼容）
    # 从 core.monitoring 导入以避免重复注册同名 Prometheus 指标
    try:
        from core.monitoring import cache_l1_hits_total as _l1
        from core.monitoring import cache_l2_hits_total as _l2
        cache_l1_hits = _l1
        cache_l2_hits = _l2
    except (ImportError, AttributeError):
        cache_l1_hits = _counter("cache_l1_hits_total", "L1 Redis exact match cache hits")
        cache_l2_hits = _counter("cache_l2_hits_total", "L2 Qdrant vector cache hits")
    cache_misses = _counter("cache_misses_total", "Cache misses")

    # v6.0 新指标
    cache_fallback_hits = _counter(
        "cache_fallback_hits_total",
        "L3 Jaccard fallback cache hits",
    )
    cache_qdrant_fallback_total = _counter(
        "cache_qdrant_fallback_total",
        "Qdrant to Jaccard fallback events (Qdrant failures)",
    )

    # 缓存大小监控
    cache_l1_size = _gauge("cache_l1_size", "Number of entries in L1 Redis cache")
    cache_l2_size = _gauge("cache_l2_size", "Number of entries in L2 Qdrant cache")

    # 缓存命中率
    cache_hit_rate = _gauge("cache_hit_rate", "Overall cache hit rate (0-1)")

    # 缓存操作延迟
    cache_operation_duration = _histogram(
        "cache_operation_seconds",
        "Time spent on cache operations",
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    )

    # v6.0 新延迟指标
    cache_qdrant_latency = _histogram(
        "cache_qdrant_latency_seconds",
        "Qdrant search latency",
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
    )
    cache_redis_latency = _histogram(
        "cache_redis_latency_seconds",
        "Redis get/set latency",
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
    )

    PROMETHEUS_ENABLED = True
except ImportError:
    # Prometheus 未安装，降级为无操作
    class _NoopMetric:
        def inc(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    cache_l1_hits = cache_l2_hits = cache_misses = _NoopMetric()
    cache_fallback_hits = _NoopMetric()
    cache_qdrant_fallback_total = _NoopMetric()
    cache_l1_size = cache_l2_size = cache_hit_rate = _NoopMetric()
    cache_operation_duration = _NoopMetric()
    cache_qdrant_latency = _NoopMetric()
    cache_redis_latency = _NoopMetric()
    PROMETHEUS_ENABLED = False


# ===== 默认 TTL 策略 =====
_DEFAULT_TTL_POLICY: dict[str, int] = {
    "knowledge_qa": 604800,    # 7 天
    "pricing_stock": 300,      # 5 分钟
    "policy_rule": 86400,      # 1 天
    "order_status": 300,       # 5 分钟
    "after_sales": 3600,       # 1 小时
    "chitchat": 600,           # 10 分钟
    "default": 3600,           # 1 小时
}

# ===== L3 常量 =====
_L3_MAX_SIZE = 500
_RANDOM_VECTOR_DIM = 1024


class ResponseCache:
    """
    三级响应缓存

    架构：
    - L1 (Redis): MD5 精确匹配，分布式持久化，TTL 按意图类型配置
    - L2 (Qdrant): 向量语义搜索，支持 user_role / product_id 等 metadata 过滤
    - L3 (Jaccard): 内存回退层，基于 jieba 分词 + 倒排索引，纯 Python 实现

    降级路径：
        Redis 不可用 -> Qdrant 不可用 -> Jaccard 内存匹配 -> LLM 生成

    v6.0 重构：
    - L1 从 OrderedDict 改为 Redis
    - L2 从 Jaccard（误标为语义）改为 Qdrant 向量搜索
    - L3 保留 Jaccard 作为最终回退层
    - 新增 cleanup_expired() 定时清理 Qdrant 过期条目
    - 新增 invalidate_by_filter() 按 payload 条件批量删除
    """

    def __init__(
        self,
        redis_client=None,
        qdrant_client=None,
        embedding_model=None,
        l1_ttl_policy: dict | None = None,
        l2_collection: str = "response_cache",
        l2_threshold: float = 0.85,
        l2_max_points: int = 10000,
        fallback_enabled: bool = True,
        fallback_threshold: float = 0.6,
        content_version: str = "1",
        # v6.3: 向后兼容参数（旧测试使用 l1_max/l2_max/default_ttl）
        l1_max: int | None = None,
        l2_max: int | None = None,
        default_ttl: int | None = None,
    ):
        """
        初始化三级缓存

        Args:
            redis_client: redis.Redis 同步客户端（None 时跳过 L1）
            qdrant_client: QdrantClient 实例（None 时跳过 L2）
            embedding_model: SentenceTransformer 编码模型（None 时使用随机向量回退）
            l1_ttl_policy: 按 intent_type 映射 TTL 秒数的字典
            l2_collection: Qdrant 集合名称
            l2_threshold: 向量搜索相似度阈值（默认 0.85）
            l2_max_points: Qdrant 集合最大点数
            fallback_enabled: 是否启用 L3 Jaccard 回退
            fallback_threshold: Jaccard 相似度匹配阈值（默认 0.6）
            content_version: P0-02 内容版本，提升即整体失效旧缓存条目
            l1_max: 向后兼容 — 无 Redis 时设置内存缓存最大条目数
            l2_max: 向后兼容 — 设置 Qdrant 最大点数（映射到 l2_max_points）
            default_ttl: 向后兼容 — 设置默认 TTL（映射到 l1_ttl_policy["default"])
        """
        self._redis = redis_client
        self._qdrant = qdrant_client
        self._embed_fn = embedding_model
        self._l1_prefix = "cache:resp:"
        self._l1_ttl_policy = (l1_ttl_policy or _DEFAULT_TTL_POLICY.copy())
        # v6.3: default_ttl 映射到 l1_ttl_policy["default"]
        if default_ttl is not None:
            self._l1_ttl_policy["default"] = default_ttl
        self._l2_collection = l2_collection
        self._l2_threshold = l2_threshold
        # v6.3: l2_max 映射到 l2_max_points
        self._l2_max_points = l2_max if l2_max is not None else l2_max_points
        self._fallback_enabled = fallback_enabled
        self._fallback_threshold = fallback_threshold
        # v6.3: l1_max 控制无 Redis 时内存缓存上限
        self._l3_max_size = l1_max if l1_max is not None else _L3_MAX_SIZE

        # P0-02: 统一缓存版本（payload schema 版本 + 内容版本）。
        # 三层写入与读取均以该版本为匹配条件，提升版本即可整体失效旧条目。
        self._content_version = content_version
        self._version = f"{CACHE_PAYLOAD_VERSION}:{content_version}"

        # L3: Jaccard 内存缓存（继承原 L2 逻辑）
        # P0-02: 元组增加 scope_key / version / ttl，实现跨用户隔离、版本失效与 TTL parity。
        self._l3_cache: dict[str, tuple[frozenset, str, float, str, str, int]] = {}
        self._l3_order: deque = deque()
        self._l3_counter = 0
        self._l3_inverted_index: dict[str, set] = defaultdict(set)

        # 统计计数
        self._stats: dict[str, int] = {
            "l1_hits": 0,
            "l2_hits": 0,
            "fallback_hits": 0,
            "misses": 0,
        }

        # Prometheus 指标刷新节流
        self._metrics_pending = 0
        self._metrics_last_flush = time.monotonic()
        self._metrics_flush_ops = 25
        self._metrics_flush_seconds = 1.0

        # Qdrant 集合存在性缓存（避免每次查询都检查）
        self._l2_collection_checked = False

        logger.info(
            f"三级缓存初始化: L1=Redis L2=Qdrant({l2_collection}) "
            f"L3=Jaccard(fallback_enabled={fallback_enabled}) "
            f"l2_threshold={l2_threshold} fallback_threshold={fallback_threshold}"
        )

    # ==================================================================
    # Public API
    # ==================================================================

    def get(self, query: str, metadata: dict | None = None) -> str | None:
        """
        从缓存中获取响应（v6.3: L1/L3 并行探测，减少串行延迟；P0-02: 跨用户隔离）

        读取发生在路由分类之前，此时 intent 未知，无法判断本次查询是公开还是
        个性化。因此读取端同时探测 SHARED 与调用方身份对应的作用域。安全性
        保证：个性化数据写入时永远不会落入 SHARED 槽位（见 _set），故 OR 探测
        不会造成跨用户泄漏。

        尝试顺序: L1 (Redis, 逐作用域键) -> L3 (Jaccard, 作用域过滤) -> L2 (Qdrant, 作用域 OR)
        全部未命中则返回 None。

        Args:
            query: 用户查询文本
            metadata: 可选元数据（intent_type, user_id, tenant_id, product_id 等）。
                读取端仅使用 user_id/tenant_id 决定可探测的作用域集合。

        Returns:
            缓存的响应字符串，未命中返回 None
        """
        start = time.time()
        normalized = self._normalize(query)

        md = metadata or {}
        read_keys = read_scope_keys(md.get("user_id"), md.get("tenant_id"))
        version = self._version

        # ----- L1: Redis 精确匹配（按可探测作用域键逐一探测）-----
        if self._redis is not None:
            for scope_key in read_keys:
                try:
                    t0 = time.time()
                    md5_key = self._l1_key(normalized, scope_key, version)
                    data = self._redis.get(md5_key)
                    cache_redis_latency.observe(time.time() - t0)
                    if data is not None:
                        entry = json.loads(data)
                        self._stats["l1_hits"] += 1
                        cache_l1_hits.inc()
                        cache_operation_duration.observe(time.time() - start)
                        self._schedule_metrics_flush()
                        return entry["response"]
                except Exception as e:
                    logger.warning(f"L1 Redis get 失败，降级到 L2/L3: {e}")

        # ----- L3: Jaccard 内存匹配（按可探测作用域键过滤）-----
        if self._fallback_enabled:
            try:
                l3_result = self._jaccard_search(normalized, read_keys, version)
                if l3_result is not None:
                    self._stats["fallback_hits"] += 1
                    cache_fallback_hits.inc()
                    cache_operation_duration.observe(time.time() - start)
                    self._schedule_metrics_flush()
                    return l3_result
            except Exception:
                logger.debug("L3 Jaccard 搜索失败")

        # ----- L2: Qdrant 向量搜索（按作用域 OR 过滤）-----
        if self._qdrant is not None:
            try:
                t0 = time.time()
                result = self._qdrant_get(normalized, read_keys, version, md)
                cache_qdrant_latency.observe(time.time() - t0)
                if result is not None:
                    self._stats["l2_hits"] += 1
                    cache_l2_hits.inc()
                    cache_operation_duration.observe(time.time() - start)
                    self._schedule_metrics_flush()
                    return result
            except Exception as e:
                logger.warning(f"L2 Qdrant get 失败: {e}")
                cache_qdrant_fallback_total.inc()

        # ----- 全部未命中 -----
        self._stats["misses"] += 1
        cache_misses.inc()
        cache_operation_duration.observe(time.time() - start)
        self._schedule_metrics_flush()
        return None

    def set(
        self,
        query: str,
        response: str,
        metadata: dict | None = None,
        policy=None,
    ):
        """
        写入三级缓存

        同时写入 L1 (Redis)、L2 (Qdrant)、L3 (Jaccard)。
        P0-02: 写入前经统一 CachePolicy 决定 cacheable/scope/ttl/version。
        个性化回答（订单/退款/投诉/售后）仅在存在可信身份时写入用户作用域；
        无身份时 fail closed（不写入任何层），绝不进入共享缓存。

        Args:
            query: 用户查询文本
            response: 要缓存的响应
            metadata: 可选元数据字典（intent_type, user_id, tenant_id, product_id 等）
            policy: 可选显式 CachePolicy；None 时由 metadata 解析
        """
        self._set(query, response, metadata, policy)

    def put(
        self,
        query: str,
        response: str,
        metadata: dict | None = None,
        policy=None,
    ):
        """
        set() 的别名，兼容旧 API
        """
        self._set(query, response, metadata, policy)

    def invalidate(
        self,
        query: str,
        metadata: dict | None = None,
        policy=None,
    ):
        """
        按查询文本与作用域删除缓存条目（P0-02: 作用域精确失效）

        仅失效调用方可达作用域的条目，避免跨用户误删。
        - 传入 policy：仅失效 policy.scope_key 对应条目。
        - 传入 metadata：失效 read_scope_keys(user_id, tenant_id) 对应条目（含 shared）。
        - 都不传：仅失效 shared 条目（向后兼容；用于公开知识更新的消息总线）。

        Args:
            query: 要失效的查询文本
            metadata: 可选元数据（user_id / tenant_id）
            policy: 可选显式 CachePolicy
        """
        normalized = self._normalize(query)
        version = self._version
        if policy is not None:
            scope_keys = [policy.scope_key]
        else:
            md = metadata or {}
            scope_keys = read_scope_keys(md.get("user_id"), md.get("tenant_id"))

        if self._redis is not None:
            for sk in scope_keys:
                try:
                    self._redis.delete(self._l1_key(normalized, sk, version))
                except Exception as e:
                    logger.warning(f"L1 Redis invalidate 失败: {e}")

        # L3: 按作用域精确失效（不跨用户误删）
        self._l3_evict_query(normalized, set(scope_keys))

    def invalidate_by_filter(self, filter_dict: dict):
        """
        按 payload 条件批量删除 L2 Qdrant 条目

        Args:
            filter_dict: 条件字典，如 {"product_id": "SKU_1", "intent_type": "pricing_stock"}
        """
        if self._qdrant is None:
            return
        try:
            from qdrant_client.http import models as qmodels

            must_conditions = []
            for key, value in filter_dict.items():
                must_conditions.append(
                    qmodels.FieldCondition(
                        key=key,
                        match=qmodels.MatchValue(value=value),
                    )
                )
            if must_conditions:
                self._qdrant.delete(
                    collection_name=self._l2_collection,
                    points_selector=qmodels.Filter(must=must_conditions),
                )
                logger.info(f"按条件删除 Qdrant 缓存: {filter_dict}")
        except Exception as e:
            logger.warning(f"Qdrant invalidate_by_filter 失败: {e}")

    def clear(self):
        """清空所有缓存层"""
        # L1: Redis
        if self._redis is not None:
            try:
                keys = list(self._redis.scan_iter(f"{self._l1_prefix}*", count=500))
                if keys:
                    self._redis.delete(*keys)
            except Exception as e:
                logger.warning(f"L1 Redis clear 失败: {e}")

        # L2: Qdrant（重建空集合）
        if self._qdrant is not None:
            try:
                self._qdrant.recreate_collection(
                    collection_name=self._l2_collection,
                    vectors_config={
                        "size": _RANDOM_VECTOR_DIM,
                        "distance": "Cosine",
                    },
                )
                self._l2_collection_checked = True
            except Exception as e:
                logger.warning(f"L2 Qdrant clear 失败: {e}")

        # L3: 内存
        self._l3_cache.clear()
        self._l3_order.clear()
        self._l3_inverted_index.clear()
        self._l3_counter = 0

        self._metrics_pending = 0
        self._update_metrics(force=True)
        logger.info("三级缓存已全部清空")

    def get_stats(self) -> dict:
        """
        返回缓存统计信息

        Returns:
            dict: 包含 l1_hits, l2_hits, fallback_hits, misses,
                  l1_size, l2_size, l3_size, total, hit_rate
        """
        total = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["fallback_hits"]
            + self._stats["misses"]
        )
        hit_count = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["fallback_hits"]
        )
        hit_rate = (hit_count / max(total, 1)) * 100

        # L1 大小（Redis scan 近似值）
        l1_size = 0
        if self._redis is not None:
            with contextlib.suppress(Exception):
                l1_size = len(
                    list(self._redis.scan_iter(f"{self._l1_prefix}*", count=100))
                )

        return {
            # v6.3: 无 Redis 时 fallback_hits 合并进 l1_hits（向后兼容旧测试）
            "l1_hits": self._stats["l1_hits"]
            + (self._stats["fallback_hits"] if self._redis is None else 0),
            "l2_hits": self._stats["l2_hits"],
            "fallback_hits": self._stats["fallback_hits"],
            "misses": self._stats["misses"],
            # v6.3: 无 Redis 时，fallback_hits 计入 l1_hits（向后兼容）
            "l1_size": l1_size if self._redis is not None else len(self._l3_cache),
            "l2_size": 0,  # Qdrant 暂不暴露 count
            "l3_size": len(self._l3_cache),
            "total": total,
            "hit_rate": f"{hit_rate:.1f}%",
        }

    # v6.3: 向后兼容属性 — 旧代码/测试通过 cache._l1 访问内存缓存
    @property
    def _l1(self) -> dict:
        """当 Redis 不可用时，返回 L3 内存缓存的字典视图（向后兼容）"""
        return self._l3_cache

    @_l1.setter
    def _l1(self, value: dict):
        """忽略 — 保持 _l3_cache 为唯一真实数据源"""
        pass

    def cleanup_expired(self) -> int:
        """
        删除 L2 Qdrant 中 expires_at < now 的过期条目

        Returns:
            清理条目数（Qdrant 可能不精确返回，0 为安全回退）
        """
        if self._qdrant is None:
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
                    ]
                ),
            )
            count = getattr(result, "count", 0) or 0
            logger.info(f"清理过期 Qdrant 缓存: {count} 条")
            return count
        except Exception as e:
            logger.warning(f"Qdrant cleanup_expired 失败: {e}")
            return 0

    # ==================================================================
    # L1: Redis 操作
    # ==================================================================

    @staticmethod
    def _normalize(text: str) -> str:
        """
        查询文本标准化

        操作：
        - 去首尾空白
        - 全角字符 -> 半角（FF01-FF5E -> 21-7E，3000 -> 20）
        - 转小写

        Args:
            text: 原始查询文本

        Returns:
            标准化后的文本
        """
        text = text.strip()
        result = []
        for ch in text:
            code = ord(ch)
            if 0xFF01 <= code <= 0xFF5E:
                result.append(chr(code - 0xFEE0))
            elif code == 0x3000:
                result.append(" ")
            else:
                result.append(ch)
        return "".join(result).lower()

    @staticmethod
    def _md5(text: str) -> str:
        """MD5 哈希（仅用于缓存键，非安全用途）"""
        return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _l1_key(self, normalized: str, scope_key: str, version: str) -> str:
        """
        P0-02: 构造带作用域与版本的 L1 Redis 键。

        key = prefix + version + ":" + scope_key + ":" + md5(normalized)
        不同用户产生不同 scope_key，互不覆盖；版本变更后键不匹配，旧条目自然失效。
        """
        return f"{self._l1_prefix}{version}:{scope_key}:{self._md5(normalized)}"

    def _resolve_policy(self, metadata: dict | None, policy):
        """
        P0-02: 解析写入端 CachePolicy。

        传入显式 policy 时直接使用；否则由 metadata（intent_type + user_id +
        tenant_id）解析。ttl_policy 与 content_version 由本缓存实例提供，
        保证三层使用同一策略源。
        """
        if policy is not None:
            return policy
        md = metadata or {}
        return resolve_cache_policy(
            intent_type=md.get("intent_type"),
            user_id=md.get("user_id"),
            tenant_id=md.get("tenant_id"),
            ttl_policy=self._l1_ttl_policy,
            content_version=self._content_version,
        )

    # ==================================================================
    # L2: Qdrant 操作
    # ==================================================================

    def _ensure_l2_collection(self) -> bool:
        """
        确保 Qdrant 集合存在（自动创建）

        Returns:
            True 表示集合可用，False 表示操作失败
        """
        if self._l2_collection_checked:
            return True
        if self._qdrant is None:
            return False
        try:
            collections = self._qdrant.get_collections()
            names = [c.name for c in collections.collections]
            if self._l2_collection not in names:
                self._qdrant.create_collection(
                    collection_name=self._l2_collection,
                    vectors_config={
                        "size": _RANDOM_VECTOR_DIM,
                        "distance": "Cosine",
                    },
                )
                logger.info(f"创建 Qdrant collection: {self._l2_collection}")
            self._l2_collection_checked = True
            return True
        except Exception as e:
            logger.warning(f"Qdrant 集合检查/创建失败: {e}")
            return False

    def _embed_query(self, query: str) -> list[float]:
        """
        将查询文本编码为向量

        使用 self._embed_fn.encode()（SentenceTransformer），
        如果未提供编码模型则使用确定性随机向量回退。

        Args:
            query: 查询文本

        Returns:
            float 向量列表（维度 _RANDOM_VECTOR_DIM）
        """
        if self._embed_fn is not None:
            try:
                vec = self._embed_fn.encode(query)
                if hasattr(vec, "tolist"):
                    return vec.tolist()
                return list(vec)
            except Exception as e:
                logger.warning(f"嵌入模型 encode 失败，使用随机回退: {e}")
        # 确定性随机回退：同一 query 始终产生同一向量
        rng = random.Random(query)
        return [rng.random() for _ in range(_RANDOM_VECTOR_DIM)]

    def _qdrant_get(
        self,
        query: str,
        read_keys: list[str],
        version: str,
        metadata: dict | None = None,  # noqa: ARG002
    ) -> str | None:
        """
        从 Qdrant 搜索缓存（P0-02: 按作用域键严格匹配）

        对每个可探测作用域键发起一次独立搜索，filter 为严格 must：
        - expires_at >= now
        - version == 当前版本
        - scope_key == 该作用域键
        任一命中即返回。使用严格 must（而非 should/min_should）避免依赖
        Qdrant server 的 min_should 默认值——在版本间行为可变，对安全隔离
        不可接受。共享作用域优先（公开 FAQ 命中即返回，无需再查用户作用域）。

        Args:
            query: 标准化查询文本
            read_keys: 读取端可探测的作用域键集合（shared + 调用方身份）
            version: 当前缓存版本

        Returns:
            缓存的响应，未命中返回 None
        """
        if not self._ensure_l2_collection():
            return None

        from qdrant_client.http import models as qmodels

        now = time.time()
        vector = self._embed_query(query)

        for scope_key in read_keys:
            must_conditions = [
                qmodels.FieldCondition(
                    key="expires_at",
                    range=qmodels.Range(gte=now),
                ),
                qmodels.FieldCondition(
                    key="version",
                    match=qmodels.MatchValue(value=version),
                ),
                qmodels.FieldCondition(
                    key="scope_key",
                    match=qmodels.MatchValue(value=scope_key),
                ),
            ]
            # P0-02 / Codex re-review AC3/AC9：qdrant-client 1.18.0 移除了 .search()，
            # 改用 query_points()（返回 QueryResponse，命中在 .points）。向量入参由
            # query_vector 改为 query；filter 仍为 query_filter。
            resp = self._qdrant.query_points(
                collection_name=self._l2_collection,
                query=vector,
                limit=1,
                score_threshold=self._l2_threshold,
                query_filter=qmodels.Filter(must=must_conditions),
            )
            points = getattr(resp, "points", None) if resp is not None else None
            if points:
                return points[0].payload["response"]
        return None

    def _qdrant_set(
        self,
        query: str,
        response: str,
        policy,
        metadata: dict | None,
        expires_at: float,
    ):
        """
        写入 Qdrant 缓存（P0-02: 作用域隔离）

        Point ID = MD5(query + scope_key + intent_type)[:16] 作为 int，
        确保相同查询 + 不同作用域的缓存不会被互相覆盖。

        Args:
            query: 标准化查询文本
            response: 响应内容
            policy: 写入端 CachePolicy（提供 scope/scope_key/version/sensitivity）
            metadata: 可选元数据（intent_type, product_id, user_role 等）
            expires_at: 过期时间戳
        """
        if not self._ensure_l2_collection():
            return

        from qdrant_client.http import models as qmodels

        md = metadata or {}
        intent_type = md.get("intent_type", "default")
        vector = self._embed_query(query)
        # P0-02: point_id 含 scope_key，避免不同用户互相覆盖
        point_id = int(self._md5(query + policy.scope_key + intent_type)[:16], 16)

        now = time.time()
        payload = {
            "response": response,
            "query_text": query,
            "intent_type": intent_type,
            "scope": policy.scope.value,
            "scope_key": policy.scope_key,
            "version": policy.version,
            "sensitivity": policy.sensitivity.value,
            "product_id": md.get("product_id", ""),
            # 保留 user_role 字段以向后兼容旧的管理面过滤；不作为隔离依据
            "user_role": md.get("user_role", ""),
            "created_at": now,
            "expires_at": expires_at,
        }

        self._qdrant.upsert(
            collection_name=self._l2_collection,
            points=[qmodels.PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    # ==================================================================
    # L3: Jaccard 内存缓存（继承原 L2 的 Jaccard 逻辑）
    # ==================================================================

    def _jaccard_search(
        self,
        query: str,
        read_keys: list[str],
        version: str,
    ) -> str | None:
        """
        使用 Jaccard 相似度 + 倒排索引搜索 L3 内存缓存（P0-02: 作用域隔离）

        仅匹配作用域键属于 read_keys 且版本一致的条目。个性化数据因 scope_key
        为用户级，跨用户读取时被自然过滤。

        Args:
            query: 标准化查询文本
            read_keys: 读取端可探测的作用域键集合
            version: 当前缓存版本

        Returns:
            最佳匹配的响应，未命中返回 None
        """
        tokens = _tokenize(query)
        if not tokens:
            return None

        read_keys_set = set(read_keys)
        candidate_keys: set = set()
        for t in tokens:
            candidate_keys.update(self._l3_inverted_index.get(t, set()))

        best_score = 0.0
        best_response = None
        now = time.time()

        for ck in candidate_keys:
            if ck not in self._l3_cache:
                continue
            cached_tokens, response, ts, scope_key, ver, ttl = self._l3_cache[ck]
            # P0-02: 作用域隔离 — 仅匹配调用方可达的作用域
            if scope_key not in read_keys_set:
                continue
            if ver != version:
                continue
            # P0-02 CACHE-5: L3 按 policy TTL 过期（与 L1 setex(policy.ttl) /
            # L2 expires_at=now+policy.ttl 一致；Codex re-review AC18 修复）。
            if now - ts >= ttl:
                self._l3_evict_key(ck)
                continue
            score = self._jaccard(tokens, cached_tokens)
            if score >= self._fallback_threshold and score > best_score:
                best_score = score
                best_response = response

        return best_response

    def _jaccard_set(self, query: str, response: str, now: float, policy):
        """
        写入 L3 内存缓存（P0-02: 携带作用域键与版本）

        Args:
            query: 标准化查询文本
            response: 响应内容
            now: 当前时间戳（由 _set 统一传入）
            policy: 写入端 CachePolicy
        """
        tokens = _tokenize(query)
        if not tokens:
            return

        if len(self._l3_cache) >= self._l3_max_size:
            self._l3_evict()

        self._l3_counter += 1
        key = f"l3:{self._l3_counter}"
        self._l3_cache[key] = (
            tokens,
            response,
            now,
            policy.scope_key,
            policy.version,
            policy.ttl,
        )
        self._l3_order.append(key)
        for t in tokens:
            self._l3_inverted_index[t].add(key)

    @staticmethod
    def _jaccard(set_a: frozenset, set_b: frozenset) -> float:
        """
        计算两个 frozenset 的 Jaccard 相似度

        J(A, B) = |A & B| / |A | B|

        Args:
            set_a: 集合 A
            set_b: 集合 B

        Returns:
            0.0 ~ 1.0 的相似度分数
        """
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union > 0 else 0.0

    def _l3_evict(self):
        """
        L3 缓存淘汰

        策略：FIFO，每次淘汰 5% 条目（避免缓存雪崩）。
        同时清理倒排索引中的对应条目。
        """
        if not self._l3_order:
            return
        n = max(1, len(self._l3_order) // 20)
        for _ in range(n):
            if not self._l3_order:
                break
            old_key = self._l3_order.popleft()
            if old_key in self._l3_cache:
                tokens = self._l3_cache[old_key][0]
                for t in tokens:
                    self._l3_inverted_index[t].discard(old_key)
                    if not self._l3_inverted_index[t]:
                        del self._l3_inverted_index[t]
                del self._l3_cache[old_key]

    def _l3_evict_key(self, key: str):
        """
        删除 L3 缓存中的单个条目（TTL 过期时调用）

        Args:
            key: 要删除的条目键
        """
        if key not in self._l3_cache:
            return
        tokens = self._l3_cache[key][0]
        for t in tokens:
            self._l3_inverted_index[t].discard(key)
            if not self._l3_inverted_index[t]:
                del self._l3_inverted_index[t]
        del self._l3_cache[key]

    def _l3_evict_query(self, query: str, scope_keys: set | None = None):
        """
        从 L3 Jaccard 缓存中移除匹配查询且属于给定作用域的条目。

        P0-02: 作用域精确失效。scope_keys 为 None 时（向后兼容的消息总线路径）
        仅失效 shared 条目；否则仅失效 scope_keys 内的条目，绝不跨用户误删。

        Args:
            query: 要失效的查询文本（已标准化或原文本，内部会再标准化）
            scope_keys: 允许失效的作用域键集合
        """
        normalized = self._normalize(query)
        tokens = _tokenize(normalized)
        if scope_keys is None:
            scope_keys = {"shared"}
        keys_to_remove = []
        for key, entry in self._l3_cache.items():
            cached_tokens = entry[0]
            scope_key = entry[3]
            # 仅当作用域允许且共享 token 时才失效
            if scope_key in scope_keys and (tokens & cached_tokens):
                keys_to_remove.append(key)
        for key in keys_to_remove:
            self._l3_evict_key(key)

    # ==================================================================
    # 内部辅助方法
    # ==================================================================

    def _set(
        self,
        query: str,
        response: str,
        metadata: dict | None = None,
        policy=None,
    ):
        """
        set/put 的内部实现（P0-02: 统一 CachePolicy）

        写入前先解析 CachePolicy：
        - cacheable=False（个性化 + 无身份）→ fail closed，不写入任何层。
        - USER/TENANT 作用域 → 三层均写入对应 scope_key，跨用户隔离。
        - SHARED 作用域 → 三层均写入 shared，所有用户共享。

        三层使用同一 scope_key / version / ttl，保证层间一致。

        Args:
            query: 用户查询文本
            response: 要缓存的响应
            metadata: 可选元数据
            policy: 可选显式 CachePolicy
        """
        policy = self._resolve_policy(metadata, policy)
        if not policy.cacheable:
            # fail closed：个性化回答缺少可信身份，绝不进入共享缓存。
            # P0-02 Step 17：日志只记 scope/reason，不写查询正文（可能含订单号等
            # customer/order data）或响应正文。
            logger.debug(
                f"跳过缓存写入（policy disabled, scope={policy.scope.value}）"
            )
            return

        now = time.time()
        normalized = self._normalize(query)

        # ----- L1: Redis -----
        if self._redis is not None:
            try:
                t0 = time.time()
                md5_key = self._l1_key(normalized, policy.scope_key, policy.version)
                value = json.dumps({"response": response, "created_at": now})
                self._redis.setex(md5_key, policy.ttl, value)
                cache_redis_latency.observe(time.time() - t0)
            except Exception as e:
                logger.warning(f"L1 Redis set 失败: {e}")

        # ----- L2: Qdrant -----
        if self._qdrant is not None:
            expires_at = now + policy.ttl
            try:
                self._qdrant_set(
                    query=normalized,
                    response=response,
                    policy=policy,
                    metadata=metadata,
                    expires_at=expires_at,
                )
            except Exception as e:
                logger.warning(f"L2 Qdrant set 失败: {e}")

        # ----- L3: Jaccard 内存回退 -----
        if self._fallback_enabled:
            with contextlib.suppress(Exception):
                self._jaccard_set(normalized, response, now, policy)

        self._update_metrics(force=True)

    def _schedule_metrics_flush(self, force: bool = False):
        """
        按节奏刷新 Prometheus 指标，避免每次缓存命中都做全量写入
        """
        self._metrics_pending += 1
        now = time.monotonic()
        if not force:
            pending_ops = self._metrics_pending < self._metrics_flush_ops
            not_due_yet = (now - self._metrics_last_flush) < self._metrics_flush_seconds
            if pending_ops and not_due_yet:
                return
        self._update_metrics(force=force)

    def _update_metrics(self, force: bool = False):
        """
        更新 Prometheus 监控指标（v6.3: scan_iter 节流，避免每次 set 都做 O(n) 扫描）
        """
        total = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["fallback_hits"]
            + self._stats["misses"]
        )
        if total > 0:
            hit_rate = (
                self._stats["l1_hits"]
                + self._stats["l2_hits"]
                + self._stats["fallback_hits"]
            ) / total
            cache_hit_rate.set(hit_rate)

        # v6.3: L1 大小扫描节流 — scan_iter 是 O(n)，每次 set 都做会严重拖慢写入
        # 改为最多每 60 秒扫描一次
        l1_size = 0
        if self._redis is not None:
            now = time.monotonic()
            if force or (now - getattr(self, "_l1_size_last_scan", 0)) > 60.0:
                try:
                    l1_size = len(
                        list(self._redis.scan_iter(f"{self._l1_prefix}*", count=100))
                    )
                    self._l1_size_last_scan = now
                    self._l1_size_cached = l1_size
                except Exception as e:
                    logger.debug(f"更新 L1 缓存指标失败: {e}")
                    l1_size = getattr(self, "_l1_size_cached", 0)
            else:
                l1_size = getattr(self, "_l1_size_cached", 0)
        cache_l1_size.set(l1_size)

        # L2 大小近似 = L3 大小（Qdrant 不实时暴露 count）
        cache_l2_size.set(len(self._l3_cache))

        self._metrics_pending = 0
        self._metrics_last_flush = time.monotonic()

    # ==================================================================
    # v6.1: 消息总线支持（主动失效）
    # ==================================================================

    async def subscribe_to_bus(self, bus):
        """订阅消息总线上的缓存失效事件。

        监听 "cache:invalidate" 主题，收到消息后按 payload 中的 query 失效对应缓存条目。

        P0-02 作用域语义：本处理器调用 `invalidate(query)`（无 metadata），因此
        仅失效 **shared 作用域** 的条目。这适用于公开知识更新（FAQ/政策/产品信息
        变更 → shared 缓存失效）。个性化（用户/租户作用域）条目不随公开知识更新
        失效——它们不源于知识库，无需随其更新失效。若未来需要按身份定向失效，
        应在消息 payload 中携带 user_id/tenant_id 并传给 `invalidate(query, metadata=...)`。

        Args:
            bus: MessageBus 实例
        """
        async def _handle_invalidation(message):
            payload = message.payload if isinstance(message.payload, dict) else {}
            query = payload.get("query")
            if query:
                # 若 payload 携带身份则定向失效，否则仅 shared（公开知识更新场景）
                meta = {
                    k: payload[k]
                    for k in ("user_id", "tenant_id")
                    if k in payload
                } or None
                self.invalidate(query, metadata=meta)

        await bus.subscribe("cache:invalidate", _handle_invalidation)
        logger.info("已订阅缓存失效事件 (topic=cache:invalidate)")
