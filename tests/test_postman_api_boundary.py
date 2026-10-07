from __future__ import annotations

from scripts.generate_postman import _request


def test_postman_path_parameters_are_executable_variables():
    item = _request(
        {},
        "/platform/v1/operations/{operation_id}/timeline",
        "get",
        {
            "parameters": [
                {
                    "in": "path",
                    "name": "operation_id",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                }
            ],
        },
    )
    assert (
        item["request"]["url"]["raw"]
        == "{{base_url}}/platform/v1/operations/{{operation_id}}/timeline"
    )
    assert item["request"]["url"]["path"] == [
        "platform",
        "v1",
        "operations",
        "{{operation_id}}",
        "timeline",
    ]


def test_optional_headers_are_disabled_until_caller_supplies_them():
    item = _request(
        {},
        "/platform/v1/operations/{operation_id}",
        "get",
        {
            "parameters": [
                {
                    "in": "header",
                    "name": "X-Tenant-ID",
                    "required": False,
                    "schema": {"type": "string"},
                },
            ]
        },
    )
    tenant = next(h for h in item["request"]["header"] if h["key"] == "X-Tenant-ID")
    assert tenant["disabled"] is True
