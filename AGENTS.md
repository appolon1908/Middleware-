# Codestra Agent Governance Standard

<!-- CODESTRA_AGENT_GOVERNANCE_V1 -->

This root contract is authoritative for agent-driven development. Repository-specific rules may add constraints but may not weaken these rules.

1. Every task is Product -> Section -> Subsection -> Atomic Task. Implement only in the assigned subsection worktree. Promotion is only: subsection/* -> section/* -> development -> testing -> staging -> production.
2. One implementation owner per subsection. Review/test agents may inspect but do not modify the lane unless reassigned. Claim a lease before editing.
3. Pre-work: fetch/prune, verify branch/parent, record SHA, require clean tree, know ahead/behind, safely sync parent, run preflight, require dependencies CERTIFIED, validate environment, keep production effects off. Unknown local work or divergence means BLOCKED.
4. Atomic tasks include implementation, tests, error handling, contract/docs/migration/observability/security changes when applicable. Do not mix unrelated changes.
5. No placeholders, TODO-as-implementation, dead/duplicate code, bypasses, broad exception swallowing, hard-coded credentials, production secrets, unexplained suppressions, disabled tests/security checks or temporary production flags.
6. Before push run applicable format/lint/type/unit/integration/contract/migration/security/secret checks and git diff --check. Do not knowingly push broken code.
7. After each valid atomic checkpoint commit, push, verify remote SHA and record evidence. Do not leave completed work only on a workstation.
8. Commits are one understandable unit with descriptive messages such as feat(area), fix(area), test(area), refactor(area).
9. Before merge fetch parent, integrate current parent safely, resolve intentionally, rerun gates, push exact branch and require CI on the new SHA. Old CI never certifies a changed SHA.
10. Do not directly develop on main, development, testing, staging or production. Force pushes to protected branches are prohibited.
11. Defaults: PRODUCTION_GO=NO, LIVE_CAPABILITIES_ENABLED=NO, EXTERNAL_EFFECTS=false. Implementation must not silently enable external/production effects.
12. COMPLETE requires final branch/SHA, parent SHA, changed files, tests, security, migration/API evidence where applicable, dependency changes, limitations/TODOs, CI and reviewer state.
13. COMPLETE requires implementation, applicable tests/contracts/security/docs/migrations, pushed clean tree, local/remote SHA match and no blockers.
14. CERTIFIED additionally requires exact-SHA CI, independent review, parent integration/regression/security/performance evidence as applicable, no unresolved critical/high defects, and stored completion evidence. Only CERTIFIED promotes.
15. After integration archive evidence and safely clean stale worktrees/locks/refs; never delete unmerged work.
16. Fail closed. If safety cannot be proven, stop and mark BLOCKED. Never guess around secrets, migrations, authorization, ancestry, production effects, destructive operations or incomplete CI evidence.


## Repository-specific additions
# Middleware Single-Lane Agent Contract

This file is mandatory authority for every human, Claude, Codex, Copilot, CI, or other automation acting on this repository.

## Entry gate

Before reading mission-specific instructions or editing a tracked file, run:

```bash
./scripts/agent_preflight.sh --start --branch "$(git branch --show-current)"
```

A failing preflight is a STOP condition. Do not work around it with another worktree, host, port, route, provider, database path, or Git write path.

## Single writer

- Exactly one worktree/branch may be declared as the active write lane for a mission.
- Every other checkout is preserved read-only reconciliation/evidence until its unique semantics are proven reachable or deliberately migrated.
- Never start a second agent on the same repository/worktree while a writer is active.
- Never reset, clean, stash, discard, rebase, rewrite, force-push, delete branches/worktrees, or overwrite unproven history.
- Never develop directly on `main`, a detached HEAD, a stale branch, or a branch whose upstream differs from its remote head.

## Canonical platform boundaries

- Public request path: Client -> Caddy -> Kong -> Middleware `:8095`.
- Do not introduce alternative canonical Middleware ports such as `:8080` or `:8096`.
- Preserve the repository's approved route authorities; do not create parallel aliases or direct-provider shortcuts.
- `/metrics` and `/internal/*` stay private and must not be exposed as public application routes.
- Preserve the canonical request metadata contracts: authentication/tenant context, `X-Correlation-ID`, `X-Causation-ID` where applicable, and `Idempotency-Key` on effect-capable commands.
- Do not introduce direct provider writes, direct Odoo database authority, direct SMTP delivery, or bypass the durable command/ledger/outbox authority.

## Production safety

Production/provider effects remain fail-closed unless a separately approved release mission explicitly authorizes them. An agent must not enable live email/SMS/calling, payment/money movement, direct production database writes, or provider mutations as a side effect of implementation or testing.

## Certification and publication

Before publication:

```bash
./scripts/agent_preflight.sh --certify --branch "$(git branch --show-current)"
```

Then immediately before a normal non-force push, perform a remote-head compare-and-swap:

```bash
branch="$(git branch --show-current)"; local_head="$(git rev-parse HEAD)"; upstream_head="$(git rev-parse "@{u}")"; remote_head="$(git ls-remote origin "refs/heads/$branch" | awk '{print $1}')"; test "$local_head" = "$upstream_head" && test "$remote_head" = "$upstream_head"
```

If that comparison fails, STOP. Fetch and reconcile on the owning workstation; never force-push or publish through a different write path.

The mission handoff must record host, worktree, branch, local HEAD, upstream SHA, remote SHA, dirty state, tests/validators run, and the exact next command.
