# PAS-266 — Video media artifact handoff through Middleware

Status: implementation candidate  
Historical implementation issue: PAS-266 — NABEEL-07 — Video post-production & publishing pipeline  
Base: `main@0be6d28b4e8c3cbd5fb7564b6d30689a265d3ed9`

## Outcome

The Codestra Video Controller registers completed media artifacts with Middleware.
The Video Controller MUST NOT call Codestra Social/Postiz directly.

Canonical control path:

```
Video Controller
  -> Middleware private authenticated ingress
  -> /api/v1/social/media
  -> PostgreSQL social_media_assets
  -> later governed MEDIA_UPLOAD/draft/publication command
  -> Codestra Social
```

Registering an artifact does not upload it to a provider, create a social post,
schedule content, or publish anything.

## Authority

Middleware owns cross-system command authorization, tenant binding, durable
idempotency, audit and effect gating. `social_media_assets` is the durable
handoff record. Codestra Social remains authoritative for social-domain media
metadata after a separately authorized provider/product delivery.

## API

### POST /api/v1/social/media

Requires:
- verified Keycloak bearer;
- `social.write`;
- tenant authority for `tenant_id`;
- `Idempotency-Key` (16..255);
- media type, content type, storage reference, SHA-256 checksum.

Returns the durable asset and `provider_delivery: disabled`.

The asset UUID is deterministic from tenant + idempotency key. Identical replay
returns the same record. Reusing the key with a different artifact returns
`SOCIAL_IDEMPOTENCY_CONFLICT`.

### GET /api/v1/social/media/{asset_id}

Requires verified Keycloak bearer, `social.read`, and tenant-bound database
context. RLS remains authoritative.

## Persistence

No migration is required. The existing `social_media_assets` table from
migration 0036 stores:
- tenant;
- media/content types;
- storage reference;
- SHA-256;
- safe metadata;
- creation timestamp.

A `MEDIA_REGISTERED` audit row is written for the first successful
registration. Provider payloads and credentials are never stored.

## Effects and safety

These registration endpoints do not consult or change:
- `SOCIAL_PUBLISH_ENABLED`;
- `POSTIZ_PUBLISH_ENABLED`;
- `POSTIZ_MEDIA_UPLOAD_ENABLED`;
- provider credentials.

Provider delivery remains behind the existing adapter capability and runtime
flags. Live publishing stays disabled.

## Tests

- OpenAPI route presence in `tests/test_social_publishing.py`.
- Real PostgreSQL durable/idempotent media registration, audit evidence and
  conflict rejection in `tests/integration/test_social_staging_runtime.py`.

## Rollback

Application rollback is a normal revert. Existing registered media rows and
audit evidence are preserved. No schema downgrade is needed.

LIVE_CAPABILITIES_ENABLED=NO  
PRODUCTION_DEPLOYED=NO
