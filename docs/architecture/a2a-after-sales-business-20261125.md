# A2A after-sales business delegation

The existing fictional FAQ remains a protocol regression. The new separately
started policy specialist handles an actual approved after-sales procedure
catalog owned by the policy team. Main customer service delegates only
enumerated issue, purchase channel and reported product/delivery stage.
The specialist selects a current rule, returns a source/revision/digest and
required procedure or REVIEW_REQUIRED. It does not query ERP or authorize
refunds. The main Agent must still verify order ownership and use existing HITL.

Configure the same explicit settings in the main app and sidecar:
`APP_ENV=staging` (or local/test), `A2A_AFTERSALES_ENABLED=true`,
`A2A_AFTERSALES_ENDPOINT=http://127.0.0.1:41243`,
`A2A_AFTERSALES_TOKEN` (private random bearer), and
`A2A_AFTERSALES_POLICY_SHA256` (digest of the approved private catalog).
Loopback HTTP is available in local/staging environments. For an independently
deployed enterprise peer, use the explicit private HTTPS mode described below.
Neither public-network deployment nor mutual TLS is claimed.

Catalog envelope: {"schema":"after-sales-procedures/v1","rules":[...]}.
Each rule contains issue (damaged, wrong_item, return_request, delivery_delay,
invoice), channel (online, store, distributor, unknown), stage (not_received,
received_unopened, received_opened, unknown), route (collect_evidence,
refer_policy_owner, explain_procedure), procedure, source_id, revision,
valid_from, valid_until, audience=customer_public. Duplicate combinations,
unapproved digest and nonpublic rules fail closed. Missing or expired match
routes to human review. The request descriptors remain unverified.

Start the independent process with:
```bash
python -m integrations.a2a_policy.business_server --catalog /private/approved-policy.json --authorized-customer-disclosure
```
The main ToolRegistry registers `after_sales_procedure_review` for standard
Compose and approved external deployments. The two peers validate
card identity and exact RPC endpoint, bearer, bounded deadline, returned scope,
policy digest, dates and nonauthorization fields. Only the operator attests
catalog approval. A2A is justified where policy is independently owned and
deployed; for one in-process service, a native function is simpler.

CI tests use fictional policy fixtures through the actual rule engine and
SDK; no actual company policy, production deployment, model routing,
real ERP, or historic employer accomplishment is claimed.

## Private HTTPS enterprise deployment (2026-11-25)

This opt-in path admits approved catalogs in staging/production without moving
customer data or write authority across A2A. Configure both peers with:

- `APP_ENV=staging` or `production`, `A2A_AFTERSALES_ENABLED=true`.
- `A2A_AFTERSALES_PRIVATE_HTTPS=true`, `A2A_AFTERSALES_ENDPOINT=https://policy.internal.example` (replace with the actual approved DNS name).
- `A2A_AFTERSALES_CA_FILE=/private/company-ca.pem` (absolute approved trust anchor), the existing pinned catalog SHA-256 and a random URL-safe bearer token. In Compose, set host-side `A2A_AFTERSALES_CA_SOURCE=/private/company-ca.pem`; app/worker mount it read-only at `/run/a2a-ca/ca.pem`, which is the in-container `A2A_AFTERSALES_CA_FILE`. Ensure the mounted file is readable by the unprivileged app user.

The client verifies the DNS certificate against the explicit CA, does not
follow redirects or ambient proxies, and verifies the Agent Card RPC URL
against the exact operator-configured origin. Start the specialist with the
existing catalog/disclosure flags and `--listen-port 41243`; the HTTP listener
is loopback only. An operator-managed TLS reverse proxy on that same host
must route only the Agent Card and JSON-RPC endpoints, enforce an 8 KiB request
body limit (including chunked bodies), rate limits, restricted network access,
TLS certificate rotation, and authentication. Mount catalog, CA and secret
read-only, restrict egress/DNS to the reviewed peer, disable public access and
redact request/authorization logs. Industrial profile requires explicit
external mode, approved catalog digest, private HTTPS, CA and bearer; missing
configuration fails closed. Rollback is `A2A_AFTERSALES_ENABLED=false`.

Acceptance before real traffic: valid private CA and hostname complete the
real SDK round trip; wrong CA/hostname, unauthorized caller, redirected card,
expired catalog, oversized request and timeout fail closed; independent policy
owner approves disclosure. TaskStore remains in-memory for synchronous read-only
tasks; do not use it for durable business writes. No real remote deployment,
certificate, ERP integration, or availability measurement is claimed by this PR.

Application request boundary: the shared A2A ASGI middleware now authenticates
before consuming the body, rejects ambiguous credentials/length headers and
enforces 8 KiB on actual received bytes even without Content-Length. It replays
only the bounded body to the SDK. The reverse proxy must still enforce body
timeouts, rate/concurrency limits and request size; the application boundary
does not replace those network controls. CI regression exercises streaming
oversize, length mismatch, duplicate auth and normal SDK round trip.
