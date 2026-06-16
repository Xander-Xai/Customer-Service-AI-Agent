"""
二级缓存系统（v3.4 - jieba 中文分词优化版）
L1: 精确缓存（MD5 哈希）- O(1) 查找
L2: 语义缓存（Jaccard 相似度 + 倒排索引 + jieba 分词）- 动态阈值
+ 结构化日志

v3.4 优化：
- L2 tokenize 改用 jieba 中文分词（与 session_manager 共享），提升中文语义匹配精度
- 缓存淘汰策略优化：每次淘汰 5% 而非 20%，避免缓存雪崩

v5.4 新增：
- Prometheus 监控指标（命中率、缓存大小、响应时间）
"""

import asyncio
import hashlib
import time
from collections import OrderedDict, defaultdict, deque

from core import config
from core.logger import get_logger
from core.session.session_manager import _tokenize_chinese as _tokenize

logger = get_logger("cache")

# v5.4: Prometheus 监控指标
try:
    from prometheus_client import Counter, Gauge, Histogram
    
    # 缓存命中统计
    cache_l1_hits = Counter(
        'cache_l1_hits_total',
        'L1 exact match cache hits',
    )
    cache_l2_hits = Counter(
        'cache_l2_hits_total', 
        'L2 semantic match cache hits',
    )
    cache_misses = Counter(
        'cache_misses_total',
        'Cache misses (no match found)',
    )
    
    # 缓存大小监控
    cache_l1_size = Gauge(
        'cache_l1_size',
        'Number of entries in L1 cache',
    )
    cache_l2_size = Gauge(
        'cache_l2_size',
        'Number of entries in L2 cache',
    )
    
    # 缓存命中率
    cache_hit_rate = Gauge(
        'cache_hit_rate',
        'Overall cache hit rate (0-1)',
    )
    
    # 缓存操作延迟
    cache_operation_duration = Histogram(
        'cache_operation_seconds',
        'Time spent on cache operations',
        buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    )
    
    PROMETHEUS_ENABLED = True
except ImportError:
    # Prometheus 未安装，降级为无操作
    class _NoopMetric:
        def inc(self, *args, **kwargs): pass
        def set(self, *args, **kwargs): pass
        def observe(self, *args, **kwargs): pass
    
    cache_l1_hits = cache_l2_hits = cache_misses = _NoopMetric()
    cache_l1_size = cache_l2_size = cache_hit_rate = _NoopMetric()
    cache_operation_duration = _NoopMetric()
    PROMETHEUS_ENABLED = False


