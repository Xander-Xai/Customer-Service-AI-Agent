# Public repository secret handling

## Scope

This repository is public. Private keys, API tokens, passwords, and other
credentials must never be committed, including in development certificates.
The existing `.certs/` directory and runtime deployment TLS directory remain
ignored. Deployment TLS files are expected to be mounted from an
environment-controlled secret store.

Production startup now fails closed when those files are missing; it no longer
generates a localhost self-signed key as a fallback.

The historical `.certs/key.pem` found in commit
`d9a48894d814c81a0b7dc5477a5f459d7e83a319` was a localhost self-signed
development RSA key. It is retired and must not be reused. This change does
not rewrite public history: rewriting would change published commit IDs and
would not restore trust in a key that has appeared in a public repository.

## Preventive controls

- `scripts/check_secrets.py` scans the current tracked tree for high-confidence
  private-key and token patterns without printing secret contents.
- The normal CI security job runs the current-tree scan on every push and pull
  request through `ci.yml`.
- `secret-history-scan.yml` runs a complete reachable-history scan weekly and
  on demand. Historical findings are reported by object/path metadata only.
- Run `python scripts/check_secrets.py --history` before a repository migration
  or after rotating a credential. A historical finding requires credential
  retirement and incident assessment; it is not fixed by merely deleting the
  current file.

## Incident response

1. Stop using the exposed credential and rotate/revoke it at its issuer.
2. Preserve the commit and path evidence without copying secret contents into
   tickets or logs.
3. Check current deployment references and runtime mounts.
4. Add or update an allowlisted test fixture only with synthetic values.
5. Consider history rewriting only as a separately approved repository
   migration; it is not part of routine remediation.
