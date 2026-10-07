# Codestra Agent Rules

1. Work only in the assigned subsection branch/worktree.
2. Follow: subsection -> section -> development -> testing -> staging -> production.
3. Fetch first; require a clean tree; record base SHA; check ahead/behind.
4. Never overwrite unknown local work or bypass conflicts.
5. Every atomic task needs code, tests, docs/contracts/migrations when applicable.
6. Run applicable lint, type, unit, integration, contract, migration, security, secret-scan, and diff checks before push.
7. Push every validated checkpoint and verify remote SHA.
8. No direct work on main/development/testing/staging/production and no force push to protected branches.
9. Keep PRODUCTION_GO=NO, LIVE_CAPABILITIES_ENABLED=NO, EXTERNAL_EFFECTS=false unless an approved activation mission explicitly changes them.
10. COMPLETE requires clean tree, pushed branch, tests, docs, zero unresolved blockers, and local/remote SHA match.
11. CERTIFIED additionally requires exact-SHA CI, independent review, regression/security gates, and stored evidence.
12. If safety or correctness cannot be proven, mark BLOCKED and stop.

## Canonical platform authorities

- Public traffic remains Client -> Caddy -> Kong -> Middleware on the canonical Middleware service port.
- Preserve authentication and tenant authority on every governed request; the canonical tenant header is `X-Tenant-ID` where a header form is required.
- Preserve `X-Correlation-ID` end to end for request tracing and evidence linkage.
- Preserve `X-Causation-ID` where command/event causality is carried across boundaries.
- Require `Idempotency-Key` for effect-capable commands and do not introduce alternate spellings.
- `/metrics` and `/internal/*` remain private.
- Do not introduce direct provider, direct SMTP, direct Odoo database, or other paths that bypass Middleware ledger/outbox authority.
- Production and provider effects remain fail-closed unless a separately approved activation mission authorizes them.