class ResponseCache:
    """
    二级响应缓存
    
    架构设计：
    - L1 Cache: MD5精确匹配，O(1)查找速度，适合完全相同的查询
    - L2 Cache: Jaccard语义匹配，支持相似查询召回，使用倒排索引加速
    - Redis持久化：可选层，重启后恢复缓存数据
    
    缓存策略：
    - L1淘汰：LRU策略，达到上限时淘汰最久未使用的5%
    - L2淘汰：FIFO策略，达到上限时淘汰最早进入的5%
    - TTL过期：默认3600秒，可配置
    
    性能特征：
    - L1命中率：预期60-80%（取决于查询重复率）
    - L2命中率：预期20-40%（取决于语义相似度）
    - 平均延迟：<1ms（内存缓存）
    
    使用示例：
        >>> cache = ResponseCache(l1_max=500, l2_max=2000)
        >>> await cache.init_redis("redis://localhost:6379")
        >>> result = cache.get("产品成分是什么？")
        >>> if result is None:
        ...     result = await generate_response("产品成分是什么？")
        ...     cache.set("产品成分是什么？", result)
    """

    def __init__(
        self,
        l1_max: int = 500,
        l2_max: int = 2000,
        default_ttl: float = 3600,
        threshold_short: float = config.CACHE_SEMANTIC_THRESHOLD_SHORT,
        threshold_long: float = config.CACHE_SEMANTIC_THRESHOLD_LONG,
        short_text_max_len: int = 20,
    ):
        """
        初始化二级缓存
        
        Args:
            l1_max: L1缓存最大条目数（默认500）
            l2_max: L2缓存最大条目数（默认2000）
            default_ttl: 默认过期时间秒（默认3600）
            threshold_short: 短文本语义匹配阈值（默认0.75）
            threshold_long: 长文本语义匹配阈值（默认0.6）
            short_text_max_len: 短文本最大长度（默认20字符）
        """
        self._l1: OrderedDict = OrderedDict()  # key -> (response, ts)
        self._l1_max = l1_max
        self._l2: dict[str, tuple[frozenset, str, float]] = {}
        self._l2_order: deque = deque()  # v3.6: 改用 deque，淘汰从 O(n) 优化到 O(1)
        self._l2_counter = 0  # v3.7: 单调递增计数器，防止淘汰后键碰撞
        self._l2_max = l2_max
        self._inverted_index: dict[str, set] = defaultdict(set)
        self._default_ttl = default_ttl
        self._threshold_short = threshold_short
        self._threshold_long = threshold_long
        self._short_text_max_len = short_text_max_len
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}
        logger.info(
            f"初始化: L1_max={l1_max} L2_max={l2_max} TTL={default_ttl}s "
            f"threshold_short={threshold_short} threshold_long={threshold_long} "
            f"short_text_max_len={short_text_max_len}"
        )

    async def _init_redis(self, redis_url: str = None):
        """可选：初始化 Redis 持久化层（懒加载，不影响主功能）"""
        try:
            import redis

            url = redis_url or config.REDIS_URL
            self._redis = redis.Redis.from_url(url, decode_responses=True)
            await asyncio.to_thread(self._redis.ping)
            await self._warm_up_from_redis()
            logger.info("Redis 持久化层已连接")
        except Exception as e:
            self._redis = None
            logger.debug(f"Redis 不可用，仅使用内存缓存: {e}")

    async def _warm_up_from_redis(self):
        """从 Redis 预热 L1 缓存"""
        if not self._redis:
            return
        try:
            import json

            keys = await asyncio.to_thread(
                lambda: list(self._redis.scan_iter("cache:resp:*", count=100))
            )
            for key in keys[:100]:  # 限制预热数量
                data = await asyncio.to_thread(self._redis.get, key)
                if data:
                    entry = json.loads(data)
                    md5_key = key.replace("cache:resp:", "")
                    self._l1[md5_key] = (entry["response"], entry["ts"])
            # 保持 OrderedDict 插入顺序
            logger.info(f"Redis 预热完成: {len(self._l1)} 条")
        except Exception as e:
            logger.warning(f"Redis 预热失败: {e}")

    def get(self, query: str) -> str | None:
        """
        从缓存中获取响应
        
        Args:
            query: 用户查询文本
            
        Returns:
            str | None: 缓存的响应，未命中返回None
        """
        start_time = time.time()
        
        key_md5 = self._md5(query)
        if key_md5 in self._l1:
            resp, ts = self._l1[key_md5]
            if time.time() - ts < self._default_ttl:
                self._stats["l1_hits"] += 1
                cache_l1_hits.inc()  # v5.4: Prometheus指标
                # LRU：移到末尾（最近使用）
                self._l1.move_to_end(key_md5)
                
                # v5.4: 更新命中率指标
                self._update_metrics()
                
                cache_operation_duration.observe(time.time() - start_time)
                return resp
            else:
                del self._l1[key_md5]

        result = self._semantic_search(query)
        if result:
            self._stats["l2_hits"] += 1
            cache_l2_hits.inc()  # v5.4: Prometheus指标
            
            # v5.4: 更新命中率指标
            self._update_metrics()
            
            cache_operation_duration.observe(time.time() - start_time)
            return result

        self._stats["misses"] += 1
        cache_misses.inc()  # v5.4: Prometheus指标
        
        # v5.4: 更新命中率指标
        self._update_metrics()
        
        cache_operation_duration.observe(time.time() - start_time)
        return None
    
    def _update_metrics(self):
        """v5.4: 更新Prometheus监控指标"""
        total = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["misses"]
        if total > 0:
            hit_rate = (self._stats["l1_hits"] + self._stats["l2_hits"]) / total
            cache_hit_rate.set(hit_rate)
        
        # 更新缓存大小
        cache_l1_size.set(len(self._l1))
        cache_l2_size.set(len(self._l2))

    def set(self, query: str, response: str):
        """设置缓存（兼容旧 API 别名）"""
        self._set(query, response)

    def put(self, query: str, response: str):
        """兼容旧 API 的别名，等同于 set"""
        self._set(query, response)

    def _set(self, query: str, response: str):
        now = time.time()
        key_md5 = self._md5(query)
        if len(self._l1) >= self._l1_max:
            self._evict_l1()
        # LRU：新条目插入末尾
        self._l1[key_md5] = (response, now)
        self._l1.move_to_end(key_md5)

        # 可选：同步写入 Redis 持久化层（非阻塞，通过线程执行）
        if hasattr(self, "_redis") and self._redis:
            try:
                import json
                import threading

                data = json.dumps({"response": response, "ts": now}, ensure_ascii=False)
                key = f"cache:resp:{key_md5}"
                ttl = int(self._default_ttl)
                t = threading.Thread(target=self._redis.setex, args=(key, ttl, data), daemon=True)
                t.start()
            except Exception:
                pass

        tokens = _tokenize(query)
        if len(self._l2) >= self._l2_max:
            self._evict_l2()
        self._l2_counter += 1
        cache_key = f"k{self._l2_counter}"
        self._l2[cache_key] = (tokens, response, now)
        self._l2_order.append(cache_key)
        for t in tokens:
            self._inverted_index[t].add(cache_key)

    def invalidate(self, query: str):
        key_md5 = self._md5(query)
        self._l1.pop(key_md5, None)

    def clear(self):
        self._l1.clear()
        self._l2.clear()
        self._l2_order.clear()
        self._inverted_index.clear()
        self._redis = None  # v3.6: 重置 Redis 客户端
        logger.info("缓存已清空")

    def get_stats(self) -> dict[str, int]:
        total = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["misses"]
        hit_rate = (self._stats["l1_hits"] + self._stats["l2_hits"]) / max(total, 1) * 100
        return {
            **self._stats,
            "l1_size": len(self._l1),
            "l2_size": len(self._l2),
            "total": total,
            "hit_rate": f"{hit_rate:.1f}%",
        }

    def _semantic_search(self, query: str) -> str | None:
        tokens = _tokenize(query)
        if not tokens:
            return None
        threshold = (
            self._threshold_short
            if len(query) <= self._short_text_max_len
            else self._threshold_long
        )

        candidate_keys: set = set()
        for t in tokens:
            candidate_keys.update(self._inverted_index.get(t, set()))

        best_score = 0.0
        best_response = None
        now = time.time()

        for ck in candidate_keys:
            if ck not in self._l2:
                continue
            cached_tokens, response, ts = self._l2[ck]
            if now - ts >= self._default_ttl:
                continue
            score = self._jaccard(tokens, cached_tokens)
            if score >= threshold and score > best_score:
                best_score = score
                best_response = response

        return best_response

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _jaccard(set_a: frozenset, set_b: frozenset) -> float:
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union > 0 else 0.0

    def _evict_l1(self):
        """v3.4: LRU 淘汰 — 每次淘汰 5% 条目（避免 20% 淘汰导致缓存雪崩）"""
        if not self._l1:
            return
        n = max(1, len(self._l1) // 20)  # v3.4: 从 1/5 改为 1/20
        for _ in range(n):
            self._l1.popitem(last=False)

    def _evict_l2(self):
        """v3.4: L2 淘汰 — 每次淘汰 5% 条目"""
        if not self._l2_order:
            return
        n = max(1, len(self._l2_order) // 20)  # v3.4: 从 1/5 改为 1/20
        for _ in range(n):
            if not self._l2_order:
                break
            old_key = self._l2_order.popleft()  # v3.6: O(1) 淘汰（原 pop(0) 为 O(n)）
            if old_key in self._l2:
                tokens = self._l2[old_key][0]
                for t in tokens:
                    self._inverted_index[t].discard(old_key)
                    if not self._inverted_index[
                        t
                    ]:  # v3.8 fix: clean empty sets to prevent memory leak
                        del self._inverted_index[t]
                del self._l2[old_key]
