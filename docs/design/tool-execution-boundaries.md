# Tool execution governance boundary (2026)

This document distinguishes **native function tools**, **MCP tools** and
**authorized ERP reads** instead of pretending that all calls use the same
runtime stack.

## Code paths and authority

| Path | Call site | Governance boundary | Side effects |
| --- | --- | --- | --- |
| Function Calling / native tools | `agents/base_agent.py` / `tools/tool_registry.py` | ToolRegistry schema, risk checks, deadline, receipt, HITL and durable ledger | **Only** registered writes via governed registry |
| External MCP tools | `tools/mcp_adapter.py` → ToolRegistry registration | MCP server allowlist, payload limits, transport deadline, registry risk checks | MCP writes must be explicitly marked and governed |
| Native ERP domain read | Billing, Product, General Agent → authorized ERP adapter | ERP authorization wrapper; bounded read deadline; `erp_read_status`; `degraded_reason` | **Read-only**; not a shortcut for refund/order modification |

## Required result semantics

- A handler result is not success just because no exception escaped.
  `ToolExecutionReceipt.ok` records the ToolRegistry tool outcome only,
  while `outcome_verified` does **not** establish confirmed customer FCR.
- ERP read exceptions and deadlines become explicit `erp_read_status=error`
  or `timeout` and a `degraded_reason`. No tool-execution proof is fabricated.
- Direct ERP read errors must never put full exception text, customer data or
  credentials in logs.
- Graph mode results must propagate read failure metadata from nested Agent
  state copies. A clean LLM answer following a failed ERP lookup cannot
  constitute a verified ERP business result.
- ToolRegistry/HITL governs writes; direct ERP reads cannot bypass HITL or
  introduce side effects. Riskier ERP mutations should be registered in
  ToolRegistry, with stable operation keys and authorization checks.
- MCP is for cross-process tool integration, not a mandatory wrapper for
  every in-process Python function.

## Explicit limits

The **direct ERP read path is not fully unified** with ToolRegistry telemetry
or the same structured receipt type. The change here provides bounded read
timeouts, honest failure propagation and code-level tests while retaining the
existing ERP authorization adapter. Real Kingdee ERP staging, authorization
mappings, downstream idempotency and response/latency evidence remain
`NOT_VERIFIED`; do not claim a production contract.

Related outstanding validation: issue #33.
