# mw-02 command kernel validation — 2026-10-07

Branch: `section/mw-02-command-kernel`. Continued handoff commit `62092c4f`
without resetting or replacing its implementation.

## Final repairs

- Bound execution-time adapter readiness to the adapter timeout. Exceptions and
  timeouts use the existing known-safe retry budget before any execution attempt.
- Apply exponential backoff to worker-certified safe retries in the durable
  outbox. At exhaustion, atomically record a dead-letter audit and resolve the
  quarantine as dead-lettered. The live lease owner must still match; manual,
  foreign and expired workers cannot bypass retry or ownership fences.
- Update legacy command tests for worker-driven resource-version increments and
  required completion evidence. Assert stale versions and missing proof fail.
- Normalize formatting in the carried-forward API/security tests; their ASTs
  are unchanged. No schema migration or replacement authority was added.

## Executed checks

Python 3.12; dependencies installed from `requirements-test.txt` and
`requirements-quality.txt` with hash verification in an ignored local `.venv`.

| Check | Result |
| --- | --- |
| All `test_platform_*.py`, commands, retry boundaries, Temporal transport/wiring, Odoo command acceptance | 332 passed |
| Worker, storage contracts, operations, operation contract parity/domain validation, reconciliation activity/identity, canonical contracts, outbox process modes | 73 passed |
| `integration/test_platform_kernel_postgres.py` and `integration/test_outbox_dispatch_lease.py` | 19 passed |
| PostgreSQL tests in `integration/test_postgres_redis.py` (`-k 'not redis_replay'`) | 21 passed; unrelated Redis-only test deselected |
| `integration/test_temporal_workflows.py`, `TEMPORAL_INTEGRATION_TESTS=1` | 1 passed with disposable time-skipping server |
| Ruff 0.12.10 lint and format, all 17 touched Python files including the handoff | Passed |
| Mypy 1.17.1, `--no-incremental --platform linux app/commands.py app/storage.py app/platform app/temporal_workflows.py` | Passed, 21 source files |
| AST parsing, formatting-only AST comparison, added-line secret-pattern scan, `git diff --check` | Passed |

Total: **446 passed** across the listed suites. Initial integration skips were
subsequently run with their required flags. This is section certification, not a
claim that the entire repository or unrelated service integrations were run.

PostgreSQL 16.2 ran in a newly initialized, disposable cluster under
`.venv/kernel-pgdata`, bound to `127.0.0.1:55432`, database
`middleware_test_mw02`. Existing PostgreSQL binaries were reused read-only;
no other workstation database was touched. The runtime integration guard used
`RUNTIME_INTEGRATION_ALLOW_DISPOSABLE=YES`. The selected tests do not use Redis.
Runtime SQL migrations 1–11 were applied by the PostgreSQL fixtures.

Native Windows mypy reports four Unix `os.geteuid/getegid` attribute errors in
existing configuration code. Linux deployment-target checking passes. WSL is
unavailable under LocalSystem; native Python/PostgreSQL/Temporal testing removed
that environment dependency.

`PRODUCTION_GO=NO`, `LIVE_CAPABILITIES_ENABLED=NO`, `EXTERNAL_EFFECTS=false`
were set for every test invocation. Provider behavior was synthetic. No live
provider, production infrastructure, deployment, or remote publication was used.

Remaining external blockers for the checks listed above: **none**.
