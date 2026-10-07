# Middleware runtime source authority

## Canonical rule

The only forward-looking source authority is protected `main` in
`appolon1908-hue/Middleware-`. A static SHA stored in repository metadata is not
an authority because it becomes stale as soon as another protected merge lands.
Every release or certification workflow must instead resolve the exact GitHub
event SHA for protected `main` and bind that immutable source to the generated
release manifest, source tree, image digest, provenance, SBOM, vulnerability
evidence, migration head, and runtime profile.

`config/runtime-source-state.json` is the machine-readable authority declaration.
It deliberately does not claim that any runtime is deployed or certified.

## Runtime truth

Repository source and runtime state are separate facts. Runtime truth exists only
when `.github/workflows/production-runtime-certification.yml`, under issue #118,
produces and verifies evidence for the exact signed candidate. That evidence must
include effective source/digest/schema/profile read-back, fail-closed capability
read-back, backup plus isolated restore, rollback rehearsal, data integrity, and
zero external-effect movement.

The signed release path is:

1. `.github/workflows/release.yml` — build, scan, sign, and bind the release
   manifest to exact protected source;
2. `.github/workflows/automated-production-promotion.yml` — automated admission
   of that exact signed candidate without enabling business effects;
3. `.github/workflows/production-runtime-certification.yml` — restricted,
   internal-only production read-only canary and immutable runtime evidence.

The manifest schema is `contracts/release-manifest.v1.schema.json`; locked runtime
profiles are registered in `config/runtime-profiles.v1.json`.

## Historical evidence

`MIDDLEWARE-AUTHORITY-RECONCILIATION.yaml` and
`docs/SERVER-RUNTIME-RECONCILIATION-MAP.md` remain useful historical snapshots of
the source and server reconciliation that led to the current model. Their pinned
branches, SHAs, container counts, or comparison classifications must never be
used as current deployment input or runtime proof.

## Fail-closed boundary

This authority declaration does not approve a deployment, public route, provider
binding, credential, production write, Odoo/n8n effect, email, SMS, social
publication, crawler effect, external-model call, PSTN call, payment, or trading
action. All such capabilities remain disabled unless a separate, exact-candidate
activation release satisfies its own protected approvals and evidence gates.
