"""
P0-02: Cache Cross-User Leakage 跨用户隔离测试。

验证三层缓存（L1 Redis / L2 Qdrant / L3 Jaccard）遵守统一 CachePolicy：
- User A 的订单/退款/投诉回答绝不可能被 User B 命中。
- 公开 FAQ 仍可跨用户共享。
- 个性化回答在无身份时 fail closed（不缓存、不读共享槽）。
- 三层 scope/TTL/version 一致。

使用内存 FakeRedis / FakeQdrant，无需外部依赖。
"""

from __future__ import annotations

import time
from collections import namedtuple

import pytest

from cache.cache_policy import (
    CACHE_PAYLOAD_VERSION,
    CacheScope,
    hash_identity,
    resolve_cache_policy,
)
from cache.response_cache import ResponseCache
from rag.embedding_status import EmbeddingDimensionError, EmbeddingUnavailableError

# ============================================================================
# 内存 FakeRedis / FakeQdrant
# ============================================================================


class FakeRedis:
    """最小化 Redis 内存实现：get/setex/delete/scan_iter，支持 TTL 过期。"""

    def __init__(self):
        self._store: dict[str, tuple[str, float, int]] = {}  # key -> (value, set_at, ttl)

    def get(self, key):
        item = self._store.get(key)
        if item is None:
            return None
        value, set_at, ttl = item
        if ttl > 0 and (time.time() - set_at) >= ttl:
            del self._store[key]
            return None
        return value

    def setex(self, key, ttl, value):
        self._store[key] = (value, time.time(), int(ttl))

    def delete(self, *keys):
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    def scan_iter(self, pattern, count=100):  # noqa: ARG002
        prefix = pattern.rstrip("*")
        for k in list(self._store.keys()):
            if k.startswith(prefix):
                yield k


_SearchHit = namedtuple("_SearchHit", ["id", "score", "payload"])
_Collection = namedtuple("_Collection", "collections")
_Col = namedtuple("_Col", "name")
# 模拟 qdrant-client 1.18.0 QueryResponse（命中在 .points）— 与真实 API 形状一致
_QueryResp = namedtuple("_QueryResp", ["points"])


class FakeQdrant:
    """
    最小化 Qdrant 内存实现，支持 search/upsert/delete 与 must/should 过滤。
    仅覆盖 ResponseCache 实际使用的字段与算子（match / range gte,lt）。
    """

    def __init__(self):
        self._collections: set[str] = set()
        self._points: dict[int, dict] = {}  # point_id -> {"vector":..., "payload":...}

    def get_collections(self):
        return _Collection(collections=[_Col(name=n) for n in self._collections])

    def create_collection(self, collection_name, vectors_config=None):  # noqa: ARG002
        self._collections.add(collection_name)

    def recreate_collection(self, collection_name, vectors_config=None):  # noqa: ARG002
        self._collections.add(collection_name)
        self._points.clear()

    def upsert(self, collection_name, points):  # noqa: ARG002
        for p in points:
            self._points[int(p.id)] = {
                "vector": list(p.vector),
                "payload": dict(p.payload),
            }

    def delete(self, collection_name, points_selector):  # noqa: ARG002
        # points_selector may be a Filter (must/should) — best-effort remove matching
        if hasattr(points_selector, "must") or hasattr(points_selector, "should"):
            must = getattr(points_selector, "must", None) or []
            should = getattr(points_selector, "should", None) or []
            to_remove = []
            for pid, rec in self._points.items():
                if self._match(rec["payload"], must, should):
                    to_remove.append(pid)
            for pid in to_remove:
                del self._points[pid]
        return namedtuple("_DelResult", ["count"])(count=len(self._points))

    def query_points(self, collection_name, query, limit=1, score_threshold=0.0, query_filter=None):  # noqa: ARG002
        """模拟 qdrant-client 1.18.0 query_points()：返回 QueryResponse 形状（.points）。

        与真实 API 一致：向量入参 query，filter 为 query_filter，命中在 .points。
        """
        must = getattr(query_filter, "must", None) or [] if query_filter else []
        should = getattr(query_filter, "should", None) or [] if query_filter else []
        results = []
        for pid, rec in self._points.items():
            payload = rec["payload"]
            if not self._match(payload, must, should):
                continue
            # 简化评分：完全相同的 query_text → 1.0；否则基于向量内积近似（测试够用）
            score = self._score(query, rec["vector"])
            if score >= score_threshold:
                results.append(_SearchHit(id=pid, score=score, payload=payload))
        results.sort(key=lambda r: r.score, reverse=True)
        return _QueryResp(points=results[:limit])

    # ---- 过滤求值 ----
    @staticmethod
    def _eval_field(payload, cond):
        key = cond.key
        if hasattr(cond, "match") and cond.match is not None:
            return payload.get(key) == cond.match.value
        rng = getattr(cond, "range", None)
        if rng is None:
            return True
        val = payload.get(key)
        if val is None:
            return False
        if rng.gte is not None and val < rng.gte:
            return False
        if rng.lte is not None and val > rng.lte:
            return False
        if rng.lt is not None and val >= rng.lt:
            return False
        return not (rng.gt is not None and val <= rng.gt)

    def _match(self, payload, must, should):
        for cond in must:
            if not self._eval_field(payload, cond):
                return False
        # 至少一条 should 满足（server 默认 min_should=1）
        return not (should and not any(self._eval_field(payload, cond) for cond in should))

    @staticmethod
    def _score(a, b):
        # 测试向量由 ResponseCache._embed_query 产生；同 query_text 相同向量 → 1.0
        if a == b:
            return 1.0
        # 余弦近似（仅用于排序）
        try:
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb) if na and nb else 0.0
        except Exception:
            return 0.0


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def qdrant():
    return FakeQdrant()


