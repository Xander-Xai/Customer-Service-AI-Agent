"""
统一缓存策略 (P0-02: Cache Cross-User Leakage)

所有响应在写入缓存前必须先经过 CachePolicy，由它决定：
- cacheable: 是否允许缓存
- scope: 共享 / 用户级 / 租户级 / 禁用
- ttl: 过期秒数（按 intent_type）
- sensitivity: 公开 / 个人
- version: 内容版本（变更 schema 时整体失效旧条目）

安全语义（allowlist / fail-closed）
--------------------------------
安全边界是 **公开意图 allowlist**，而非个性化意图 blocklist：

- intent ∈ PUBLIC_ALLOWLIST（FAQ、成分功效、政策、产品信息、闲聊…）→ SHARED，
  所有用户共享，提升命中率。
- intent ∉ PUBLIC_ALLOWLIST（含个性化意图 order_status/refund/complaint/售后、
  以及未知/空/非法/缺失 intent_type）→ **不进入 SHARED**：
  - 有可信 user_id → 写入用户作用域，仅本人可命中（命中率略降但不泄漏）。
  - 有可信 tenant_id（无 user_id）→ 写入租户作用域。
  - 无可信身份 → fail closed（不写、不读共享槽位）。

allowlist 而非 blocklist 的关键意义：当路由误分类、LLM 返回未知意图、或
intent_type 缺失时，个性化回答绝不会落入 SHARED 槽位被跨用户读取，而是退化到
调用方用户作用域或彻底不缓存。

读取端在路由分类之前执行（此时 intent 未知），因此读取时同时探测 SHARED 与
调用方身份对应的作用域：因为非公开数据永远不会落入 SHARED 槽位，所以 OR 探测
既不会泄漏，又能命中公开 FAQ。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class CacheScope(str, Enum):
    """缓存作用域。str 子类便于 JSON 序列化与 Qdrant payload 比较。"""

    SHARED = "shared"      # 公开内容，所有用户共享
    USER = "user"          # 用户级，按 user_id 隔离
    TENANT = "tenant"      # 租户级，按 tenant_id 隔离（预留）
    DISABLED = "disabled"  # 不可缓存（fail closed）


class CacheSensitivity(str, Enum):
    """内容敏感度。"""

    PUBLIC = "public"
    PERSONAL = "personal"


# 公开意图 allowlist：只有显式列入此集合的意图才可进入 SHARED 共享缓存。
# P0-02 安全边界：未知/空值/非法/缺失 intent_type 一律视为非公开 → fail-closed
# （有身份 → 用户/租户作用域；无身份 → DISABLED 不缓存）。这是 allowlist 而非
# blocklist 语义——任何未明确声明为公开的意图都不进入 shared，从而避免
# 路由误分类或 LLM 返回未知意图时将个性化回答泄漏到共享槽。
# 该集合可通过 resolve_cache_policy(public_intents=...) 覆盖。
_DEFAULT_PUBLIC_INTENTS = frozenset(
    {
        # 售前 / 产品 / 技术 / 通用（router INTENT_CLASSES 中的公开意图）
        "product_info",
        "recommendation",
        "technical_support",
        "usage_guide",
        "greeting",
        "general",
        "general_inquiry",
        # 知识库 / 价格库存 / 政策 / 闲聊（CACHE_TTL_POLICY 中的公开意图）
        "knowledge_qa",
        "pricing_stock",
        "policy_rule",
        "chitchat",
        # 兼容
        "cosmetic_advice",
    }
)

# 缓存 payload 版本：当 L1/L2/L3 的存储结构或作用域语义发生破坏性变更时，
# 提升 CACHE_PAYLOAD_VERSION 即可使所有旧条目无法被读取（version 不匹配），
# 无需显式迁移。
CACHE_PAYLOAD_VERSION = "v1"

# 内容版本：当知识库/提示词/产品目录发生大规模变更需要整体失效缓存时提升。
_DEFAULT_CONTENT_VERSION = "1"

# 默认 TTL（秒），与 core.config.CACHE_TTL_POLICY 保持一致；调用方可覆盖。
_DEFAULT_TTL_POLICY: dict[str, int] = {
    "knowledge_qa": 604800,
    "pricing_stock": 300,
    "policy_rule": 86400,
    "order_status": 300,
    "after_sales": 3600,
    "chitchat": 600,
    "default": 3600,
}


def hash_identity(identity: str | None) -> str:
    """
    将 user_id/tenant_id 转为稳定的短哈希，用于缓存键分区。

    哈希目的仅为生成稳定分区键（非安全用途）；同时避免将原始身份字符串直接
    写入 Redis key / Qdrant payload，降低日志与转储中的身份暴露面。

    Args:
        identity: user_id 或 tenant_id（可能为 None/空）

    Returns:
        12 字符十六进制哈希；空身份返回 "anon"
    """
    if not identity:
        return "anon"
    return hashlib.md5(  # noqa: S324 — 非安全用途，仅作分区键
        str(identity).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]


@dataclass(frozen=True)
class CachePolicy:
    """
    不可变缓存策略对象。frozen=True 保证策略一旦解析即不可被下游修改，
    避免某个缓存层意外篡改 scope/ttl 造成层间不一致。
    """

    cacheable: bool
    scope: CacheScope
    ttl: int
    sensitivity: CacheSensitivity
    version: str
    user_id: str | None = None
    tenant_id: str | None = None

    @property
    def scope_key(self) -> str:
        """
        稳定的作用域分区键，供 L1 Redis key / L3 内存元组 / L2 Qdrant payload 共用。

        - SHARED → "shared"
        - USER   → "u:" + hash_identity(user_id)
        - TENANT → "t:" + hash_identity(tenant_id)
        - DISABLED → "disabled"
        """
        if self.scope is CacheScope.SHARED:
            return "shared"
        if self.scope is CacheScope.USER:
            return "u:" + hash_identity(self.user_id)
        if self.scope is CacheScope.TENANT:
            return "t:" + hash_identity(self.tenant_id)
        return "disabled"


def resolve_cache_policy(
    intent_type: str | None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    *,
    ttl_policy: dict[str, int] | None = None,
    public_intents: frozenset[str] | None = None,
    content_version: str = _DEFAULT_CONTENT_VERSION,
) -> CachePolicy:
    """
    根据意图与可信身份解析缓存策略（写入端使用）—— **allowlist / fail-closed**。

    决策规则（安全边界是公开意图 allowlist，而非个性化意图 blocklist）：

        intent ∈ PUBLIC_ALLOWLIST                    → SHARED，可缓存（与身份无关）
        intent ∉ PUBLIC_ALLOWLIST（含未知/空/非法/缺失/个性化）
            + user_id                                → USER 作用域，可缓存
            + tenant_id（无 user_id）                → TENANT 作用域，可缓存
            + 无身份                                 → DISABLED，fail closed（不缓存）

    该 allowlist 语义保证：路由误分类、LLM 返回未知意图、或 intent_type 缺失时，
    个性化回答绝不会落入 SHARED 槽位被跨用户读取，而是退化到调用方用户作用域
    （命中率略降但不泄漏）或在无身份时彻底不缓存。

    Args:
        intent_type: 路由分类的意图（order_status / knowledge_qa / ...）；None/空视为 default（非公开）
        user_id: 可信认证用户 ID（来自 P0-04 的 state["user_id"]）
        tenant_id: 可信租户 ID（可选）
        ttl_policy: intent → TTL 秒数映射；None 使用默认
        public_intents: 公开意图 allowlist；None 使用默认
        content_version: 内容版本，用于整体失效

    Returns:
        CachePolicy
    """
    intent = (intent_type or "default").strip() or "default"
    policy = ttl_policy or _DEFAULT_TTL_POLICY
    public = public_intents or _DEFAULT_PUBLIC_INTENTS
    ttl = int(policy.get(intent, policy.get("default", 3600)))
    version = f"{CACHE_PAYLOAD_VERSION}:{content_version}"

    # 仅显式 allowlist 内的公开意图 → SHARED。
    if intent in public:
        return CachePolicy(
            cacheable=True,
            scope=CacheScope.SHARED,
            ttl=ttl,
            sensitivity=CacheSensitivity.PUBLIC,
            version=version,
            user_id=user_id,
            tenant_id=tenant_id,
        )

    # 未知/空/非法/个性化意图 → 必须有可信身份才可缓存（fail-closed）。
    if user_id:
        return CachePolicy(
            cacheable=True,
            scope=CacheScope.USER,
            ttl=ttl,
            sensitivity=CacheSensitivity.PERSONAL,
            version=version,
            user_id=user_id,
            tenant_id=tenant_id,
        )
    if tenant_id:
        return CachePolicy(
            cacheable=True,
            scope=CacheScope.TENANT,
            ttl=ttl,
            sensitivity=CacheSensitivity.PERSONAL,
            version=version,
            user_id=user_id,
            tenant_id=tenant_id,
        )
    # 非公开意图 + 无可信身份 → fail closed，绝不进入共享缓存。
    return CachePolicy(
        cacheable=False,
        scope=CacheScope.DISABLED,
        ttl=0,
        sensitivity=CacheSensitivity.PERSONAL,
        version=version,
        user_id=user_id,
        tenant_id=tenant_id,
    )


def read_scope_keys(
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> list[str]:
    """
    读取端作用域键集合（路由分类前 cache-check 使用）。

    读取发生在意图分类之前，无法判断本次查询是公开还是个性化，因此同时探测
    SHARED 与调用方所有可用身份对应的作用域。安全性保证：个性化数据写入时
    永远不会落入 SHARED 槽位（见 resolve_cache_policy），故 OR 探测不会造成
    跨用户泄漏。

    当 user_id 与 tenant_id 同时存在时，两者作用域均纳入探测——因为可能存在
    早期仅以 tenant_id 写入的条目，后续读取若带 user_id 而漏探 tenant 作用域
    会造成静默未命中（不会泄漏，但破坏 OR 探测一致性）。

    Returns:
        作用域键列表，始终包含 "shared"，并在存在身份时追加对应作用域键。
    """
    keys = ["shared"]
    if user_id:
        keys.append("u:" + hash_identity(user_id))
    if tenant_id:
        keys.append("t:" + hash_identity(tenant_id))
    return keys
