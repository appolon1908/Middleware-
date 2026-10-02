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

## Codestra continuation contract

The repository-local rules above are the immediate authority. The external Codestra continuation protocol remains an additional coordination contract where accessible.

Canonical protocol:
https://github.com/ingtrader21-spec/codestra/blob/main/docs/AGENT-CONTINUATION-PROTOCOL.md

Quick start:
https://github.com/ingtrader21-spec/codestra/blob/main/docs/AGENT-QUICKSTART.md

Before changing code:
1. Read `.codestra-mission/*` when present.
2. Read the active Linear issue and linked Notion architecture when available.
3. Inspect exact Git branch/HEAD/dirty/worktree/upstream/PR/CI state.
4. Preserve all existing local work.
5. If acting as Builder, verify exclusive issue ownership and use the declared active worktree.
6. Do not invent or self-assign the next task.
7. Update GitHub + Linear + Notion + the mission checkpoint before handoff when those systems are part of the mission.
8. Do not cross the live-production approval boundary.

The no-loss, one-writer, protected-merge, checkpoint, and production-boundary rules remain mandatory even when the external protocol repository is unavailable.

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
