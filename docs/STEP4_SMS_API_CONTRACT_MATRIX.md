# Step 4 SMS API Contract Matrix

Date: 2026-08-30

| Surface | State | Contract |
|---|---|---|
| `POST /v1/communications/messages` | Implemented | `channel=sms`; exactly one E.164 recipient; approved E.164 or alphanumeric sender; text only; mandatory tenant, correlation, idempotency, bearer scope. |
| `GET /v1/communications/messages` | Implemented | Tenant-scoped list with `channel=sms` and canonical status filtering. |
| `GET /v1/communications/messages/{messageId}` | Implemented | Tenant-scoped command/read-model reconciliation. Unknown submission remains `indeterminate`. |
| `GET /v1/communications/messages/{messageId}/events` | Implemented | Ordered canonical timeline with provider status retained as evidence. |
| `POST /v1/communications/messages/{messageId}/cancel` | Implemented | Idempotently dead-letters a persisted/queued command before provider dispatch; refuses an in-flight command. |
| `GET /v1/communications/usage` | Implemented | Provider-neutral accepted/delivered/failed/suppressed counts split by email and SMS. |
| `GET /v1/communications/providers/health` | Prepared | Returns both Klyrow and Telnexa as disabled until provider bindings are reviewed and activated. |
| `POST /api/v1/telnexa/events` | Implemented | OIDC, HMAC, freshness, durable inbox replay control, then canonical DLR/MO/STOP/HELP normalization. |

## Durable command mapping

An accepted SMS creates exactly one `sms.message.submit.v1` command targeting
`telnexa-sms` with capability `SMS_DELIVERY`. Its payload is validated against
`contracts/telnexa-sms-command.v1.schema.json` and carries normalized sender,
destination, content, encoding, character count, segment count, category,
client reference, schedule, and optional billing/campaign references.

No provider password, Jasmin credential, bearer token, or private key is part of
the command payload.

## Signed Telnexa events

The reviewed event allowlist is:

- `codestra.events.sms_received`
- `codestra.sms.help_requested`
- `codestra.sms.inbound.received`
- `codestra.sms.message.delivered`
- `codestra.sms.message.failed`
- `codestra.sms.recipient.opted_out`

Exact event replays are acknowledged without a second timeline effect. Reusing
an event identity with different content is rejected. A late `sent` event never
downgrades a delivered SMS.
