# Contributing

Thanks for helping improve this independent customer-service Agent reference implementation. Changes should be tied to a concrete issue or reproducibility gap and stay within the repository's documented architecture and evidence boundaries.

## Development setup

Supported backend runtimes are Python 3.10–3.12; the web toolchain uses Node.js 22. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
cp .env.test .env
make test
make lint
make audit-docs
```

For frontend changes, run `npm ci`, the relevant `npm test` target and `npm run build`. Do not use real provider keys, ERP credentials or production data in ordinary tests. The offline lane must remain offline.

## Module boundaries

- `api/` owns HTTP and authenticated request handling; `core/graph_builder.py` is the production graph builder.
- `runtime/` owns durable run lifecycle, checkpoint resume, worker ownership, recovery and DLQ behavior.
- `core/session/` is conversational context only. Durable business facts and replay evidence come from the run ledger, checkpoint and authorized receipts.
- `tools/` and approved adapters own tool registration/execution. Never use model output or conversation summaries as authorization.
- `evaluation/agent_eval/` owns the behavior-evaluation contract. Scripted tests do not measure real model quality.
- `docs/architecture.md` is the architecture source; follow its architecture-freeze gate before proposing new roles, frameworks or remote boundaries.

## Change and pull request process

1. Search existing Issues and PRs; reference the relevant one in the PR body.
2. Use a focused branch and descriptive commit subject (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Do not force-push shared branches.
3. Include the problem, change, impact, risk level, verification evidence, security effect, documentation updates and rollback approach in the PR template.
4. Update current documentation and generated contracts when behavior, interfaces, configuration or deployment changes. Keep dated reports labelled with their snapshot date.
5. Before merge, review the exact diff and exact-head checks. Do not treat skipped or pending required checks as passing. Verify the merge commit is present on `main` and inspect post-merge CI separately.

## Verification levels

Choose the highest level required by the changed boundary; a lower level is not a substitute:

| Level | Change scope | Minimum evidence |
|---|---|---|
| **L0 — docs/config** | Wording, navigation, static configuration | `make audit-docs`, Markdown/link checks and any documented command affected |
| **L1 — isolated logic** | Pure functions, local policy, serialization | Targeted unit tests and `make lint` |
| **L2 — service integration** | API, database, cache, protocol adapter | Relevant integration/contract tests with the declared service or protocol fixtures |
| **L3 — critical runtime/deployment** | Persistence, replay, concurrency, security, external side effects, Compose | Real PostgreSQL/Redis/process tests or protocol SDK roundtrips as applicable, failure cases, full required CI; no fixture is production evidence |

Useful targets include `make demo-offline`, `make agent-eval`, `make runtime-e2e`, `make runtime-chaos`, and `make mcp-verify`. Runtime targets require their documented real dependencies and must fail if they are absent.

## Security and data

- Never commit `.env`, credentials, private certificates, customer records, internal order data or unapproved third-party material.
- Keep tool authorization server-side and fail closed when identity, approval, receipt or idempotency evidence is unknown.
- Do not claim ERP exactly-once from a local ledger. Unknown external side-effect outcomes require reconciliation, not blind replay.
- Preserve data provenance and label synthetic fixtures. See [SECURITY](.github/SECURITY.md) and [DATA_PROVENANCE](DATA_PROVENANCE.md).

## Bug reports

Use the Bug Report form with a minimal reproduction, expected/observed behavior, environment and exact command/output. Redact secrets and private business data. For vulnerabilities, do not open a public issue; use the private reporting channel in [SECURITY](.github/SECURITY.md).
