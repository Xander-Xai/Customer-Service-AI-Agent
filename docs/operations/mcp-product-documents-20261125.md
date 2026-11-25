# Business MCP: approved product support documents

This retrofit adds an actual customer-support tool to the existing application
MCP registration path. It is not a second FAQ demo: product data is an
operator-supplied, digest-pinned, customer-public catalog from the product
documentation owner. The old synthetic FAQ remains a protocol regression only.

Use the tool to retrieve an approved SKU's usage, storage or packaging
instructions. Native ERP still handles products, stock and orders; this
separate service supplies versioned customer-facing documentation. The model
cannot specify a file path or arbitrary SQL, and no personal data is accepted.

Private catalog schema: {"schema":"customer-product-documents/v1","records":[...]}.
Each record requires sku, category (usage/storage/packaging), source_id,
revision, valid_from and valid_until (ISO date), audience (customer_public),
and text (up to 1200 characters). Duplicate SKU/category is rejected. Unknown
or expired entries return NOT_AVAILABLE; a model must not invent a replacement.
Source owner review is an operator attestation, not independent licensing proof.
Keep the catalog outside the repository, rotate its digest on approval, and
restart the service to load a new revision.

Example private runtime configuration (for Compose, place the approved catalog
in the ignored host `data/` directory and use its `/app/data/...` container path;
never commit the catalog or confidential text):

```dotenv
MCP_ENABLED=true
MCP_FAIL_CLOSED=true
MCP_SERVERS=[{"name":"productdocs","transport":"stdio","command":"python3","args":["/app/tools/readonly_mcp_product_documents.py","--catalog","/app/data/product-docs.json","--sha256","<64-hex-digest>","--authorized-customer-disclosure"],"allowed_tools":["lookup_product_support_document"],"risk_level":"low","timeout_seconds":5}]
```

The standard Compose deployment starts with the committed synthetic fixture and
registers `mcp__productdocs__lookup_product_support_document` into the actual
ToolRegistry through the official MCP SDK and real stdio subprocess. First boot
creates A2A credentials in a private Docker volume; no secret is committed.
Its result provenance is `SYNTHETIC_LOCAL_FIXTURE`, not operator approval.
Industrial mode requires an operator-approved catalog and digest. Set
`MCP_ENABLED=false` for rollback. CI/local fixture evidence does not prove
product correctness or enterprise approval.
Use scripts.verify_mcp_staging with an independently reviewed expected document
as a deployment gate. CI fixture proves the SDK/registry/subprocess path, not
actual product accuracy, remote authentication, model routing or historical
employer deployment.

The stdio executable uses an absolute installed script path and does not depend on the caller's current working directory. Keep its trusted sibling protocol module in the same installed directory.
