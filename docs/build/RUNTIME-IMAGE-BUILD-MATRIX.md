# Runtime image build matrix

The repository has one supported build definition, `Dockerfile.runtime`. CI
selects explicit named targets. There is no separate migration or specialized
adapter Dockerfile: the migration runner is packaged in the API/worker runtime,
and no specialized adapter image is canonical yet.

## Immutable bases

| Purpose | Human-readable tag | Immutable digest | Package policy |
|---|---|---|---|
| Python dependency builder | `python:3.13.15-slim-bookworm` | `sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1` | No OS package installation or upgrade |
| Test | `python:3.13.15-bookworm` | `sha256:62eafe52c91cad83c2c74e630bfde917da8c253673e695665d454def84fc9a13` | Uses base-provided OpenSSL 3.0.20; no OS package installation or upgrade |
| Production final | `gcr.io/distroless/python3-debian13:nonroot` | `sha256:f3d5ddc6c64a019fe520e7f005f2880be21e6afc461b10a3c15ef2e4edc71e33` | No shell, package manager, or OS mutation |

## Supported targets

| IMAGE_ROLE | DOCKERFILE | TARGET | BASE_IMAGE | BASE_DIGEST | PACKAGE_MANAGER | OS_PACKAGES | PYTHON_DEPENDENCIES | RUN_USER | ENTRYPOINT/CMD | HEALTHCHECK | OCI_SOURCE_LABEL | OCI_REVISION_LABEL | OCI_VERSION_LABEL | BUILD_STATUS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Middleware API | `Dockerfile.runtime` | `runtime` | distroless Python Debian 13 nonroot | `sha256:f3d5ddc6c64a019fe520e7f005f2880be21e6afc461b10a3c15ef2e4edc71e33` | None in final | None added | `requirements-runtime.txt`, exact hashes | `65532:65532` | inherited Python entrypoint; `-m uvicorn app.main:create_app --factory` | `/health` liveness; `/ready` is separate readiness | build arg | build arg | build arg | PASS locally |
| Outbox/Temporal worker family | `Dockerfile.runtime` | `worker` | distroless Python Debian 13 nonroot | same final digest | None in final | None added | `requirements-runtime.txt`, exact hashes | `65532:65532` | inherited Python entrypoint; default `-m workers.run_outbox`; deployments may select the packaged Temporal module | Process liveness; readiness is worker/dependency telemetry, not an HTTP liveness probe | build arg | build arg | build arg | PASS locally |
| Connector Runtime | `Dockerfile.runtime` | `connector-runtime` | distroless Python Debian 13 nonroot | same final digest | None in final | None added | `requirements-connector-runtime.txt`, exact hashes; installed connector SDK and service wheels | `65532:65532` | inherited Python entrypoint; `-m uvicorn codestra_connector_runtime.main:app` | `/healthz` liveness; `/readyz` readiness | build arg | build arg | build arg | PASS independently |
| Test image | `Dockerfile.runtime` | `test` | Python 3.13.15 Bookworm | `sha256:62eafe52c91cad83c2c74e630bfde917da8c253673e695665d454def84fc9a13` | None invoked | Base-provided OpenSSL 3.0.20 | `requirements-test.txt`, exact hashes | `10001:10001` | `pytest -q` | Not long-running | build arg | build arg | build arg | PASS; 111 passed, 23 skipped |
| Migration invocation | `Dockerfile.runtime` | `runtime` | same as API | same final digest | None | None added | runtime lock | `65532:65532` | override inherited entrypoint arguments to run `/app/scripts/migrate_runtime.py` | Job exit status | build arg | build arg | build arg | Packaged; no separate image |

All Codestra-built targets accept `SOURCE_REPOSITORY`, `SOURCE_REVISION`,
`SOURCE_VERSION`, and `BUILD_DATE`. API and worker expose immutable metadata as
`APP_SOURCE_SHA`, `APP_VERSION`, and `BUILD_TIME`; the connector exposes
`CONNECTOR_RUNTIME_RELEASE_SHA`.

## Deployment security contract

Production composition must set `read_only: true`, `cap_drop: [ALL]`, and
`security_opt: [no-new-privileges:true]`. `/tmp` is an explicit bounded tmpfs.
The connector's encrypted webhook body directory must be an explicitly owned
ephemeral or dedicated volume; application source remains read-only. No
canonical target requires a Linux capability.
