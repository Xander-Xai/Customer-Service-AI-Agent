# Next release notes — DRAFT (not published)

**Status:** draft only; no version or publication date assigned. Do not create a tag, publish a GitHub Release, or push an image based on this document.

## Candidate changes on current `main`

- Agent behavior evaluation uses successful `tool_executions` receipts as execution evidence and reports workflow, response, governance and task completion separately with explicit denominators/exclusions.
- A regression comparator checks dataset SHA and clean source provenance before comparing directional gates; the committed scripted baseline is reproducible and fail-closed on incompatible artifacts.
- Adversarial safety tests are a blocking CI lane. A test-only PostgreSQL regression covers the external-operation-success/local-receipt-commit-failure window and requires reconciliation rather than blind replay.
- Standard Compose enables the existing synthetic read-only ProductDocs MCP tool and A2A peer; this is protocol fixture support, not enterprise catalog or full application readiness.
- Repository documentation, contribution/security guidance and engineering navigation are being normalized separately; include them only after their documentation PR is merged.

## Compatibility

- Runtime version remains `6.3`; GitHub Release tags use an independent SemVer sequence. This draft does not decide the next release version.
- Current changes add evaluation evidence and tests; no schema migration or API version change is identified from PRs #88/#89.
- PR #87 is merged for its source/config behavior, but full standard Compose API/Worker readiness is still unverified; do not describe it as deployment acceptance or release-ready protocol operations.

## Evidence and limitations

- PR #88 exact-head CI passed on `bfd8f841b2d1ea179fe0649cd80cbb082bcc1302`; PR #89 exact-head CI passed on `891bb97bd65ae8b0765de76c15b78ec93a0be9a4`. Current `main` merge commit and post-merge CI must be checked again before a release.
- Main SHA `31a11f91d9a09ba6d5e8763db84151be930f9a27` CI run [45785580625](https://github.com/Xander-Xai/Customer-Service-AI-Agent/actions/runs/45785580625) passed and published GHCR image tags `31a11f9` / `latest`, digest `sha256:fd50bd3ceee95a8d020b1f54fb4c21e8fc95b90fd91a2037ff07c123ef0586c7`; deploy was skipped. This is not a stable release or production validation.
- The 115-case baseline uses a deterministic scripted LLM and fixtures; `human_confirmed_only=0`, overall `INCONCLUSIVE`. It is not real-model quality or independent human review.
- The receipt crash-window test uses a deterministic fake external operation and real PostgreSQL; it does not establish real ERP receipt reconciliation or cross-system exactly-once.
- Real ERP, authorized catalogs/TLS, human staff UAT, independent labels and production traffic remain unverified; see [Production Readiness](../../production-readiness.md).

## Release gate

Do not publish a stable release until exact current-main CI is green, required security/build checks pass, supported deployment/recovery commands are verified, and the release scope excludes any unaccepted Draft PR. No production deployment is authorized by this draft.
