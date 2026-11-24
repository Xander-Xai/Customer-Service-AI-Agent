# Human support handoff (separate from HITL approval)

The **human support handoff** is a persistent customer-service ticket and
message queue. It is not the `human_approval_gate` that approves high-risk
writes such as refunds.

## Enablement gate

- Migrate the database to `007_durable_human_handoff` in a controlled
  staging environment before enabling the feature.
- Provide actual authenticated customer and `agent`/`supervisor` accounts,
  train staff to monitor the ticket queue, and verify token/session ownership.
- Set `HUMAN_HANDOFF_ENABLED=true` **only after** staff coverage and escalation
  instructions are operational. It defaults to `false`.
- Do not claim production readiness without manual staging checks.

## HTTP contract

| Operation | Method/path | Access |
| --- | --- | --- |
| Create/reuse ticket | `POST /api/handoffs` | JWT customer + valid session token |
| Lookup own session | `GET /api/handoffs/session/{session_id}` | JWT customer |
| Queue | `GET /api/handoffs?status=OPEN` | JWT staff |
| Claim | `POST /api/handoffs/{ticket_id}/claim` | JWT staff |
| Read messages | `GET /api/handoffs/{ticket_id}/messages` | Owner or authorized staff |
| Reply | `POST /api/handoffs/{ticket_id}/messages` | Owner or assigned staff |
| Close | `POST /api/handoffs/{ticket_id}/close` | Assigned staff/supervisor |
| Reopen | `POST /api/handoffs/{ticket_id}/reopen` | Owner or supervisor |
| Audit events | `GET /api/handoffs/{ticket_id}/events` | Owner or authorized staff |

For message POST, send a **unique, stable** `client_message_id` with `body`.
If the caller retries with the same key and content, the server returns the
existing message. Reusing the key with different content is rejected (409).

The queue uses a conditional database UPDATE to guarantee only one staff
account can claim an OPEN ticket. A customer or unauthorized staff account
cannot read another customer's ticket or send as an unassigned agent.

Graph-triggered handoffs use the authenticated `user_id` and `session_id`.
They are not inferred from prompts. The graph persists a ticket before
announcing the handoff, then prevents further autonomous answers while the
ticket is OPEN or CLAIMED. Chat REST/SSE exposes `handoff_ticket_id` and
`handoff_status` so a client can switch to message polling.

## Browser entrypoints

- `/handoffs.html`: customer creates/reuses a current-session ticket, reads messages and can reopen a closed ticket; agent/supervisor/admin sees the queue and can claim, reply and close according to server-side roles.
- `/` and `/admin.html` link to the handoff page. Frontend requires the same authenticated JWT as the rest of the application. Message content is rendered using `textContent` rather than HTML insertion.
- Vite builds the page as a fourth HTML entry. If the server has not deployed fresh built assets, the page may return HTTP 404 even when the backend API has been deployed.

## Operational validation

1. With the flag disabled, ticket APIs return 503 and normal bot behavior
   is unchanged.
2. Apply migration and enable the flag in isolated staging.
3. Ask for a human handoff with a signed-in customer; verify a ticket in DB.
4. Claim simultaneously from two distinct agent accounts: one succeeds, the
   other receives 409.
5. Send a reply with `client_message_id`, retry it, confirm one message.
   Confirm the customer can read it and another customer cannot.
6. Run another bot request in the same session while CLAIMED; it must *not*
   invoke a domain Agent. Close, then verify the bot is available again.
7. Verify audit event history and failure behavior when the DB is unavailable.
8. Verify API authentication, latency and operational staffing before any
   production use.

## Evidence boundary and known gaps

- Database-backed tickets, staff queue and message polling are code-level
  features; this is **not** a connected human-contact-center provider.
- A minimal authenticated browser ticket desk is available at `/handoffs.html` for customers and staff. It polls the existing REST APIs; there is still no push notifier, SLA escalation scheduler or staff shift management. A staff member must keep the queue actively monitored.
- Automatic handoff requires authenticated identity; anonymous requests do
  not create tickets.
- End-to-end production/staging tests with real staff and actual ERP are
  `NOT_VERIFIED`.
- This feature does not approve or execute refunds. HITL approval and
  side-effect idempotency remain separate workflows.

Tracked follow-ups: #31 (staff UI/push/SLA), #33 (business evidence).

## 2026-11-25: staff task-episode triage

The claimed-ticket staff UI reads `GET /api/handoffs/{ticket_id}/task-episode`.
The backend derives the latest run from the ticket's session AND customer
identity AND thread (never from a caller-supplied run ID). Only assigned staff
on claimed tickets or supervisors/admins can inspect it; customers and
unassigned agents cannot. The view shows bounded approval/side-effect statuses
and the next safe inspection, not raw queries, tool payloads or secrets.
This is the latest matching session run, not a guaranteed causal link to
the escalation. It is a non-atomic diagnostic view and never grants
execution authority. Human staff must still use the existing approval,
reconciliation and checkpoint workflows. Real production validation remains open.
