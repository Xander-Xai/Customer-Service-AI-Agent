# Security Policy

## Supported versions

This repository is a reference implementation. Security fixes are applied to the
`main` branch; no older release line is separately maintained. There is no
versioned release tag, so "supported" means `main`.

## Reporting a vulnerability

**Please do not report unpatched vulnerabilities in a public GitHub Issue.**

This repository uses **GitHub private vulnerability reporting** as its single
canonical, non-public disclosure channel:

1. Open the repository's **Security** tab →
   [Report a vulnerability](https://github.com/Xander-Xai/Customer-Service-AI-Agent/security/advisories/new).
2. GitHub creates a private security advisory visible only to the reporter and
   the maintainers.

This is the only supported channel. There is no dedicated security email
address or third-party security portal for this project; please do not invent
or rely on one.

### What to include

- A description of the issue and its impact.
- Reproduction steps or a minimal proof of concept.
- Affected files, endpoints or configuration, if known.
- Any suggested remediation.

### What to expect

This is a single-maintainer reference project without a paid on-call rotation,
so response is **best-effort**. There is no contractual SLA. Reports are
acknowledged and triaged as maintainer time allows, and coordinated disclosure
timing is agreed with the reporter in the private advisory.

## Scope and boundaries

- Runtime security controls that are actually implemented are documented in
  [`docs/design/security.md`](../docs/design/security.md). That file describes
  implemented controls, not production-verified guarantees.
- Capabilities that are not production-verified are tracked as
  `NOT_VERIFIED` / `NOT_MEASURED` in the repository's evidence documentation.
  A control existing in code is not a claim that it has been validated under
  real production traffic.
- Out of scope: findings that require a real production cluster, real ERP
  credentials, real provider billing or multi-tenant deployment, which this
  repository does not operate.

## Public issues

Public Issues are welcome for ordinary bug reports, documentation problems and
feature discussion **that do not contain vulnerability details**. If you are
unsure whether a report is security-sensitive, use the private channel above.
