# PR ledger

Run `20260930T014438Z-7f809414`; code candidate `b8e75ab9a7cf11eb10ae17e85e227867679b5ca9`. Source of truth: `pr-ledger.jsonl`.

| PR | Ledger status | File level | Behaviour | Replacement | Reason |
|---|---|---|---|---|---|
| #304 | HOLD #312/#334 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #397 + #312/#334 | DB-TLS slice on #397; PAS-146/179 evidence + Postman cert test live on #312/#334 |
| #308 | DECISION NEEDED | UNKNOWN | DECISION_NEEDED | - | Evidence packet for superseded heads (PAS-179); not carried |
| #309 | CLOSED (D6) | REJECT_APPROVED | REJECTED_BY_DECISION | none (declined) | Staging reconciler declined by D6: "Drop #309/#390" |
| #311 | DECISION NEEDED | UNKNOWN | DECISION_NEEDED | - | CLAUDE.md external-protocol pointers conflict with AGENTS.md |
| #312 | KEEP (D12) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | Operator reconciliation resolve API + SQL + PAS-44 freeze deferred |
| #316 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | WhatsApp V3 family/adapter carried |
| #317 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Expat CVE row carried |
| #318 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | DJONE connector carried (D2 both owners) |
| #319 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | Certify gate textual rules reject idempotency component names and provider-prohibition wording |
| #321 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Obsolete trust transition (superseded MCR tree) |
| #322 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | command_reliability/DLQ + migration 0008 not carried |
| #323 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Obsolete trust transition (orphan tree) |
| #325 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Fail-closed webhook persistence (Batch 1) |
| #326 | DECISION NEEDED | UNKNOWN | DECISION_NEEDED | - | Superseded staging-preflight evidence packet (PAS-180); not carried |
| #327 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #389 -> #397 | Superseded by #389's role-isolation certification |
| #328 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | PAS-78 DB access-path evidence map + tests/test_db_access_path_map.py not carried |
| #329 | KEEP (D17) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | PAS-234 staging packet to be re-reviewed on the final head |
| #330 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Codestra Connect V3 authority carried |
| #331 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | tests/test_runner_label_governance.py not carried (D1 implemented differently) |
| #333 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | .codestra/MIDDLEWARE-INTEGRATION-CONTRACT.md not carried |
| #334 | KEEP (D12) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | Reconciliation list/get/readback/resolve routes deferred |
| #335 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | .codestra/deployment-contract.json + DEPLOYMENT.md not carried |
| #336 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Email JWT hardening (Batch 1) |
| #337 | HOLD #334 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #334 (+#312) | Launcher transition over an older #334 snapshot |
| #338 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #339 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #340 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #341 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #342 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #343 | HOLD #356 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #356 (+#370) | Superseded by #356 production adapter registry (D3) |
| #344 | HOLD #342 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #342 | Older copy of #342's MCR release harness + stale pin |
| #345 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #346 | HOLD #341 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #341 | MCR-L attestation superseded by #341 (MCR stack) |
| #347 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #357 -> #397 | v1 rehearsal superseded by #357 (app/platform/rehearsal.py) |
| #348 | HOLD #354 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #354 | Staging preflight superseded by #354 TEST_SYN certifier |
| #349 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #358 -> #397 | v1 callback preflight superseded by #358 (app/telnexa_callback_identity.py) |
| #350 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #353 -> #397 | 0067 release seal superseded by #353 (scripts/release_gate.py, 0071) |
| #351 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Postman safe-execution guard (Batch 8) |
| #352 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Internal DB auth order + correlation evidence (D14) |
| #353 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Release gate, re-pinned to 0071 |
| #354 | KEEP (gate legacy-port) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | Certify gate rejects any added line naming  legacy-port, including this refusal of it |
| #355 | KEEP (MCR gate) | KEEP_BLOCKED | NOT_CONTAINED_GATE_BLOCKED | - | MCR stack (see #319) |
| #356 | KEEP (D3) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | Production adapter registration waits for a release mission |
| #357 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | No-effect rehearsal |
| #358 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Telnexa mTLS callback identity |
| #359 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Release certification gate |
| #360 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Bounded canary controller |
| #361 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | GO/NO_GO decision gate |
| #362 | CLOSED (D9) | REJECT_APPROVED | REJECTED_BY_DECISION | none (declined) | 410 permanent denial declined by D9: "Keep deprecated" |
| #363 | HOLD #334 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #334 | Older snapshot of #334; reconciler part declined by D6; CLAUDE.md needs a decision |
| #366 | HOLD #356/#370 | SUPERSEDED | SUPERSEDED_BY_OPEN_PR | #397 + #356/#370 | Dispatcher recovery on #397; production registration waits for D3 (#356/#370) |
| #368 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | Section 4 evidence + TEST_SYN Postman collection/environment not carried |
| #369 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | PAS-85 tenant table/schema authority |
| #370 | KEEP (D15/D16) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | FACE-ID/identity services + connector-runtime bridge deferred |
| #371 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | command_reliability, command_dead_letter, identity missions, authz-engine tests not carried |
| #376 | KEEP (D15) | DEFER_APPROVED | DEFERRED_BY_DECISION | - | FACE-ID manifests travel with the identity-services mission |
| #377 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Authorizes #376's validator, not the lane |
| #379 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | agent_provisioning_lifecycle, integration_admin_auth, social_auth, 0012_tenant_rls.sql not carried |
| #380 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | asyncpg app/db/tenant_context.py + 3 tests not carried |
| #381 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | asyncpg app/db/tenant_context.py + 3 tests not carried |
| #382 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Only a stale pin over the PAS-89 stack |
| #383 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | PAS-80 DB-TLS authority (D8, #334 variant) |
| #384 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Authorizes #312's validator, not the lane |
| #386 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | PAS-86 inherited tenant ownership |
| #387 | KEEP (unique) | UNIQUE_REQUIRED | NOT_CONTAINED_UNIQUE | - | tests/test_process_settings_native_dsn.py not carried |
| #388 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Migration metadata coverage (Batch 2) |
| #389 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 | Runtime role-isolation certification (Batch 2) |
| #390 | CLOSED (D6) | REJECT_APPROVED | REJECTED_BY_DECISION | none (declined) | Same reconciler as #309; declined by D6 |
| #393 | HOLD #397 | CONTAINED | CONTAINED_IN_CANDIDATE | #397 pins | Authorizes #371's validator, not the lane |

## Totals

- CONTAINED_IN_CANDIDATE: 29
- DECISION_NEEDED: 3
- DEFERRED_BY_DECISION: 6
- NOT_CONTAINED_GATE_BLOCKED: 9
- NOT_CONTAINED_UNIQUE: 11
- REJECTED_BY_DECISION: 3
- SUPERSEDED_BY_OPEN_PR: 8
- total: 69
