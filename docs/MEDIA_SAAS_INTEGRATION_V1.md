# Media SaaS Integration V1

## Authority and topology

Blender, Natron, and Kdenlive remain independent product/runtime repositories.
Middleware owns only the cross-system command, idempotency, audit, read-back, and
reconciliation contract.

Integrated traffic is:

```text
client
  -> Caddy
  -> Kong
  -> Middleware :8095
  -> private media SaaS adapter
  -> standalone media API
  -> native engine worker
```

There is no approved Caddy or Kong route to a native Blender, NatronRenderer,
MLT/melt, or Mixxx listener. Large media payloads must use object storage; the
Middleware command contains project/asset references and bounded job parameters,
not the media bytes.

The public edge does not need a new media-specific path. The existing canonical
`POST /platform/v1/commands` route is the only public submit path. Kong applies
the existing `middleware-api` audience and `platform.command` scope. Middleware
then resolves the command family to exactly one connector.

## Command families

| Command type | Connector | Standalone job kind |
| --- | --- | --- |
| `media.blender.render.frame.v1` | `blender-render` | `render_frame` |
| `media.blender.render.animation.v1` | `blender-render` | `render_animation` |
| `media.natron.render.project.v1` | `natron-compositor` | `render_project` |
| `media.natron.render.writer.v1` | `natron-compositor` | `render_writer` |
| `media.kdenlive.render.timeline.v1` | `kdenlive-render` | `render_timeline` |

Every family requires `MEDIA_RENDER`. The capability is committed as
`false` in `config/capabilities.v2.json`; source integration therefore cannot
activate live render execution by configuration drift or by merely merging this
change.

## Request contract

The generic Middleware command payload for a media job is:

```json
{
  "command_type": "media.blender.render.frame.v1",
  "command_version": "1.0",
  "tenant_id": "TENANT",
  "requested_by": "verified-subject",
  "correlation_id": "correlation-id",
  "idempotency_key": "stable-idempotency-key",
  "payload": {
    "project_id": "project-123",
    "parameters": {
      "source_path": "scene.blend",
      "frame": 42
    }
  }
}
```

Middleware binds `target` and `capability` from the generated connector command
registry. A caller cannot redirect a Blender command to Natron or Kdenlive.

## Private adapter contract

Each adapter sends:

- `Authorization: Bearer <private service credential>`
- `X-Tenant-ID`
- `X-Actor-ID` on submit/cancel
- `X-Correlation-ID`
- `Idempotency-Key` on submit/cancel
- tracing headers from the Middleware execution context

The current standalone bootstrap APIs use private bearer service credentials.
The connector manifests declare the target OAuth2 client-credentials shape and
remain `UNVERIFIED_TEMPLATE_ONLY`; production activation is not authorized
until identity/mTLS certification replaces the bootstrap transport.

Private endpoints used by the bridge:

- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/by-idempotency?idempotency_key=...`
- `POST /v1/jobs/{job_id}/cancel`
- `GET /readyz`

The idempotency lookup is mandatory for ambiguous-submit recovery. Middleware
can determine whether a timed-out submit reached the standalone service without
creating a second render.

## Failure and read-back policy

A successful submit returns the standalone `job_id` as
`provider_operation_id`.

Read-back state mapping:

| Standalone state | Middleware read-back |
| --- | --- |
| `succeeded` | `MATCHED` |
| `queued`, `running`, `cancel_requested` | `UNAVAILABLE` / still pending |
| `failed`, `cancelled` | `MISMATCH` |
| 404 | `NOT_FOUND` |

Connect failures are safe pre-effect transient failures. Timeouts or other
transport failures after connection are treated as ambiguous and require
read-back. Provider credentials are never included in safe evidence.

## Configuration

The runtime bridge is inert unless both values for an engine are configured:

- `BLENDER_SAAS_BASE_URL` and `BLENDER_SAAS_SERVICE_TOKEN`
- `NATRON_SAAS_BASE_URL` and `NATRON_SAAS_SERVICE_TOKEN`
- `KDENLIVE_SAAS_BASE_URL` and `KDENLIVE_SAAS_SERVICE_TOKEN`

Non-loopback origins must be HTTPS. Plain HTTP is accepted only for
`localhost`, `127.0.0.1`, or `::1` development/runtime co-location.

## Activation boundary

This source change does **not** authorize production rendering. Before
`MEDIA_RENDER` can be enabled, the release must separately prove:

1. exact merged source SHAs for all three standalone services and Middleware;
2. Keycloak/OAuth2 or mTLS service identity aligned with the connector manifests;
3. OpenBao-backed secret delivery;
4. private network reachability with no native public listener;
5. object-storage media flow and tenant isolation;
6. staging submit/read-back/cancel tests for each engine;
7. resource quotas and render-worker isolation;
8. rollback and reconciliation evidence.

Until those gates are satisfied, the standalone services can be tested
independently while the integrated Middleware render capability remains closed.
