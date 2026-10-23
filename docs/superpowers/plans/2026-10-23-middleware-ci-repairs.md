# Middleware scoped CI repair implementation plan

> For agentic workers: execute natively in this authorized session. Preserve the existing owners' checkouts.

**Goal:** Apply the owner's session-specific lease exception and publish verified CI repairs for PRs #448, #453 and #457.
**Architecture:** One isolated active repair lane at a time, based on each exact PR head. Fast-forward only, with remote-parent comparison before publication.
**Tech stack:** Python 3.12, Ruff 0.12.10, mypy 1.17.1, pytest 9.1.1, GitHub Actions.
**Spec:** Owner's October 23 instruction and scoped-exception selection.

## Constraints
- No controller record fabrication, required-check weakening, trust approval substitution or production effects.
- Preserve independent review and normal promotion eligibility.
- Do not overwrite or clean existing owner work.

## Tasks
- [x] PR457: Reproduced 21 lint errors; corrected imports/statements and four method-override type errors. Lint/types and 123 affected tests pass; broader verification is recorded in the repair PR.
- [x] PR448: Reproduced delayed-collection 401/403 mismatch; normal and delayed security matrix each pass 27 cases. Resolved inherited typing errors and added five fail-closed regression cases; 285 affected tests pass.
- [x] Record the scoped exception in AGENTS.md on repaired PR branches.
- [ ] Verify exact remote parent, publish non-force updates, read back the new head and inspect fresh CI.
- [ ] Report promotion/trust decisions separately where they remain external prerequisites.

## Review focus
Keep every existing test assertion and migration SQL literal unchanged for lint repairs. Token expiry must remain 401 and over-policy lifetime 403. No cross-task edits or controller/production mutations.


## Execution rulings
- Ruling: publish #457 through a subsection repair PR because its section head is protected; integration still requires its normal checks and review.
- Ruling: use the local workspace for validation after the connected host stalled under disk/swap pressure. Preserve the host checkouts as evidence.
- Ruling: fix the newly exposed mypy errors without suppressions. Use pytest method overrides for #457; annotate non-returning guards and preserve fail-closed handling in #448.
- Ruling: regenerate only the exact-source closure with the repository derivation tool after edits. Independent launcher trust approval and promotion certification remain external prerequisites.
- Ruling: omit inherited shell proxy variables from offline pytest runs after reproducing their missing-SOCKS-dependency failures. Keep repository dependencies unchanged. Database/Docker integration remains a GitHub CI requirement.
