"""
P0-03 — ERP IDOR / Authorization security tests.

These tests assert the DESIRED secure behavior (ownership enforced at the
Service/Tool boundary from the P0-04 authenticated principal). They are the
TDD red phase: they fail until ErpAuthorizationService exists and enforces.

Invariants covered (CODEX_PROJECT_REMEDIATION_SPEC P0-03 + user Steps 3/16):
  AUTHZ-1 Trusted Identity   — ownership from authenticated principal only
  AUTHZ-2 Resource Ownership  — only own orders/customers returned
  AUTHZ-3 Prompt Cannot Elevate — prompt/model customer_id cannot override
  AUTHZ-4 Missing ID Safe     — no-ID returns only principal's own orders
  AUTHZ-5 Non-Enumeration     — unauthorized == not-found externally
  AUTHZ-6 AuthZ Before Disclosure — denied payload never exposed
  AUTHZ-7 Shared Policy       — Billing + Aftersales + Tool share one boundary
"""

import contextlib
import logging

import pytest

from agents.base_agent import BaseAgent
from erp.authorization import (
    ErpAuthorizationService,
    erp_principal,
    get_erp_entrypoint,
    get_erp_principal,
    set_erp_entrypoint,
    set_erp_principal,
)
from erp.authorization import (
    logger as _authz_logger,
)
from erp.kingdee_adapter import KingdeeMockAdapter
from tools.erp_tools import create_erp_tools

# Mock ERP ownership (authoritative, server-trusted mapping):
#   user_001 -> C001 (王女士) owns ORD20260530001, ORD20260530003
#   user_002 -> C002 (李女士) owns ORD20260530002
ORDER_A = "ORD20260530001"  # C001 王女士
ORDER_B = "ORD20260530002"  # C002 李女士
ORDER_A2 = "ORD20260530003"  # C001 王女士
USER_A = "user_001"  # -> C001
USER_B = "user_002"  # -> C002


@pytest.fixture
def mock_adapter() -> KingdeeMockAdapter:
    return KingdeeMockAdapter()


@pytest.fixture
def authz(mock_adapter) -> ErpAuthorizationService:
    return ErpAuthorizationService(mock_adapter)


class _OrderFetchSpy(KingdeeMockAdapter):
    """Counts full-payload query_order calls on the adapter. get_order_owner is
    a separate minimal-metadata call and is NOT counted here — the distinction
    proves the full payload is never fetched before the ownership decision."""

    def __init__(self):
        super().__init__()
        self.full_query_order_calls = 0

    async def query_order(self, order_id: str = "", customer_id: str = "") -> list[dict]:
        self.full_query_order_calls += 1
        return await super().query_order(order_id=order_id, customer_id=customer_id)


# ============================================================================
# Step 5 — Trusted identity -> customer mapping (authoritative, server-side)
# ============================================================================


class TestTrustedCustomerResolution:
    """AUTHZ-1: ownership must derive from a trusted, server-side mapping,
    never from prompt/LLM/order-self-declaration."""

    async def test_resolve_known_user_to_customer(self, mock_adapter):
        assert await mock_adapter.resolve_customer_by_user(USER_A) == "C001"
        assert await mock_adapter.resolve_customer_by_user(USER_B) == "C002"

    async def test_resolve_unknown_user_returns_none_fail_closed(self, mock_adapter):
        """Unknown / unauthenticated user resolves to None -> fail closed."""
        assert await mock_adapter.resolve_customer_by_user("attacker") is None
        assert await mock_adapter.resolve_customer_by_user("") is None
        assert await mock_adapter.resolve_customer_by_user(None) is None

    async def test_service_resolve_delegates_to_adapter(self, authz):
        assert await authz.resolve_customer_by_user(USER_A) == "C001"


# ============================================================================
# AUTHZ-1 / AUTHZ-6 — missing identity fails closed, before disclosure
# ============================================================================


