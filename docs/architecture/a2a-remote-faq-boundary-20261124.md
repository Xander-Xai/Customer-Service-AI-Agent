# A2A 1.0: independently deployed read-only FAQ specialist (2026-11-24 retrofit)

## Portfolio: employer/timeline separation

| Repository | Employer context | Principal competence |
| --- | --- | --- |
| Customer-Service-AI-Agent | Earlier employer | LangGraph internal multi-Agent, tools/ERP/HITL/checkpoints, **this retrofitted A2A interface** |
| Beauty-Industry-RAG-QA-System | Same earlier employer | Multi-modal RAG, permission and evidence controls; NO A2A |
| Gaobao-Advisor | Later/different employer | Agent harness, session memory, governed tools and synthetic sandbox; NO A2A |

This A2A integration is a **later portfolio retrofit**, NOT a claim that a
historical employer deployed the A2A service or achieved an A2A business KPI.

## Why A2A belongs in customer service

The seven expert agents and graph nodes in the original customer-service app
are **in-process orchestration**, not independent A2A peers. MCP standardizes
tools; A2A standardizes communications between autonomous *agent services*.
Here we introduce a separate process with its own A2A Card and RPC endpoint.
The main service delegates through one explicitly opted-in ReAct read-only tool.
Nothing in a refund/order write path is delegated; no second or third repository
adds an A2A layer.

### Protocol version and reference

- Protocol: A2A **1.0**, JSON-RPC binding, one text/plain skill.
- SDK: official Python a2a-sdk **1.3.1** (2026-11-17 release), isolated
  optional dependency \`requirements-a2a.txt\`.
- Agent Card: \`GET /.well-known/agent-card.json\` with JSONRPC 1.0
  interface, advertised HTTP Bearer security, one fictional FAQ skill.
- RPC: \`POST /a2a/jsonrpc\` with A2A SendMessage → Task/Artifact →
  completed/rejected. SDK implements task lookup semantics, no custom
  imitation of JSON-RPC or protocol types.
- No streaming, push notifications, long-running tasks or cross-user
  persistent task storage is claimed. TaskStore is **in-memory** only.

Sources:
- https://a2a-protocol.org/latest/specification/
- https://github.com/a2aproject/a2a-python
- https://a2aproject.github.io/a2a-python/docs/migrations/v1_0/

## Threat boundary / default-deny

1. **Explicit opt-in in both processes**: \`APP_ENV=development\` (or
   \`local/test/testing\`) and \`A2A_FAQ_EXPERIMENT_ENABLED=true\`. Unset/staging
   /production fail closed. Main service uses no A2A SDK unless opt-in.
2. **Service auth**: \`A2A_FAQ_SHARED_TOKEN\` with at least 32 ASCII bytes,
   Bearer header checked in constant time on every RPC request. Agent Card
   publicly describes the required authentication. Use a random token only,
   and transmit through local loopback, not a public network.
3. **No SSRF / untrusted card redirection**: Remote base URL must be
   \`http://127.0.0.1:PORT\` or \`http://localhost:PORT\`; card identity,
   single JSONRPC 1.0 interface, skill and **exact RPC URL** are checked
   *before* creating SDK client. No redirects; no ambient proxy env.
4. **Data minimization**: Only three fictional ID strings can travel across
   the boundary: \`returns_demo\`, \`shipping_demo\`, \`invoice_demo\`.
   Any raw user query, customer ID, memory, order text, refund instruction
   or arbitrary tool argument is rejected **before** network traffic.
5. **No writes**: The remote executor reads fixed synthetic module constants;
   it imports no ERP, no database and no LLM. Result is explicitly labeled
   \`SYNTHETIC EXAMPLE ONLY\`. A completed Task with multiple/no artifacts
   or missing provenance fails validation.
6. **Timeout/failure**: 3-second client deadline, no retries on unknown
   responses, no auto tool success on errors; tool result cache disabled.
   No secrets in error messages. The optional tool registration is idempotent
   by rejecting duplicate names, and does not shadow native ERP tools.
7. **No public API**: \`integrations.a2a_policy.server\` is an independent
   loopback process. It is **not mounted** in customer-service FastAPI
   production routes.

## Run the two independent agents (local synthetic data only)

Install the **optional** SDK, without changing main requirements:

\`\`\`bash
python -m pip install -r requirements-a2a.txt

export APP_ENV=development
export A2A_FAQ_EXPERIMENT_ENABLED=true
export A2A_FAQ_SHARED_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export A2A_FAQ_ENDPOINT=http://127.0.0.1:41243

# Terminal 1: independently running synthetic policy A2A service.
python -m integrations.a2a_policy.server --port 41243

# Terminal 2: the existing customer-service ReAct registry registers one
# additional read-only synthetic_a2a_faq_lookup tool when explicitly enabled.
# Its handler invokes the A2A client and sends only one allowlisted topic.
\`\`\`

In Windows PowerShell, set the same four environment variables with
\`$env:APP_ENV\`, \`$env:A2A_FAQ_EXPERIMENT_ENABLED\`,
\`$env:A2A_FAQ_SHARED_TOKEN\` and \`$env:A2A_FAQ_ENDPOINT\` instead of
Unix \`export\`.

## Executable verification

\`\`\`bash
python -m pip install -r requirements-a2a.txt "pytest==8.4.2" "pytest-asyncio==0.23.8"
python -m pytest -q tests/unit/test_a2a_contract.py tests/integration/test_a2a_sdk_interop.py -o addopts=''
\`\`\`

The separate \`.github/workflows/a2a-interop.yml\` installs the official
SDK for Python 3.10 and 3.11 and tests Agent Card discovery, Bearer rejection,
the A2A SDK SendMessage/Task/Artifact roundtrip, topic rejection, and endpoint
guards without contacting an external LLM.

## Not yet a production A2A feature

No cross-service deployment in a real company, user-scoped authorization,
multi-tenant TaskStore, streaming/resume, push subscriptions, service mesh,
task-level persistence, certificate authentication, outbound failure budget
metrics or realistic FAQ corpora have been validated. Do NOT put "production
A2A integration" or fabricated effectiveness numbers on a résumé.
