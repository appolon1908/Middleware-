import json,yaml
from app.main import create_app

METHODS={"get","post","put","patch","delete"}
def _routes(paths): return {(method.upper(),path) for path,item in paths.items() for method in item if method in METHODS}

def test_generated_openapi_contract_and_matrix_match_runtime(test_settings):
    runtime=create_app(settings=test_settings).openapi()
    generated=json.load(open("contracts/platform/middleware-openapi.generated.json",encoding="utf-8"))
    contract=yaml.safe_load(open("contracts/platform/integration-fabric-api.v2.yaml",encoding="utf-8"))
    matrix=yaml.safe_load(open("config/api-completion-matrix.yaml",encoding="utf-8"))
    expected=_routes(runtime["paths"])
    assert expected==_routes(generated["paths"])==_routes(contract["paths"])
    assert expected=={(row["method"],row["path"]) for row in matrix["operations"]}
    assert matrix["classification_complete"] is True and matrix["unknown_endpoints"]==0
    assert not {"MISSING","PARTIAL","UNKNOWN"}&{row["runtime_state"] for row in matrix["operations"]}
    assert generated["components"]["securitySchemes"]["bearerAuth"]["scheme"]=="bearer"

def test_generated_contract_documents_mutation_headers():
    contract=yaml.safe_load(open("contracts/platform/integration-fabric-api.v2.yaml",encoding="utf-8"))
    for path,method in (("/v1/inbox/{record_id}/quarantine","post"),("/v1/outbox/{record_id}/cancel","post"),("/v1/operations/{command_id}/cancel","post")):
        names={item["name"] for item in contract["paths"][path][method]["parameters"]}
        assert {"X-Tenant-ID","X-Correlation-ID","Idempotency-Key"}<=names


def test_communication_success_responses_use_typed_schemas(test_settings):
    schema = create_app(settings=test_settings).openapi()
    expected = {
        ("/v1/communication/messages", "post", "CommunicationMessage"),
        ("/v1/communications/messages", "post", "CommunicationMessage"),
        ("/v1/communications/messages", "get", "CommunicationMessagePage"),
        ("/v1/communications/messages/{messageId}", "get", "CommunicationMessage"),
        ("/v1/communications/messages/{messageId}/events", "get", "CommunicationEventPage"),
        ("/v1/communications/messages/{messageId}/cancel", "post", "CommunicationMessage"),
        ("/v1/communications/providers/health", "get", "ProviderHealthReport"),
        ("/v1/communications/reputation", "get", "ProviderReputationReport"),
        ("/v1/communications/usage", "get", "CommunicationUsageReport"),
    }
    for path, method, model in expected:
        response = schema["paths"][path][method]["responses"]["200"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{model}"
        }


def test_communication_usage_query_timestamps_are_validated(test_settings):
    schema = create_app(settings=test_settings).openapi()
    parameters = schema["paths"]["/v1/communications/usage"]["get"]["parameters"]
    timestamps = {item["name"]: item["schema"] for item in parameters if item["name"] in {"from", "to"}}
    assert timestamps == {
        "from": {"anyOf": [{"format": "date-time", "type": "string"}, {"type": "null"}], "title": "From"},
        "to": {"anyOf": [{"format": "date-time", "type": "string"}, {"type": "null"}], "title": "To"},
    }
    responses = schema["paths"]["/v1/communications/usage"]["get"]["responses"]
    assert "422" not in responses
    assert responses["400"]["content"]["application/json"]["schema"]["required"] == ["error"]


def test_webhook_business_422_responses_remain_documented(test_settings):
    schema = create_app(settings=test_settings).openapi()
    webhook_operations = [
        operation
        for item in schema["paths"].values()
        for method, operation in item.items()
        if method == "post"
        and (
            str(operation.get("operationId", "")).startswith("ingress_")
            or "webhook" in str(operation.get("operationId", ""))
            or str(operation.get("operationId", "")).startswith("crm_event_")
        )
    ]
    assert webhook_operations
    for operation in webhook_operations:
        response = operation["responses"]["422"]
        assert response["description"] == "Event type is not allowed for this webhook"
        assert response["content"]["application/json"]["schema"]["required"] == ["error"]