class TestMissingIdentityFailsClosed:
    async def test_order_query_without_principal_returns_empty(self, authz):
        # No erp_principal set on the ContextVar.
        assert get_erp_principal() is None
        result = await authz.query_order(order_id=ORDER_B)
        assert result == []  # denied, no payload

    async def test_customer_query_without_principal_returns_none(self, authz):
        assert get_erp_principal() is None
        assert await authz.query_customer("C001") is None

    async def test_unresolvable_principal_fails_closed(self, authz):
        # Authenticated principal exists but maps to no ERP customer.
        with erp_principal("attacker_no_mapping"):
            assert await authz.query_order(order_id=ORDER_A) == []
            assert await authz.query_customer("C001") is None


# ============================================================================
# AUTHZ-2 — resource ownership (owner allowed, non-owner denied)
# ============================================================================


class TestResourceOwnership:
    async def test_order_owner_can_read_own_order(self, authz):
        with erp_principal(USER_A):
            orders = await authz.query_order(order_id=ORDER_A)
        assert len(orders) == 1
        assert orders[0]["order_id"] == ORDER_A
        assert orders[0]["customer_id"] == "C001"

    async def test_non_owner_cannot_read_order(self, authz):
        with erp_principal(USER_A):
            orders = await authz.query_order(order_id=ORDER_B)  # belongs to C002
        assert orders == []

    async def test_order_idor_denied(self, authz):
        """Explicit cross-user IDOR regression: User A reading User B's order."""
        with erp_principal(USER_A):
            assert await authz.query_order(order_id=ORDER_B) == []
        # Symmetric: User B cannot read User A's order.
        with erp_principal(USER_B):
            assert await authz.query_order(order_id=ORDER_A) == []

    async def test_customer_lookup_is_scoped(self, authz):
        with erp_principal(USER_A):
            own = await authz.query_customer("C001")
            other = await authz.query_customer("C002")
        assert own is not None and own["name"] == "王女士"
        assert other is None  # cannot fetch another customer's profile


# ============================================================================
# AUTHZ-6 / Section 10 — full payload must not be fetched before ownership
# ============================================================================


class TestMinimalOwnershipMetadata:
    """Section 10 residual risk: the authz service must decide ownership using
    MINIMAL metadata (the order's owner customer_id), and only fetch the full
    order payload (customer_name / total / tracking) AFTER ownership is
    confirmed. A denied order's full row must never enter the authz service's
    memory."""

    @staticmethod
    def _spy() -> _OrderFetchSpy:
        return _OrderFetchSpy()

    async def test_full_payload_not_fetched_on_deny(self):
        spy = self._spy()
        authz = ErpAuthorizationService(spy)
        with erp_principal(USER_A):
            result = await authz.query_order(order_id=ORDER_B)  # belongs to C002
        assert result == []
        assert spy.full_query_order_calls == 0, "full payload fetched before ownership check"

    async def test_full_payload_fetched_once_after_owner_confirmed(self):
        spy = self._spy()
        authz = ErpAuthorizationService(spy)
        with erp_principal(USER_A):
            result = await authz.query_order(order_id=ORDER_A)  # belongs to C001
        assert len(result) == 1
        assert result[0]["order_id"] == ORDER_A
        assert spy.full_query_order_calls == 1

    async def test_nonexistent_order_does_not_fetch_full_payload(self):
        spy = self._spy()
        authz = ErpAuthorizationService(spy)
        with erp_principal(USER_A):
            result = await authz.query_order(order_id="ORD_DOES_NOT_EXIST")
        assert result == []  # not-found == denied (AUTHZ-5)
        assert spy.full_query_order_calls == 0

    async def test_no_principal_does_not_fetch_full_payload(self):
        spy = self._spy()
        authz = ErpAuthorizationService(spy)
        result = await authz.query_order(order_id=ORDER_B)  # no principal
        assert result == []
        assert spy.full_query_order_calls == 0


