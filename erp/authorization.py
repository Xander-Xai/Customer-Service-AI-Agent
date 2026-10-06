"""
ERP Authorization Boundary (P0-03 — ERP IDOR / Authorization).

在 Service/Tool 边界强制执行订单/客户资源的归属校验，依据 P0-04 已建立的
authenticated principal（state["user_id"]）。LLM / Agent 只能 *提议* order_id /
customer_id，不能 *授权* 资源归属。

数据流：

    authenticated principal (ContextVar，由 P0-04 在可信请求边界写入、
        BaseAgent.process_with_retry 按 request 传播)
        -> resolve trusted customer_id (权威 user→customer 映射)
        -> requested resource (order_id / customer_id 来自 prompt / 模型参数)
        -> ownership decision (allow / deny)
        -> authorized data (或安全空结果，避免资源枚举)

安全不变量（CODEX_PROJECT_REMEDIATION_SPEC P0-03）：
  AUTHZ-1 归属仅来自 authenticated principal，不来自 prompt/LLM/资源自声明。
  AUTHZ-2 仅返回属于当前 principal 的订单/客户。
  AUTHZ-3 prompt / 模型参数中的 customer_id 不能覆盖 authenticated owner。
  AUTHZ-4 缺失 order_id 时只返回 principal 自己的订单，绝不返回任意他人订单。
  AUTHZ-5 未授权与不存在对外结果一致（均返回空），不泄漏资源存在性。
  AUTHZ-6 授权校验发生在 ERP 数据返回 Agent/LLM 之前。
  AUTHZ-7 Billing / Aftersales / ERP Tool 共用同一 boundary。
"""

import contextlib
import contextvars
import hashlib
import re
from typing import Any

from core.logger import get_logger, get_trace_id
from core.protocols import ERPProtocol

logger = get_logger("erp.authorization")

# AC16: resource-type-aware strict ERP id shapes. A resource identifier is
# logged verbatim ONLY if it matches the proven shape for its claimed
# resource type. Everything else — pure-digit phones, "tel13800000000" (a
# letter prefix glued to a phone-digit run), natural-language text, an
# oversized blob, a type-mismatched id — is hashed. Invariant: any
# user/model-controlled resource_id that cannot be PROVEN to be a real ERP
# id of the claimed type must never enter an erp.* log raw. Real ids carry a
# known type-prefix (ORD/C/P/user_) + digits; "tel13800000000" shares the
# letters-then-digits shape of a real order id, so only a known-prefix
# allowlist (not a "has-a-letter" rule) can separate it from ORD20260530001.
_RESOURCE_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "order": re.compile(r"^ORD\d+$"),
    "customer": re.compile(r"^C\d+$"),
    "product": re.compile(r"^P\d+$"),
    "user": re.compile(r"^user_\d+$"),
}
# Fallback (caller has no resource_type): accept any id matching a known ERP
# id shape, hash everything else. Still rejects phones / NL / "tel…".
_ALL_KNOWN_SHAPES_RE = re.compile(r"^(?:ORD\d+|C\d+|P\d+|user_\d+)$")
_UNTRUSTED_PREFIX = "untrusted:"
_HASH_LEN = 12
_MAX_LOGGED_ID_LEN = 64


