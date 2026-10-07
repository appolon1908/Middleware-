# mw-03 connector runtime implementation

## Intent and design

Middleware remains the privileged integration boundary. This work is confined to
connector SDK/runtime, connector contracts, tests and documentation in the section
worktree. No real adapter is registered, no provider is activated, and production
flags remain false. The section owner implements and reviews changes inline.

The existing manifest catalog and PostgreSQL management storage are retained.
Registry mutations must validate the complete ownership namespace atomically.
Adapter results must be typed, JSON-safe, redacted, and lifecycle-consistent.
Runtime command execution gains a pluggable operation journal: claim before submit,
cache terminal results, reconcile uncertain submissions, never blindly retry them.
Only explicit retryable failures may retry within the manifest budget, with the
same request identity. PostgreSQL supplies a durable journal with tenant RLS.
The canonical platform API aliases existing connector controls and adds capability
and status discovery. Installation ownership validation occurs inside its database
transaction to prevent concurrent namespace collisions. Activation remains excluded.

## Execution sequence

1. Add failing registry/result/runtime regression tests; enforce namespace ownership,
   upgrade binding invalidation, normalized result/error and health contracts.
2. Add failing operation journal tests; implement semantic replay, safe retries,
   unknown reconciliation and bounded observability. Implement PostgreSQL journal
   and migration with RLS and restart/concurrency integration tests.
3. Add failing management contract tests; provide canonical platform routes,
   capability/status discovery, transactional install ownership checks and safe
   disabled upgrade handling with optimistic locking and audit/outbox persistence.
4. Validate formatting, lint, types, SDK/unit/integration/contract tests, migration
   replay, security and secret scanning. Record exact limitations. Commit atomic
   validated changes on section/mw-03-connectors and attempt a normal section push.

## Review focus

- Prefix and route conflicts through direct registration and concurrent installs.
- Malformed adapter output and sensitive configuration/result fields.
- Same key with changed semantic content or caller; cross-tenant replay.
- Crash after submit, reconciliation timeout, and repeated unknown outcomes.
- Disabled control flags, stale ETags, unbound adapters and platform route auth.