# ============================================================================
# AUTHZ-3 — prompt / model arguments cannot override request identity
# ============================================================================


class TestPromptAndModelCannotOverride:
    async def test_model_cannot_override_request_identity(self, authz):
        """Model/tool args claim customer_id=C001 (User A) while requesting
        User B's order. Authorized principal is still User A (C001). The order
        belongs to C002, so it must be denied regardless of the arg."""
        with erp_principal(USER_A):
            orders = await authz.query_order(order_id=ORDER_B, customer_id="C001")
        assert orders == []

    async def test_customer_id_from_prompt_cannot_override_identity(self, authz):
        """Prompt supplies customer_id=C002 (someone else). Authorized principal
        is User A (C001). No order_id given -> must return ONLY User A's own
        orders, never C002's."""
        with erp_principal(USER_A):
            orders = await authz.query_order(customer_id="C002")
        assert orders, "principal's own orders should still be returned"
        # Every returned order must belong to the principal's customer (C001).
        assert all(o["customer_id"] == "C001" for o in orders)
        assert not any(o["order_id"] == ORDER_B for o in orders)


# ============================================================================
# AUTHZ-4 — missing order ID must not list other users' orders
# ============================================================================


class TestMissingOrderIdIsSafe:
    async def test_missing_order_id_returns_only_own_orders(self, authz):
        with erp_principal(USER_A):
            orders = await authz.query_order()  # no order_id, no customer_id
        ids = {o["order_id"] for o in orders}
        # User A (C001) owns ORDER_A and ORDER_A2, never ORDER_B.
        assert ORDER_B not in ids
        assert ids.issubset({ORDER_A, ORDER_A2})

    async def test_missing_order_id_for_user_b_returns_only_user_b_orders(self, authz):
        with erp_principal(USER_B):
            orders = await authz.query_order()
        ids = {o["order_id"] for o in orders}
        assert ORDER_A not in ids
        assert ORDER_A2 not in ids
        assert ORDER_B in ids


# ============================================================================
# AUTHZ-5 — unauthorized vs not-found must be indistinguishable externally
# ============================================================================


class TestNonEnumeration:
    async def test_unauthorized_and_not_found_both_empty(self, authz):
        with erp_principal(USER_A):
            unauthorized = await authz.query_order(order_id=ORDER_B)  # exists, not yours
            not_found = await authz.query_order(order_id="ORD_DOES_NOT_EXIST")
        assert unauthorized == []
        assert not_found == []
        # Observable shape identical -> no existence leak.

    async def test_customer_unauthorized_and_not_found_both_none(self, authz):
        with erp_principal(USER_A):
            unauthorized = await authz.query_customer("C002")  # exists, not yours
            not_found = await authz.query_customer("C999")  # doesn't exist
        assert unauthorized is None
        assert not_found is None


# ============================================================================
# AUTHZ-6 / Step 14 — tool direct-call path cannot bypass the boundary
# ============================================================================


class TestToolAuthorizationBoundary:
    def _registry(self, authz):
        return create_erp_tools(authz)

    async def test_tool_owner_can_read_own_order(self, authz):
        registry = self._registry(authz)
        with erp_principal(USER_A):
            result = await registry.execute("query_order", {"order_id": ORDER_A})
        assert ORDER_A in result

    async def test_tool_non_owner_denied_before_disclosure(self, authz):
        registry = self._registry(authz)
        with erp_principal(USER_A):
            result = await registry.execute("query_order", {"order_id": ORDER_B})
        # C002's order details / customer name must NOT appear.
        assert ORDER_B not in result
        assert "李女士" not in result

    async def test_direct_tool_call_without_principal_fails_closed(self, authz):
        """Attack F: bypass the agent, call the ERP tool directly with no
        authenticated principal. Must NOT return the resource."""
        registry = self._registry(authz)
        assert get_erp_principal() is None
        result = await registry.execute("query_order", {"order_id": ORDER_B})
        assert ORDER_B not in result
        assert "李女士" not in result

    async def test_tool_model_args_cannot_override_identity(self, authz):
        registry = self._registry(authz)
        with erp_principal(USER_A):
            result = await registry.execute(
                "query_order", {"order_id": ORDER_B, "customer_id": "C001"}
            )
        assert ORDER_B not in result
        assert "李女士" not in result

    async def test_tool_customer_lookup_scoped(self, authz):
        registry = self._registry(authz)
        with erp_principal(USER_A):
            own = await registry.execute("query_customer", {"customer_id": "C001"})
            other = await registry.execute("query_customer", {"customer_id": "C002"})
        assert "王女士" in own
        assert "李女士" not in other


