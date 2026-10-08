from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
import json

import httpx
import pytest

from app.commands import CommandEnvelope, CommandOperation
from app.core.config import Settings
from app.platform.adapter import (
    AdapterConfigurationError,
    AdapterContext,
    Outcome,
    ReadbackStatus,
)
from app.platform.adapters.media import (
    MediaSaaSAdapter,
    media_saas_adapters,
)


def media_settings(**overrides: str) -> Settings:
    values = {
        "APP_ENV": "test",
        "ALLOW_IN_MEMORY_STORAGE": "true",
        "BLENDER_SAAS_BASE_URL": "https://blender.internal",
        "BLENDER_SAAS_SERVICE_TOKEN": "blender-test-token",
        "NATRON_SAAS_BASE_URL": "https://natron.internal",
        "NATRON_SAAS_SERVICE_TOKEN": "natron-test-token",
        "KDENLIVE_SAAS_BASE_URL": "https://kdenlive.internal",
        "KDENLIVE_SAAS_SERVICE_TOKEN": "kdenlive-test-token",
        **overrides,
    }
    return Settings.from_env(values)


def command(
    *,
    command_type: str = "media.blender.render.frame.v1",
    target: str = "blender-render",
) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "command_id": str(uuid4()),
            "command_type": command_type,
            "command_version": "1.0",
            "target": target,
            "tenant_id": "TEST_SYN",
            "requested_by": "media-user-1",
            "correlation_id": "corr-media-1",
            "idempotency_key": "media-idem-0001",
            "capability": "MEDIA_RENDER",
            "payload": {
                "project_id": "project-1",
                "parameters": {
                    "source_path": "scene.blend",
                    "frame": 12,
                },
            },
        }
    )


def operation(
    cmd: CommandEnvelope,
    provider_id: str | None = "job_123",
) -> CommandOperation:
    now = datetime.now(timezone.utc)
    return CommandOperation(
        **cmd.model_dump(exclude={"payload"}),
        state="accepted",
        created_at=now,
        updated_at=now,
        provider_operation_id=provider_id,
    )


def context(
    cmd: CommandEnvelope,
    client: httpx.AsyncClient,
) -> AdapterContext:
    return AdapterContext(
        tenant_id=cmd.tenant_id,
        command_id=str(cmd.command_id),
        correlation_id=cmd.correlation_id,
        attempt=1,
        timeout_seconds=5,
        environment="staging",
        deployment_sha="test",
        http=client,
        payload=cmd.payload,
    )


def blender_adapter(client: httpx.AsyncClient) -> MediaSaaSAdapter:
    adapter = MediaSaaSAdapter(
        adapter_id="blender-render",
        provider_family="media-blender",
        connector_ids=("blender-render",),
        command_kinds={
            "media.blender.render.frame.v1": "render_frame",
            "media.blender.render.animation.v1": "render_animation",
        },
        base_url="https://blender.internal",
        service_token="blender-test-token",
        http=client,
    )
    adapter.validate_config()
    return adapter


