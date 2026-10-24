"""Fail-closed edge cases exposed by the required CI type check."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.api.v1.campaign_recycling import BoundaryError, dispatch
from app.commands import CommandEnvelope, CommandPolicy, CommandPolicyRegistry, CommandService, MemoryCommandStore
from app.core.campaign_recycling import CampaignRecyclingConflict, PostgresCampaignRecyclingStore
from app.platform.rehearsal import NoEffectRehearsal


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_id", ["campaign_engine_plan", "delivery_event_ingest", "suppression_record"])
async def test_body_operations_reject_missing_validated_body(operation_id):
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": FastAPI()})
    with pytest.raises(BoundaryError) as caught:
        await dispatch(request, {"operationId": operation_id}, {"X-Tenant-ID": "TEST_SYN_TENANT"}, {}, None)
    assert (caught.value.status, caught.value.code) == (400, "invalid_request")


@pytest.mark.asyncio
async def test_unknown_global_backlog_fails_rehearsal():
    async def backlog(tenant_id):
        return 0, None

    runtime = SimpleNamespace(
        settings=SimpleNamespace(),
        commands=SimpleNamespace(backlog=backlog),
        platform=SimpleNamespace(safety=SimpleNamespace(switches=SimpleNamespace(
            limits=SimpleNamespace(global_backlog_bound=10000)))),
    )
    runner = NoEffectRehearsal(runtime, runtime_schema_version=11, contract_digest=None)
    check = await runner._worker_backlog()
    assert check.status == "fail"
    assert check.detail["error"] == "global_backlog_unavailable"


@pytest.mark.asyncio
async def test_atomic_exposure_rejects_nontransactional_command_store():
    class PoolTrap:
        def acquire(self):
            raise AssertionError("database accessed before checking command store")

    key = "mcr1:" + "2" * 64
    command = CommandEnvelope(
        command_id=uuid4(), command_type="test.syn.mcr.reserve.v1", command_version="1.0",
        target="test-syn", tenant_id="TEST_SYN_TENANT", requested_by="svc-mcr",
        correlation_id="corr-ci-guard", idempotency_key=key, capability="TEST_SYN_EXECUTE",
        payload={"synthetic": True},
    )
    store = MemoryCommandStore()
    service = CommandService(store=store, policies=CommandPolicyRegistry(
        (CommandPolicy(prefix="test.syn.", target="test-syn", capability="TEST_SYN_EXECUTE", readback_required=True),),
        {"TEST_SYN_EXECUTE": True},
    ))
    with pytest.raises(CampaignRecyclingConflict, match="transactional command store is required"):
        await PostgresCampaignRecyclingStore(PoolTrap()).reserve_exposure_and_command(
            tenant_id=command.tenant_id, lead_id="100-L-00000001", campaign_id="klyrow:test-syn-mcr",
            campaign_version=1, channel="voice", touch_index=1, exposure_id=uuid4(), decision_id=uuid4(),
            policy_version="mcr-policy-1.0.0", sender_identity_id=None, idempotency_key=key,
            command=command, command_service=service, authenticated_subject="svc-mcr",
            authenticated_client_id="mcr-test", synthetic_execution_authorized=True,
        )
    assert store._commands == {}
    assert store._outbox == []
