# PR #432 / PR #434 repair evidence — 2026-10-07

[Before / after diagram](before-after.svg)

PR #432 branch: `fix/source-hygiene-post430-20261005`.
PR #434 branch: `convergence/middleware-saas-certified-20261005`.

## Observed failure

[Run 37560401308, job 112596107496](https://github.com/appolon1908/Middleware-/actions/runs/37560401308/job/112596107496) ran on `codestra-ci-middleware-dev-desktop`.
Both digest-pinned images pulled successfully. Redis was healthy. PostgreSQL started
at 14:01:57 UTC; initdb reached its temporary socket-only server at 14:02:49,
then began shutting it down at 14:02:51. The shutdown checkpoint was still running
when health retries expired and the runner observed `unhealthy` at 14:03:06.
Checkout and every actual test/validator step were skipped. The locale warning
was not a fatal error: initdb reported success and created the database.

## Implemented candidate

The PostgreSQL service now has a bounded 180-second health start period and probes
`127.0.0.1` over TCP. The temporary initialization server listens only on its Unix
socket, so it cannot prematurely satisfy readiness or end the startup grace period.
The normal 5-second interval, 5-second timeout and 12 retries remain unchanged.
Image digests, dynamic ports, exact-SHA checkout, required tests, egress isolation,
final status publication and production denial controls remain in place.
No Compose stack participates in this pre-checkout service initialization.

References: [Docker health-check semantics](https://docs.docker.com/reference/dockerfile/#healthcheck)
and [official PostgreSQL entrypoint](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh).
A regression contract checks the final-server TCP probe, bounded grace, original
retry settings, image identity, dynamic port, exact-SHA checkout and status dependency.

## Verification status

Implementation and local validation are in progress. The diagram's AFTER lane is
an intended candidate path, not evidence of passing GitHub CI or a completed merge.
Docker is unavailable on PATH on this Windows host; live container startup,
Docker build and database certification have not been run locally.
PR #434 has not yet been merged. Publication must obey the repository preflight
and compare-and-swap requirements. No production effects are authorized.
