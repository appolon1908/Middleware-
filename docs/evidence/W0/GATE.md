# Gate W0 — Contract decision and repository-governance baseline

- **Status:** LIVE_GOVERNANCE_APPLIED — TAG_PENDING
- **Original baseline commit:** `382683958feefce73458ee56a1589092bad632b3`
- **Verified governance source head:** `8e4e1e2e7ed38fffbd71f1b50d45b26ce325bc0e`
- **Live verification workflow:** `33781342722`
- **Active authority ruleset:** `middleware-main-production-authority` (`22120968`)
- **Decision:** Middleware adopts automation v2 (ADR-0001)
- **Live writes changed:** no
- **Deployment changed:** no

## Evidence

- The target repository policy remains encoded in
  `config/repository-governance.v1.json`.
- Workflow run `33781342722` completed successfully on the exact default-branch
  event SHA. Source identity, encoded-policy validation, live settings apply,
  independent ruleset verification, and non-secret evidence publication all
  passed.
- The live `middleware-main-production-authority` ruleset is active on the
  default branch with no bypass actors.
- The live ruleset blocks deletion and non-fast-forward updates, requires linear
  history, requires one approving review, dismisses stale reviews after a push,
  requires review-thread resolution, and permits squash merges only.
- All eleven encoded Middleware status checks are required with strict
  up-to-date-branch enforcement.
- Repository profile and merge settings match the encoded policy: `main` is the
  default branch, the documented description and topics are present, the wiki
  is disabled, web commit sign-off is required, squash merge and auto-merge are
  enabled, merge commits and rebase merges are disabled, merged branches are
  deleted, and branch updates are allowed.
- The conformance harness, route authority, compatibility policy, skip
  ownership, and fail-closed release defaults remain unchanged.
- No runtime, provider, credential, server, deployment, or external-effect
  capability was activated by applying repository governance.

## Exit conditions

- [x] Option A recorded
- [x] Conformance harness present
- [x] Route authority and compatibility policy recorded
- [x] Skipped-test ownership documented
- [x] PR exact-head and merge-result checks green
- [x] GitHub settings applied and live audit green
- [ ] Tag `w0-complete`

The remaining tag must be created only after this evidence refresh reaches
protected `main`. Creating that repository tag does not authorize runtime
promotion or any live write.