# ============================================================================
# AUTHZ-7 — Billing + Aftersales share one authorization boundary
# ============================================================================


class TestAgentAuthZParity:
    def _billing(self, authz):
        from agents.billing_agent import BillingAgent

        agent = BillingAgent()
        agent.erp = authz
        return agent

    def _aftersales(self, authz):
        from agents.aftersales_agent import AftersalesAgent

        agent = AftersalesAgent()
        agent.erp = authz
        return agent

    async def test_billing_owner_can_read_own_order(self, authz):
        agent = self._billing(authz)
        with erp_principal(USER_A):
            data = await agent._query_erp(f"查询订单 {ORDER_A} 物流")
        assert ORDER_A in data

    async def test_billing_non_owner_denied(self, authz):
        agent = self._billing(authz)
        with erp_principal(USER_A):
            data = await agent._query_erp(f"查询订单 {ORDER_B} 物流")
        assert ORDER_B not in data
        assert "李女士" not in data

    async def test_billing_missing_id_does_not_leak_other_orders(self, authz):
        agent = self._billing(authz)
        with erp_principal(USER_A):
            data = await agent._query_erp("帮我看看我的订单")
        assert ORDER_B not in data
        assert "李女士" not in data

    async def test_billing_prompt_customer_cannot_override(self, authz):
        agent = self._billing(authz)
        with erp_principal(USER_A):
            data = await agent._query_erp("我是客户C002 查一下我的订单")
        assert "李女士" not in data
        assert ORDER_B not in data

    async def test_aftersales_non_owner_denied(self, authz):
        agent = self._aftersales(authz)
        with erp_principal(USER_A):
            data = await agent._query_erp(ORDER_B)
        assert "李女士" not in data

    async def test_billing_and_aftersales_share_authz_policy(self, authz):
        """Both agents, same AuthZ service, same principal -> same denial."""
        billing = self._billing(authz)
        aftersales = self._aftersales(authz)
        with erp_principal(USER_A):
            b_data = await billing._query_erp(f"订单 {ORDER_B}")
            a_data = await aftersales._query_erp(ORDER_B)
        assert "李女士" not in b_data
        assert "李女士" not in a_data


# ============================================================================
# Step 11 — structured security event on authorization failure
# ============================================================================


@contextlib.contextmanager
def _capture_authz_events():
    """Attach a capture handler directly to the erp.authorization logger.

    get_logger sets propagate=False, so pytest's caplog (root handler) cannot
    see these records. A directly-attached handler captures them with their
    `extra=` attributes intact.
    """
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    handler.setLevel(logging.WARNING)
    _authz_logger.addHandler(handler)
    try:
        yield records
    finally:
        _authz_logger.removeHandler(handler)


def _last_authz_event(records: list[logging.LogRecord]) -> logging.LogRecord:
    """Return the most recent erp_authz event record, asserting one exists."""
    denial = [r for r in records if getattr(r, "event_type", None) == "erp_authz"]
    assert denial, "expected an erp_authz security event to be emitted"
    return denial[-1]


