"""Readback and repair-intent routes must refuse another tenant's request.

agent_provisioning_request is on the deferred-RLS list (migration 0071), so
the route's own tenant check is the only isolation for these paths.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import agent_provisioning as api
from app.core.provisioning_auth import ProvisioningPrincipal

PRINCIPAL_A = ProvisioningPrincipal(
    subject="svc-a", authorized_party="provisioning", tenant_ids=frozenset({"tenantA"})
)


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("no query may run for another tenant's request")

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


def _foreign_request():
    return SimpleNamespace(id=uuid4(), tenant_id="tenantB", state="EFFECTIVE")


@pytest.fixture
def foreign(monkeypatch):
    request = _foreign_request()

    async def _no_context(*_args, **_kwargs):
        return None

    async def _get(request_id, session, *, for_update=False):
        return request

    monkeypatch.setattr(api, "_set_provisioning_rls_context", _no_context)
    monkeypatch.setattr(api, "_get_request", _get)
    return request


def test_readback_refuses_another_tenants_request(foreign) -> None:
    session = _Session()
    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            api.provisioning_readback(foreign.id, "tenantA", PRINCIPAL_A, session)
        )
    assert denied.value.status_code == 403


def test_repair_intent_refuses_another_tenants_request(foreign) -> None:
    session = _Session()
    body = api.RepairIntentRequest(
        tenant_id="tenantA", drift_class="REPAIR_REQUIRED", proposed_action="resync"
    )
    with pytest.raises(HTTPException) as denied:
        asyncio.run(api.create_repair_intent(foreign.id, body, PRINCIPAL_A, session))
    assert denied.value.status_code == 403
    assert session.added == [] and session.committed is False
