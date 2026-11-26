# Agent V1 Runtime: scope, acceptance and evidence

**工业准入代码已在主线实现**：用户/会话/任务绑定、幂等冲突、checkpoint 失败拒绝、API/Worker 配置一致性及严格会话存储见 [Industrial Runtime Admission](../architecture/industrial-runtime-admission.md)。新增或通过的测试不等于真实业务、真实跨进程生产运行或隐私迁移已验收；实时证据与限制见 [Production Readiness](../production-readiness.md)。

> CURRENT engineering task register. This is an independently maintained public reference implementation with synthetic test data. Only source at the checked-out commit and its own CI artifacts establish status. The scope is narrower than production certification.

## Module scope

| Module | Code ownership | Evidence boundary |
| --- | --- | --- |
| M01–M04 | Python/FastAPI, model/tool loop, server schema/authorization | `api/`, `agents/`, `tools/tool_registry.py`; standalone loop is a separate opt-in reference, not the online LangGraph path |
| M05–M06, M12 | LangGraph, route selection, orchestration, HITL handoff | `core/graph_builder.py`, `collaboration/modes.py`; fixed Supervisor–Workers (not dynamic planning); paired task benefit still needs measured cases |
| M09–M10 | AgentRun, leases, queue, checkpoints, side-effect ledger | `runtime/`, `tests/integration/runtime/`; PostgreSQL+Redis tests do not establish external ERP exactly-once |
| M11 | Internal tool validation, MCP/A2A business protocol | `tools/mcp_adapter.py`, `integrations/a2a_policy/`; standard Compose enables synthetic SKU docs and an independent after-sales peer; generated bearer is stored in a private volume; industrial profile requires approved catalogs and private HTTPS/CA. Enterprise data and deployment remain unverified |
| M13–M16 | Agent evaluation, telemetry, security, service packaging | `evaluation/agent_eval/`, `core/monitoring.py`, `deploy/`; real business success and production P95/cost remain unverified |

The separate cosmetics RAG reference is responsible for complex document ingestion and retrieval ablations. The separate Gaobao reference owns its own read-only synthetic business Harness/Skills evaluations. This repository does not duplicate those pipelines.

## Runtime acceptance work

| Key | Verifiable goal | Status |
| --- | --- | --- |
| CSA-P0-01 | Model→Tool→Observation loop, eight basic scenarios plus malformed-input and uncertain-write cases | **IMPLEMENTED; verify exact-head CI**: `core/standalone_agent_loop.py` and `tests/unit/test_standalone_agent_loop.py` |
| CSA-P0-01a | Existing staging refund tool plus lost-response reconciliation | **IMPLEMENTED; verify exact-head CI**: `tests/unit/test_standalone_loop_staging_integration.py`; not the online LangGraph entrypoint |
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


## Parallel collaboration — partial failure contract (2026-11-24)

The online ParallelMode retains completed child results, actual tool receipts
and pending approvals on a read-only deadline, marks missing/failed/timed-out
children as degraded, redacts exception details and cancels/drains unfinished
children. Durable-run deadlines still raise for Runtime classification; this
does not establish external write reconciliation or a hard deadline for a
non-cooperative child. Regression entry: `pytest -q tests/unit/test_collaboration_modes.py`.
Refs #46, #33. Real-model/ERP outcome measurement remains open.

## Reviewed retrieval gold evidence integrity (#33)

The reviewed-gold evaluator refuses schema-only formal execution, ambiguous
query identities and duplicate corpus/ranked document IDs. Pending or excluded
judgments prevent a closed recall/NDCG population. Required retrieval stages
must be observed in the trace; a rerank skipped for zero or one candidate is
a valid no-op, but a suite with no applied rerank is incomplete. Partial arms,
subset runs and execution failures remain incomplete; diagnostic metric means
cover successful queries only and must not be presented as full-population
results. A local corpus hash and stage trace alone cannot establish live index
identity, source authorization or human relevance review. Publication stays
ineligible pending independent index correspondence and provenance evidence.
The legacy 649-query dataset has not been relabeled by this change.
