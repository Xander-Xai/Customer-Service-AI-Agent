"""P0-02: CachePolicy 解析单元测试。"""

import pytest

from cache.cache_policy import (
    CACHE_PAYLOAD_VERSION,
    CacheScope,
    CacheSensitivity,
    hash_identity,
    read_scope_keys,
    resolve_cache_policy,
)


class TestResolveCachePolicy:
    """resolve_cache_policy 决策规则验证。"""

    def test_personal_intent_with_user_id_is_user_scoped(self):
        """个性化意图 + user_id → USER 作用域，可缓存。"""
        p = resolve_cache_policy("order_status", user_id="user_001")
        assert p.cacheable is True
        assert p.scope is CacheScope.USER
        assert p.sensitivity is CacheSensitivity.PERSONAL
        assert p.scope_key == "u:" + hash_identity("user_001")

    def test_personal_intent_without_identity_fails_closed(self):
        """个性化意图 + 无身份 → DISABLED，fail closed。"""
        p = resolve_cache_policy("order_status")
        assert p.cacheable is False
        assert p.scope is CacheScope.DISABLED
        assert p.sensitivity is CacheSensitivity.PERSONAL
        # DISABLED 的 scope_key 为 "disabled"，绝不落入 shared 或用户槽位
        assert p.scope_key == "disabled"

    @pytest.mark.parametrize(
        "intent",
        ["order_status", "order_query", "billing", "refund", "complaint", "after_sales"],
    )
    def test_all_personal_intents_are_personal(self, intent):
        """spec 列举的个性化意图均判定为 PERSONAL。"""
        p = resolve_cache_policy(intent, user_id="u1")
        assert p.sensitivity is CacheSensitivity.PERSONAL
        assert p.scope is CacheScope.USER

    def test_public_intent_is_shared_regardless_of_identity(self):
        """公开意图 → SHARED，与身份无关。"""
        p_with = resolve_cache_policy("knowledge_qa", user_id="user_001")
        p_without = resolve_cache_policy("knowledge_qa")
        assert p_with.scope is CacheScope.SHARED
        assert p_without.scope is CacheScope.SHARED
        assert p_with.scope_key == "shared"
        assert p_without.scope_key == "shared"

    @pytest.mark.parametrize(
        "intent",
        [
            "product_info",
            "usage_guide",
            "knowledge_qa",
            "pricing_stock",
            "policy_rule",
            "chitchat",
            "greeting",
            "general_inquiry",
        ],
    )
    def test_public_allowlist_intents_are_shared(self, intent):
        p = resolve_cache_policy(intent, user_id="u1")
        assert p.scope is CacheScope.SHARED
        assert p.sensitivity is CacheSensitivity.PUBLIC

    @pytest.mark.parametrize("intent", ["recommendation", "technical_support"])
    def test_context_dependent_intents_are_user_scoped(self, intent):
        """会话上下文参与生成的回答不得进入跨用户 shared cache。"""
        with_identity = resolve_cache_policy(intent, user_id="u1")
        without_identity = resolve_cache_policy(intent)

        assert with_identity.scope is CacheScope.USER
        assert with_identity.sensitivity is CacheSensitivity.PERSONAL
        assert without_identity.scope is CacheScope.DISABLED
        assert without_identity.cacheable is False

    def test_unknown_or_missing_intent_is_not_shared(self):
        """None/空/未知/非法 intent 一律不进入 SHARED（allowlist fail-closed）。"""
        for intent in (None, "", "  ", "unknown_intent", "default", "totally_invalid"):
            p = resolve_cache_policy(intent, user_id="u1")
            assert p.scope is CacheScope.USER, f"intent={intent!r} 应为 USER，实得 {p.scope}"
            assert p.sensitivity is CacheSensitivity.PERSONAL
        # 无身份 + 未知/缺失意图 → DISABLED（fail closed）
        for intent in (None, "", "unknown_intent", "default"):
            p = resolve_cache_policy(intent)
            assert p.scope is CacheScope.DISABLED
            assert p.cacheable is False

    def test_tenant_scoped_when_only_tenant_id(self):
        """个性化 + 仅 tenant_id（无 user_id）→ TENANT 作用域。"""
        p = resolve_cache_policy("complaint", tenant_id="tenant_A")
        assert p.cacheable is True
        assert p.scope is CacheScope.TENANT
        assert p.scope_key == "t:" + hash_identity("tenant_A")

    def test_user_takes_precedence_over_tenant(self):
        """同时有 user_id 与 tenant_id → 优先 USER。"""
        p = resolve_cache_policy("billing", user_id="u1", tenant_id="t1")
        assert p.scope is CacheScope.USER

    def test_none_intent_is_user_scoped_with_identity(self):
        """None/空 intent 视为 default，非公开 → 有身份则 USER 作用域（非 SHARED）。"""
        p = resolve_cache_policy(None, user_id="u1")
        assert p.scope is CacheScope.USER
        assert p.sensitivity is CacheSensitivity.PERSONAL

    def test_ttl_from_policy(self):
        """TTL 按 intent_type 从 ttl_policy 读取。"""
        p = resolve_cache_policy(
            "order_status",
            user_id="u1",
            ttl_policy={"order_status": 300, "default": 3600},
        )
        assert p.ttl == 300

    def test_ttl_falls_back_to_default(self):
        """未知 intent 的 TTL 回退到 default（需有身份，否则 DISABLED ttl=0）。"""
        p = resolve_cache_policy(
            "unknown_intent",
            user_id="u1",
            ttl_policy={"order_status": 300, "default": 3600},
        )
        assert p.ttl == 3600

    def test_disabled_policy_has_zero_ttl(self):
        p = resolve_cache_policy("complaint")
        assert p.ttl == 0

    def test_version_includes_payload_and_content(self):
        p = resolve_cache_policy("knowledge_qa", content_version="2")
        assert p.version == f"{CACHE_PAYLOAD_VERSION}:2"

    def test_policy_is_frozen(self):
        """CachePolicy 不可变，防止下游层篡改 scope。"""
        p = resolve_cache_policy("order_status", user_id="u1")
        # frozen dataclass 在赋值时抛 FrozenInstanceError（AttributeError 子类）
        with pytest.raises(AttributeError):
            p.scope = CacheScope.SHARED  # type: ignore[misc]

    def test_public_intents_override(self):
        """可通过 public_intents 参数自定义公开 allowlist。"""
        custom_public = frozenset({"order_status", "my_intent"})
        # order_status 被显式声明为公开 → SHARED
        p = resolve_cache_policy("order_status", user_id="u1", public_intents=custom_public)
        assert p.scope is CacheScope.SHARED
        # my_intent 也公开
        p1 = resolve_cache_policy("my_intent", user_id="u1", public_intents=custom_public)
        assert p1.scope is CacheScope.SHARED
        # knowledge_qa 不在自定义 allowlist → 不再公开 → USER（fail-closed 到用户）
        p2 = resolve_cache_policy("knowledge_qa", user_id="u1", public_intents=custom_public)
        assert p2.scope is CacheScope.USER


