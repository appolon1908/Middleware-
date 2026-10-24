import json

from fastapi.routing import APIRoute

from app.api.v1.leads_workstation import router as leads_router
from app.router_registry import CANONICAL_ROUTERS


def test_leads_router_is_registered_as_canonical_surface():
    assert leads_router in CANONICAL_ROUTERS


def test_canonical_leads_operations_are_declared():
    operations = {
        (method, route.path)
        for route in leads_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    expected = {
        ("GET", "/platform/v1/leads"),
        ("POST", "/platform/v1/leads"),
        ("GET", "/platform/v1/leads/campaigns"),
        ("GET", "/platform/v1/leads/{lead_id}"),
        ("GET", "/platform/v1/leads/{lead_id}/contacts"),
        ("PATCH", "/platform/v1/leads/{lead_id}"),
        ("POST", "/platform/v1/leads/{lead_id}/assign"),
        ("POST", "/platform/v1/leads/{lead_id}/transition"),
        ("POST", "/platform/v1/leads/{lead_id}/suppress"),
        ("POST", "/platform/v1/leads/{lead_id}/consent"),
    }
    assert expected <= operations


def test_generated_public_contract_declares_leads_scopes():
    with open("deploy/public-api-route-contract.json", encoding="utf-8") as fh:
        contract = json.load(fh)
    routes = {
        (row["method"], row["path"]): row
        for row in contract["routes"]
        if row["path"].startswith("/platform/v1/leads")
    }
    assert routes[("GET", "/platform/v1/leads")]["scope"] == "platform.leads.read"
    assert routes[("POST", "/platform/v1/leads")]["scope"] == "platform.leads.write"
    assert routes[("PATCH", "/platform/v1/leads/{lead_id}")]["scope"] == "platform.leads.write"
