# Agent provisioning lifecycle

Odoo sends one provisioning command per person to Middleware and observes the
resulting saga. Middleware alone calls Keycloak, Vicidialer-Codestra (VICIdial),
Klyrow and Telnexa; Odoo never receives provider credentials. Implementation:
`app/api/v1/agent_provisioning.py` (write side), `app/api/v1/agent_provisioning_reads.py`
(read side), `app/core/agent_provisioning_lifecycle.py` (pure rules) and
`app/workers/agent_provisioning_reconciler.py` (crash recovery). Schema:
Alembic `0060_agent_provisioning` (no lifecycle-specific migration; every state
below is already allowed by `ck_agent_provisioning_state`).

## Routes

All routes require a verified provisioning bearer (`identity.request` scope, an
authorized `azp`) whose `tenant_ids` claim covers the row's tenant. A row in a
tenant the caller does not cover answers `404`, exactly like a missing row.

| Route | Purpose |
| --- | --- |
| `POST /platform/v1/agent-provisioning/requests` | Create and run a saga. Requires `Idempotency-Key` and the current `X-Policy-Revision`. |
| `GET /platform/v1/agent-provisioning/requests/{id}` | Saga state, binding and step history. |
| `POST .../requests/{id}/reconcile` | Re-run the saga from a settled state. |
| `POST .../requests/{id}/suspend` | Deprovision and hold the person in `SUSPENDED`. |
| `POST .../requests/{id}/reactivate` | Re-enable identity and re-run the saga. |
| `POST .../requests/{id}/revoke` | Deprovision for good (`REVOKED`). |
| `GET /platform/v1/users/{employee_id}[/campaigns|/entitlements]` | Latest saga for a person (`tenant_id` query is authorized before any lookup). |
| `GET /platform/v1/telephony/assignments`, `/email/identities`, `/sms/identities` | Tenant-scoped, cursor-paged channel views. |

Lifecycle actions take `{"reason": "..."}`. When `X-Policy-Revision` is sent it
must be current (`409` otherwise).

## States

In flight (a saga run owns the row): `REQUESTED`, `VALIDATING`, `IDENTITY`,
`ENTITLEMENTS`, `CHANNEL_PROVISIONING`, `READBACK`, `RECONCILING`.

Settled: `EFFECTIVE` (every step ok), `PARTIAL` (nothing failed but a kill
switch, missing configuration or unimplemented capability adapter gated a
step), `FAILED` (a provider genuinely errored), `SUSPENDED`, `REVOKED`.

| Action | Allowed from | Result |
| --- | --- | --- |
| `reconcile` | `EFFECTIVE`, `PARTIAL`, `FAILED` | Saga re-run; `EFFECTIVE`/`PARTIAL`/`FAILED`. Never from `SUSPENDED` (would silently restore withdrawn access). |
| `suspend` | settled states except `REVOKED`; `SUSPENDED` again to retry an incomplete deprovision | `SUSPENDED` |
| `reactivate` | `SUSPENDED` only | Saga re-run, or stays `SUSPENDED` with `REACTIVATION_GATED`/`REACTIVATION_FAILED`. |
| `revoke` | every settled state, including `SUSPENDED` and `REVOKED` | `REVOKED` (final: no action leads back). |

Any action against a row whose run is still in flight answers `409`. A row
left in flight longer than `AGENT_PROVISIONING_LEASE_SECONDS` (default 300)
was abandoned by a crashed runner: `reconcile`, `suspend` and `revoke` (not
`reactivate`) accept it, as do a same-key create retry and the reconciler.

Every transition appends a hash-stamped row to `agent_provisioning_audit` and
bumps `version`. Repeating a fully completed `suspend`/`revoke` changes
nothing and records nothing.

## Saga steps

`IDENTITY` queries-then-creates the Keycloak user; `ENTITLEMENTS` assigns the
approved campaign roles (voicemail, recording and monitoring grants are
recorded as `blocked` / `CAPABILITY_ADAPTER_NOT_CONFIGURED` until their
adapters exist); `CHANNEL_PROVISIONING` provisions email (Klyrow), sms
(Telnexa) and then phone/webrtc: `sync_agent` -> `reserve_extension` (from
`telephony.extension_pool`) or `adopt_extension` (`telephony.existing_extension`)
-> `provision_webrtc`; `READBACK` re-reads the Keycloak user.

The saga commits after every step. A step whose effect is still live (its
latest succeeded call is not followed by a succeeded inverse) is skipped on
every re-run, so reconcile, recovery and reactivate re-issue only what is
missing. Inverses: `sync_agent` <- `disable_agent`, `provision_webrtc` <-
`revoke_webrtc`, `disable_user` <- `enable_user`. Extensions have no release
route at the provider and stay bound to their VICIdial user.

