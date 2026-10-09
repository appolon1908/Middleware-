# mw-05-api certification — 2026-10-07

Status: canonical API changes implemented and locally validated; the full
completion definition is **not yet certified**. Linux and disposable-service
integration gates remain outstanding.

Branch: `section/mw-05-api`. The transferred desktop handoff `49dee0da` was
preserved. No deployment, remote push, protected-branch write, provider delivery,
or command worker was performed. Newman used only a disposable localhost fixture
on port 8095 with external effects disabled.

## Changes

- Bound canonical Command, Service, Connector and Automation bodies before typed
  handler parsing, including absent and dishonest Content-Length values.
- Validate supplied identity metadata before canonical handler execution, rejecting
  duplicates, controls, surrounding whitespace, non-ASCII values and excessive
  lengths. Preserve legacy authentication-before-tenant-validation ordering and
  the legacy empty-bearer 401 response.
- Keep boundary registration separate from authentication exemptions and router
  mounting. The connector facade still delegates to connector-runtime; the
  command kernel remains the command authority.
- Document boundary errors and Automation authentication/header requirements in
  runtime OpenAPI. Add exact integration-profile OpenAPI, Postman and completion
  artifacts alongside the existing control-plane artifacts. Generate LF output
  consistently across platforms.
- Refresh the route-authority report for the transferred connector facade.
- Make the Newman runner locate the executable and clean up the Windows venv
  interpreter process tree. Extend the collection with all four API-family header
  denials and an oversized HTTP request.
- Extend the staging source validator for bounded buffering and contract-driven
  connector registration. Reviewed AST fingerprints fail closed on executable
  changes; connector contract paths still undergo governed-route shadow checks.

## Passing evidence

Environment: Windows, Python 3.12, dependencies installed from the hash-locked
`requirements-test.txt`, Newman 6.2.1 in an ignored local environment.

| Gate | Result |
| --- | --- |
| Focused API/security/route/contract suite | 230 passed |
| Follow-up boundary, Automation, artifact and authority suite | 65 passed |
| Final legacy calling and staging source-validator suite | 105 passed, including negative mutation tests |
| Final synthetic Newman run | 16 requests, 42 assertions, zero failures; fixture cleanup succeeded |
| OpenAPI generation `--check` | PASS; control-plane 350 operations, integration 328 operations |
| Postman generation `--check` | PASS for both profiles |
| Route-authority generation `--check` | MATCH; 339 entries including denied legacy routes, zero direct-effect bypasses |
| Changed-source Ruff | PASS |
| Changed-source mypy | PASS targeting Linux, the deployment platform |

The test batches overlap; their counts must not be summed. The integration
contract test compares full runtime schemas, parameters, responses and security,
then checks the Postman and completion-matrix operation sets.

## Remaining gates and full-suite findings

The full Windows run finished with **3,534 passed, 115 failed, 260 skipped**, plus
91 passing subtests, in 858.62 seconds. This run preceded the final compatibility
fixes. Its empty-bearer regression and staging-source validation failure were
addressed with focused follow-up checks.

The full suite is not green. Remaining findings include POSIX secret-file and
workspace permission assumptions, unavailable Linux shell/OpenSSL tooling,
release trust/source-closure drift, and a projection-worker timing assertion.
No claim is made that all remaining failures share one cause or that they have
been cleared. Local raw output is retained in `.venv/mw05-pytest.log`.

The skipped tests include infrastructure-dependent coverage. PostgreSQL/Redis
integration was not certified: this workstation has no configured disposable
services or Docker executable. The Newman fixture and mocked service/connector
tests do not replace those checks. Rerun the repository's Linux CI and its
disposable PostgreSQL/Redis integration gates before declaring the mission fully
complete; preserve the integration fixtures' disposable-target safeguards.

Reproduction commands for generation and Newman are documented in
`docs/API-COMPLETION-MATRIX.md`.