def _cache(redis=None, qdrant=None, **kw):
    """构造启用全部三层的 ResponseCache。

    P0-05: 注入真实（mock）embedding (_SemanticEmbedding)，不再依赖
    deterministic-random 回退——该回退已被删除。同一查询产生同一向量，
    供 L2 语义层验证 scope_key 隔离。
    """
    defaults = {
        "fallback_enabled": True,
        "fallback_threshold": 0.1,
    }
    defaults.update(kw)
    return ResponseCache(
        redis_client=redis,
        qdrant_client=qdrant,
        embedding_model=_SemanticEmbedding(),
        **defaults,
    )


ORDER_QUERY = "我的订单 ORD20260530001 的物流状态"
FAQ_QUERY = "烟酰胺能美白吗"


# ============================================================================
# 1. test_cross_user_cache_isolation  (核心 AC)
# ============================================================================


class TestCrossUserCacheIsolation:
    """User A 的订单回答绝不可能由 User B 命中（三层分别验证）。"""

    def test_l1_user_a_order_not_served_to_user_b(self, redis, qdrant):
        """L1 Redis: User A 写入订单回答，User B 查同句不得命中。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            ORDER_QUERY,
            "User A 的私有订单物流：已发货",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # User B 查同句 → 不得返回 A 的私有回答
        got = cache.get(ORDER_QUERY, metadata={"user_id": "user_B"})
        assert got != "User A 的私有订单物流：已发货"
        assert got is None

    def test_l1_user_a_can_hit_own_order(self, redis):
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            ORDER_QUERY,
            "User A 的私有订单物流：已发货",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == (
            "User A 的私有订单物流：已发货"
        )

    def test_l3_jaccard_user_isolation(self, qdrant):
        """L3 Jaccard: 即使查询文本高度相似，跨用户也不得命中。"""
        cache = _cache(redis=None, qdrant=None)
        cache.put(
            "查询我的退款进度 RD001",
            "User A 退款 50 元",
            metadata={"intent_type": "refund", "user_id": "user_A"},
        )
        # User B 用相似句查询 → 不得命中 A 的退款
        got = cache.get(
            "查询我的退款进度 RD001",
            metadata={"user_id": "user_B"},
        )
        assert got is None
        # User A 本人可命中
        got_a = cache.get(
            "查询我的退款进度 RD001",
            metadata={"user_id": "user_A"},
        )
        assert got_a == "User A 退款 50 元"

    def test_l2_qdrant_user_isolation(self, qdrant):
        """L2 Qdrant: User A 的订单 payload 不得被 User B 命中。"""
        cache = _cache(redis=None, qdrant=qdrant, fallback_enabled=False)
        cache.put(
            ORDER_QUERY,
            "User A 的私有订单物流：已发货",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # 关闭 L3 后仅 L2 生效；User B 查同句不得命中
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) is None
        # User A 本人可命中
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == (
            "User A 的私有订单物流：已发货"
        )

    def test_unknown_intent_personal_response_not_leaked(self, redis):
        """未知/缺失 intent_type 的个人响应不得进入 SHARED（reviewer 攻击向量）。

        场景：路由误分类或 LLM 返回未知意图，但响应含个人数据。allowlist 语义
        要求未知意图退化为用户作用域，User B 不得命中。
        """
        cache = _cache(redis=redis, qdrant=None)
        # 未知 intent_type + user_A 写入
        cache.put(
            ORDER_QUERY,
            "PRIVATE-A",
            metadata={"intent_type": "unknown_intent", "user_id": "user_A"},
        )
        # User B 查同句 → 不得命中 A 的私有回答
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) is None
        # User A 本人可命中（USER 作用域）
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == "PRIVATE-A"
        # shared 槽位不应存在该条目
        for k in redis.scan_iter("cache:resp:*"):
            assert "shared" not in k

    def test_missing_intent_personal_response_not_leaked(self, redis):
        """intent_type 完全缺失（None）的个人响应不得进入 SHARED。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            ORDER_QUERY,
            "PRIVATE-A-missing-intent",
            metadata={"user_id": "user_A"},  # 无 intent_type
        )
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) is None
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == (
            "PRIVATE-A-missing-intent"
        )

    def test_unknown_intent_no_identity_fail_closed(self, redis):
        """未知/缺失 intent + 无身份 → fail closed（不写、不读）。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            ORDER_QUERY,
            "不应缓存",
            metadata={"intent_type": "unknown_intent"},  # 无 user_id
        )
        assert list(redis.scan_iter("cache:resp:*")) == []
        assert cache.get(ORDER_QUERY) is None

    @pytest.mark.parametrize("intent", ["recommendation", "technical_support"])
    def test_context_dependent_response_is_not_shared(self, intent, redis):
        """带会话历史生成的推荐/技术回答必须保持用户作用域。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            "请继续根据我的情况回答",
            f"User A 的 {intent} 私人回答",
            metadata={"intent_type": intent, "user_id": "user_A"},
        )

        assert cache.get("请继续根据我的情况回答", metadata={"user_id": "user_B"}) is None
        assert cache.get("请继续根据我的情况回答", metadata={"user_id": "user_A"}) == (
            f"User A 的 {intent} 私人回答"
        )