VICIdial calls carry `expected_version` = the number of succeeded mutations of
that resource for the employee; the provider stays authoritative and a
mismatch surfaces as `VICIDIAL_RESOURCE_CONFLICT`. The WebRTC registration
credential returned by `provision_webrtc` is validated and discarded, never
stored, logged or returned; a `409` from the edge means a session is already
active and counts as provisioned.

## Deprovisioning

`suspend`/`revoke` revoke WebRTC, then disable the VICIdial agent, then
disable the Keycloak user, attempting every step even when an earlier one
fails. Klyrow/Telnexa sender identities are shared campaign resources and
stay in place. If any step is gated or fails the state still changes, with
`last_error_code=DEPROVISION_INCOMPLETE`; repeating the action retries only
what is still live. While a revoke is incomplete the bindings stay reserved.

## Bindings and conflicts

A Keycloak subject (per tenant), a VICIdial user id and an extension belong to
at most one employee until that employee's request is cleanly revoked.
Checks run under a PostgreSQL advisory lock per resource.

| Code | Meaning | Retryable |
| --- | --- | --- |
| `IDENTITY_BINDING_CONFLICT` | Keycloak identity bound to another employee | no |
| `AGENT_BINDING_CONFLICT` | VICIdial user id bound to another employee | no |
| `EXTENSION_BINDING_CONFLICT` | Extension bound to another employee | no |
| `EXTENSION_CONFLICT` | Provider refused the reservation/adoption (`409`) | no |
| `VICIDIAL_RESOURCE_CONFLICT` | Provider version conflict (`409`) | no |
| `VICIDIAL_UNAUTHORIZED` / `VICIDIAL_REJECTED` | Provider `401/403` / other `4xx` | no |
| `READBACK_MISMATCH` | Provider confirmed a different user/extension/subject | no |
| `VICIDIAL_NOT_CONFIGURED` | mTLS transport not configured | no |
| `VICIDIAL_UNAVAILABLE` | Provider `5xx`, timeout or network error | yes |
| `VICIDIAL_ADAPTER_ERROR`, `KEYCLOAK_ADAPTER_ERROR`, `KLYROW_ADAPTER_ERROR`, `TELNEXA_ADAPTER_ERROR` | Other adapter failure | yes |

Each step in the response carries `retryable`. Provider response bodies are
never inspected or echoed.

## Idempotency

`Idempotency-Key` is hashed with the scope `agent_provisioning`. The same key
and body replay the stored response; the same key with a different body is
`409`; the same key while the first run is still in flight is `409` (retry
later); the same key after that run crashed resumes it. A reused `request_id`
under a different key is `409` with no side effects.

## Crash recovery

`AGENT_PROVISIONING_RECONCILER_ENABLED` (default `false`) adds a pass to the
`middleware-reconciler` process (`app.entrypoints.reconciliation_worker`): up
to `AGENT_PROVISIONING_RECONCILER_BATCH_SIZE` rows whose lease expired are
claimed with `FOR UPDATE SKIP LOCKED` and resumed (audit action `recover`).
It never starts sagas and never retries settled `FAILED`/`PARTIAL` rows. A
row whose resumption raises is rolled back, counted and skipped so it cannot
starve the rest of the batch.

## Safety switches

Nothing reaches a provider unless its own switch is open:
`LIVE_IDENTITY_PROVISIONING_ENABLED` (Keycloak), `VICIDIAL_WRITE_ENABLED` +
`LIVE_WRITES_ENABLED` (VICIdial), `KLYROW_WRITE_ENABLED` + `LIVE_WRITES_ENABLED`,
`TELNEXA_WRITE_ENABLED` + `LIVE_WRITES_ENABLED`. A closed switch records a
`skipped` step with `KILL_SWITCH_CLOSED` and settles the saga as `PARTIAL`, never
as a false success. Provisioning never activates a VICIdial agent (`active:
false`) and never places a call.

## Events and metrics

Outbox topics: `platform.user.provisioned`, `platform.user.provision_failed`,
`platform.user.suspended`, `platform.user.revoked`, `platform.user.reactivated`.

Prometheus: `codestra_agent_provisioning_transitions_total{action,from_state,to_state}`,
`codestra_agent_provisioning_steps_total{system,operation,state}`,
`codestra_agent_provisioning_conflicts_total{kind}`,
`codestra_agent_provisioning_stale_recovered_total`,
`codestra_agent_provisioning_stale_recovery_failed_total`. Structured logs
`agent_provisioning_step` and `agent_provisioning_transition` carry the
correlation id and never a credential.
