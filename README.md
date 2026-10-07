# Codestra Middleware

`appolon1908-hue/Middleware-` is the principal Git source for the Codestra cross-system integration control plane. It owns Middleware application/runtime source, durable integration state, trusted adapters, read-back/reconciliation and the contracts that govern cross-system writes.

> **Security:** keep this repository secret-free and environment-neutral. Never commit credentials, certificates, customer data, private connection strings or secret-bearing runtime evidence.

## Permanent repository-authority rule

**If a Codestra component has its own GitHub repository, that repository is the principal source for that component.** Middleware may integrate with it; Middleware must not become a second application/runtime source for it.

The rule is machine-readable in [`config/repository-authorities.v1.json`](config/repository-authorities.v1.json) and enforced by [`scripts/validate_repository_authorities.py`](scripts/validate_repository_authorities.py).

Critical principal repositories include:

| Component | Principal repository |
|---|---|
| Middleware | `appolon1908-hue/Middleware-` |
| Caddy | `appolon1908-hue/Caddy` |
| Kong | `appolon1908-hue/Kong` |
| Keycloak | `appolon1908-hue/Keycloak` |
| n8n | `appolon1908-hue/N8N` |
| Odoo | `appolon1908-hue/Odoo` |
| Telnexa SMS/Jasmin | `appolon1908-hue/telnexa` |
| Telnexa website | `appolon1908-hue/Telnexa-web` |
| Klyrow email platform | `appolon1908-hue/klyrow.com` |
| Klyrow website | `appolon1908-hue/klyrow-Website-` |
| Kyqra crawler | `appolon1908-hue/kyqra-crawler` |
| VICIdial/Asterisk connector | `appolon1908-hue/Vicidialer-Codestra` |
| Provisioning | `appolon1908-hue/codestra-provisioning-service` |
| SDK / connector kit | `appolon1908-hue/SDK-repository` |
| Social/Postiz | `appolon1908-hue/social.codestra.co` |

Independent products keep their own repositories as well. Middleware can hold integration contracts/adapters for MoneyBee, Beyvra, Breero, LARIM-A and other products, but their application source remains in their product repositories.

See [`docs/REPOSITORY-AUTHORITY-POLICY.md`](docs/REPOSITORY-AUTHORITY-POLICY.md) and [`docs/STAGE0_GROUND_TRUTH_20260829.md`](docs/STAGE0_GROUND_TRUTH_20260829.md).

## `codestra-production-platform` is reference only

`appolon1908-hue/codestra-production-platform` is retained as historical runtime/deployment/reconciliation and rollback evidence. It may be consulted to migrate old source into the correct dedicated repository, but it is not a central release authority and must not receive new implementation for a component that has its own repository.

Example: the historical `operations/caddy/api.codestra.co.caddy` baseline is a migration reference; future shared Caddy source belongs in `appolon1908-hue/Caddy`.

Historical records are preserved for provenance. They do not override the accepted source in an owning repository.

## Middleware owns the cross-system write boundary

The permanent integration rule is:

```text
                         Keycloak
                            |
                            v
                           Kong
                            ^
                            |
                          Caddy
                            |
                            v
                       Middleware
           +----------------+----------------+
           |                |                |
           v                v                v
  VICIdial connector   Telnexa/Jasmin   Klyrow/Email
           |             SMS only            |
           v                                 v
   VICIdial/Asterisk                      Postal/Mautic
           |
           +-------------> Odoo <-------------+
                              ^
                              |
                             n8n
                      orchestration only
```

Middleware owns authorization at the cross-system boundary, tenant isolation, canonical command/event contracts, semantic idempotency, durable inbox/outbox/audit state, Temporal command execution, provider adapter invocation, read-back, reconciliation, bounded retry/dead-letter and capability enforcement.

No browser, site, crawler, scraper, n8n workflow or provider should write directly across system boundaries when the mutation belongs to Middleware governance.

## Correct adapter/runtime split

Derived integration code in Middleware is allowed when it serves the Middleware boundary and does not duplicate the destination runtime.

Examples:

- `TelnexaAdapter` in Middleware: **allowed**. Jasmin/Telnexa runtime source: **belongs in `telnexa`**.
- VICIdial command/read-back adapter in Middleware: **allowed**. Restricted Asterisk/VICIdial connector runtime: **belongs in `Vicidialer-Codestra`**.
- Odoo command mapping in Middleware: **allowed**. Odoo addons/modules: **belong in `Odoo`**.
- generated Kong expectation/contract fixture: **allowed**. Kong service/routes/plugin implementation: **belongs in `Kong`**.
- generated n8n contract/template fixture: **allowed**. n8n workflow source: **belongs in `N8N`**.
- Middleware Connector Runtime: **belongs here**. Developer-facing connector kit/SDK packages: **belong in `SDK-repository`**.

