# M1-F01 — Application Factory Design

**Task:** MIDDLEWARE_:M1-F01-01
**Stage:** DESIGN
**Authority:** Middleware V3 / M1 Runtime / Application factory
**Base:** 820d58fed22bbca684c941a6ec8d18c6efe4bf66

## Decision

`app/application.py::create_app` is the sole application factory for the primary Middleware HTTP runtime. M1-F01 extends and certifies that authority; it does not create a parallel factory.

The factory owns composition. Domain modules own handlers and services. The router registry owns route grouping. Entry points select a profile and may supply the service identity, but they must not reconstruct the application.

## Construction authority (M1-F01-I01)

`app/factory.py` is the only module that constructs a FastAPI object. `app/application.py::create_app` composes the Middleware API on :8095 and obtains its FastAPI object from `app.factory.build_application`; `app.factory.create_app` resolves to it. Every separately deployed internal service (event gateway, policy engine, controller, extension allocator, telephony provisioning, webphone issuer, Server A agent, email, observability alerts, Qwen verifier, workers' operational endpoints) is constructed through `app.factory.create_service_app`, with interactive documentation off unless the service opts in. `app/factory.py` imports only FastAPI so service images do not load the API router graph.

The deployed API entrypoint is `app.entrypoints.integration_api` on :8095; no second API entry module exists. `websocket_gateway/` and `services/connector-runtime/` are separate deployables with their own packaging and are outside `app/`.

## Factory contract

Inputs are canonical Settings, an optional injected RuntimeContainer, AppProfile, the compatibility-only legacy_monolith switch, and an optional service identity. Production callers inject neither settings nor runtime.

The output is exactly one FastAPI instance. The factory initializes app.state.settings, runtime_state, runtime, profile, service, and observability; installs the canonical request guard and health/readiness authority once; mounts routers only through app/router_registry.py; refuses duplicate method/path operations; and installs canonical error/OpenAPI behavior after routing.

## Lifecycle and failure semantics

entrypoint -> create_app -> validate configuration -> create RuntimeState -> construct FastAPI/lifespan -> install RequestGuard -> register health/readiness -> mount registry groups -> assert route uniqueness -> install canonical errors/OpenAPI -> return app.

At lifespan start, RuntimeState builds RuntimeContainer when one was not injected and exposes state.runtime. At shutdown RuntimeState closes owned resources and clears state.runtime.

Configuration failure is startup-fatal. Runtime dependency failure is represented by the existing readiness authority and must not be hidden by a second lifecycle implementation.

## Profile composition

| Profile | Canonical | Common | Integration | Appolon | Monolith | Legacy monolith |
|---|---|---|---|---|---|---|
| INTEGRATION | yes | yes | yes | no | no | no |
| CONTROL_PLANE | yes | yes | no | yes | no | no |
| MONOLITH | yes | yes | yes | yes | yes | yes |

The deployed integration API and control-plane canary are strict subsets of the monolith. Edge-denied legacy aliases remain monolith-only.

## Ownership boundaries

| Concern | Authority | Factory responsibility |
|---|---|---|
| configuration | app/core/config.py, app/core/bootstrap.py | validate/select |
| runtime resources | app/core/runtime.py | lifecycle orchestration |
| dependency providers | app/core/providers.py | expose runtime state |
| request policy | app/core/request_guard.py | install exactly once |
| health/readiness | app/core/health.py | register exactly once |
| route membership | app/router_registry.py | mount declared groups |
| errors/OpenAPI | app/appolon_routes.py | install canonical behavior |
| entrypoints | app/main.py, app/entrypoints/integration_api.py | select profile only |

Narrow independently deployed services listed by architecture governance are explicit exceptions; they are not alternate factories for the primary Middleware runtime.

## Security invariants

1. A profile cannot bypass the canonical request guard.
2. A primary-runtime route cannot be mounted directly by an entrypoint.
3. Health/readiness has one authority and cannot be shadowed.
4. Duplicate operations fail at build time instead of relying on route order.
5. Production configuration is validated before serving.
6. Runtime ownership is explicit so injected resources are not accidentally double-owned or leaked.
7. Application construction performs no provider business effect; provider effects remain behind existing fail-closed command/adapter controls.

## Extension rule

A new primary-runtime API capability must add or extend an APIRouter in its domain module, register it in exactly one appropriate registry group, rely on create_app for guard/health/lifecycle/errors/OpenAPI, add profile/authorization/route-uniqueness tests, and never add a new FastAPI() or create_app() for convenience.

A genuinely isolated process requires an architecture-governance exception with its own deployment/runtime contract before construction is permitted.

## Acceptance proof

M1-F01 DESIGN is satisfied when automated governance proves the primary factory remains unique, the factory exposes the canonical state contract, every profile has declared composition and unique operations, deployed entrypoints delegate to the factory, direct primary-runtime router mounting remains forbidden, health/request-guard ownership remain singular, and this design names the canonical authorities.

No production effect, deployment, schema migration, or provider call belongs to this DESIGN task.