class TestSecurityEvent:
    async def test_denial_emits_structured_security_event(self, authz):
        with _capture_authz_events() as records, erp_principal(USER_A):
            await authz.query_order(order_id=ORDER_B)  # denied (not owner)

        denial = [r for r in records if getattr(r, "event_type", None) == "erp_authz"]
        assert denial, "an erp_authz security event must be emitted on denial"
        rec = denial[-1]
        assert getattr(rec, "authorization_result", None) == "denied"
        assert getattr(rec, "reason_code", None) == "not_owner"
        assert getattr(rec, "resource_type", None) == "order"
        assert getattr(rec, "principal", None) == USER_A  # safe identifier only
        # Rendered message must not leak secrets / PII / order payload.
        rendered = rec.getMessage()
        for secret in ("Bearer", "JWT", "api_key", "password", "李女士", "139****"):
            assert secret not in rendered, f"security event leaked '{secret}'"

    async def test_unauthenticated_emits_event(self, authz):
        with _capture_authz_events() as records:
            await authz.query_order(order_id=ORDER_B)  # no principal

        recs = [r for r in records if getattr(r, "event_type", None) == "erp_authz"]
        assert recs, "no-principal denial must emit an event"
        assert getattr(recs[-1], "reason_code", None) in (
            "no_principal",
            "unauthenticated",
        )
        assert getattr(recs[-1], "principal", None) is None


# ============================================================================
# AC16 — security event must not log raw user/PII input as resource_id
# ============================================================================


class TestSecurityEventSanitization:
    """AC16 / Section 13: the denial event's resource_id must be an allowlist
    identifier (or a stable hash) — never raw prompt/tool input that may carry
    PII. trace_id must always be non-empty so the event is auditable even in
    direct-call scenarios with no request context."""

    async def test_valid_resource_id_logged_verbatim(self, authz):
        # No principal -> no_principal denial. ORDER_B is a well-formed id.
        with _capture_authz_events() as records:
            await authz.query_order(order_id=ORDER_B)
        rec = _last_authz_event(records)
        assert getattr(rec, "resource_id", None) == ORDER_B

    async def test_untrusted_resource_id_not_logged_raw(self, authz):
        """Aftersales passes a natural-language query as order_id. That raw
        text (which may contain PII) must NOT appear in the security log —
        only a stable hash marker is permitted."""
        nl = "我的订单ORD20260530001物流到哪了 请帮忙查一下 13800000000"
        with _capture_authz_events() as records:
            await authz.query_order(order_id=nl)
        rec = _last_authz_event(records)
        rid = getattr(rec, "resource_id", "")
        # Raw NL / phone must not survive into the log.
        assert nl not in rid
        assert "13800000000" not in rid
        assert nl not in rec.getMessage()
        # Reduced to a bounded, stable, non-PII form.
        assert rid.startswith("untrusted:")

    async def test_resource_id_truncated_when_oversized(self, authz):
        """An oversized blob (even of valid chars) must not be logged in full."""
        long_id = "ORD" + "A" * 200  # 203 chars, valid charset, far over limit
        with _capture_authz_events() as records:
            await authz.query_order(order_id=long_id)
        rec = _last_authz_event(records)
        rid = getattr(rec, "resource_id", "")
        assert len(rid) <= 64
        assert long_id not in rid
        assert long_id not in rec.getMessage()

    async def test_untrusted_resource_id_hash_is_stable(self, authz):
        """Same raw input -> same hash, so repeated attempts are correlatable
        without recording the PII itself."""
        nl = "随便一段不可信自然语言查询abc"
        with _capture_authz_events() as r1:
            await authz.query_order(order_id=nl)
        with _capture_authz_events() as r2:
            await authz.query_order(order_id=nl)
        rid1 = getattr(_last_authz_event(r1), "resource_id", "")
        rid2 = getattr(_last_authz_event(r2), "resource_id", "")
        assert rid1 == rid2
        assert rid1.startswith("untrusted:")

    async def test_denial_trace_id_always_non_empty(self, authz):
        """Direct-call scenario (no HTTP request sets trace_id) must still
        emit a non-empty trace_id so the event is never un-correlatable."""
        with _capture_authz_events() as records:
            await authz.query_order(order_id=ORDER_B)
        rec = _last_authz_event(records)
        assert getattr(rec, "trace_id", "")  # non-empty

    async def test_customer_resource_id_also_sanitized(self, authz):
        """query_customer denial path must sanitize too (Aftersales passes NL
        as customer_id)."""
        nl = "查一下我的客户信息 谢谢"
        with _capture_authz_events() as records:
            await authz.query_customer(nl)
        rec = _last_authz_event(records)
        rid = getattr(rec, "resource_id", "")
        assert nl not in rid
        assert nl not in rec.getMessage()
        assert rid.startswith("untrusted:")

    async def test_pure_digit_phone_number_is_hashed_not_logged(self, authz):
        """AC16 (re-review probe): a pure-digit phone number passed as
        order_id is NOT a valid ERP id shape — real ids carry a letter
        type-prefix (ORD/C/P/user_), and a pure-digit string is exactly the
        phone-PII shape. It must be hashed, never logged verbatim."""
        phone = "13800000000"
        with _capture_authz_events() as records:
            await authz.query_order(order_id=phone)  # no principal -> denied
        rec = _last_authz_event(records)
        rid = getattr(rec, "resource_id", "")
        assert rid != phone
        assert phone not in rid
        assert phone not in rec.getMessage()
        assert rid.startswith("untrusted:")

    async def test_letter_prefixed_phone_digits_is_hashed_not_logged(self, authz):
        """AC16 (re-review probe #2): "tel13800000000" — a letter prefix
        glued to a phone-digit run — is user/model-controllable as an
        order_id and is NOT a provably-valid ERP order id (real order ids
        match ^ORD\\d+$). It shares the letters-then-digits shape of a real
        order id, so only a known-prefix allowlist can separate it. It must
        be hashed, never logged verbatim, even though it contains a letter
        (the old 'has-a-letter' rule let it through)."""
        bad = "tel13800000000"
        with _capture_authz_events() as records:
            await authz.query_order(order_id=bad)  # no principal -> denied
        rec = _last_authz_event(records)
        rid = getattr(rec, "resource_id", "")
        assert rid != bad
        assert bad not in rid
        assert "13800000000" not in rid
        assert "13800000000" not in rec.getMessage()
        assert rid.startswith("untrusted:")


