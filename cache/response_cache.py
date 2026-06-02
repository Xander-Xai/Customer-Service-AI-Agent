"""
二级缓存系统（v3.0）
L1: 精确缓存（MD5 哈希）- O(1) 查找
L2: 语义缓存（Jaccard 相似度 + 倒排索引）- 动态阈值
+ 结构化日志
"""
import hashlib
import time
import re
from collections import defaultdict, OrderedDict
from typing import Dict, List, Optional, Tuple
from logger import get_logger
import config

logger = get_logger("cache")


class ResponseCache:
    """二级响应缓存"""

    def __init__(self, l1_max: int = 500, l2_max: int = 2000, default_ttl: float = 3600,
                 threshold_short: float = config.CACHE_SEMANTIC_THRESHOLD_SHORT,
                 threshold_long: float = config.CACHE_SEMANTIC_THRESHOLD_LONG,
                 short_text_max_len: int = 20):
        self._l1: OrderedDict = OrderedDict()  # key -> (response, ts)
        self._l1_max = l1_max
        self._l2: Dict[str, Tuple[frozenset, str, float]] = {}
        self._l2_order: List[str] = []
        self._l2_max = l2_max
        self._inverted_index: Dict[str, set] = defaultdict(set)
        self._default_ttl = default_ttl
        self._threshold_short = threshold_short
        self._threshold_long = threshold_long
        self._short_text_max_len = short_text_max_len
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}
        logger.info(f"初始化: L1_max={l1_max} L2_max={l2_max} TTL={default_ttl}s "
                    f"threshold_short={threshold_short} threshold_long={threshold_long} "
                    f"short_text_max_len={short_text_max_len}")

    def _init_redis(self, redis_url: str = None):
        """可选：初始化 Redis 持久化层（懒加载，不影响主功能）"""
        try:
            import redis
            url = redis_url or config.REDIS_URL
            self._redis = redis.Redis.from_url(url, decode_responses=True)
            self._redis.ping()
            self._warm_up_from_redis()
            logger.info("Redis 持久化层已连接")
        except Exception as e:
            self._redis = None
            logger.debug(f"Redis 不可用，仅使用内存缓存: {e}")

    def _warm_up_from_redis(self):
        """从 Redis 预热 L1 缓存"""
        if not self._redis:
            return
        try:
            import json
            for key in self._redis.scan_iter("cache:resp:*", count=100):
                data = self._redis.get(key)
                if data:
                    entry = json.loads(data)
                    md5_key = key.replace("cache:resp:", "")
                    self._l1[md5_key] = (entry["response"], entry["ts"])
            # 保持 OrderedDict 插入顺序
            logger.info(f"Redis 预热完成: {len(self._l1)} 条")
        except Exception as e:
            logger.warning(f"Redis 预热失败: {e}")

    def get(self, query: str) -> Optional[str]:
        key_md5 = self._md5(query)
        if key_md5 in self._l1:
            resp, ts = self._l1[key_md5]
            if time.time() - ts < self._default_ttl:
                self._stats["l1_hits"] += 1
                # LRU：移到末尾（最近使用）
                self._l1.move_to_end(key_md5)
                return resp
            else:
                del self._l1[key_md5]

        result = self._semantic_search(query)
        if result:
            self._stats["l2_hits"] += 1
            return result

        self._stats["misses"] += 1
        return None

    def put(self, query: str, response: str):
        now = time.time()
        key_md5 = self._md5(query)
        if len(self._l1) >= self._l1_max:
            self._evict_l1()
        # LRU：新条目插入末尾
        self._l1[key_md5] = (response, now)
        self._l1.move_to_end(key_md5)

        # 可选：同步写入 Redis 持久化层
        if hasattr(self, '_redis') and self._redis:
            try:
                import json
                self._redis.setex(f"cache:resp:{key_md5}", int(self._default_ttl),
                                  json.dumps({"response": response, "ts": now}, ensure_ascii=False))
            except Exception:
                pass

        tokens = self._tokenize(query)
        if len(self._l2) >= self._l2_max:
            self._evict_l2()
        cache_key = f"k{len(self._l2_order)}"
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
        logger.info("缓存已清空")

    def get_stats(self) -> Dict[str, int]:
        total = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["misses"]
        hit_rate = (self._stats["l1_hits"] + self._stats["l2_hits"]) / max(total, 1) * 100
        return {
            **self._stats,
            "l1_size": len(self._l1),
            "l2_size": len(self._l2),
            "total": total,
            "hit_rate": f"{hit_rate:.1f}%",
        }

    def _semantic_search(self, query: str) -> Optional[str]:
        tokens = self._tokenize(query)
        if not tokens:
            return None
        threshold = self._threshold_short if len(query) <= self._short_text_max_len else self._threshold_long

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
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _tokenize(text: str) -> frozenset:
        words = re.findall(r"[\w一-鿿]+", text.lower())
        return frozenset(words)

    @staticmethod
    def _jaccard(set_a: frozenset, set_b: frozenset) -> float:
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union > 0 else 0.0

    def _evict_l1(self):
        """LRU 淘汰：移除最久未使用的 20% 条目（O(n) 但无排序开销）"""
        if not self._l1:
            return
        n = max(1, len(self._l1) // 5)
        for _ in range(n):
            self._l1.popitem(last=False)

    def _evict_l2(self):
        if not self._l2_order:
            return
        n = max(1, len(self._l2_order) // 5)
        for _ in range(n):
            if not self._l2_order:
                break
            old_key = self._l2_order.pop(0)
            if old_key in self._l2:
                tokens = self._l2[old_key][0]
                for t in tokens:
                    self._inverted_index[t].discard(old_key)
                del self._l2[old_key]
