# Security policy

## Reporting

Do not open a public issue for a suspected vulnerability, leaked credential,
tenant-isolation defect, authentication bypass, replay weakness, or external-write
safety failure. Use GitHub private vulnerability reporting for this repository.

Include the affected commit, route or component, reproduction conditions, impact,
and whether any live effect may have occurred. Never include production secrets,
tokens, customer data, or raw payloads.

## Supported source

Only the exact protected `main` commit and immutable images produced from it are
supported. Stale integration branches, mutable image tags, and unreviewed server
copies are not supported release artifacts.

## Safety boundary

`ODOO_WRITE`, `live_apply_authorized`, SMS/email/PSTN delivery, and all equivalent
external-effect switches remain disabled unless a separately approved production
activation identifies the exact image digest, evidence packet, rollback artifact,
and human approver.
