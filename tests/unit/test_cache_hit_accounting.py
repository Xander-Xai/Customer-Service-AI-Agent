"""缓存命中统计口径（scenario 9）回归。

历史缺陷（本轮修复前）：

1. ``ResponseCache.get()`` 在解析 L1 条目**之前**就 ``self._stats["l1_hits"] += 1``，
   紧接 ``entry["response"]`` 抛 ``KeyError`` 被同一 ``except`` 吞掉。于是一条
   损坏的缓存条目会让**同一个请求**既记 L1 命中、又在末尾记一次未命中 —— 命中率
   被同时抬高又压低，两个计数之和大于真实请求数。
2. 后端异常（Redis/Qdrant 抛错）被记成 ``miss``，与"缓存里确实没有"混为一谈，
   失败率被算进未命中分母。

本文件把两条口径钉死：一个请求最终只贡献 hit 或 miss/error 之一，且"后端全挂"
记 ``errors`` 而非 ``miss``。
"""

from __future__ import annotations

from cache.response_cache import ResponseCache


class _DictRedis:
    """最小 Redis 替身：只实现 ResponseCache.get() 用到的读路径。"""

    def __init__(self, store: dict | None = None, *, get_error: bool = False):
        self._store = store or {}
        self._get_error = get_error

    def get(self, key):
        if self._get_error:
            raise RuntimeError("redis get unavailable")
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    def scan_iter(self, pattern, count=100):  # noqa: ARG002
        return iter(list(self._store.keys()))


def _cache(redis=None, *, fallback_enabled=False):
    return ResponseCache(
        redis_client=redis,
        qdrant_client=None,
        embedding_model=None,
        fallback_enabled=fallback_enabled,
        fallback_threshold=0.0,
    )


class TestSingleRequestContributesOneOutcome:
    def test_corrupt_l1_entry_is_not_counted_as_both_hit_and_miss(self):
        # 合法 JSON 但缺 "response" -> 解析值失败；修复前 l1_hits 已 +1，
        # 然后落到末尾再 misses +1。
        redis = _DictRedis()
        cache = _cache(redis)
        # 无论 scope key 是哪个，都返回同一份损坏 payload
        redis.get = lambda key: '{"unexpected": "shape"}'  # type: ignore[assignment]

        assert cache.get("任意查询") is None

        stats = cache.get_stats()
        assert stats["l1_hits"] == 0, "损坏条目不得记为命中"
        assert stats["misses"] == 1
        assert stats["errors"] == 0
        # 一次请求只贡献一个结果计数
        assert stats["l1_hits"] + stats["l2_hits"] + stats["fallback_hits"] + stats["misses"] == 1

    def test_valid_l1_entry_counts_once(self):
        redis = _DictRedis()
        cache = _cache(redis)
        redis.get = lambda key: '{"response": "cached answer"}'  # type: ignore[assignment]

        assert cache.get("任意查询") == "cached answer"
        stats = cache.get_stats()
        assert stats["l1_hits"] == 1
        assert stats["misses"] == 0
        assert stats["errors"] == 0

    def test_true_miss_counts_once(self):
        cache = _cache(_DictRedis())
        assert cache.get("查不到的查询") is None
        stats = cache.get_stats()
        assert stats["misses"] == 1
        assert stats["l1_hits"] == 0
        assert stats["errors"] == 0


class TestBackendFailureIsNotAMiss:
    def test_all_backends_error_is_errors_not_misses(self):
        cache = _cache(_DictRedis(get_error=True))
        assert cache.get("任意查询") is None
        stats = cache.get_stats()
        assert stats["errors"] == 1, "后端全挂不得记成缓存未命中"
        assert stats["misses"] == 0
        assert stats["l1_hits"] == 0

    def test_failure_does_not_inflate_the_miss_denominator(self):
        # 4 次后端失败 + 1 次真实未命中：总口径里应只有 1 个 miss，命中率不被拉低。
        cache = _cache(_DictRedis(get_error=True))
        for _ in range(4):
            cache.get("任意查询")
        stats = cache.get_stats()
        assert stats["errors"] == 4
        assert stats["misses"] == 0
        assert stats["total"] == 0  # hit_rate 分母只含 命中 + 未命中
        assert stats["hit_rate"] == "0.0%"
