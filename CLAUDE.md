# Claude Code repository guidance

Use [`AGENTS.md`](AGENTS.md) as the canonical repository instruction file. It
defines project scope, evidence rules, architecture and memory boundaries,
security requirements, verification levels, and GitHub merge/closure rules.

**RAG 当前 649-query 正式指标为 NOT_VERIFIED**；不得发布当前检索分数。See
the canonical [RAG evaluation reference](docs/reference/rag-evaluation.md).

Before changing code, read the relevant module documentation and tests. For
Runtime or Memory changes, also read:

- [`docs/reference/memory-engineering-positioning-20261124.md`](docs/reference/memory-engineering-positioning-20261124.md)
- [`docs/reference/current-state.md`](docs/reference/current-state.md)
- [`docs/architecture.md`](docs/architecture.md)

Do not infer current repository status from this file. Resolve the live GitHub
`main` SHA and inspect exact-head checks, releases, open PRs, Issues, and
deployment evidence as required by `AGENTS.md`.

Dated audit reports labelled `HISTORICAL AUDIT SNAPSHOT` preserve evidence from
their recorded point in time; they do not replace current-state documentation.
