# Single-Lane Development Handoff

## Authority

Every new Middleware agent starts with `AGENTS.md` and `scripts/agent_preflight.sh`. A mission may have only one write-enabled worktree. All other worktrees remain preserved reconciliation/evidence until their unique semantics are proven superseded.

## Current publication lane

- Repository: `ingtrader21-spec/Middleware-`
- Current PR: #397 (Middleware V3 final repository convergence)
- Branch: `mission/middleware-v3-final-repository-convergence-20260927`
- Remote head when this handoff was written: `d7e7ed60ee39bb8dc2124b601f4187e582cc6e17`
- Write lane: the owning workstation's checkout of that branch (`./scripts/agent_preflight.sh --start` on that host records the worktree); every other checkout is read-only reconciliation/evidence
- Production/provider effects: fail-closed

## Absorbed lanes

PR #355 (`mission/mcr-c-completion-safe-20260924`, certified head `d03f7099e05b7bb7888ca8631cdbc10401cc72bb`) and the preserved `mcr-c-final-convergence` worktree (`7d713735e46a0d0efc437808ad34f42ecdc41c6c`) are absorbed into #397: the MCR runtime shell is the single journey/next-action authority, with the MCR migrations renumbered 0072–0074 behind `0071_defer_unbound_tenant_rls`. Their worktrees stay preserved read-only; do not restart a writer on them.

## One-line continuation

Ubuntu inspection (from the convergence checkout):

```bash
./scripts/agent_preflight.sh --start --branch mission/middleware-v3-final-repository-convergence-20260927
```

Pre-publication certificate:

```bash
./scripts/agent_preflight.sh --certify --branch mission/middleware-v3-final-repository-convergence-20260927
```

Appolon PowerShell review path (read/check before publication; never overwrite a newer remote):

```powershell
cd C:\Users\agent\Documents\GitHub\Middleware-; git fetch origin; git status --short --branch; git ls-remote origin refs/heads/mission/middleware-v3-final-repository-convergence-20260927
```

## Publication rule

Immediately before every normal non-force push, compare local HEAD, upstream HEAD, and `git ls-remote` for the exact intended branch. Any mismatch is a STOP condition.

## Historical lanes

Do not delete, reset, clean, or repurpose old worktrees merely because a newer lane exists. Reconcile unique commits/patches first; then mark the checkout read-only/preserved. Worktree removal is a separate maintenance action only after provenance is recorded.