class TestScopeKeyAndHash:
    """scope_key / hash_identity 稳定性与隔离性。"""

    def test_hash_identity_stable(self):
        assert hash_identity("user_001") == hash_identity("user_001")

    def test_hash_identity_distinct(self):
        assert hash_identity("user_001") != hash_identity("user_002")

    def test_hash_identity_empty_returns_anon(self):
        assert hash_identity(None) == "anon"
        assert hash_identity("") == "anon"

    def test_scope_key_user_isolation(self):
        a = resolve_cache_policy("order_status", user_id="user_A")
        b = resolve_cache_policy("order_status", user_id="user_B")
        assert a.scope_key != b.scope_key
        assert a.scope_key.startswith("u:")
        assert b.scope_key.startswith("u:")

    def test_scope_key_shared_constant(self):
        p = resolve_cache_policy("knowledge_qa", user_id="u1")
        assert p.scope_key == "shared"


class TestReadScopeKeys:
    """读取端作用域集合（路由前 cache-check 使用）。"""

    def test_no_identity_only_shared(self):
        assert read_scope_keys() == ["shared"]
        assert read_scope_keys(None, None) == ["shared"]

    def test_with_user_id_includes_user_scope(self):
        keys = read_scope_keys(user_id="user_001")
        assert "shared" in keys
        assert "u:" + hash_identity("user_001") in keys
        assert len(keys) == 2

    def test_both_user_and_tenant_probed_when_both_present(self):
        """user_id 与 tenant_id 同时存在时，两者作用域均纳入读取探测（OR 一致性）。"""
        keys = read_scope_keys(user_id="u1", tenant_id="t1")
        assert any(k.startswith("u:") for k in keys)
        assert any(k.startswith("t:") for k in keys)
        assert "shared" in keys

    def test_tenant_only(self):
        keys = read_scope_keys(tenant_id="t1")
        assert "t:" + hash_identity("t1") in keys
