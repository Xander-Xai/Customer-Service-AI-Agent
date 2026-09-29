# Production evidence measurement

This harness makes Issue #7 measurable without claiming that local fixtures are production evidence.

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
- retrieval supports Hit@K, Recall@K, and MRR; NDCG requires relevance labels
- task success uses deterministic evidence, field, tool, and degraded-signal assertions; an LLM judge is not the sole grader
- recoverability distinguishes `normal_success`, `degraded_success`, `graceful_failure`, and `hard_failure`

## External seams

`provider`, `redis`, `erp`, and `qdrant` suites are disabled by default and produce `NOT_VERIFIED` records without making external calls. Future adapters must be explicitly enabled, use staging-safe credentials, and never write raw provider responses, PII, credentials, or live Qdrant migration results to artifacts.

Qdrant migration is limited to dry-run, disposable local collections, or explicitly approved staging validation. No production migration is performed by this harness.

## Artifact privacy and interpretation

`artifacts/evidence/` is ignored. Reviewers must inspect workload version, Git SHA, configuration hash, sample counts, source labels, and skipped/missing values before interpreting a result. A successful local run proves only that the measurement harness and fixtures are reproducible; it does not verify provider billing, production latency, Redis, ERP, Qdrant, BM25 production traffic, or production task success.

The controlled staging attempt associated with this harness received HTTP 401 for every measured request. It collected no provider token usage or provider billing, so it does not close any production-validation item in Issue #7. Its application-measured failure timing is troubleshooting evidence only, not production latency evidence.

The follow-up direct auth probe also returned HTTP 401 on its single request. The observed root cause is authentication failure for the supplied credential at the official SiliconFlow endpoint; whether the key is expired, revoked, account-mismatched, or otherwise invalid is not established by this probe. Chat, streaming, and staging reruns are blocked until a valid credential is supplied.
