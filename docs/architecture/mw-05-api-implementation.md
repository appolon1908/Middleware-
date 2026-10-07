# mw-05 API boundary implementation

The section owns the HTTP boundary of the integration profile on port 8095.
V3 is the architecture version; existing versioned URLs remain authoritative.
Command execution and connector behavior remain delegated to their existing
kernel and domain layers. No deployment or provider effect is authorized.

Implementation sequence:

- Audit registered Command, Service, Connector and Automation surfaces and their
  boundary tests against the canonical application and generated contracts.
- Reproduce and repair boundary validation defects with regression tests:
  identity header hygiene, bounded typed request bodies, correlation consistency.
- Verify runtime/OpenAPI/Postman parity and regenerate affected artifacts.
- Run formatting, lint, types, unit/contract/integration and security checks;
  record exact unavailable prerequisites rather than claiming certification.
- Commit independently validated changes on section/mw-05-api and attempt a
  normal push, retaining local commits if remote authorization blocks it.

Review focus: oversized streaming requests, duplicated identity headers,
control characters in identity headers, cross-tenant reads and mutations,
and effect-disabled replay. Existing tenant and scope authorities must remain
in place. API changes must not introduce a second command or connector engine.
