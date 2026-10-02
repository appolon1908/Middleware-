# Repository Agent Instructions

This `AGENTS.md` applies to the entire repository unless a deeper `AGENTS.md` provides more specific instructions for a subdirectory.

## Repository identity and authority
- This GitHub checkout is currently hosted at `appolon1908/Middleware-`.
- Do not infer release authority from the hosting owner alone. The protected release/governance identity must match the repository-owned authority files and workflows (including `config/repository-authorities.v1.json`) until an explicit governance migration changes them.
- Resolve every cross-repository dependency against its recorded principal owner. Do not bulk-rewrite GitHub URLs, package sources, submodules, CI references, image names, or release identities just because this repository was transferred or mirrored.

## Working rules
- Read this file, the active `.codestra/` governance, relevant mission files, and the current Git/PR/CI state before changing code.
- Preserve repository history, architecture, public APIs, and compatibility unless the assigned task explicitly requires a change.
- Use one active task branch/worktree per implementation lane.
- Do not develop directly on a protected default branch.
- Never force-push, rewrite shared history, or discard another contributor's work.
- Before publishing, verify the exact repository, branch, upstream, remote SHA, and clean working-tree state; fetch the exact target branch first.
- Do not create duplicate command engines, connector runtimes, adapters, schemas, or sources of truth when a canonical implementation already exists.

## Middleware V3 boundaries
- Preserve the canonical request path: Caddy -> Kong -> Middleware V3 on `:8095`.
- Keep authentication, tenant binding, policy/safety checks, idempotency, command ledger, durable outbox, worker execution, adapter boundaries, readback, and reconciliation fail-closed.
- Do not expose private `/metrics`, `/internal`, database, Redis, provider-admin, or alternate runtime ports as public product APIs.
- Effectful provider actions remain default-deny unless the assigned task explicitly authorizes the effect and all required gates are satisfied.

## Implementation and verification
- Implement requested behavior completely and add or update tests for changed behavior.
- Run the relevant unit, integration, lint, type-check, build, API/OpenAPI, Postman, security, and exact-SHA checks that exist in this repository.
- Do not disable, delete, bypass, or weaken CI/security checks merely to make a pipeline green; fix the underlying issue.
- Keep code, migrations, schemas, generated contracts, OpenAPI definitions, Postman artifacts, and tests synchronized when a change affects them.
- A documentation or governance change that alters the tracked source closure must regenerate the repository-owned trust pins before merge.

## Security and production effects
- Never commit passwords, tokens, API keys, private keys, certificates, production credentials, or other secrets.
- Default-deny live payments, payouts, calls, SMS, email, WhatsApp, social publishing, provider mutations, and production deployment unless the task explicitly authorizes them.
- Do not bypass authentication, authorization, tenant isolation, idempotency, audit, reconciliation, or production approval gates.
- Keep production and staging credentials outside source code and test fixtures.

## Handoff requirements
- Report the branch, exact HEAD SHA, files changed, tests/checks run, results, and remaining blockers.
- Distinguish code-ready, merge-ready, staging-ready, and production-ready states.
- Do not claim certification, deployment, or production readiness without exact evidence.