# ============================================================================
# 2. test_personalized_response_not_shared
# ============================================================================


class TestPersonalizedResponseNotShared:
    """个性化回答绝不进入共享作用域。"""

    @pytest.mark.parametrize(
        "intent",
        ["order_status", "refund", "complaint", "after_sales", "billing"],
    )
    def test_personalized_not_in_shared_slot(self, intent, redis):
        """写入个性化回答后，shared 作用域键不得存在该回答。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            f"{intent} 查询 ABC123",
            "私有回答",
            metadata={"intent_type": intent, "user_id": "user_A"},
        )
        # 无身份读取只能探测 shared 槽位 → 不得命中个性化回答
        assert cache.get(f"{intent} 查询 ABC123") is None

    def test_complaint_does_not_leak_between_users(self, redis):
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            "我的投诉 CP001 处理进度",
            "User A 投诉已升级",
            metadata={"intent_type": "complaint", "user_id": "user_A"},
        )
        assert cache.get(
            "我的投诉 CP001 处理进度", metadata={"user_id": "user_B"}
        ) is None


# ============================================================================
# 3. test_public_faq_can_be_shared
# ============================================================================


class TestPublicFaqCanBeShared:
    """公开 FAQ 可跨用户共享缓存。"""

    def test_faq_written_by_a_hit_by_b(self, redis):
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            FAQ_QUERY,
            "烟酰胺可抑制黑色素转移",
            metadata={"intent_type": "knowledge_qa", "user_id": "user_A"},
        )
        # User B 无需提供身份也能命中公开 FAQ
        assert cache.get(FAQ_QUERY) == "烟酰胺可抑制黑色素转移"
        assert cache.get(FAQ_QUERY, metadata={"user_id": "user_B"}) == (
            "烟酰胺可抑制黑色素转移"
        )

    def test_public_jaccard_shared_across_users(self, qdrant):
        cache = _cache(redis=None, qdrant=None)
        cache.put(
            "玫瑰精华液成分",
            "含玫瑰精油与透明质酸",
            metadata={"intent_type": "product_info", "user_id": "user_A"},
        )
        # User B 用相似句也能命中（L3 共享作用域）
        got = cache.get(
            "玫瑰精华液的成分是什么", metadata={"user_id": "user_B"}
        )
        assert got == "含玫瑰精油与透明质酸"


# ============================================================================
# 4. test_cache_ttl_policy
# ============================================================================


class TestCacheTTLPolicy:
    """TTL 按 intent_type 生效；DISABLED 不写入。"""

    def test_personalized_ttl_shorter_than_public(self):
        order = resolve_cache_policy("order_status", user_id="u1")
        faq = resolve_cache_policy("knowledge_qa", user_id="u1")
        assert order.ttl < faq.ttl

    def test_disabled_not_cached(self, redis):
        """无身份的个性化意图 → DISABLED → 不写 L1。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            ORDER_QUERY,
            "不应被缓存",
            metadata={"intent_type": "order_status"},  # 无 user_id
        )
        # L1 不应存在任何键
        keys = list(redis.scan_iter("cache:resp:*"))
        assert keys == []
        assert cache.get(ORDER_QUERY) is None

    def test_ttl_applied_to_l1_setex(self, redis):
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            FAQ_QUERY,
            "公开回答",
            metadata={"intent_type": "knowledge_qa", "user_id": "user_A"},
        )
        # 验证 setex 的 TTL 与策略一致（knowledge_qa=604800）
        items = list(redis._store.values())
        assert items, "L1 should have one entry"
        assert items[0][2] == 604800


# ============================================================================
# 5. test_cache_scope_consistency
# ============================================================================


