# Durable command ledger

All effectful middleware requests enter through `POST /v1/commands`. The API
validates the canonical command envelope, authenticates the requesting subject,
authorizes the tenant, owning adapter, and capability, then commits two records
in one PostgreSQL transaction:

1. the command in `middleware_commands`; and
2. its `temporal-command` outbox intent.

The caller receives `202 Accepted` and a `Location` header for
`GET /v1/operations/{command_id}`. An exact replay returns the same operation
with `200 OK`; reuse of either the command ID or idempotency key with different
content returns `409 Conflict`.

## State machine

```text
persisted -> queued -> dispatching -> accepted -> readback_pending -> completed
                         |               |               |
                         +---------------+---------------+
                                         |
                              reconciliation_required
```

`completed` is legal only after a provider read-back activity returns `matched`.
An ambiguous adapter error, failed read-back, or mismatch moves the command to
`reconciliation_required`; it is never reported as success. Every state change
is appended to `middleware_command_audit`, and each dispatch attempt is tracked
in `middleware_command_attempts`.

The outbox dispatcher uses a workflow ID derived from `(tenant_id, command_id)`
with duplicate workflow starts rejected or attached to the existing run. This
makes PostgreSQL redelivery safe without creating a second command execution.

## Activation boundary

`connectors/generated/command-registry.v1.json` maps each command prefix to
exactly one adapter, capability, and mandatory read-back policy.
`config/capabilities.v2.json` defaults every capability to `false`. The Temporal
worker's provider activities also fail closed until a reviewed adapter is
explicitly bound. Enabling a configuration flag alone therefore cannot activate
a provider write.