# ============================================================================
# Step 12 — authorization is transport-agnostic (consumes P0-04 identity)
# ============================================================================


class TestTransportAgnosticAuthorization:
    """The boundary lives in ErpAuthorizationService and reads the principal
    that P0-04 threads uniformly through REST/SSE/WebSocket/multimodal
    (state["user_id"]). Therefore denial is identical regardless of how the
    request arrived. This test asserts the boundary decision given a
    principal; transport identity parity itself is proven by P0-04's tests
    (tests/unit/test_sse_identity.py)."""

    async def test_denial_identical_regardless_of_principal_source(self, authz):
        # Same principal (as any transport would place into state.user_id) ->
        # same denial for a foreign order.
        with erp_principal(USER_A):
            r1 = await authz.query_order(order_id=ORDER_B)
        with erp_principal(USER_A):
            r2 = await authz.query_order(order_id=ORDER_B)
        assert r1 == r2 == []

    async def test_owner_access_identical_across_calls(self, authz):
        with erp_principal(USER_A):
            r1 = await authz.query_order(order_id=ORDER_A)
            r2 = await authz.query_order(order_id=ORDER_A)
        assert r1 == r2 and r1[0]["order_id"] == ORDER_A


# ============================================================================
# Step 14 — container wiring injects the AuthZ boundary (no raw-adapter bypass)
# ============================================================================


