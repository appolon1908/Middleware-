# mw-02 canonical command kernel implementation plan

Authority: section/mw-02-command-kernel only. Preserve existing command ledger,
outbox, policy and safety authorities. PRODUCTION_GO=NO,
LIVE_CAPABILITIES_ENABLED=NO, EXTERNAL_EFFECTS=false. No live provider access.

1. Audit submission, canonical serialization, transaction boundaries and immutable
   audit; exercise existing idempotency, cancellation and replay certification.
2. Add failing regression cases for quarantine dispatch, crash recovery from each
   active state, completion without matched evidence and ambiguous transient results.
3. Repair these behaviors in existing stores, execution bus and reconciler.
   Recovery must read before writes; ambiguous outcomes stay quarantined.
4. Fence reconciliation finalization by command resource version and validate
   unexpired outbox ownership. Preserve recoverability across separate commits:
   queued ledger plus quarantined outbox must resolve to retry on the next claim.
5. Verify unit/contract/security suites and isolated PostgreSQL integration;
   run formatting, lint, typing and secret scanning, recording pre-existing failures.
6. Document behavioral changes and schema impact; commit validated atomic work
   on the section branch, attempt ordinary push, and leave the worktree clean.

No replacement tables or schema changes are necessary for these repairs: existing
resource_version, attempt_number and lease timestamps carry the fences.