## Canonical Middleware runtime

The executable intake runtime persists each accepted signed event and its NATS JetStream outbox record atomically. Critical workflows use Temporal. The tenant-scoped command ledger makes effectful commands durable and requires provider confirmation/read-back before completion.

Relevant source and documentation include:

- `app/` — Middleware API, security, storage, workers, command and Temporal runtime.
- `middleware/connector_sdk/` — privileged Middleware connector interfaces and validation.
- `services/connector-runtime/` — independently packaged Middleware Connector Runtime.
- `contracts/platform/` — canonical durable event/command shapes.
- `connectors/manifests/` — Middleware integration manifests and derived contract inputs.
- [`docs/COMMAND-LEDGER.md`](docs/COMMAND-LEDGER.md)
- [`docs/TEMPORAL-WORKFLOWS.md`](docs/TEMPORAL-WORKFLOWS.md)
- [`docs/CANONICAL-CONTRACTS.md`](docs/CANONICAL-CONTRACTS.md)

RabbitMQ is not the central Codestra bus; it remains within provider/product boundaries where applicable.

## Provider boundaries

Telnexa/Jasmin is SMS-only. VICIdial/Asterisk is the voice/contact-center system, with its specific connector owned by `Vicidialer-Codestra`. Klyrow owns email/customer-communications runtime. `kyqra-crawler` is the canonical Kyqra crawler source; legacy `kyqra` is retained only for historical reference.

The approved business-write pattern remains:

```text
site/provider/crawler event
        -> Caddy/Kong or private governed ingress
        -> Middleware durable signed boundary
        -> policy + idempotency + reconciliation
        -> trusted adapter
        -> destination principal system
        -> authoritative read-back
```

## Caddy and Kong

`appolon1908-hue/Caddy` owns shared Caddy edge source. `appolon1908-hue/Kong` owns Kong gateway source. Middleware may validate their compatibility, but it does not own either runtime.

Any Middleware `platform/caddy` or route-registry material is compatibility/reference material only. It must not be deployed as a competing Caddy source.

## Operating and release model

1. Change Middleware source only for a Middleware-owned responsibility or versioned integration contract.
2. Change an external component in its principal repository.
3. Run exact-head CI in every affected repository.
4. Build immutable artifacts from accepted source identities.
5. Keep live effects disabled during staging integration.
6. Prove authentication, tenant isolation, duplicate/replay behavior, migrations, provider read-back, backup/restore and rollback.
7. Record every participating repository SHA/artifact in the combined evidence note.
8. Activate capabilities separately and only after explicit approval.

There is no central mutable repository that can silently override another repository's accepted release.

Because Middleware is the cross-system write boundary, this repository owns the **combined release-evidence note**, not the release of the other repositories. See:

- [`docs/CROSS_REPOSITORY_RELEASE_EVIDENCE.md`](docs/CROSS_REPOSITORY_RELEASE_EVIDENCE.md)
- [`docs/releases/RELEASE_EVIDENCE_TEMPLATE.md`](docs/releases/RELEASE_EVIDENCE_TEMPLATE.md)
- [`docs/CI-ENVIRONMENTS-AND-HANDOFF.md`](docs/CI-ENVIRONMENTS-AND-HANDOFF.md)

## Repository scope

Commit here:

- Middleware application and worker source;
- Middleware migrations and durable-state contracts;
- Middleware Connector Runtime;
- trusted adapter translations and destination read-back/reconciliation logic;
- versioned cross-system contracts;
- non-secret compatibility fixtures and generated desired-state expectations;
- Middleware deployment templates, tests, observability and operational docs;
- cross-repository evidence templates.

Do not commit here as principal source:

- Caddy runtime configuration owned by `Caddy`;
- Kong implementation owned by `Kong`;
- Keycloak realm/client implementation owned by `Keycloak`;
- n8n workflow implementation owned by `N8N`;
- Odoo module implementation owned by `Odoo`;
- Telnexa/Jasmin runtime owned by `telnexa`;
- Klyrow/Postal/Mautic runtime owned by `klyrow.com`;
- VICIdial/Asterisk connector runtime owned by `Vicidialer-Codestra`;
- crawler runtime owned by `kyqra-crawler`;
- provisioning runtime owned by `codestra-provisioning-service`;
- distributable SDK/connector-kit packages owned by `SDK-repository`;
- independent product application source.

Never commit `.env`, passwords, tokens, private keys, certificates, live connection strings, databases, queue state, customer PII, recordings, logs or secret-bearing backups/evidence.

## Current safety posture

Repository source readiness is not runtime activation. Provider/business effect flags remain fail-closed until their separate staging and approval gates pass. No documentation or authority change in this repository authorizes production deployment, Caddy reload, SMS/email delivery, production dialing, unrestricted crawling or Odoo/provider writes.