def _digest_resource_id(value: str) -> str:
    """Stable 12-hex SHA-256 digest for hashing an untrusted resource id."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_LEN]


# P0-03: 当前请求的 authenticated principal（user_id）与入口标识。
# 由 BaseAgent.process_with_retry 从 state["user_id"] 设置（P0-04 保证所有
# transport 一致写入该字段）。ErpAuthorizationService 通过此 ContextVar 取得
# 可信主体，不依赖 prompt / 模型参数。
_erp_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "erp_principal", default=None
)
_erp_entrypoint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "erp_entrypoint", default=None
)


def set_erp_principal(user_id: str | None) -> None:
    """设置当前请求的 authenticated principal（在可信请求边界 / Agent 入口）。"""
    _erp_principal.set(user_id)


def get_erp_principal() -> str | None:
    """获取当前 authenticated principal；缺失时返回 None（调用方须 fail closed）。"""
    return _erp_principal.get()


def set_erp_entrypoint(name: str | None) -> None:
    """设置入口标识（Agent 名 / 工具名），用于安全事件审计。"""
    _erp_entrypoint.set(name)


def get_erp_entrypoint() -> str | None:
    return _erp_entrypoint.get()


@contextlib.contextmanager
def erp_principal(user_id: str | None):
    """测试/辅助上下文管理器：设置 principal 并在退出时恢复，避免跨用例污染。"""
    token = _erp_principal.set(user_id)
    try:
        yield
    finally:
        _erp_principal.reset(token)


@contextlib.contextmanager
def bound_erp_request(user_id: str | None, entrypoint: str | None):
    """P0-03 Remaining Risk #3: 绑定当前请求的 authenticated principal +
    entrypoint，退出时 token-reset 两者。

    process_with_retry 用此上下文管理器包裹整个请求处理，确保请求结束
    （成功 / 永久异常 / 重试耗尽）后 ContextVar 恢复到调用前的值。顶层
    调用前值为 None -> 请求结束后 get_erp_principal() 仍为 None，后续无
    authenticated identity 的调用一律 fail closed，杜绝跨请求身份泄漏。

    使用 ContextVar token 语义正确处理嵌套调用与异常路径。
    """
    ptoken = _erp_principal.set(user_id)
    etoken = _erp_entrypoint.set(entrypoint)
    try:
        yield
    finally:
        _erp_principal.reset(ptoken)
        _erp_entrypoint.reset(etoken)


def sanitize_resource_id(resource_id: str | None, resource_type: str | None = None) -> str:
    """Resource-type-aware strict allowlist + length-limit + stable-hash for
    any erp.* security/audit log (AC16 / Section 13).

    Shared by the AuthZ boundary (``_emit_denial``) and the real ERP
    adapter's ``get_order_owner`` exception log, so ONE policy governs every
    erp.* log — no raw prompt / PII / order_id is ever recorded anywhere.

    A resource identifier is logged verbatim ONLY if it matches the proven
    shape for its claimed resource type (``ORD\\d+`` for orders, ``C\\d+``
    for customers, ``P\\d+`` for products, ``user_\\d+`` for users) AND is
    within the length cap. A caller that omits ``resource_type`` gets the
    type-agnostic fallback (any known ERP id shape). Everything else — a
    pure-digit phone, "tel13800000000", natural-language text, an oversized
    blob, an attacker sentinel — is reduced to ``untrusted:<hash>`` so
    repeated attempts stay correlatable without recording PII.
    """
    if not resource_id:
        return ""
    rid = str(resource_id)
    # Length cap: never log a huge blob even if it begins with a valid prefix.
    if len(rid) > _MAX_LOGGED_ID_LEN:
        return f"{_UNTRUSTED_PREFIX}{_digest_resource_id(rid)}"
    shape = _RESOURCE_ID_SHAPES.get(resource_type) if resource_type else None
    matched = shape.match(rid) if shape is not None else _ALL_KNOWN_SHAPES_RE.match(rid)
    if matched:
        return rid
    return f"{_UNTRUSTED_PREFIX}{_digest_resource_id(rid)}"


def _emit_denial(
    resource_type: str,
    resource_id: str,
    reason_code: str,
    principal: str | None,
) -> None:
    """授权失败时发出结构化安全事件（Step 11 / AC16）。

    只记录安全审计所需的最小字段；绝不记录 JWT / API key / password /
    订单完整正文 / 不必要 PII。resource_id 经 sanitize_resource_id(resource_type)
    过滤：仅当 resource_id 可证明为其所属资源类型的真实 ERP id（ORD\\d+ 等已知
    前缀形状）时才原样记录，否则一律稳定 hash，禁止原始 prompt/tool 自然语言
    文本或疑似 PII（纯数字手机号 / tel+数字 等）进入日志。trace_id 在直接调用
    场景（无请求上下文）下回退为 ``no-trace``，确保审计字段始终非空。
    """
    safe_rid = sanitize_resource_id(resource_id, resource_type)
    # AC16: direct-call paths may have no request trace set; fall back to a
    # deterministic non-empty marker so every event is auditable/correlatable.
    trace_id = get_trace_id() or "no-trace"
    entrypoint = get_erp_entrypoint()
    logger.warning(
        "erp_authz denied resource_type=%s resource_id=%s reason=%s "
        "principal=%s entrypoint=%s trace_id=%s",
        resource_type,
        safe_rid,
        reason_code,
        principal,
        entrypoint,
        trace_id,
        extra={
            "event_type": "erp_authz",
            "resource_type": resource_type,
            "resource_id": safe_rid,
            "authorization_result": "denied",
            "reason_code": reason_code,
            "principal": principal,
            "entrypoint": entrypoint,
            "trace_id": trace_id,
        },
    )


class ErpAuthorizationService:
    """ERP 授权边界（P0-03）。

    包装原始 ERPProtocol 适配器，对私人资源（订单、客户）强制归属校验；
    公共资源（产品、库存）直接委托，不做授权。

    authorized principal 来自 erp_principal ContextVar（P0-04 在可信请求边界
    写入 state["user_id"]，BaseAgent.process_with_retry 按 request 传播）。
    缺失或不可解析的 principal 一律 fail closed（不返回任何资源）。
    """

    def __init__(self, adapter: ERPProtocol):
        self._adapter = adapter

    async def resolve_customer_by_user(self, user_id: str | int | None) -> str | None:
        """委托权威映射解析 trusted customer_id。"""
        return await self._adapter.resolve_customer_by_user(user_id)

    async def query_product(self, keyword: str = "") -> list[dict[str, Any]]:
        # 公共资源：无归属校验。
        return await self._adapter.query_product(keyword)

    async def query_inventory(
        self, product_id: str = "", keyword: str = ""
    ) -> list[dict[str, Any]]:
        # 公共资源：无归属校验。
        return await self._adapter.query_inventory(product_id=product_id, keyword=keyword)

    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict[str, Any]]:
        """归属校验后的订单查询。

        - 缺失 principal / 不可解析 -> fail closed（空）。
        - 给定 order_id -> 先以最小归属元数据（get_order_owner，仅 customer_id）
          判定 ownership，通过后才取完整 payload；不属于 principal 或不存在则
          拒绝（空），对外与“不存在”不可区分（完整 payload 永不进入 authz /
          Agent / LLM 内存）。AUTHZ-6 / Section 10。
        - 未给定 order_id -> 只返回 principal 自己的订单。prompt / 模型参数中的
          customer_id 不被信任为归属，忽略之（不能越权）。
        """
        principal = get_erp_principal()
        if not principal:
            _emit_denial("order", order_id or customer_id, "no_principal", principal)
            return []
        trusted_cid = await self._adapter.resolve_customer_by_user(principal)
        if not trusted_cid:
            _emit_denial("order", order_id or customer_id, "unresolved_principal", principal)
            return []

        if order_id:
            # AUTHZ-6 / Section 10: 最小归属元数据先行 — 仅取订单归属 customer_id，
            # 绝不在 ownership 判定前取得完整订单正文。拒绝路径下完整 payload
            # 永不被请求，故永不入 authz / Agent / LLM 内存。
            owner_cid = await self._adapter.get_order_owner(order_id)
            if not owner_cid:
                # 不存在或归属不可判定 -> fail closed（对外与 not_owner 一致）。
                _emit_denial("order", order_id, "not_found", principal)
                return []
            if owner_cid != trusted_cid:
                _emit_denial("order", order_id, "not_owner", principal)
                return []
            # Ownership confirmed — only now fetch the full authorized payload.
            rows = await self._adapter.query_order(order_id=order_id)
            # 防御性 scope：仅保留属于 principal 的行，防止 adapter 对该
            # order_id 返回额外行。
            return [r for r in rows if r.get("customer_id") == trusted_cid]

        # 无 order_id：只返回 principal 自己的订单。忽略 prompt customer_id。
        return await self._adapter.query_order(customer_id=trusted_cid)

    async def query_customer(self, customer_id: str) -> dict[str, Any] | None:
        """归属校验后的客户查询：仅当请求的 customer_id == trusted customer_id
        时返回；否则拒绝（None），对外与“不存在”不可区分。"""
        principal = get_erp_principal()
        if not principal:
            _emit_denial("customer", customer_id, "no_principal", principal)
            return None
        trusted_cid = await self._adapter.resolve_customer_by_user(principal)
        if not trusted_cid:
            _emit_denial("customer", customer_id, "unresolved_principal", principal)
            return None
        if customer_id != trusted_cid:
            _emit_denial("customer", customer_id, "not_owner", principal)
            return None
        return await self._adapter.query_customer(customer_id)

    async def close(self) -> None:
        """资源清理委托给底层适配器。"""
        if hasattr(self._adapter, "close"):
            await self._adapter.close()