class TestContainerWiring:
    """The container must inject ErpAuthorizationService (not the raw adapter)
    into agents and ERP tools. Otherwise a direct tool call or any agent would
    bypass the ownership boundary."""

    def test_get_erp_authz_wraps_raw_adapter(self):
        from core.container import ServiceContainer

        container = ServiceContainer()
        container.erp = KingdeeMockAdapter()
        svc = container._get_erp_authz()
        assert isinstance(svc, ErpAuthorizationService)

    def test_get_erp_authz_cached_for_same_adapter(self):
        from core.container import ServiceContainer

        container = ServiceContainer()
        container.erp = KingdeeMockAdapter()
        assert container._get_erp_authz() is container._get_erp_authz()

    def test_get_erp_authz_rewraps_when_adapter_replaced(self):
        from core.container import ServiceContainer

        container = ServiceContainer()
        container.erp = KingdeeMockAdapter()
        first = container._get_erp_authz()
        container.erp = KingdeeMockAdapter()  # new instance -> re-wrap
        second = container._get_erp_authz()
        assert second is not first
        assert isinstance(second, ErpAuthorizationService)

    def test_get_erp_authz_none_when_erp_uninitialized(self):
        from core.container import ServiceContainer

        container = ServiceContainer()
        assert container.erp is None
        # Preserves existing behavior for paths that never initialize ERP.
        assert container._get_erp_authz() is None

    async def test_agents_receive_authz_boundary(self):
        from core.container import ServiceContainer

        container = ServiceContainer()
        container.erp = KingdeeMockAdapter()
        await container._init_agents()
        billing = container.agents_dict["billing_agent"]
        aftersales = container.agents_dict["aftersales_agent"]
        react = container.agents_dict["react_agent"]
        # Every agent that can reach private ERP data gets the AuthZ boundary,
        # never the raw adapter.
        for agent in (billing, aftersales, react):
            assert isinstance(agent.erp, ErpAuthorizationService)

    async def test_tool_registry_built_on_authz_boundary(self):
        from core.container import ServiceContainer
        from tools.erp_tools import create_erp_tools

        container = ServiceContainer()
        container.erp = KingdeeMockAdapter()
        # Tool creation must use the authz-wrapped adapter (not the raw one),
        # so a direct tool call with no principal fails closed (Attack F).
        registry = create_erp_tools(container._get_erp_authz())
        result = await registry.execute("query_order", {"order_id": ORDER_B})
        assert ORDER_B not in result
        assert "李女士" not in result


# ============================================================================
# Remaining Risk #3 — erp_principal ContextVar must not leak across requests
# ============================================================================


class _PrincipalProbeAgent(BaseAgent):
    """Test double: records the ERP principal visible inside process()."""

    def __init__(self):
        super().__init__(name="probe", role="test", expertise=[])
        self.seen_principal = None

    async def process(self, state: dict) -> dict:
        self.seen_principal = get_erp_principal()
        return state


class TestPrincipalSetterApi:
    """The imperative principal/entrypoint setters + getters remain available
    as public API (bound_erp_request is the preferred request-scoped helper).
    They must still round-trip correctly for external callers."""

    async def test_set_and_get_principal_and_entrypoint(self):
        try:
            set_erp_principal("user_999")
            assert get_erp_principal() == "user_999"
            set_erp_entrypoint("external_caller")
            assert get_erp_entrypoint() == "external_caller"
            # Clearing via the imperative setter resets to None.
            set_erp_principal(None)
            set_erp_entrypoint(None)
            assert get_erp_principal() is None
            assert get_erp_entrypoint() is None
        finally:
            # Belt-and-suspenders cleanup so this test never leaks identity.
            set_erp_principal(None)
            set_erp_entrypoint(None)