@pytest.mark.asyncio
async def test_execute_maps_command_to_standalone_job_and_preserves_identity() -> None:
    cmd = command()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs"
        assert request.headers["authorization"] == "Bearer blender-test-token"
        assert request.headers["x-tenant-id"] == cmd.tenant_id
        assert request.headers["x-actor-id"] == cmd.requested_by
        assert request.headers["x-correlation-id"] == cmd.correlation_id
        assert request.headers["idempotency-key"] == cmd.idempotency_key
        payload = __import__("json").loads(request.content)
        assert payload == {
            "kind": "render_frame",
            "project_id": "project-1",
            "parameters": {
                "source_path": "scene.blend",
                "frame": 12,
            },
        }
        return httpx.Response(
            202,
            json={
                "id": "job_123",
                "status": "queued",
                "kind": "render_frame",
                "project_id": "project-1",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await blender_adapter(client).execute(
            cmd, context(cmd, client)
        )

    assert result.outcome is Outcome.ACCEPTED
    assert result.provider_operation_id == "job_123"
    assert result.safe_details["status"] == "queued"


@pytest.mark.asyncio
async def test_readback_can_recover_job_by_idempotency_after_ambiguous_submit() -> None:
    cmd = command()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/by-idempotency"
        assert request.url.params["idempotency_key"] == cmd.idempotency_key
        assert request.headers["x-tenant-id"] == cmd.tenant_id
        return httpx.Response(
            200,
            json={
                "id": "job_recovered",
                "status": "succeeded",
                "kind": "render_frame",
                "project_id": "project-1",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await blender_adapter(client).readback(
            operation(cmd, None),
            context(cmd, client),
        )

    assert result.status is ReadbackStatus.MATCHED
    assert result.provider_operation_id == "job_recovered"


@pytest.mark.asyncio
async def test_readback_keeps_running_job_nonterminal_and_encodes_job_id() -> None:
    cmd = command()
    provider_id = "job/unsafe?fragment"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v1/jobs/job%2Funsafe%3Ffragment"
        return httpx.Response(
            200,
            json={
                "id": provider_id,
                "status": "running",
                "kind": "render_frame",
                "project_id": "project-1",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await blender_adapter(client).readback(
            operation(cmd, provider_id),
            context(cmd, client),
        )

    assert result.status is ReadbackStatus.UNAVAILABLE
    assert result.safe_error_code == "media_job_running"


@pytest.mark.asyncio
async def test_cancel_uses_private_job_api_and_is_idempotent() -> None:
    cmd = command()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/job_123/cancel"
        assert request.headers["x-actor-id"] == cmd.requested_by
        assert request.headers["idempotency-key"] == cmd.idempotency_key
        return httpx.Response(
            200,
            json={
                "id": "job_123",
                "status": "cancelled",
                "kind": "render_frame",
                "project_id": "project-1",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await blender_adapter(client).cancel(
            operation(cmd),
            context(cmd, client),
        )

    assert result.outcome is Outcome.COMPLETED
    assert result.provider_operation_id == "job_123"


@pytest.mark.asyncio
async def test_media_factory_is_disabled_when_unconfigured_and_accepts_loopback_http() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"status": "ready"})
        )
    ) as client:
        configured = media_saas_adapters(media_settings(), http=client)
        assert {item.adapter_id for item in configured} == {
            "blender-render",
            "natron-compositor",
            "kdenlive-render",
        }

        disabled = media_saas_adapters(
            media_settings(
                BLENDER_SAAS_BASE_URL="",
                BLENDER_SAAS_SERVICE_TOKEN="",
                NATRON_SAAS_BASE_URL="",
                NATRON_SAAS_SERVICE_TOKEN="",
                KDENLIVE_SAAS_BASE_URL="",
                KDENLIVE_SAAS_SERVICE_TOKEN="",
            ),
            http=client,
        )
        assert disabled == ()

        local = MediaSaaSAdapter(
            adapter_id="blender-render",
            provider_family="media-blender",
            connector_ids=("blender-render",),
            command_kinds={"media.blender.render.frame.v1": "render_frame"},
            base_url="http://127.0.0.1:18121",
            service_token="local-test",
            http=client,
        )
        local.validate_config()
        assert local.base_url == "http://127.0.0.1:18121"

        public_plaintext = MediaSaaSAdapter(
            adapter_id="blender-render",
            provider_family="media-blender",
            connector_ids=("blender-render",),
            command_kinds={"media.blender.render.frame.v1": "render_frame"},
            base_url="http://blender.internal",
            service_token="bad",
            http=client,
        )
        with pytest.raises(AdapterConfigurationError):
            public_plaintext.validate_config()



def test_media_command_registry_is_wired_but_render_capability_defaults_closed() -> None:
    from app.commands import CommandPolicyRegistry
    from app.platform.registry import AdapterRegistry

    policies = CommandPolicyRegistry.load()
    assert policies.capabilities["MEDIA_RENDER"] is False
    assert policies.resolve("media.blender.render.frame.v1").target == "blender-render"
    assert policies.resolve("media.natron.render.project.v1").target == "natron-compositor"
    assert policies.resolve("media.kdenlive.render.timeline.v1").target == "kdenlive-render"

    # Registry ownership itself is proved by the generated connector policies;
    # runtime adapter behavior is covered by the HTTP tests above.
    registry = AdapterRegistry(policies)
    assert registry.ownership("media.blender.render.frame.v1") is None


def test_render_capability_has_explicit_default_deny_safety_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    safety = json.loads((root / "config/platform-safety.v1.json").read_text())
    capabilities = json.loads((root / "config/capabilities.v2.json").read_text())
    assert capabilities["capabilities"]["MEDIA_RENDER"] is False
    gate = safety["capability_gates"]["MEDIA_RENDER"]
    assert gate["classification"] == "external_effect"
    assert gate["umbrella_controls"] == ["EXTERNAL_DELIVERY_ENABLED"]
    assert gate["environments"] == ["staging", "production"]
    assert gate["campaign_scoped"] is False
    for provider in ("blender-render", "kdenlive-render", "natron-compositor"):
        assert safety["provider_kill_switches"][provider] is True
