# Behavioural audit findings (S2) and what this sweep did with them

Audits A-F ran read-only against `7f809414`. "Fixed" means a commit on the candidate with
a regression test that fails without the fix.

## Fixed on the candidate

| Id | Finding | Commit | Test |
|---|---|---|---|
| A-D1/D2 | Provisioning readback and repair-intent routes did not check the request's tenant (the table is on the deferred-RLS list) | `557513ae` | `tests/test_agent_provisioning_tenant_scope.py` |
| B1 | Klyrow email, Telnexa SMS and Odoo upsert errors raised *after* the send mapped to TRANSIENT/RETRYABLE_BEFORE_EFFECT (safe retry) | `f5952174` | `tests/test_provider_unknown_outcome.py` (+ tightened adapter tests) |
| B3 | CRM read-back ignored the bridge's configured tenant | `ad5f8fed` | `tests/test_platform_adapter_conformance.py::test_odoo_bridge_readback_tenant_binding_and_ambiguity` |
| E1 | Suspend/revoke recorded SUSPENDED/REVOKED even when the Keycloak disable failed | `b37b4a07` | `tests/test_agent_provisioning.py::test_suspend_and_revoke_do_not_claim_success_when_identity_disable_fails` |
| E2 | WebRTC revoke marked sessions REVOKED and logged a succeeded step without calling VICIdial | `95ad89d4` | `tests/test_agent_provisioning.py::test_webrtc_revoke_never_records_a_revocation_it_did_not_perform` |
| F1-F3 | Text still naming schema head 0067 (docs status lines, supply-chain doc, certification manifest, rehearsal usage, two test names) | `67052999` | text only |

Trust pins re-derived in `b8e75ab9` (`ACTIVE_STALE_AFTER=0`).

## Recorded, not changed (needs an owner or security decision)

| Id | Finding | Why not changed here |
|---|---|---|
| B1 (Temporal lane) | The Temporal activity still raises the new unknown-outcome errors as a retryable `ProviderAdapterError`; only Postly and VICIdial are non-retryable | Changes the workflow retry policy; the providers key on `command_id`, so dedupe may hold. Needs a decision. |
| B2 | CRM single-record read-back checks the id, not the updated fields | Needs Odoo's field shapes to compare safely; a naive compare would misreport. |
| B4 | `crm.task.update` reads back UNSUPPORTED and dead-letters; the note parent is unchecked | Needs a bridge read surface. |
| B5 | CRM create after an unknown outcome cannot be reconciled (it dead-letters, it does not falsely succeed) | Needs a bridge lookup by idempotency key. |
| E3 / C5 | Idempotency-Key missing on 12 write routes; CRM writes fall back to the correlation id | Owner decision on whether to enforce. |
| E4 | WebRTC tickets are never expired locally | Depends on whether VICIdial expires the credential itself; unverified. |
| E6 | Provisioning read-back reflects local rows only | Needs provider read endpoints. |
| C1 | PAS-44 route-authority decision lives only in `346102d7`/`bac8f883` (#313/#312) | Port or re-approve: owner decision. |
| C2 | Two route-authority overrides (#359/#361) have no recorded approval | Owner decision. |
| C3/C4 | WebRTC ticket/revoke and other VICIdial/Keycloak routes are classified as non-effectful; the edge contract disagrees with the handler on scope, idempotency and correlation | Correct classification makes DIRECT_EFFECT_BYPASSES non-zero; governance decision. |
| C8 | OpenAPI/Postman are generated from the CONTROL_PLANE profile, not INTEGRATION on 8095 | Owner decision on the authoritative profile. |
| F3 | `scripts/reconcile_legacy_staging_database.py` has `TARGET_HEAD=0068`, pinned by its test | Confirm intent. |
| A (hardening) | No RLS rebind after a mid-request commit; no startup check for a restricted role; 75 review_required tables; no restricted-role tests; IdempotencyRecord has no tenant key | Hardening backlog; the 50-table RLS deferral is deliberate (migrations 0071, 0015, automation/0003). |
| D | MCR stack and #354 absent; 53 added lines trip the certify gate, none a real violation | Renames proposed (component key -> `Idempotency-Key`, `$defs` -> `mcr_idempotency_key`, legacy-port names); the #354 allow-list is a behaviour change. Owner decision. |
| D | FACE-ID (#376/#370) and PAS-53 production adapters absent | Deferred by D15/D16 and D3. |
