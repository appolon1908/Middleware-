# Middleware API Inventory

This file is intentionally **non-authoritative**.

The previous hand-maintained EXISTS/BUILD inventory drifted behind the registered
FastAPI runtime. It must not be used for implementation, release, or edge-routing
decisions.

## Canonical authorities

- `app/application.py` and `app/router_registry.py` — registered runtime source.
- `contracts/platform/middleware-openapi.generated.json` — generated OpenAPI.
- `contracts/platform/integration-fabric-api.v2.yaml` — generated YAML contract.
- `config/api-completion-matrix.yaml` — generated operation inventory and state.

Regenerate the API authority artifacts with:

```bash
python scripts/generate_api_contracts.py
```

CI verifies exact parity with:

```bash
python scripts/generate_api_contracts.py --check
```

The live OpenAPI also carries `x-codestra-auth-mode` on every HTTP operation so
reviewers can distinguish public, signed-ingress, handler-bearer,
shared-secret-bearer, and route-dependency surfaces without inferring policy
from path prefixes.
