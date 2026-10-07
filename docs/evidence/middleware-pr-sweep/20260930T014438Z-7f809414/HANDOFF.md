# Handoff - run 20260930T014438Z-7f809414

| Field | Value |
|---|---|
| Host | Claude Code cloud container (ephemeral; anything unpushed is lost when it is reclaimed) |
| Worktree | session scratchpad `replay` (the single write lane) |
| Branch | `mission/middleware-v3-final-repository-convergence-20260927` (PR #397) |
| Code candidate | `b8e75ab9a7cf11eb10ae17e85e227867679b5ca9` |
| Evidence commit | the commit that adds this directory (child of the code candidate) |
| Upstream / remote before publication | `7f809414677c19eacb7d3c52caa9d412077b6c8d` |
| Dirty state | clean (ignored caches only) |
| Tests and validators | see `test-matrix.md` |
| Preservation | `preserve/pr-sweep-20260930-replay-wt` (`99f34e9e`), `preserve/pr-sweep-20260930-local-refs` (`ff22191d`) |

## Publication

Published by a normal fast-forward push after `agent_preflight.sh --certify` passed and the
remote head was confirmed equal to the upstream (`7f809414`) and to be an ancestor of the
local head. The AGENTS.md one-liner requires local == upstream, which no push of new commits
can meet. The ancestry form of the compare-and-swap was used, as in earlier publications on
this lane.

## Next command

On the owning lane, once artifact storage is freed and the hosted-runner billing is fixed:

```bash
./scripts/agent_preflight.sh --start --branch "$(git branch --show-current)"
```

Then re-run the quota-blocked jobs on #397's head once, and merge only when
`codestra/required-ci` and the other required checks are green, the approvals on the new head
are valid, and every review thread is resolved. After the merge, close the 29 PRs marked
`HOLD #397` in `pr-ledger.md`, each with a preservation comment and its branch kept.
