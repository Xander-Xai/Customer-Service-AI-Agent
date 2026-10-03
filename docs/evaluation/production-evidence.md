# Production evidence measurement

This harness makes Issue #7 measurable without claiming that local fixtures are production evidence.

## Three evidence families

This repository maintains three distinct evidence families; do not substitute one for the other:

1. **Provider / production evidence** — authentication, token usage/billing, latency, staging outcomes (this document, `scripts/run_production_evidence.py`, `scripts/probe_provider_auth.py`).
2. **RAG retrieval evaluation evidence** — benchmark retrieval quality across controlled ablations (canonical reference: [docs/reference/rag-evaluation.md](../reference/rag-evaluation.md)).
3. **Distributed Agent Runtime evidence** — correctness of the async Run / Celery worker / checkpoint / lock / idempotency layer (see §"Distributed Agent Runtime Evidence" below; commands `make runtime-e2e`, `make runtime-chaos`, `make runtime-verify`).

A local RAG benchmark on `tests/eval/rag_benchmark.json` is **not** a production customer outcome: it measures retriever/search quality on a fixed query set, not end-user resolution quality, latency SLOs, or business metrics.

Equally important: **passing the distributed runtime acceptance suite is not production validation.** See the Level 1/2/3 ladder below.

## Distributed Agent Runtime Evidence

This family answers one question: **is the distributed execution layer correct under real
infrastructure?** It is deliberately separated from the provider/production family above,
because "the runtime behaved correctly against real PostgreSQL + Redis" and
"production is validated" are different claims at different confidence levels.

### Evidence levels (canonical ladder)

| Level | Meaning | Current state |
|---|---|---|
| **Level 1 — IMPLEMENTED** | Code exists and is readable. Proves design intent, nothing more. | PostgreSQL checkpoint, Redis session, Redis per-thread lock, `AgentRun` canonical state, Celery + Redis broker, worker execution, `run_id` dispatch, `acks_late` / `reject_on_worker_lost` / `visibility_timeout`, retry, tool ledger, Prometheus metrics |
| **Level 2 — LOCALLY VERIFIED / CI VERIFIED** | Real infrastructure + a command + a provenance-bearing artifact. Proves correctness against real PostgreSQL + Redis + multi-process Celery. | `make runtime-e2e`, `make runtime-chaos`, `make runtime-verify` |
| **Level 3 — PRODUCTION VERIFIED** | Real production cluster, sustained multi-replica operation, real user traffic, **real ERP write operations**, large queue backlogs, K8s autoscaling, multi-region. | **`NOT_VERIFIED`** |

**`LOCALLY_VERIFIED != PRODUCTION_VERIFIED`.** Level 2 must never be reported as
"production cluster verified" in documentation, résumés, or interview answers. The
specific gaps that Level 2 does **not** close:

- Single-Redis mutual exclusion is proven; **Redis failover / Redlock-cluster behaviour is not**.
- Lease renewal during execution is proven; **fencing token is not implemented** (a worker
  paused longer than the lock TTL can resume concurrently with the new owner; terminal
  writes are rejected by the `from_statuses={RUNNING}` conditional update, but
  node-level side effects still rely on the tool idempotency ledger).
- Correctness against real infrastructure is proven; **production throughput, P99 latency,
  and queue backlog behaviour are `NOT_MEASURED`**.
- Simulated failures and process kills are proven; **real ERP write operations are
  `NOT_VERIFIED`** (the ERP adapter runs in Mock mode).

### Required evidence dimensions

Every distributed-runtime evidence claim must name one of these dimensions. A claim that
does not map to a dimension below is not admissible.

