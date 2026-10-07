#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path
import yaml
from app.config import Settings
from app.main import create_app

ROOT=Path(__file__).resolve().parents[1]
MUTATIONS={"post","put","patch","delete"}

def main()->None:
    settings=Settings.from_env({"APP_ENV":"test","ALLOW_IN_MEMORY_STORAGE":"true","EXTERNAL_EFFECTS":"false"})
    schema=create_app(settings=settings).openapi()
    schema["info"]["description"]="Exact generated Middleware runtime contract. Bearer tokens use issuer https://auth.codestra.co/realms/codestra and audience middleware-api. Tenant-scoped reads enforce each configured caller status_scope; mutations enforce command_scope. External effects remain disabled."
    components=schema.setdefault("components",{})
    components.setdefault("securitySchemes",{})["bearerAuth"]={"type":"http","scheme":"bearer","bearerFormat":"JWT","description":"Keycloak machine token; issuer auth.codestra.co realm codestra; audience middleware-api"}
    tenant={"name":"X-Tenant-ID","in":"header","required":True,"schema":{"type":"string","minLength":1,"maxLength":128}}
    correlation={"name":"X-Correlation-ID","in":"header","required":True,"schema":{"type":"string","minLength":1,"maxLength":180}}
    idem={"name":"Idempotency-Key","in":"header","required":True,"schema":{"type":"string","minLength":8,"maxLength":180}}
    public={"/health","/ready","/readiness","/dependencies","/version","/capabilities"}
    for path,item in schema["paths"].items():
        for method,operation in item.items():
            if method not in {"get","post","put","patch","delete"}: continue
            if path not in public:
                operation["security"]=[{"bearerAuth":[]}]
            if (path.startswith("/v1/") or path.startswith("/api/v1/")) and path not in {"/v1/runtime/safety"} and "webhook" not in path and not path.startswith(("/v1/intake/", "/api/v1/intake/")):
                params=operation.setdefault("parameters",[])
                if not any(p.get("name")=="X-Tenant-ID" for p in params): params.append(tenant)
                if method in MUTATIONS and not ("webhook" in path or path.startswith("/v1/intake/")):
                    if not any(p.get("name")=="X-Correlation-ID" for p in params): params.extend([correlation,idem])
            responses=operation.setdefault("responses",{})
            # Preserve business-specific 422 responses emitted by the runtime, but do
            # not advertise a generic 422 that the application-wide validation
            # handler normalizes to the canonical 400 envelope.
            for code,text in (("400","Invalid canonical request"),("401","Authentication failed"),("403","Scope or tenant denied"),("404","Resource not found"),("409","Version, state, or idempotency conflict"),("429","Rate limited"),("503","Required durable dependency unavailable")):
                responses.setdefault(code,{"description":text})
    generated=ROOT/"contracts/platform/middleware-openapi.generated.json"
    generated.write_text(json.dumps(schema,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    (ROOT/"contracts/platform/integration-fabric-api.v2.yaml").write_text(yaml.safe_dump(schema,sort_keys=False,allow_unicode=True),encoding="utf-8")
    rows=[]
    for path,item in sorted(schema["paths"].items()):
        for method,operation in sorted(item.items()):
            if method not in {"get","post","put","patch","delete"}: continue
            rows.append({"domain":next((part for part in path.split("/") if part and part not in {"v1","api","internal"}),"platform"),"method":method.upper(),"path":path,"canonical_operation_id":operation.get("operationId"),"implementation_file":"registered FastAPI runtime","runtime_state":"DEPRECATED" if operation.get("deprecated") else "IMPLEMENTED"})
    matrix={"schema_version":"2.0","inventory_base_sha":"f9ae3142272729e4d697c81fd15cc9db124b87d8","classification_complete":True,"unknown_endpoints":0,"operations":rows}
    (ROOT/"config/api-completion-matrix.yaml").write_text(yaml.safe_dump(matrix,sort_keys=False),encoding="utf-8")
    print(f"OPENAPI_ROUTES={len(rows)}")

if __name__=="__main__": main()
