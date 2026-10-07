# Middleware API completion matrix

The authoritative inventory is `config/api-completion-matrix.yaml`. It is
generated from the registered FastAPI runtime by
`scripts/generate_api_contracts.py`; the same run writes the JSON OpenAPI and
`contracts/platform/integration-fabric-api.v2.yaml`.

The inventory contains every registered method/path operation, classifies each
entry as `IMPLEMENTED` or `DEPRECATED`, and contains no `UNKNOWN`, `PARTIAL`, or
`MISSING` entry. Deprecated compatibility routes remain executable. Run
`scripts/generate_api_contracts.py --check` to prove that all three committed
artifacts still match the current generator and registered runtime surface.

The deployed integration profile on `:8095` has its own exact runtime inventory:
`config/api-integration-completion-matrix.yaml`, with OpenAPI at
`contracts/platform/middleware-integration-openapi.generated.json` and Postman at
`postman/generated/Middleware-Integration-API.postman_collection.json`. It includes
the Service catalog routes that are absent from the control-plane profile.
Both profiles are generated and checked by the same commands:

```sh
python -m scripts.generate_api_contracts
python -m scripts.generate_postman
python -m scripts.generate_api_contracts --check
python -m scripts.generate_postman --check
python -m scripts.generate_route_authority_report --check
```

Canonical Command, Service, Connector and Automation requests are bounded before
typed handler parsing, including streams with missing or dishonest Content-Length.
Supplied identity headers reject duplicate values, control characters, non-ASCII
values and surrounding whitespace. Boundary registration grants no authentication
exemptions; legacy reads retain authentication-before-tenant-validation behavior.

Run `python -m scripts.certify_mw05_api` with Newman 6.2.1 on PATH to start a
disposable localhost-only fixture, exercise the synthetic collection, and stop it.
The script refuses an occupied port and never starts a command worker. Its 16
requests cover command replay/idempotency, tenant/scope denial, correlation,
malformed headers across all four API families and oversized-body rejection.
Service database behavior and connector delegation are tested separately with
mocked dependencies; this Newman run is not database-backed certification.

Tenant-scoped reads validate a bearer token for the `middleware-api` audience,
enforce the configured caller `status_scope`, and require the token tenant to
match `X-Tenant-ID`. Mutations enforce `command_scope` and, where applicable,
require `X-Correlation-ID`, `Idempotency-Key`, a safe reason, and
`expected_version`.

Inbox, outbox, command, operation, mutation, audit, and reconciliation state is
PostgreSQL-backed. Provider webhooks enforce configured bearer identity, HMAC
signature, timestamp tolerance, payload digest, replay protection, durable
inbox storage, immutable event evidence, and transactional outbox insertion.

All external-effect capabilities remain disabled. Live email, SMS, PSTN,
social publishing, advertising, external model calls, and N8N provider writes
are denied. `CALLS_PLACED` remains zero.