| Dimension | What it must demonstrate | How it is established |
|---|---|---|
| `checkpoint_cross_process` | A checkpoint written by one **independent OS process** is readable by another, and execution resumes from the checkpoint's `next` rather than restarting from the first node. Two saver instances inside one process do **not** satisfy this. | `tests/integration/runtime/test_cross_process_checkpoint.py`; `scripts/verify_distributed_runtime.py` |
| `same_thread_serialization` | Two runs on the same `thread_id` never overlap. Asserted on database `started_at` / `finished_at` intervals — not on "SETNX returned true". | `tests/integration/runtime/test_run_semantics.py` |
| `different_thread_parallelism` | Distinct `thread_id`s genuinely execute in parallel (wall-clock, not merely "lock acquired"). | `tests/integration/runtime/test_run_semantics.py` |
| `worker_crash_recovery` | After `SIGKILL` of the worker **process group**, the unacknowledged task is redelivered, a second worker takes over the expired lease (`attempt` increases), and execution resumes from the checkpoint. | `tests/integration/runtime/test_worker_checkpoint_recovery.py`; `make runtime-chaos` |
| `tool_idempotency` | After crash recovery, the side-effect counter is still `1` **and** the tool was genuinely invoked ≥ 2 times (otherwise "1" may simply mean it never retried). | `tests/integration/runtime/test_tool_idempotency.py`; `make runtime-chaos` |
| `retry_recovery` | Transient errors back off and eventually succeed; permanent errors are not retried; exhausted retries reach `DEAD_LETTER`, write an immutable `agent_dead_letters` row, and replay reuses the **original `run_id`**. | `tests/integration/runtime/test_run_semantics.py` |

Supplementary dimensions also covered by the acceptance suite, and admissible when named:
`queue_worker_decoupling`, `event_delivery_semantics`, `sse_checkpoint_serialization`,
`run_event_redaction`, `error_classification`, `secret_non_leak`, `cross_user_isolation`.

### Artifacts and provenance

| Command | Artifact | Schema | Provenance fields |
|---|---|---|---|
| `make runtime-verify` | `artifacts/distributed-runtime/<UTC ts>/report.json` | `distributed-runtime-evidence/v2` | `tested_code_sha`, `generated_at`, `overall_status`, `checks` |
| `make runtime-chaos` | `artifacts/runtime/chaos-<ts>.json` | ad hoc | step log + result; **no git SHA / schema version** — weakest provenance in the repository, treat as a step trace rather than a formal artifact |
| `make runtime-e2e` | pytest output (no committed artifact) | — | CI job `.github/workflows/ci.yml::runtime-e2e` (postgres + redis service containers) |

`scripts/verify_distributed_runtime.py` is **fail-closed**: exit `0` only when every check
is `PASS`, exit `1` on any `FAIL`, exit `2` on `NOT_RUN` / `PARTIAL`. "Did not run" is
deliberately *not* success — treating it as success is evidence pollution.
Historical `distributed-runtime-evidence/v1` artifacts are preserved unmodified.

### Reading an artifact

- `overall_status == "PASS"` means every listed check passed **against the real
  infrastructure at `tested_code_sha`**. It does not mean production is verified.
- `artifact_commit_sha` is `null` by design: the artifact is committed *after* generation,
  so its own commit is unknowable at write time. Use `tested_code_sha` for provenance.
- A passing artifact with a `tested_code_sha` that is not an ancestor of the current
  `HEAD` describes an older codebase — re-verify before quoting it.

## RAG metric contract (canonical: rag-evaluation.md)

- **4 retrieval configurations (ablation)**: `vector_only`, `bm25_only`, `hybrid_no_rerank`, `hybrid_rerank` (production-like)
- **multi-K metrics**: Hit@K, Recall@K, Precision@K, NDCG@K, MRR@K (K ∈ {1,3,5,8})
- **measured stage latency**: VECTOR / BM25 / FUSION_RRF / RERANK taken from the retrieval trace (measured, not derived)
- **evaluation populations**: `all_queries` (primary end-to-end denominator), `retrieval_eligible`, `full_gold_covered` — all computed at runtime, never hardcoded; citations must name the population
- **failure taxonomy**: TIMEOUT / PROVIDER_ERROR / GOLD_NOT_INDEXED / MISS_ALL / LOW_RANK (+ channel diagnostics)

**Current status: the formal 649-query metrics are `NOT_VERIFIED`.** The latest committed preflight evidence (`artifacts/evaluation/rag-649/preflight-20261002T194209Z/report.json`, schema `rag-eval-evidence/v2`, `status: BLOCKED`, `primary_blocker: EMBEDDING_PROVIDER_AUTH`) shows the provider-authentication blocker; no formal metrics artifact exists yet. Blocker semantics are per-stage and must not be flattened: `EMBEDDING_PROVIDER_AUTH` (HTTP 401, blocks corpus import) is the root cause, `VECTOR_INDEX_EMPTY` is a downstream symptom carrying `caused_by`, and `RERANKER_PROVIDER_AUTH` (HTTP 401) is `blocking: false` and blocks only `hybrid_rerank`. The artifact states `formal evaluation not run; no metrics generated`, so `declared_queries: 649` is not a completed formal evaluation. The earlier v1 artifact (`preflight-20260929T191128Z`) is retained as historical evidence, not backfilled. See rag-evaluation.md §3.4 for the live status.

