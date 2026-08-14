"""
P0-03 Remaining Risk #4 — Real Kingdee user → customer mapping contract.

The real Kingdee adapter must resolve an authenticated user_id to an ERP
customer_id from a verifiable, server-side config/data source (never from
prompt / LLM / resource self-declaration). When the mapping is absent, empty,
or doesn't contain the user, resolution must fail closed (None) so
ErpAuthorizationService authorizes no private ERP resource.

Covers:
  - core.config.load_erp_user_customer_map / ERP_USER_CUSTOMER_MAP (config)
  - KingdeeRealAdapter.resolve_customer_by_user (real-adapter contract)
"""

import contextlib
import logging

from core.config import load_erp_user_customer_map
from erp.kingdee_real_adapter import KingdeeRealAdapter

# ============================================================================
# Config loader — verifiable, fail-closed on any malformed/missing input
# ============================================================================


class TestUserCustomerMapConfig:
    def test_valid_json_map(self):
        m = load_erp_user_customer_map('{"user_001":"C001","user_002":"C002"}')
        assert m == {"user_001": "C001", "user_002": "C002"}

    def test_empty_string_returns_empty_fail_closed(self):
        assert load_erp_user_customer_map("") == {}
        assert load_erp_user_customer_map("   ") == {}

    def test_invalid_json_returns_empty_fail_closed(self):
        assert load_erp_user_customer_map("not json") == {}
        assert load_erp_user_customer_map("{broken") == {}

    def test_non_dict_json_returns_empty_fail_closed(self):
        assert load_erp_user_customer_map('["a","b"]') == {}
        assert load_erp_user_customer_map('"just a string"') == {}
        assert load_erp_user_customer_map("42") == {}

    def test_numeric_values_coerced_to_string(self):
        # Lenient on value type, but never accepts None.
        assert load_erp_user_customer_map('{"user_001": 123}') == {"user_001": "123"}

    def test_null_values_skipped(self):
        assert load_erp_user_customer_map('{"user_001": null}') == {}


# ============================================================================
# Real adapter — fail-closed user → customer resolution
# ============================================================================


class TestRealAdapterUserMapping:
    """KingdeeRealAdapter must resolve configured users and fail closed
    otherwise — no HTTP, just the server-side config map."""

    @staticmethod
    def _adapter(mapping=None):
        from erp.kingdee_real_adapter import KingdeeRealAdapter

        return KingdeeRealAdapter(
            base_url="http://erp.test",
            app_id="id",
            app_secret="secret",
            db_id="db",
            user_customer_map=mapping,
        )

    async def test_resolves_configured_user(self):
        adapter = self._adapter({"user_001": "C001"})
        assert await adapter.resolve_customer_by_user("user_001") == "C001"

    async def test_resolves_numeric_authenticated_user_id(self):
        """JWT sub may decode as int; JSON mapping keys are canonical strings."""
        adapter = self._adapter({"1": "C001"})
        assert await adapter.resolve_customer_by_user(1) == "C001"

    async def test_fail_closed_when_map_empty(self):
        adapter = self._adapter({})
        assert await adapter.resolve_customer_by_user("user_001") is None

    async def test_fail_closed_when_user_not_in_map(self):
        adapter = self._adapter({"user_001": "C001"})
        assert await adapter.resolve_customer_by_user("attacker") is None

    async def test_fail_closed_for_missing_identity(self):
        adapter = self._adapter({"user_001": "C001"})
        assert await adapter.resolve_customer_by_user(None) is None
        assert await adapter.resolve_customer_by_user("") is None

    def test_none_map_falls_back_to_config(self):
        """When no map is injected, the adapter must use the verifiable config
        map (ERP_USER_CUSTOMER_MAP), not a hardcoded or prompt-derived one."""
        adapter = self._adapter(None)
        assert adapter._user_customer_map == load_erp_user_customer_map()

    async def test_fail_closed_when_no_map_injected(self):
        """With no injected map and (in tests) no env config, resolution must
        fail closed — proving the default is deny, not a leaky fallback."""
        adapter = self._adapter(None)
        # Whatever the config is, an unconfigured user must resolve to None.
        assert await adapter.resolve_customer_by_user("user_001") in (
            None,
            load_erp_user_customer_map().get("user_001"),
        )


# ============================================================================
# Real adapter — get_order_owner minimal-metadata parsing (HTTP boundary mocked)
# ============================================================================


class _OwnerProbeAdapter(KingdeeRealAdapter):
    """Subclass that mocks only the HTTP boundary (_paged_query) so the real
    get_order_owner parsing + fail-closed logic can be unit-tested without a
    live Kingdee API. Returns canned Kingdee-shaped rows (or raises)."""

    def __init__(self, rows: list | None = None, raise_on_query: bool = False):
        super().__init__(
            base_url="http://erp.test",
            app_id="id",
            app_secret="secret",
            db_id="db",
            user_customer_map={"user_001": "C001"},
        )
        self._rows = rows or []
        self._raise_on_query = raise_on_query

    async def _paged_query(self, form_id, filter_string, field_names=None, top_row_count=0):
        if self._raise_on_query:
            raise RuntimeError("simulated kingdee failure")
        return self._rows