class TestCacheScopeConsistency:
    """相同查询文本、不同用户 → 不同 L1 键 / L3 条目。"""

    def test_l1_keys_differ_per_user(self, redis):
        cache = _cache(redis=redis, qdrant=None)
        cache.put(ORDER_QUERY, "A", metadata={"intent_type": "order_status", "user_id": "user_A"})
        cache.put(ORDER_QUERY, "B", metadata={"intent_type": "order_status", "user_id": "user_B"})
        keys = sorted(redis.scan_iter("cache:resp:*"))
        assert len(keys) == 2
        # 键含不同用户作用域哈希
        k_a = [k for k in keys if hash_identity("user_A") in k][0]
        k_b = [k for k in keys if hash_identity("user_B") in k][0]
        assert k_a != k_b

    def test_same_query_different_users_no_collision(self, redis):
        """User A/B 各写各的订单回答，互不覆盖。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(ORDER_QUERY, "A-order", metadata={"intent_type": "order_status", "user_id": "user_A"})
        cache.put(ORDER_QUERY, "B-order", metadata={"intent_type": "order_status", "user_id": "user_B"})
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == "A-order"
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) == "B-order"

    def test_public_query_single_shared_key(self, redis):
        """公开查询无论多少用户只产生一个 shared 键。"""
        cache = _cache(redis=redis, qdrant=None)
        for u in ["user_A", "user_B", "user_C"]:
            cache.put(FAQ_QUERY, "faq", metadata={"intent_type": "knowledge_qa", "user_id": u})
        keys = list(redis.scan_iter("cache:resp:*"))
        assert len(keys) == 1


# ============================================================================
# 6. test_l1_l2_l3_apply_same_cache_policy  (三层 parity)
# ============================================================================


class TestLayerPolicyParity:
    """三层缓存遵守相同 scope / version / fail-closed 语义。"""

    def test_all_three_layers_isolate_personalized(self, redis, qdrant):
        """User A 个性化回答在 L1/L2/L3 三层均不可被 User B 命中。"""
        cache = _cache(redis=redis, qdrant=qdrant)
        cache.put(
            ORDER_QUERY,
            "User A 私有",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # User B 在三层均不得命中
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) is None
        # 直接检查 L1/L2/L3 内部存储均无 B 可达的条目
        # L1: 无 shared 键，无 user_B 键
        for k in redis.scan_iter("cache:resp:*"):
            assert "shared" not in k or hash_identity("user_A") in k
        # L2: 所有 payload scope != shared（均为 user 作用域，scope_key=hash(A)）
        for rec in qdrant._points.values():
            assert rec["payload"]["scope"] == CacheScope.USER.value
            assert rec["payload"]["scope_key"] == "u:" + hash_identity("user_A")

    def test_all_three_layers_share_public(self, redis, qdrant):
        cache = _cache(redis=redis, qdrant=qdrant)
        cache.put(
            FAQ_QUERY,
            "公开 FAQ",
            metadata={"intent_type": "knowledge_qa", "user_id": "user_A"},
        )
        # 任意用户可命中（探测 shared + 自身作用域）
        assert cache.get(FAQ_QUERY, metadata={"user_id": "user_B"}) == "公开 FAQ"
        assert cache.get(FAQ_QUERY) == "公开 FAQ"
        # L2 payload 为 shared
        for rec in qdrant._points.values():
            assert rec["payload"]["scope"] == CacheScope.SHARED.value

    def test_version_in_all_payloads(self, redis, qdrant):
        cache = _cache(redis=redis, qdrant=qdrant)
        cache.put(FAQ_QUERY, "v", metadata={"intent_type": "knowledge_qa", "user_id": "user_A"})
        expected = f"{CACHE_PAYLOAD_VERSION}:1"
        # L1 key 含 version
        for k in redis.scan_iter("cache:resp:*"):
            assert expected in k
        # L2 payload 含 version
        for rec in qdrant._points.values():
            assert rec["payload"]["version"] == expected

    def test_version_mismatch_blocks_stale_entry(self, redis, qdrant):
        """版本号变更后，旧条目不可被读取（整体失效）。"""
        cache = _cache(redis=redis, qdrant=qdrant, content_version="1")
        cache.put(FAQ_QUERY, "旧版本回答", metadata={"intent_type": "knowledge_qa", "user_id": "u"})
        # 切换 content_version → 旧条目 version 不匹配，不得命中
        cache2 = _cache(redis=redis, qdrant=qdrant, content_version="2")
        assert cache2.get(FAQ_QUERY) is None


# ============================================================================
# Backward compatibility
# ============================================================================


class TestCacheAPIBackwardCompat:
    """现有 Cache API 不被无计划破坏（AC）。"""

    def test_no_metadata_no_identity_fails_closed(self, redis):
        """无 metadata 且无身份 → 非公开意图 → fail closed（不缓存）。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put("你好", "您好！")  # 无 intent_type，无 user_id
        assert cache.get("你好") is None

    def test_explicit_public_intent_shared_without_identity(self, redis):
        """显式公开意图（chitchat）即使无身份也可共享缓存（向后兼容公开 FAQ）。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put("你好", "您好！", metadata={"intent_type": "chitchat"})
        assert cache.get("你好") == "您好！"

    def test_public_intent_metadata_still_works(self, redis):
        """旧式 metadata（intent_type=公开意图 + user_role）仍工作。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put("test", "result", metadata={"intent_type": "knowledge_qa", "user_role": "vip"})
        assert cache.get("test") == "result"
        assert cache.get("test", metadata={"user_role": "vip"}) == "result"

    def test_invalidate_does_not_leak_across_users(self, redis):
        """invalidate(query) 不应错误地跨用户失效（应按作用域精确失效）。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(ORDER_QUERY, "A", metadata={"intent_type": "order_status", "user_id": "user_A"})
        cache.put(ORDER_QUERY, "B", metadata={"intent_type": "order_status", "user_id": "user_B"})
        # 失效 A 的条目
        cache.invalidate(ORDER_QUERY, metadata={"intent_type": "order_status", "user_id": "user_A"})
        # B 的条目应仍存在
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) == "B"


class TestExplicitPolicyAndTenantScope:
    """显式 CachePolicy 写入路径与租户作用域隔离（补充覆盖）。"""

    def test_set_with_explicit_policy_user_scope(self, redis):
        """set/put 接受显式 policy，绕过 metadata 解析，按 policy 作用域写入。"""
        from cache.cache_policy import resolve_cache_policy

        cache = _cache(redis=redis, qdrant=None)
        policy = resolve_cache_policy("order_status", user_id="user_X")
        cache.put(ORDER_QUERY, "X 的订单", policy=policy)
        # 仅 user_X 可命中
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_X"}) == "X 的订单"
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_Y"}) is None

    def test_set_with_explicit_disabled_policy_skips_write(self, redis):
        """显式 DISABLED policy 不写入任何层。"""
        from cache.cache_policy import resolve_cache_policy

        cache = _cache(redis=redis, qdrant=None)
        policy = resolve_cache_policy("complaint")  # 无身份 → DISABLED
        assert not policy.cacheable
        cache.put(ORDER_QUERY, "不应缓存", policy=policy)
        assert list(redis.scan_iter("cache:resp:*")) == []
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_X"}) is None

    def test_invalidate_with_explicit_policy_only_removes_that_scope(self, redis):
        """invalidate(policy=...) 仅失效该作用域条目，不影响其他用户。"""
        from cache.cache_policy import resolve_cache_policy

        cache = _cache(redis=redis, qdrant=None)
        pa = resolve_cache_policy("order_status", user_id="user_A")
        pb = resolve_cache_policy("order_status", user_id="user_B")
        cache.put(ORDER_QUERY, "A", policy=pa)
        cache.put(ORDER_QUERY, "B", policy=pb)
        cache.invalidate(ORDER_QUERY, policy=pa)
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) is None
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) == "B"

    def test_tenant_scope_isolation(self, redis):
        """租户作用域：tenant_A 的投诉不得被 tenant_B 命中。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            "我的投诉 CP001",
            "tenant_A 投诉",
            metadata={"intent_type": "complaint", "tenant_id": "tenant_A"},
        )
        assert cache.get(
            "我的投诉 CP001", metadata={"tenant_id": "tenant_A"}
        ) == "tenant_A 投诉"
        assert cache.get(
            "我的投诉 CP001", metadata={"tenant_id": "tenant_B"}
        ) is None

    def test_invalidate_with_tenant_metadata_is_scoped(self, redis):
        """invalidate(query, metadata={tenant_id}) 仅失效该租户作用域条目。"""
        cache = _cache(redis=redis, qdrant=None)
        cache.put(
            "我的投诉 CP001",
            "A",
            metadata={"intent_type": "complaint", "tenant_id": "tenant_A"},
        )
        cache.put(
            "我的投诉 CP001",
            "B",
            metadata={"intent_type": "complaint", "tenant_id": "tenant_B"},
        )
        cache.invalidate(
            "我的投诉 CP001",
            metadata={"intent_type": "complaint", "tenant_id": "tenant_A"},
        )
        # tenant_A 失效，tenant_B 仍存在
        assert cache.get(
            "我的投诉 CP001", metadata={"tenant_id": "tenant_A"}
        ) is None
        assert cache.get(
            "我的投诉 CP001", metadata={"tenant_id": "tenant_B"}
        ) == "B"

    def test_l3_scoped_invalidate_preserves_other_scope(self, qdrant):
        """L3 Jaccard 按作用域失效：失效 shared 不影响用户作用域条目。"""
        cache = _cache(redis=None, qdrant=None)
        # 用户作用域条目
        cache.put(
            "退款进度 RD001",
            "User A 退款",
            metadata={"intent_type": "refund", "user_id": "user_A"},
        )
        # 失效 shared 作用域（消息总线风格，无身份）
        cache.invalidate("退款进度 RD001")
        # 用户作用域条目仍存在
        assert cache.get(
            "退款进度 RD001", metadata={"user_id": "user_A"}
        ) == "User A 退款"

    def test_personalized_disabled_does_not_pollute_shared_l3(self, qdrant):
        """无身份的个性化回答不写入 L3 shared 槽，后续无身份读取不得命中。"""
        cache = _cache(redis=None, qdrant=None)
        cache.put(
            "我的订单 ORD002 状态",
            "私有",
            metadata={"intent_type": "order_status"},  # 无 user_id → DISABLED
        )
        # 无身份读取 → 仅探测 shared → 不应命中
        assert cache.get("我的订单 ORD002 状态") is None

    def test_l3_ttl_expiry_evicts_entry(self, qdrant):
        """L3 条目超过 default TTL 后应被淘汰且不再命中（真实过期行为）。"""
        # default TTL=0 → 写入即过期
        cache = _cache(
            redis=None, qdrant=None, l1_ttl_policy={"default": 0, "knowledge_qa": 0}
        )
        cache.put(
            FAQ_QUERY,
            "已过期",
            metadata={"intent_type": "knowledge_qa", "user_id": "user_A"},
        )
        # 写入后立即读取 → TTL=0 触发淘汰分支 → 不命中
        assert cache.get(FAQ_QUERY, metadata={"user_id": "user_A"}) is None
        # 条目应已被 _l3_evict_key 移除
        assert all(entry[3] != "shared" for entry in cache._l3_cache.values())

    def test_l3_expires_by_policy_ttl_not_default(self, monkeypatch):
        """L3 必须按 policy TTL 过期，与 L1/L2 一致（CACHE-5 TTL parity）。

        order_status policy TTL=300s，default TTL=3600s。写入后推进时间到 >300s 且
        <3600s：L3 应已按 policy TTL 过期（None）；若 L3 误用 default TTL 则仍命中
        （即 parity bug —— L1/L2 用 policy.ttl，L3 用 default）。Codex re-review AC18。
        """
        import cache.response_cache as rcm

        cache = _cache(redis=None, qdrant=None)  # 仅 L3 路径
        base = 1000.0
        # 写入 order_status（policy.ttl=300）于 t=base
        monkeypatch.setattr(rcm.time, "time", lambda: base)
        cache.put(
            "我的订单 ORD-PARITY 状态",
            "A 的私有订单",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # 推进到 base+400：> policy 300，< default 3600
        monkeypatch.setattr(rcm.time, "time", lambda: base + 400)
        got = cache.get("我的订单 ORD-PARITY 状态", metadata={"user_id": "user_A"})
        # L3 应按 policy TTL 过期 → None；用 default 则仍返回私有回答（bug）
        assert got is None, "L3 must expire by policy TTL (300s), not default (3600s)"


class TestBusInvalidationScope:
    """消息总线失效事件的作用域语义（subscribe_to_bus）。"""

    async def test_bus_invalidate_with_identity_is_user_scoped(self, redis):
        """携带 user_id 的失效消息只失效该用户作用域，不影响 shared 与其他用户。"""
        from core.message_bus import Message, MessageBus

        bus = MessageBus()
        cache = _cache(redis=redis, qdrant=None)
        await cache.subscribe_to_bus(bus)

        # shared 公开条目 + 两个用户的个性化条目
        cache.put(FAQ_QUERY, "公开", metadata={"intent_type": "knowledge_qa", "user_id": "uA"})
        cache.put(
            ORDER_QUERY,
            "A 订单",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        cache.put(
            ORDER_QUERY,
            "B 订单",
            metadata={"intent_type": "order_status", "user_id": "user_B"},
        )

        # 发布携带 user_A 的失效事件
        await bus.publish(
            Message(
                topic="cache:invalidate",
                payload={"query": ORDER_QUERY, "user_id": "user_A"},
            )
        )

        # user_A 的订单失效；user_B 的订单与公开 FAQ 不受影响
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) is None
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_B"}) == "B 订单"
        assert cache.get(FAQ_QUERY) == "公开"

    async def test_bus_invalidate_without_identity_is_shared_only(self, redis):
        """无身份的失效消息只失效 shared 作用域（公开知识更新场景）。"""
        from core.message_bus import Message, MessageBus

        bus = MessageBus()
        cache = _cache(redis=redis, qdrant=None)
        await cache.subscribe_to_bus(bus)

        cache.put(FAQ_QUERY, "公开", metadata={"intent_type": "knowledge_qa", "user_id": "uA"})
        cache.put(
            ORDER_QUERY,
            "A 订单",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )

        # 无身份失效事件 → 只清 shared
        await bus.publish(
            Message(topic="cache:invalidate", payload={"query": FAQ_QUERY})
        )
        assert cache.get(FAQ_QUERY) is None
        # 用户作用域条目不受无身份失效影响
        assert cache.get(ORDER_QUERY, metadata={"user_id": "user_A"}) == "A 订单"


# ============================================================================
# P0-02 Step 17 — 日志/指标隐私：fail-closed 路径日志不得泄露查询/响应正文
# ============================================================================


class TestCacheLogPrivacy:
    """P0-02 Step 17：缓存日志不得泄露响应正文或查询中的客户/订单数据。

    fail-closed（DISABLED）路径的日志只允许记录 scope / reason / cache tier
    等安全字段，不得写入查询正文或响应正文（个性化查询前缀可能含订单号等
    customer/order data）。
    """

    def test_fail_closed_log_excludes_query_and_response(self, redis):
        """DISABLED 路径的 debug 日志不含查询正文与响应正文。

        core.logger 设置 propagate=False，故 caplog（root handler）无法捕获
        "cache" logger；此处直接向该 logger 挂载捕获 handler 以真实断言日志内容。
        """
        import logging

        from cache.response_cache import logger as cache_logger

        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture()
        handler.setLevel(logging.DEBUG)
        cache_logger.addHandler(handler)
        try:
            cache = _cache(redis=redis, qdrant=None, fallback_enabled=False)
            sensitive_query = "我的订单 ORD-SECRET-12345 状态"
            sensitive_response = "PRIVATE-BODY-SHOULD-NEVER-LOG"
            cache.set(
                sensitive_query,
                sensitive_response,
                metadata={"intent_type": "order_status"},
            )
        finally:
            cache_logger.removeHandler(handler)

        joined = "\n".join(records)
        # 响应正文绝不进日志（P0-02 Required Change #6：不得泄露响应正文）
        assert "PRIVATE-BODY-SHOULD-NEVER-LOG" not in joined
        # 查询中的订单号不进 fail-closed 日志（Step 17：不得记录 customer/order data）
        assert "ORD-SECRET-12345" not in joined


# ============================================================================
# Operational branch coverage
# ============================================================================


class _BrokenRedis(FakeRedis):
    """Exercise graceful degradation without a Redis server."""

    def get(self, key):  # noqa: ARG002
        raise RuntimeError("redis get unavailable")

    def setex(self, key, ttl, value):  # noqa: ARG002
        raise RuntimeError("redis set unavailable")

    def delete(self, *keys):  # noqa: ARG002
        raise RuntimeError("redis delete unavailable")

    def scan_iter(self, pattern, count=100):  # noqa: ARG002
        raise RuntimeError("redis scan unavailable")


class _SearchErrorQdrant(FakeQdrant):
    def __init__(self):
        super().__init__()
        self._collections.add("response_cache")

    def query_points(self, *args, **kwargs):  # noqa: ARG002
        # qdrant-client 1.18.0 L2 read path is query_points() (not .search());
        # 模拟真实 query_points() 失败，验证 get() 降级不抛异常。
        raise RuntimeError("qdrant query_points unavailable")


class _UpsertErrorQdrant(FakeQdrant):
    def __init__(self):
        super().__init__()
        self._collections.add("response_cache")

    def upsert(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("qdrant upsert unavailable")


class _DeleteErrorQdrant(FakeQdrant):
    def delete(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("qdrant delete unavailable")


class _RecreateErrorQdrant(FakeQdrant):
    def recreate_collection(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("qdrant recreate unavailable")


class _VectorWithToList:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class _ToListEmbedding:
    def encode(self, query):  # noqa: ARG002
        return _VectorWithToList([0.1, 0.2])


class _ListEmbedding:
    def encode(self, query):  # noqa: ARG002
        return [0.3, 0.4]


class _FailingEmbedding:
    def encode(self, query):  # noqa: ARG002
        raise RuntimeError("embedding unavailable")


class TestResponseCacheOperationalBranches:
    """Validate graceful degradation and maintenance branches of all layers."""

    def test_redis_failures_degrade_without_raising(self):
        cache = _cache(redis=_BrokenRedis(), qdrant=None, fallback_enabled=False)
        metadata = {"intent_type": "chitchat"}

        cache.put("redis failure", "response", metadata=metadata)
        assert cache.get("redis failure") is None
        cache.invalidate("redis failure")
        cache.clear()
        assert cache.get_stats()["l1_size"] == 0

    def test_qdrant_success_maintenance_paths(self):
        qdrant = FakeQdrant()
        cache = _cache(redis=None, qdrant=qdrant, fallback_enabled=False)
        cache.put("qdrant maintenance", "response", metadata={"intent_type": "chitchat"})

        cache.invalidate_by_filter({"intent_type": "chitchat"})
        assert cache.cleanup_expired() == 0
        cache.clear()
        assert qdrant._points == {}

    def test_qdrant_failures_degrade_without_raising(self):
        search_cache = _cache(
            redis=None,
            qdrant=_SearchErrorQdrant(),
            fallback_enabled=False,
        )
        # 先写入一条 shared 公开条目：非失败场景下 query_points() 会命中。
        # 此处断言 None 的唯一原因是 query_points() 抛错被降级（非空结果），避免
        # “空库即 None” 的空断言（Codex re-review：勿弱化既有验收测试）。
        search_cache.put(
            "present shared query",
            "resp",
            metadata={"intent_type": "knowledge_qa", "user_id": "uA"},
        )
        assert search_cache.get("present shared query", metadata={"user_id": "uB"}) is None
        assert search_cache.get("present shared query", metadata={"user_id": "uA"}) is None

        upsert_cache = _cache(
            redis=None,
            qdrant=_UpsertErrorQdrant(),
            fallback_enabled=False,
        )
        upsert_cache.put(
            "qdrant upsert failure",
            "response",
            metadata={"intent_type": "chitchat"},
        )

        delete_cache = _cache(
            redis=None,
            qdrant=_DeleteErrorQdrant(),
            fallback_enabled=False,
        )
        delete_cache.invalidate_by_filter({"intent_type": "chitchat"})
        assert delete_cache.cleanup_expired() == 0

        recreate_cache = _cache(
            redis=None,
            qdrant=_RecreateErrorQdrant(),
            fallback_enabled=False,
        )
        recreate_cache.clear()

    def test_embedding_and_internal_edge_paths(self):
        from cache.cache_policy import resolve_cache_policy

        no_qdrant = ResponseCache(fallback_enabled=False)
        assert no_qdrant._ensure_l2_collection() is False
        # P0-05: _embed_query fails closed — no embed_fn → EmbeddingUnavailableError
        # (no deterministic-random fallback). Wrong-dimension vectors are rejected
        # (EMB-6), not padded/truncated; runtime encode failure raises too.
        with pytest.raises(EmbeddingUnavailableError):
            no_qdrant._embed_query("fallback")
        with pytest.raises(EmbeddingDimensionError):
            ResponseCache(embedding_model=_ToListEmbedding())._embed_query("tolist")
        with pytest.raises(EmbeddingDimensionError):
            ResponseCache(embedding_model=_ListEmbedding())._embed_query("list")
        with pytest.raises(EmbeddingUnavailableError):
            ResponseCache(embedding_model=_FailingEmbedding())._embed_query("error")

        cache = _cache(redis=None, qdrant=None)
        cache._l1 = {"ignored": "value"}
        assert cache._l1 == cache._l3_cache
        cache._l3_evict_key("missing")
        policy = resolve_cache_policy("chitchat")
        cache._jaccard_set("", "ignored", time.time(), policy)
        assert cache._jaccard_search("", ["shared"], cache._version) is None

        cache._jaccard_set("versioned query", "response", time.time(), policy)
        assert cache._jaccard_search("versioned query", ["shared"], "stale") is None
        cache._l3_evict_query("versioned query", None)

        cache._metrics_flush_ops = 100
        cache._metrics_last_flush = time.monotonic()
        cache._schedule_metrics_flush()
        assert cache._metrics_pending == 1


# ============================================================================
# P0-02 re-review AC3/AC9 — L2 语义缓存隔离（真实 query_points API + 注入向量）
# ============================================================================


class _SemanticEmbedding:
    """注入式 embedding：同一簇内不同文本返回同一向量（cosine 1.0 = 语义命中）。

    模拟真实语义相似（不同查询文本、相同语义向量），用以验证 L2 语义缓存的
    scope_key 隔离：即使向量本会命中，scope_key must 过滤仍排除异用户条目。
    """

    _CLUSTERS = {
        "我的订单什么时候到": "order",  # 与下一句语义相似 → 同向量
        "我的订单大概几时能送到": "order",
        "烟酰胺能美白吗": "faq",
    }

    def __init__(self, dim: int = 1024):
        self._dim = dim

    def encode(self, query):
        cluster = self._CLUSTERS.get(query)
        if cluster == "order":
            return [1.0] + [0.0] * (self._dim - 1)
        if cluster == "faq":
            return [0.0, 1.0] + [0.0] * (self._dim - 2)
        return [0.0] * self._dim  # 未知查询 → 零向量，不产生语义命中


class TestL2SemanticIsolation:
    """L2 语义缓存隔离（Codex re-review AC3/AC9）—— query_points() 真实 API 路径。

    使用 API-faithful FakeQdrant（query_points/.points）+ 注入向量证明：
    - AC9  semantic positive：同用户、不同文本、语义相似 → 命中。
    - AC3  scope_key 强制过滤：异用户、语义相似（同向量）→ 仍 None。
    """

    @staticmethod
    def _l2(qdrant):
        return ResponseCache(
            redis_client=None,
            qdrant_client=qdrant,
            embedding_model=_SemanticEmbedding(),
            fallback_enabled=False,
            fallback_threshold=0.1,
        )

    def test_l2_semantic_same_user_hit(self, qdrant):
        """同用户、不同文本、语义相似（同向量 cosine 1.0）→ L2 命中（AC9 正向）。"""
        cache = self._l2(qdrant)
        cache.put(
            "我的订单什么时候到",
            "A 的订单回答",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # 不同文本、同向量 → 语义命中；同用户 scope → 命中
        got = cache.get("我的订单大概几时能送到", metadata={"user_id": "user_A"})
        assert got == "A 的订单回答"

    def test_l2_semantic_cross_user_miss(self, qdrant):
        """异用户、语义相似（同向量）→ scope_key must 过滤排除 → None（AC3）。

        证明 scope_key 是强制 Qdrant 过滤：向量本会命中，但 scope_key=u:hash(A)
        ∉ B 的探测集 [shared, u:hash(B)] → 不返回。没有该过滤则此处会跨用户泄漏。
        """
        cache = self._l2(qdrant)
        cache.put(
            "我的订单什么时候到",
            "A 的私有订单",
            metadata={"intent_type": "order_status", "user_id": "user_A"},
        )
        # 异用户、同向量 → 语义本会命中，但 scope 过滤排除
        assert cache.get("我的订单大概几时能送到", metadata={"user_id": "user_B"}) is None
        # 同用户仍可命中 → 证明隔离由 scope_key 决定，而非向量不匹配
        assert (
            cache.get("我的订单大概几时能送到", metadata={"user_id": "user_A"})
            == "A 的私有订单"
        )