## Local run

```bash
python3 scripts/run_production_evidence.py \
  --suite local \
  --output artifacts/evidence/local.json \
  --markdown-output artifacts/evidence/local.md
```

The local suite uses versioned, deterministic fixtures and repeated samples with a separate warmup pass. It reports fixture task-success, retrieval contracts, degraded-mode outcomes, and local fixture latency. Results are labelled `LOCAL_ONLY` / `FIXTURE`.

## Evidence sources

Every measurement is labelled `PROVIDER_REPORTED`, `APPLICATION_MEASURED`, `ESTIMATED`, `FIXTURE`, `NOT_AVAILABLE`, `NOT_MEASURED`, or `NOT_VERIFIED`. Estimated token counts cannot populate provider token or billing fields.

Provider input/output/cached tokens and provider billing remain unavailable unless a future provider adapter receives those fields directly. Missing values are serialized as `null` with an explicit source label; they are never converted to zero.

## Controlled provider staging

The provider adapter exercises the repository's OpenAI-compatible `/chat/completions` interface. It is disabled unless the explicit gate `EVAL_REAL_PROVIDER=1` is set. A dry run is safe and makes zero provider calls:

```bash
EVAL_REAL_PROVIDER=0 python3 scripts/run_production_evidence.py \
  --suite provider-staging \
  --repeat 2 --warmup 1 \
  --max-requests 20 --max-input-tokens 2000 --max-output-tokens 128 \
  --estimated-cost-cap 1 \
  --output artifacts/evidence/provider-staging.json \
  --markdown-output artifacts/evidence/provider-staging.md
```

A real staging run additionally requires an exported `OPENAI_API_KEY` and explicit estimated input/output rates for the safety cap. The adapter records only normalized usage, request identifiers, status, retry/timeout counts, and timing; it never writes prompts, response text, authorization headers, or PII. `max-requests` includes worst-case retry attempts, so the run is rejected before any call when the configured retry policy could exceed the cap.

Provider-reported input/output/cached/reasoning tokens are accepted only from response usage fields. A provider `cost` field is accepted only when returned directly by the provider. Configured rate-card calculations are labelled `ESTIMATED` and must not be described as provider billing.

## Authentication preflight

Run the bounded preflight before any staging suite:

```bash
python3 scripts/probe_provider_auth.py
```

It makes at most one direct `GET /v1/models?sub_type=chat` request using `OPENAI_API_KEY`, reports only status/category/trace metadata/model count, and never writes the response body or authorization header. `401` is `AUTH_FAILED` and non-retryable; `403` is `FORBIDDEN` and non-retryable. Missing, wrapped (`Bearer ...`), quoted, or newline-containing credentials are rejected locally.

The application configuration currently loads `.env` with `load_dotenv(override=True)`, while the probe reads the process environment directly. Operators must verify the effective credential source without committing or printing secrets. A successful auth probe proves only credential authentication; model availability and chat permission require the subsequent bounded probes.

## Metric contracts

- latency records sample count, warmup count, and the interpolation method; P50/P95/P99 are not aliases for maximum
- streaming measurements must distinguish TTFT from total completion latency
- retrieval (RAG evidence pipeline) supports Hit@K / Recall@K / Precision@K / NDCG@K / MRR@K across K = {1,3,5,8}; binary relevance for NDCG; Hit@K and Recall@K are different quantities — never conflate them
- task success uses deterministic evidence, field, tool, and degraded-signal assertions; an LLM judge is not the sole grader
- recoverability distinguishes `normal_success`, `degraded_success`, `graceful_failure`, and `hard_failure`

## External seams

`provider`, `redis`, `erp`, and `qdrant` suites are disabled by default and produce `NOT_VERIFIED` records without making external calls. Future adapters must be explicitly enabled, use staging-safe credentials, and never write raw provider responses, PII, credentials, or live Qdrant migration results to artifacts.

Qdrant migration is limited to dry-run, disposable local collections, or explicitly approved staging validation. No production migration is performed by this harness.