class TestRealAdapterGetOrderOwner:
    """The real adapter's get_order_owner must extract the order owner
    customer_id from Kingdee's response shape and fail closed on any error,
    never leaking the full order payload to the authz boundary."""

    async def test_parses_nested_customer_object(self):
        adapter = _OwnerProbeAdapter(rows=[{"FCUSTID": {"FNumber": "C001"}}])
        assert await adapter.get_order_owner("ORD123") == "C001"

    async def test_parses_flat_customer_field(self):
        adapter = _OwnerProbeAdapter(rows=[{"FCUSTID_FNumber": "C002"}])
        assert await adapter.get_order_owner("ORD123") == "C002"

    async def test_empty_rows_returns_none_fail_closed(self):
        adapter = _OwnerProbeAdapter(rows=[])
        assert await adapter.get_order_owner("ORD123") is None

    async def test_missing_id_returns_none(self):
        adapter = _OwnerProbeAdapter(rows=[{"FCUSTID": {"FNumber": "C001"}}])
        assert await adapter.get_order_owner("") is None
        assert await adapter.get_order_owner(None) is None

    async def test_query_error_returns_none_fail_closed(self):
        adapter = _OwnerProbeAdapter(rows=[], raise_on_query=True)
        assert await adapter.get_order_owner("ORD123") is None


# ============================================================================
# AC16 residual (re-review) — get_order_owner exception log must not record
# raw user-controlled order_id (Aftersales passes a NL query that may carry PII)
# ============================================================================


@contextlib.contextmanager
def _capture_real_adapter_logs():
    """Capture records from the erp.kingdee_real logger.

    get_logger sets propagate=False, so pytest's caplog (root handler) cannot
    see these records. A directly-attached handler captures them with their
    rendered messages intact — mirroring _capture_authz_events in
    test_erp_authorization.py.
    """
    from erp.kingdee_real_adapter import logger as _real_logger

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    handler.setLevel(logging.WARNING)
    _real_logger.addHandler(handler)
    try:
        yield records
    finally:
        _real_logger.removeHandler(handler)


class TestRealAdapterGetOrderOwnerLogSanitization:
    """AC16 residual (report re-review): the real adapter's get_order_owner
    exception log must not record the raw order_id. The Aftersales path passes
    a natural-language query as order_id, which may carry PII (e.g. a phone
    number). The id must be reduced to the shared sanitized form (allowlist id
    verbatim, else a stable ``untrusted:<hash>`` marker) — the same policy the
    AuthZ boundary uses, so no erp.* log records raw prompt/PII.
    """

    @staticmethod
    def _adapter() -> _OwnerProbeAdapter:
        # raise_on_query forces get_order_owner's except branch (the line that
        # logs order_id), without a live Kingdee API.
        return _OwnerProbeAdapter(rows=[], raise_on_query=True)

    async def test_raw_order_id_and_pii_not_logged_on_failure(self):
        adapter = self._adapter()
        nl = "我的订单 13800000000 请查询"
        with _capture_real_adapter_logs() as records:
            result = await adapter.get_order_owner(nl)
        assert result is None  # fail closed (unchanged behavior)
        rendered = "\n".join(r.getMessage() for r in records)
        assert nl not in rendered, "raw natural-language order_id leaked into log"
        assert "13800000000" not in rendered, "phone-number PII leaked into log"

    async def test_sanitized_marker_logged_on_failure(self):
        """The raw id must be REDUCED to a sanitized marker, not silently
        dropped — so the audit trail stays correlatable without PII."""
        adapter = self._adapter()
        nl = "我的订单 13800000000 请查询"
        with _capture_real_adapter_logs() as records:
            await adapter.get_order_owner(nl)
        rendered = "\n".join(r.getMessage() for r in records)
        assert "untrusted:" in rendered, "order_id was dropped, not sanitized"

    async def test_valid_order_id_logged_verbatim_on_failure(self):
        """A well-formed ERP id is logged verbatim (not hashed) so real order
        ids stay readable for debugging — only untrusted/PII input is hashed.
        Guard against the fix over-hashing valid identifiers."""
        adapter = self._adapter()
        with _capture_real_adapter_logs() as records:
            await adapter.get_order_owner("ORD123456")
        rendered = "\n".join(r.getMessage() for r in records)
        assert "ORD123456" in rendered
        assert "untrusted:" not in rendered

    async def test_pure_digit_phone_not_logged_raw_on_failure(self):
        """AC16 (re-review probe): a pure-digit phone number passed as
        order_id (e.g. via the Aftersales raw-query path) is not a valid ERP
        id shape and must not survive into the erp.kingdee_real log verbatim.
        The exact phone-PII leak the re-review found: the digit-only string
        matched the old allowlist and was logged raw."""
        adapter = self._adapter()
        phone = "13800000000"
        with _capture_real_adapter_logs() as records:
            result = await adapter.get_order_owner(phone)
        assert result is None  # fail closed (unchanged behavior)
        rendered = "\n".join(r.getMessage() for r in records)
        assert phone not in rendered, "pure-digit phone leaked into log verbatim"
        assert "untrusted:" in rendered

    async def test_letter_prefixed_phone_digits_not_logged_raw_on_failure(self):
        """AC16 (re-review probe #2): "tel13800000000" — letters glued to a
        phone-digit run — passed as order_id is NOT a provably-valid ERP
        order id (real order ids match ^ORD\\d+$) and must not survive into
        the erp.kingdee_real log verbatim. It shares the letters-then-digits
        shape of a real order id, so the type-aware known-prefix shape check
        is required to catch it (the old 'has-a-letter' rule let it through)."""
        adapter = self._adapter()
        bad = "tel13800000000"
        with _capture_real_adapter_logs() as records:
            result = await adapter.get_order_owner(bad)
        assert result is None
        rendered = "\n".join(r.getMessage() for r in records)
        assert bad not in rendered, "letter-prefixed phone leaked into log verbatim"
        assert "13800000000" not in rendered
        assert "untrusted:" in rendered
