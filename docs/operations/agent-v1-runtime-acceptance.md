# Agent V1 Runtime: scope, acceptance and evidence

> CURRENT engineering task register. This is an independently maintained public reference implementation with synthetic test data. Only source at the checked-out commit and its own CI artifacts establish status. The scope is narrower than production certification.

## Module scope

| Module | Code ownership | Evidence boundary |
| --- | --- | --- |
| M01–M04 | Python/FastAPI, model/tool loop, server schema/authorization | `api/`, `agents/`, `tools/tool_registry.py`; standalone learning reference is opt-in |
| M05–M06, M12 | LangGraph, route selection, orchestration, HITL handoff | `core/graph_builder.py`, `collaboration/modes.py`; dynamic Supervisor isolation still needs measured cases |
| M09–M10 | AgentRun, leases, queue, checkpoints, side-effect ledger | `runtime/`, `tests/integration/runtime/`; PostgreSQL+Redis tests do not establish external ERP exactly-once |
| M11 | Internal tool validation, optional MCP client | `tools/mcp_adapter.py`; no verified external MCP Server or A2A deployment |
| M13–M16 | Agent evaluation, telemetry, security, service packaging | `evaluation/agent_eval/`, `core/monitoring.py`, `deploy/`; real business success and production P95/cost remain unverified |

The separate cosmetics RAG reference is responsible for complex document ingestion and retrieval ablations. The separate Gaobao reference owns its own read-only synthetic business Harness/Skills evaluations. This repository does not duplicate those pipelines.

## Runtime acceptance work

| Key | Verifiable goal | Status |
| --- | --- | --- |
| CSA-P0-01 | Model→Tool→Observation loop, eight basic scenarios plus malformed-input and uncertain-write cases | **IMPLEMENTED IN THIS BRANCH, CI PENDING**: `core/standalone_agent_loop.py` and `tests/unit/test_standalone_agent_loop.py` |
| CSA-P0-01a | Existing staging refund tool plus lost-response reconciliation | **IMPLEMENTED IN THIS BRANCH, CI PENDING**: `tests/unit/test_standalone_loop_staging_integration.py`; not the online LangGraph entrypoint |
| CSA-P0-02 | ERP read timeout propagation through all collaboration paths | **PARTIAL**; validate the actual online callsite and each mode |
| CSA-P0-03 | Human-reviewed route labels and authorized real model task outcomes | **NOT_MEASURED**; scripted graph evaluation is separate from real-provider quality |
| CSA-P0-04 | Post-approval business write, persisted operation ID, downstream status check, cancellation and crash replay | **PARTIAL**; local staging ledger cannot prove an ERP/payment guarantee |
| CSA-P1-05 | Authenticated handoff desk, message/SLA operational chain | **PARTIAL**; full operator workflow is not certified |
| CSA-P1-06 | Four-layer Eval, Trace replay, P95/P99, task cost, rollback and alert delivery | **PARTIAL / NOT_MEASURED**; require a version-bound evidence bundle |

## Repeatable checks

```bash
pytest -q tests/unit/test_standalone_agent_loop.py tests/unit/test_standalone_loop_staging_integration.py --no-cov -o addopts=''
make test
make runtime-e2e
make agent-eval
make metrics-exposure-verify
```

Each report must identify the code commit, dataset revision, environment, denominator, failures and exceptions. Success on a scripted model or local staging ledger is not evidence of real payments, enterprise uptime, provider bills or complete production security. See `docs/limitations.md`, `docs/production-readiness.md`, and tracked issue #46.
