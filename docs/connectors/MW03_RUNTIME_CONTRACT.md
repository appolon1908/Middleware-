# Middleware connector runtime and control contract

Middleware owns registry authority, tenant and actor authorization, durable command
identity, credentials, submission, destination readback and reconciliation. Public
SDK consumers must submit through the canonical command kernel. This runtime does
not add another provider command HTTP endpoint.

## Registry and lifecycle

Manifest registration and directory loading reject cross-connector command-prefix
and webhook-route overlap before mutation. Semantic versions are immutable; an
upgrade invalidates the in-process adapter factory binding and returns to a disabled
state. Cached factory handles also reject invocation after an upgrade or rebind;
rebinding changes the registry generation checked before each submission. Factories
are registered by trusted application code, never imported from manifest data. Database installs and upgrades serialize catalog ownership validation
inside their transaction; preflight validation is informational.

Disabled upgrades require the upgrade policy flag, OAuth scope, one authorized
tenant, Idempotency-Key, current If-Match, advancing SemVer, matching manifest ID
and digest, and the same isolation cell. Active or failed installations cannot be
upgraded through this API. Upgrade evidence and outbox notifications commit with
the new disabled installation. Disable remains an optimistic, audited kill switch.

## Command execution and recovery

Use `build_worker_runtime` with the Middleware policy engine's authoritative
`authorize(request) -> bool`, effective tenant capability provider, trusted registry,
and PostgreSQL database. The helper requires explicit authorization and combines
it with the external-effects flag; the flag defaults false. Authorization, connector
state, manifest identity and authoritative capabilities are checked before each
submission attempt. Tokens and client capability snapshots cannot confer authority.
The framework-neutral constructor is for already-authenticated internal callers;
its default journal is in-memory and must not be used by production workers.

Identity is `(tenant, environment, connector, idempotency_key)`. The semantic digest
binds command UUID, type, version, payload, actor and manifest digest. Keys contain
8 to 180 printable ASCII characters without spaces, enforced by the runtime and
database. Trace and correlation metadata do not alter semantics. Changed content or actor conflicts.
Terminal results replay without another submission. The journal has no automatic
expiry: deleting an identity can permit a duplicate provider effect.

The journal commits UNKNOWN before adapter submission. Worker death releases its
PostgreSQL advisory lease; redelivery reconciles the existing operation rather than
submitting again. UNKNOWN and timeout/exception outcomes are never blind-retried.
Only explicit FAILED+retryable results permit another attempt, within the manifest
attempt budget and bounded exponential backoff/jitter, preserving command identity.
If a worker dies after persisting a definitive failure, redelivery returns that
failure; it does not start a fresh retry budget. In-progress concurrent callers
receive a conflict and can redeliver later. Journal failures propagate and do not
permit unjournaled execution.

Required readback keeps a completed submission nonterminal in the journal until
readback confirms the outcome. Nonterminal readback raises ReadBackRequiredError;
redelivery resumes readback. Readback exceptions produce UNKNOWN and later
reconciliation. Adapter operation identity must remain stable through settlement.
Adapters must enforce the manifest timeout on their I/O. Synchronous Python adapter
calls cannot be forcibly cancelled safely; adapters must not spawn untracked writes.

Normalized results use `command-result.v1.schema.json`, a typed CommandOutcome,
bounded operation/provider references, finite JSON safe_result (maximum 1 MiB),
boolean retryable and stable error codes. Only FAILED may be retryable. Recursive
secret-key validation applies to payloads, configuration and returned details.
Exception messages and raw provider bodies never become result or error details.

## Control API

The Connector Runtime service serves `/platform/v1/connectors` and equivalent
legacy `/v1/connectors` handlers. Kong routing must send these controls to this
service; deployment routing is outside this implementation. OAuth issuer/audience,
short token lifetime, trusted client, endpoint scopes, tenant RLS, request body limits,
correlation headers and no-store responses apply equally to both prefixes.

Catalog, manifest, validate, install, test, upgrade, disable and health are available.
`/{id}/status` exposes installation/binding status. `/{id}/capabilities` exposes
command declarations, required capability, readback and retry requirements; enabled
is false because this control service does not dispatch commands. A VERIFIED
manifest alone does not bind an adapter or enable a capability.

Read-only `/test` requires a tenant-owned connection_id and an application-registered
adapter matching the installed manifest digest. Configuration comes from tenant
storage, never from an arbitrary provider URL in the test request. Safe test code
and timestamp are persisted and audited; testing does not activate the connection.
Health calls only a trusted digest-bound adapter; unbound health reports not_bound.
The default application binds no adapters. There is no normal API activation route.
Webhook activation/replay remain protected workflows; existing ingress still requires
signed, tenant-resolved delivery and disabled-by-default ingress policy.

## Persistence and observability

Migration `20261007_0005` adds the tenant-RLS command journal to the existing
installation, connection, inbox, outbox, idempotency and audit schema. It contains
request digests and safe outcomes, never command payloads or credentials. The
journal validates results before persistence and rejects decreasing attempt counts.
Session advisory locks must be explicitly released before pooled connection reuse. The
worker helper emits structured submitted/settled/replayed events containing only
connector ID, command ID, correlation ID, outcome, safe error code and attempt count.
Telemetry failures cannot change command results. Management validation errors omit
untrusted input and exception tracebacks from responses and logs.

Validation uses fake adapters and a disposable local PostgreSQL cluster. Production
flags, source capabilities and provider bindings remain disabled. Apply migrations
and gateway routing only through the separately reviewed release process.

Run SDK regressions with `python -m pytest --noconftest -q tests/test_connector_sdk*.py`;
these independent tests include pytest functions that unittest discovery omits.
Run the runtime service tests against a disposable PostgreSQL database with both
`ADMIN_DATABASE_URL` and the non-owner `APP_DATABASE_URL` configured.