class TestPrincipalLifecycle:
    """Remaining Risk #3: process_with_retry must reset erp_principal on
    completion (success, permanent error, and retry-exhaustion) so no
    authenticated identity leaks into a later unauthenticated call."""

    @staticmethod
    def _state(user: str) -> dict:
        return {"session_id": "s1", "user_id": user, "customer_query": "q"}

    async def test_principal_visible_during_process_then_reset(self):
        probe = _PrincipalProbeAgent()
        assert get_erp_principal() is None
        await probe.process_with_retry(self._state(USER_A))
        assert probe.seen_principal == USER_A  # visible during process
        assert get_erp_principal() is None  # reset after success

    async def test_principal_reset_when_process_raises_permanent(self):
        class _BoomAgent(BaseAgent):
            def __init__(self):
                super().__init__(name="boom", role="t", expertise=[])

            async def process(self, state: dict) -> dict:
                raise ValueError("permanent failure")

        agent = _BoomAgent()
        with pytest.raises(ValueError):
            await agent.process_with_retry(self._state(USER_A))
        assert get_erp_principal() is None  # reset despite raise

    async def test_principal_reset_when_transient_retries_exhaust(self, monkeypatch):
        import agents.base_agent as ba

        monkeypatch.setattr(ba, "RETRY_MAX_ATTEMPTS", 1)
        monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 0.0)
        monkeypatch.setattr(ba, "RETRY_MAX_DELAY", 0.0)

        class _TransientBoom(BaseAgent):
            def __init__(self):
                super().__init__(name="tboom", role="t", expertise=[])

            async def process(self, state: dict) -> dict:
                raise ConnectionError("transient failure")

        agent = _TransientBoom()
        with pytest.raises(ConnectionError):
            await agent.process_with_retry(self._state(USER_A))
        assert get_erp_principal() is None  # reset after retry exhaustion

    async def test_no_stale_principal_leaks_to_direct_tool_call(self, authz):
        """Regression for the exact leak the report flagged: after a
        process_with_retry as USER_A, a direct tool call with NO principal
        set must still fail closed — the prior request's principal must not
        leak."""
        probe = _PrincipalProbeAgent()
        await probe.process_with_retry(self._state(USER_A))
        assert get_erp_principal() is None

        registry = create_erp_tools(authz)
        # No principal set: must deny, never read ORDER_B's owner payload.
        result = await registry.execute("query_order", {"order_id": ORDER_B})
        assert ORDER_B not in result
        assert "李女士" not in result


# ============================================================================
# BF-02 (Phase 1 Gate re-review): the AuthZ boundary must be an invariant of the
# tool factory itself, not just of container wiring. A raw ERP adapter passed to
# create_erp_tools must NOT bypass ErpAuthorizationService for private resources.
# ============================================================================


class TestRawAdapterBypassDenied:
    """Private ERP tools must refuse to disclose data through a raw adapter.

    ``core.container`` correctly wires ``create_erp_tools(self._get_erp_authz())``
    (an ``ErpAuthorizationService``), but the factory itself accepted and used
    ANY adapter with the right methods — so ``create_erp_tools(KingdeeMockAdapter())``
    disclosed another user's order with no ownership check.
    """

    async def test_create_erp_tools_raw_adapter_denies_private_order(self, mock_adapter):
        """Raw adapter (not AuthZ-wrapped) must NOT disclose ORDER_B's owner."""
        registry = create_erp_tools(mock_adapter)  # raw, not ErpAuthorizationService
        result = await registry.execute("query_order", {"order_id": ORDER_B})
        # Must fail closed — no disclosure of 李女士's order.
        assert "李女士" not in result, f"Raw adapter leaked private order: {result}"
        assert "486" not in result, f"Raw adapter leaked order amount: {result}"
        assert ORDER_B not in result

    async def test_create_erp_tools_raw_adapter_denies_private_customer(self, mock_adapter):
        """Raw adapter must NOT disclose C002's customer profile via query_customer."""
        registry = create_erp_tools(mock_adapter)
        result = await registry.execute("query_customer", {"customer_id": "C002"})
        assert "李女士" not in result, f"Raw adapter leaked private customer: {result}"
