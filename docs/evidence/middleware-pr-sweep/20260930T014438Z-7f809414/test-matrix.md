# Test matrix (S5) - code candidate `b8e75ab9`

Run locally on the single write lane (Claude Code cloud container), 2026-09-30.

| Check | Command (abridged) | Result |
|---|---|---|
| Unit suite | `pytest -q tests` | **4011 passed, 264 skipped, 0 failed**, 94 subtests passed |
| PostgreSQL/Redis integration (fresh disposable DB, as CI) | `RUNTIME_INTEGRATION_TESTS=1 pytest -q tests/integration` | **99 passed, 24 skipped, 0 failed** |
| Provisioning on migrated PostgreSQL (alembic head 0071) | `pytest tests/test_agent_provisioning.py tests/test_agent_provisioning_tenant_scope.py` | **22 passed** |
| New regression tests fail without their fix | each fix reversed in the working tree, test run, fix reapplied | B1: 3/3 fail; E1, E2, B3, A-D1/D2: fail |
| Secret scan on the exact commit | `git archive b8e75ab9`, then `gitleaks dir .` from inside the export (as CI) | **no leaks** |
| Ruff on changed Python files | `ruff check` | PASS |
| Generated artifacts | `generate_postman/api_contracts/route_authority_report --check` | PASS (3/3) |
| Trust derivation | `derive_trust_pins.py --check` | `ACTIVE_STALE_AFTER=0` |
| Certify preflight | `agent_preflight.sh --certify` | PASS |

Notes:

- A first integration run against a reused local database failed one test
  (`test_incident_command_and_notification_commit_atomically`). It failed identically on
  baseline `7f809414`. The cause: that database had lost two foreign keys from the incident
  tables to `middleware_commands`, so the store's readiness check failed. A fresh database,
  as CI uses, passes.
- A gitleaks run over the working directory reported 7 hits, all in the ignored,
  untracked `.mypy_cache/`. The exact-commit scan above is the one that counts.
- Not run locally: the Docker image test stage (last verified on `04597deb`: 4004 passed in
  the image) and GitHub-hosted jobs. GitHub CI on the pushed head is authoritative.
