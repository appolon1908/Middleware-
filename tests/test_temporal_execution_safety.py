from __future__ import annotations

from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from app.temporal_activities import CommandLedgerWorkflowActivities
from app.temporal_workflows import ActivityResult, CommandExecutionRequest


class DenyingSafety:
    def evaluate(self, subject, context, *, consume_budget):
        assert subject.tenant_id == "tenant-1"
        assert subject.target == "odoo-19"
        assert subject.capability == "CRM_CONTACT_WRITE"
        assert context.adapter_registered is True
        assert consume_budget is False
        return SimpleNamespace(allow=False, reason_code="global_kill_switch")


class AllowingSafety:
    def evaluate(self, subject, context, *, consume_budget):
        return SimpleNamespace(allow=True, reason_code="allowed")


class SpyAdapter:
    def __init__(self):
        self.execute_calls = 0

    async def execute(self, request):
        self.execute_calls += 1
        return ActivityResult(status="accepted", detail="sent")

    async def readback(self, request):
        return ActivityResult(status="completed", detail="readback")


def request() -> CommandExecutionRequest:
    return CommandExecutionRequest(
        command_id="11111111-1111-1111-1111-111111111111",
        command_type="crm.contact.create.v1",
        command_version="1.0",
        target="odoo-19",
        tenant_id="tenant-1",
        requested_by="actor-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
        capability="CRM_CONTACT_WRITE",
        payload={"contact_ref": "ref-1"},
        authenticated_client_id="odoo-integration",
    )


@pytest.mark.asyncio
async def test_temporal_safety_denial_precedes_provider_execution():
    adapter = SpyAdapter()
    activities = CommandLedgerWorkflowActivities(
        store=object(),  # not reached for the non-calling target
        odoo=adapter,
        safety=DenyingSafety(),
    )
    with pytest.raises(ApplicationError) as caught:
        await activities.execute_command(request())
    assert caught.value.type == "SafetyDenied"
    assert adapter.execute_calls == 0


@pytest.mark.asyncio
async def test_temporal_safety_allow_reaches_provider_once():
    adapter = SpyAdapter()
    activities = CommandLedgerWorkflowActivities(
        store=object(),
        odoo=adapter,
        safety=AllowingSafety(),
    )
    result = await activities.execute_command(request())
    assert result.status == "accepted"
    assert adapter.execute_calls == 1
