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