## Artifact privacy and interpretation

`artifacts/evidence/` is ignored. Reviewers must inspect workload version, Git SHA, configuration hash, sample counts, source labels, and skipped/missing values before interpreting a result. A successful local run proves only that the measurement harness and fixtures are reproducible; it does not verify provider billing, production latency, Redis, ERP, Qdrant, BM25 production traffic, or production task success.

The controlled staging attempt associated with this harness received HTTP 401 for every measured request. It collected no provider token usage or provider billing, so it does not close any production-validation item in Issue #7. Its application-measured failure timing is troubleshooting evidence only, not production latency evidence.

The follow-up direct auth probe also returned HTTP 401 on its single request. The observed root cause is authentication failure for the supplied credential at the official SiliconFlow endpoint; whether the key is expired, revoked, account-mismatched, or otherwise invalid is not established by this probe. Chat, streaming, and staging reruns are blocked until a valid credential is supplied.

## Current evidence boundary

This is the current evidence contract, not a claim that production has been validated.

| Area | Current status | Interpretation |
|---|---|---|
| Local deterministic tests/fixtures | Available when commands pass | Proves the tested local contract only |
| Tool Result compression/offload/cache reuse | Local code and targeted tests | Does not prove provider billing or end-to-end latency savings |
| Provider authentication | `NOT_VERIFIED` / controlled probe may return HTTP 401 | Never use a repository credential or print secrets |
| Provider token usage/billing | `NOT_AVAILABLE` | No production cost claim may be derived |
| Production latency/P99 | `NOT_MEASURED` | Local processing time is not provider or production latency |
| RAG quality (649-query formal metrics) | `NOT_VERIFIED` (provider-auth blocker in committed preflight evidence) | Must name dataset, code, model, K, population, and artifact; never conflate Hit@K with Recall@K |
| **Distributed runtime correctness** (checkpoint / locking / crash recovery / idempotency) | **Level 2 — CI VERIFIED** via `make runtime-e2e` / `runtime-chaos` / `runtime-verify` against real PostgreSQL + Redis + multi-process Celery | Proves the listed dimensions against real infrastructure; **never** report this as production-cluster validation |
| **Distributed runtime in production** (real cluster, sustained multi-replica, real ERP writes, backlog, autoscaling) | **Level 3 — `NOT_VERIFIED`** | Must not be claimed in docs, résumés, or interview answers |
| **Human-in-the-loop governance** (`WAITING_APPROVAL`, `core/hitl/`, `/api/approvals`) | **Level 2 — CI VERIFIED** via `pytest tests/unit/test_hitl_*` and `pytest tests/integration/runtime/test_hitl_*` against real PostgreSQL + Redis (deterministic staging tools) | Proves the governance contract (pre-execution gating, separation of duties, TTL fail-closed, single-consumption resume, ledger-protected approved side effects) — **not** real ERP writes |
| **HITL against a real ERP** (real refund/order-write behind approval) | **Level 3 — `NOT_VERIFIED`** | No enterprise staging artifact exists; verification used deterministic staging tools (`tools/hitl_staging_tools.py`), which is a different claim |
| **Realtime fast path (`/api/chat`) under HITL** | **Not covered by design** | The fast path has no run context, so `core/hitl/gate.py` does not gate it and no approval record is produced. Never describe `/api/chat` as HITL-protected |
| **Proactive approval notification** (webhook / IM push to reviewers) | **`TODO` — not implemented** | Approvals are discoverable only via `GET /api/approvals?status=PENDING`; an unattended approval silently expires at TTL |
| **Approval SLA / human-efficiency** | **`NOT_MEASURED`** | `agent_approval_wait_seconds` has no production distribution |
| **Application-level semantic tracing** (agent / RAG / LLM / tool spans) | **IMPLEMENTED / LOCALLY VERIFIED** — `core/telemetry.py` + `tests/unit/test_telemetry.py` | Proves the span contract only (whitelisted attributes, degrade-to-no-op, exceptions never swallowed). **Not** a trace-backend claim |
| **Live OTLP collector transport** (SDK → `OTLPSpanExporter` → network → real Collector) | **Level 2 — `LOCALLY VERIFIED`** via `make otel-collector-smoke` against real `otel/opentelemetry-collector:0.162.0` over OTLP gRPC; evidence `artifacts/observability/otel-collector-20261003T040014Z/report.json` (schema `otel-collector-evidence/v1`, `status: VERIFIED_LOCAL`, tested code `ddc050e0…`, clean tree) | All 5 semantic spans (`csai.agent.execute`, `csai.agent.execute.resume`, `csai.rag.retrieve`, `csai.llm.chat_completion`, `csai.tool.execute`) were observed in the Collector's own output; `service.name` matched; synthetic privacy canary absent. Proves exporter + network + Collector **only** — the smoke emits the spans through `core.telemetry.span` rather than a real Agent/RAG/LLM/tool workflow |
| **Persistent / queryable trace backend** (Jaeger, Langfuse, Tempo, …) | **`NOT_VERIFIED`** | The verified Collector has a single `debug` exporter: nothing is stored, no retention, no query UI, no dashboard, no alerting. "The Collector received the trace" is **not** "a trace backend exists" |
| **Production trace propagation / real traffic** | **`NOT_VERIFIED`** | Never write "production-ready tracing" / "production verified tracing" / "end-to-end production observability" — no such artifact exists. `OPENTELEMETRY_ENABLED` / `OTEL_ENABLED` still ship `false` in `.env.example` |
| FCR, human efficiency, real QPS | `NOT_MEASURED` unless an issue-level artifact exists | Remove from current factual claims |

