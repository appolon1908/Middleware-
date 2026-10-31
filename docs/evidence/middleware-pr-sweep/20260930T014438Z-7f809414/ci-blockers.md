# CI blockers on #397

Observed on head `7f809414` (the last pushed head). Every code check passes there; the
remaining failures are account-level, not code.

## 1. Artifact storage quota (blocks the required check)

`actions/upload-artifact` fails with `Artifact storage quota has been hit`:

| Check | What passed before the upload step |
|---|---|
| Test exact repository SHA -> `codestra/required-ci` (**required**) | steps 1-15 |
| container-security | grype: no high-severity fixable findings |
| production-integration-lock | 13 tests OK, `decision=NO_GO`, `calls_placed=0` |
| production-reviewer-access-policy | `PRODUCTION_REVIEWER_ACCESS=PASS` |
| validate (middleware-ci aggregate) | fails only because container-security failed |

The owner's cleanup removed about 17 MB of `.dockerbuild` records. About 472 MB of closed-PR
SBOMs and about 58 MB of `required-ci-*` evidence remain (see the cleanup proposal in the
#397 thread). Nothing is rerun until storage is shown to be free. Evidence uploads stay
required; none were disabled or deleted by this sweep.

## 2. Hosted runner never assigned (billing)

`runner_id 0`, no steps, about 3 s: identity-webhook-contract, observability-alert-contract,
repository-authority-contract, validate-beyvra-email, orchestrator-contract,
trusted-orchestrator-evidence. This pattern points to exhausted minutes or a $0 spending limit.
It needs an owner billing decision.

## 3. Not blockers

Pre-existing validator findings (validate_write_boundary: 1, hardening: 189) fail identically
on `main` and are not part of the required set.
