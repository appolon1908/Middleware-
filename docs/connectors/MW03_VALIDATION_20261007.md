# Connector runtime validation ? 2026-10-07

Branch: `section/mw-03-connectors`. Continued from handoff `b4a29763`.
All prior authority regression tests were retained.

## Changes verified

- Cached trusted factories reject calls after upgrade or rebinding; retries check
  the binding generation. Failed overlapping upgrades preserve registry state.
- Malformed submission results retain the UNKNOWN crash marker; redelivery
  reconciles without resubmission. Settlement rejects malformed or changed provider
  references and preserves known references when omitted by a provider.
- Durable journal writes validate normalized results and monotonic bounded attempts
  before persistence. Database idempotency keys enforce printable non-space ASCII.
- Generated API uses OpenAPI 3.1.1. SDK CI collects pytest regressions. Runtime CI
  prepares the non-owner role required for tenant isolation; test dependencies
  include the YAML parser used by contract tests.

## Local evidence

Windows, Python 3.12.10, portable PostgreSQL 17.6 on loopback port 55433.
Dependencies installed into an isolated environment outside the repository.
All adapters were test doubles; no live providers were activated.

- Combined SDK and runtime suite: **113 passed, 11 subtests passed**.
- Complete runtime suite after downgrade-to-base and upgrade-to-head:
  **49 passed**, including API, worker restart/replay, concurrent workers,
  tenant RLS with a non-owner role, secret rejection, and environment isolation.
- Migration head: `20261007_0005`.
- PostgreSQL custom-format backup/restore preserved row counts: command journal 15,
  installations 20, audit log 30.
- Generated artifact check, SDK standards/manifests validation, isolated connector
  contract validation, Ruff, and `git diff --check`: passed.
- Mypy: no issues in 29 SDK/runtime source files.

Combined test invocation (with local database URLs and both source roots in
`PYTHONPATH`):

```text
python -B -m pytest --noconftest -q -p no:cacheprovider   -c services/connector-runtime/pyproject.toml   tests/test_connector_sdk_v1.py tests/test_connector_sdk_standards_v1.py   tests/test_connector_sdk_review_findings.py tests/test_connector_sdk_runtime_authority.py   services/connector-runtime/tests
```

`--noconftest` isolates these suites from unrelated application-wide fixtures.
The only test warning was Starlette's upstream httpx TestClient deprecation.
Local validation does not substitute for the configured Linux/Python 3.13?3.14 CI
matrix; those hosted jobs were not run during this workstation session.
