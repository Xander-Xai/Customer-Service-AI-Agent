# Real ERP read failure semantics (2026-11-24)

Refs #33. The real adapter previously collapsed connection and response-format
failures into normal empty lists or None. The Agent's existing bounded read
contract can now receive ERPReadUnavailable for product, inventory, authorized
order and customer payload reads. Explicit empty rows remain valid.

The BillQuery envelope must contain Data as a list of mapping rows or
Data.Rows as a list of mapping rows. Missing or malformed data is not a verified
empty result. Query-boundary errors have a generic message and suppressed
exception chaining. Existing ownership lookup remains fail-closed; failure
there is not differentiated from absence in this change. Existing upstream
HTTP logs and real vendor business-status envelope compatibility still require
separate audit/staging verification.

This is a current retrofit to the earlier customer-service application, not
historical deployment evidence. All regression tests use mocked responses.
No staging endpoint or external ERP write was contacted.

```bash
python -m pytest -q tests/integration/test_erp_integration.py tests/unit/test_erp_read_contract.py
```

An additional HTTPX MockTransport regression exercises token acquisition, HTTP request headers, JSON decoding, a valid empty page and an invalid HTTP-200 envelope without replacing the adapter API call. This is local transport-contract evidence, not vendor staging.