### What the semantic tracing claim may and may not cover

Wired at these call sites (span names read from code, not from this document):

| Span | Call site |
|---|---|
| `csai.agent.execute` / `csai.agent.execute.resume` | `runtime/executor.py` (one span per execution attempt) |
| `csai.rag.retrieve` + `rag.stage.*` events | `rag/qdrant_knowledge_base.py` |
| `csai.llm.chat_completion` | `llm/client.py` (one span per logical call, retries included) |
| `csai.tool.execute` | `tools/tool_registry.py` |

Two boundaries travel with the claim:

- **Resume correlation is not one span.** A run may sit in `WAITING_APPROVAL` until its
  TTL and the decision arrives in a different HTTP request, so the pre-pause and
  post-resume segments are separate traces correlated by `run_id` / `approval_id`
  (see the `core/telemetry.py` module docstring).
- **Attributes are a whitelist, not a sample.** Raw prompts, user text, retrieved
  documents, tool arguments, PII and credentials never reach a trace
  (`ALLOWED_ATTRIBUTES` + `FORBIDDEN_SUBSTRINGS`, pinned by unit tests).

Remaining tracing work is the evidence layer above the span layer: a persistent/queryable
backend, and propagation under real deployment traffic. The live-collector transport leg
is done (`make otel-collector-smoke`); what it does **not** cover is a real
Agent → RAG → LLM → tool workflow, because the smoke emits the spans directly through
`core.telemetry.span` to avoid dragging in a real LLM, Qdrant, ERP and Celery run.
Call-site wiring is covered separately by `tests/unit/test_telemetry.py`.

Reproduce the collector leg with:

```bash
make otel-collector-smoke   # starts the traces-only Collector, waits for its
                            # zpages servicez readiness endpoint, emits the spans,
                            # force-flushes via the SDK lifecycle, asserts the
                            # Collector received them, writes the evidence report,
                            # then removes only the container it started
```

Safe local entry points include `python3 scripts/benchmark_tool_result_context.py`,
`python3 scripts/benchmark_tool_result_cache_reuse.py`, and deterministic
`scripts/repro_*.py` scripts. Label their outputs local/fixture evidence. Provider
and production runs require an approved environment, redacted output, and a
provenance-bearing artifact.

## Distributed runtime commands

```bash
make runtime-e2e        # tests/integration/runtime: real PostgreSQL + Redis + multi-process Celery
make runtime-chaos      # SIGKILL the worker process group; emits artifacts/runtime/chaos-<ts>.json
make runtime-verify     # emits artifacts/distributed-runtime/<ts>/report.json (schema v2)
make runtime-replay-help
```

These require real PostgreSQL and Redis. The Makefile always injects
`TEST_DISTRIBUTED_DB_URL` / `TEST_REDIS_URL`, so missing infrastructure is a hard FAIL
rather than a silent skip. Full operational procedure:
[operations/distributed-runtime-runbook.md](../operations/distributed-runtime-runbook.md).
