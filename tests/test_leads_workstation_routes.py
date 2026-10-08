from fastapi.routing import APIRoute
import yaml

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


def test_permissions_manifest_declares_leads_scopes():
    with open(".codestra/permissions.yaml", encoding="utf-8") as fh:
        permissions = yaml.safe_load(fh)
    assert "platform.leads.read" in permissions["scopes"]["read"]
    assert "platform.leads.write" in permissions["scopes"]["write"]
