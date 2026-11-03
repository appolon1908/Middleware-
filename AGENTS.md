# Codestra Agent Governance Standard

<!-- CODESTRA_AGENT_GOVERNANCE_V1 -->

This root contract is authoritative for agent-driven development. Repository-specific rules may add constraints but may not weaken these rules.

1. Every task has an assigned product and bounded atomic scope. Implement only in its assigned worktree/branch. Source changes use protected GitHub pull requests to the approved target; production promotion requires a separate release mission and environment approval.
2. One implementation owner per subsection. Review/test agents may inspect but do not modify the lane unless reassigned. Claim a lease before editing.
3. Pre-work: fetch, verify branch/parent, record SHA, require a clean assigned worktree, know ahead/behind, safely sync parent, run preflight, validate applicable source dependencies and test isolation, and keep production effects off. Unknown local work or divergence means BLOCKED. Follow the explicit first-publication procedure below for a newly assigned branch.
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
14. Source acceptance requires exact-SHA native CI, verified commit signatures, fresh independent review, parent integration/regression/security evidence as applicable, no unresolved critical/high defects, and a protected GitHub merge. Production certification and deployment additionally require their separate release evidence and environment approval.
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

A failing preflight is a STOP condition except for the explicitly assigned unpublished-branch procedure below. Do not work around an existing branch's stale remote, divergence, dirty state, or safety failure with another worktree, host, port, route, provider, database path, or Git write path.

For an explicitly assigned new branch, `missing_upstream` is expected until its first publication. Record that result; verify a clean dedicated worktree at the current approved parent SHA and that the assigned remote branch does not exist. Prepare and independently review the bounded change before publication. Do not fabricate an upstream or a passing preflight. At first publication, compare the parent again, create only the assigned remote branch at that verified parent, and publish the reviewed tree as a verified signed commit with an expected-head check. Fetch the result, verify its entire tree and signature, establish the same-name upstream, and rerun preflight. Any other preflight failure remains a STOP condition.

## Native GitHub source authority

GitHub is the source of truth for source CI, verified signatures, fresh independent review, and protected merges. `middleware-ci.yml` runs the product, integration, image and security gates for every PR target. `required-ci.yml` runs the exact-head product suite and publishes its real result as `codestra/required-ci`; preserve both its tests and publisher. A Codestra name does not make a real native CI check obsolete.

Source merges do not depend on installed `codestra-github-check` programs, controller databases, historical branch-route exceptions, or polling an already published CI result. The retired hierarchy workflow provided those dependencies and no product tests. `governance/promotion_guard.py` and its policy remain historical tooling; they do not authorize or block source acceptance. Required-check settings must use the real native jobs with GitHub Actions provenance and retain all product/security gates.

Source acceptance does not authorize deployment or certify a runtime. Preserve the staging/production environments, independent release approval, immutable artifact checks, production safety defaults, and existing release workflows.

## Single writer per file scope

- Exactly one writer may hold a file scope. A writer is an `ACTIVE` lane in `governance/file-ownership.v1.json`; lane scopes are disjoint and at most `max_concurrent_writers` lanes are `ACTIVE` at once.
- Each writer uses its own worktree and its own `fix/*` branch cut from current `main`, and changes only files its lane owns plus shared files it currently holds by a recorded handoff. `scripts/validate_lane_file_scope.py` must pass before push.
- Merges to `main` stay one at a time: before review each PR merges current `main` into its branch (normal push, no rebase) and re-derives the release pin.
- Every other checkout is preserved read-only reconciliation/evidence until its unique semantics are proven reachable or deliberately migrated.
- Never start a second agent on the same worktree or on a file scope held by another writer.
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

For an existing published branch, immediately before a normal non-force push, verify that the remote still equals the recorded upstream and is an ancestor of the reviewed local head:

```bash
branch="$(git branch --show-current)"
local_head="$(git rev-parse HEAD)"
upstream_head="$(git rev-parse '@{u}')"
remote_head="$(git ls-remote origin "refs/heads/$branch" | awk '{print $1}')"
test "$remote_head" = "$upstream_head"
git merge-base --is-ancestor "$upstream_head" "$local_head"
```

If that comparison fails, STOP. Fetch and reconcile on the owning workstation; never force-push. Ordinary pushes must contain verified signed commits. GitHub's authenticated signed-commit API may publish the exact independently reviewed tree using `expectedHeadOid`; verify the returned tree and signature before adopting it locally. A read-only remote comparison alone is not an atomic compare-and-swap; the push must remain fast-forward, or the API must enforce the expected head.

The mission handoff must record host, worktree, branch, local HEAD, upstream SHA, remote SHA, dirty state, tests/validators run, and the exact next command.

## Continuation protocol

Cross-repository continuation rules live in the canonical Codestra protocol:
https://github.com/appolon1908/codestra/blob/main/docs/AGENT-CONTINUATION-PROTOCOL.md

Read `.codestra-mission/*` when present, the active Linear issue and its linked Notion architecture before changing code, and end with the protocol's structured checkpoint. Where the protocol and this file differ, this file's single-lane, publication and production-safety rules govern this repository.
