# Standalone local MCP FAQ (2026-11-24)

Earlier-employer customer-service portfolio maintenance, not historic deployment.

A separate stdio process exposes exactly one tool for three fixed fictional FAQ
topics. The existing official SDK client, adapter and ToolRegistry call it.
The implementation is a narrow local MCP wire-protocol server, not a complete
SDK server, third-party service or authenticated remote production server.

After installing the repository's optional MCP SDK, set the following in a
private local environment before starting the customer-service application:

```dotenv
MCP_ENABLED=true
MCP_FAIL_CLOSED=true
MCP_SERVERS=[{"name":"syntheticfaq","transport":"stdio","command":"python3","args":["-m","tools.readonly_mcp_faq"],"allowed_tools":["lookup_demo_faq"],"risk_level":"low","timeout_seconds":5}]
```

Start from the repository root. No customer information or real business rules
may be passed. Production needs its own reviewed service and access policy.
The runtime fallback remains disabled without an allowlist. Standard Compose now
uses the business product-document tool with a deterministic synthetic catalog;
this older FAQ process remains a protocol regression fixture, not a production
customer-support source. No silent substitution with the test fake is allowed.

Acceptance:
```bash
python -m pytest -q -o addopts='' tests/unit/test_readonly_mcp_faq.py tests/integration/test_readonly_mcp_faq_stdio.py
```

The integration test invokes the existing staging receipt verifier with a real
subprocess and checks the independent expected output. No executed tests or
external production connections are claimed by this change.
