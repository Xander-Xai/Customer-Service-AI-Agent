# Default Agent configuration (2026-11-24)

The copied `.env.example` enables deterministic tool-result optimization and HITL with an explicit amount threshold of 1.0 (conservative example, not a business-approved refund policy). Explicit tool risk declarations and the side-effect fallback remain in force. Existing `.env` and raw no-env runtime defaults are unchanged. Review rules, migrate the database and verify an authorized staging approval before using writes. A fast-path request does not acquire a durable approval by setting this flag.

Keep HUMAN_HANDOFF_ENABLED off until the migration and staffed queue are verified; MCP_ENABLED off until an audited read-only server is allowlisted; semantic summary, external telemetry, and real-provider eval require their own credentials and consent. A2A peer is tracked by PR #64, not duplicated here.

Acceptance: copy the example, configure required secrets, validate startup configuration, run HITL and tool-result unit/integration tests; verify high-risk write blocks before approval. No tests or production deployment are claimed by this commit. Earlier employment and November 2026 maintenance remain separate.
