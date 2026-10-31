"""Gateway statuses that arrive after the send was forwarded are ambiguous.

The internal Evolution transport answers ``502`` whenever the upstream
provider (Evolution API or Meta Graph) answered any non-2xx status, after the
upstream call was made, so the message may already exist. A ``504`` comes from
a proxy that forwarded the request and stopped waiting. The transport ignores
``Idempotency-Key``, so a retry is a second send. Both statuses must
therefore park the operation for reconciliation (``UNKNOWN``/``AMBIGUOUS``) and
never take the known-safe retry path.

Statuses that are answered before anything is forwarded stay retryable:
``503`` (the transport's only 503 is ``service_auth_not_configured``, returned
before the provider call), ``429`` (the transport maps upstream 429 to 502, so
a 429 can only come from a rate limiter in front of it), and ``408``/``425``
(the request was not processed).

The workflow tests are in-memory evidence only: the real ``CommandKernel``,
in-memory ledger/outbox, ``AdapterDispatch``, ``Reconciler`` and
``WhatsAppProviderAdapter`` against a scripted ``httpx.MockTransport``
provider. No socket is opened and no credential is real.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from app.commands import (
    CommandEnvelope,
    CommandPolicyRegistry,
    CommandService,
    MemoryCommandStore,
)
from app.control_plane_auth import ControlPlaneCaller
from app.core.config import Settings
from app.platform.adapter import AdapterContext, ErrorClass, Outcome
from app.platform.adapters.fixtures import development_fixtures
from app.platform.adapters.whatsapp import WhatsAppProviderAdapter
from app.platform.bus import BusSettings
from app.platform.memory import MemoryExecutionBus
from app.platform.principal import KernelPrincipal
from app.platform.reconciler import Reconciler
from app.platform.runtime import build_platform_runtime, command_policies
from app.platform.safety import SafetyGate, SafetySwitches

TENANT = "tenant-wa-ambiguous"
BASE_URL = "https://evolution.test.invalid"
SEND_PATH = "/internal/v1/whatsapp/transport/messages"
CAPABILITY = "WHATSAPP_DELIVERY"
TARGET = "evolution-whatsapp"
CLIENT_ID = "middleware-api"

WHATSAPP_CALLER = ControlPlaneCaller(
    client_id=CLIENT_ID,
    command_scope="platform.command",
    status_scope="platform.command.read",
    allowed_command_prefixes=("whatsapp.",),
    allowed_targets=frozenset({TARGET}),
    connector_commands_allowed=True,
    compatibility_only=False,
)

POST_FORWARD_STATUSES = (502, 504)
PRE_FORWARD_STATUSES = (408, 425, 429, 503)


def envelope() -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "command_id": str(uuid4()),
            "command_type": "whatsapp.message.send.v1",
            "command_version": "1.0",
            "target": TARGET,
            "tenant_id": TENANT,
            "requested_by": "user-1",
            "correlation_id": "corr-" + uuid4().hex[:12],
            "idempotency_key": "idem-wa-" + uuid4().hex,
            "capability": CAPABILITY,
            "payload": {
                "recipient": "15550000000",
                "campaign_id": "cmp-1",
                "instance_id": "test-instance",
                "message": {"type": "text", "text": "hello"},
                "business_context": {"campaign_id": "cmp-1"},
            },
        }
    )


def adapter_context(cmd: CommandEnvelope, client: httpx.AsyncClient) -> AdapterContext:
    return AdapterContext(
        tenant_id=cmd.tenant_id,
        command_id=str(cmd.command_id),
        correlation_id=cmd.correlation_id,
        attempt=1,
        timeout_seconds=5,
        environment="test",
        deployment_sha="test",
        http=client,
        payload=cmd.payload,
    )


def adapter_settings() -> Settings:
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "ALLOW_IN_MEMORY_STORAGE": "true",
            "EVOLUTION_WHATSAPP_BASE_URL": BASE_URL,
            "EVOLUTION_WHATSAPP_SERVICE_TOKEN": "test-only-token",
            "EVOLUTION_WHATSAPP_PROVIDER": "evolution",
        }
    )


async def execute_with_status(status_code: int) -> Any:
    cmd = envelope()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": "gateway"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await WhatsAppProviderAdapter(adapter_settings()).execute(
            cmd, adapter_context(cmd, client)
        )


# ----------------------------------------------------------------------------
# adapter level
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", POST_FORWARD_STATUSES)
async def test_post_forward_gateway_status_is_ambiguous_not_transient(
    status_code: int,
) -> None:
    result = await execute_with_status(status_code)
    assert result.outcome is Outcome.UNKNOWN
    assert result.error_class is ErrorClass.AMBIGUOUS
    assert result.safe_error_code == f"adapter_http_{status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", PRE_FORWARD_STATUSES)
async def test_pre_forward_status_stays_retryable_before_effect(
    status_code: int,
) -> None:
    result = await execute_with_status(status_code)
    assert result.outcome is Outcome.TRANSIENT
    assert result.error_class is ErrorClass.RETRYABLE_BEFORE_EFFECT
    assert result.safe_error_code == f"adapter_http_{status_code}"


# ----------------------------------------------------------------------------
# workflow level: kernel -> in-memory outbox -> bus -> adapter -> reconciler
# ----------------------------------------------------------------------------
@dataclass
class GatewayErrorAfterSendProvider:
    """Records the send, then answers ``failure_status`` instead of 202.

    Every later POST would be accepted, so a retry would show up as a second
    recorded send for the same ``Idempotency-Key``.
    """

    failure_status: int
    posts: Counter[str] = field(default_factory=Counter)
    sends: Counter[str] = field(default_factory=Counter)
    readbacks: int = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "POST" and path == SEND_PATH:
            key = request.headers["idempotency-key"]
            self.posts[key] += 1
            self.sends[key] += 1
            body = json.loads(request.content)
            if self.posts[key] == 1:
                return httpx.Response(
                    self.failure_status, json={"error": {"code": "provider_error"}}
                )
            return httpx.Response(
                202,
                json={
                    "command_id": body["command_id"],
                    "correlation_id": body["correlation_id"],
                    "result": {
                        "provider": "evolution",
                        "provider_message_id": f"wamid.{key}.{self.sends[key]}",
                    },
                },
            )
        if request.method == "GET" and path.startswith(SEND_PATH + "/"):
            self.readbacks += 1
            return httpx.Response(
                404, json={"error": {"code": "provider_message_not_found"}}
            )
        return httpx.Response(404, json={"error": {"code": "unexpected_route"}})


class Harness:
    def __init__(self, base: Settings, provider: GatewayErrorAfterSendProvider) -> None:
        self.provider = provider
        self.settings = base.replace(
            enable_external_delivery=True,
            effect_whatsapp_delivery_enabled=True,
            umbrella_external_delivery_enabled=True,
            evolution_whatsapp_base_url=BASE_URL,
            evolution_whatsapp_service_token="test-only-token",
            evolution_whatsapp_provider="evolution",
        )
        loaded = CommandPolicyRegistry.load()
        policies = command_policies(
            self.settings,
            CommandPolicyRegistry(
                loaded.policies, {**loaded.capabilities, CAPABILITY: True}
            ),
        )
        switches = SafetySwitches.load()
        gates = dict(switches.gates)
        gate = gates[CAPABILITY]
        gates[CAPABILITY] = type(gate)(
            **{
                **gate.__dict__,
                "environments": (*gate.environments, self.settings.app_env),
            }
        )
        safety = SafetyGate(
            self.settings,
            policies,
            type(switches)(**{**switches.__dict__, "gates": gates}),
        )
        self.store = MemoryCommandStore()
        self.commands = CommandService(store=self.store, policies=policies)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
        bus_settings = BusSettings(max_attempts=4, breaker_threshold=100)
        self.platform = build_platform_runtime(
            self.settings,
            commands=self.commands,
            http=self.http,
            pool=None,
            service_id="middleware-integration-api",
            adapters=(*development_fixtures(), WhatsAppProviderAdapter(self.settings)),
            safety=safety,
            bus_settings=bus_settings,
        )
        assert self.platform.registry_error is None, self.platform.registry_error
        self.clock = [1_000.0]
        self.bus = MemoryExecutionBus(
            self.store,
            self.platform.dispatch,
            max_attempts=bus_settings.max_attempts,
            clock=lambda: self.clock[0],
        )
        self.reconciler = cast(Reconciler, self.platform.reconciler)
        self.reconciler.source.clock = lambda: self.clock[0]  # type: ignore[attr-defined]

    def intent(self, command: CommandEnvelope) -> Any:
        rows = [
            item
            for item in self.store._outbox
            if item.command_id == str(command.command_id)
        ]
        assert len(rows) == 1
        return rows[0]

    async def drain_with_backoff(self, *, limit: int = 20) -> None:
        for _ in range(limit):
            if not await self.bus.run_once():
                self.clock[0] += 10_000.0
                if not await self.bus.run_once():
                    return

    def expire_leases(self) -> None:
        for item in self.store._outbox:
            if item.lease_until is not None:
                item.lease_until = self.clock[0] - 1.0


@pytest.fixture(autouse=True)
def whatsapp_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(client_id: str) -> ControlPlaneCaller:
        assert client_id == CLIENT_ID
        return WHATSAPP_CALLER

    monkeypatch.setattr("app.platform.bus.caller_for_client_id", lookup)


def principal() -> KernelPrincipal:
    return KernelPrincipal(
        subject="user-1",
        client_id=CLIENT_ID,
        tenants=(TENANT,),
        roles=(),
        scopes=("platform.command", "platform.command.read"),
        caller=WHATSAPP_CALLER,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", POST_FORWARD_STATUSES)
async def test_gateway_error_after_send_is_reconciled_never_resent(
    test_settings: Settings, status_code: int
) -> None:
    provider = GatewayErrorAfterSendProvider(failure_status=status_code)
    harness = Harness(test_settings, provider)
    command = envelope()
    try:
        await harness.platform.kernel.submit(command, principal())
        await harness.drain_with_backoff()

        operation = await harness.commands.get(TENANT, command.command_id)
        assert operation.state == "reconciliation_required"
        assert harness.intent(command).reconciliation_required_at is not None
        assert provider.posts == Counter({command.idempotency_key: 1})

        harness.clock[0] += 10_000.0
        harness.expire_leases()
        decision = await harness.reconciler.run_once()
        assert decision is not None
        assert decision.action == "dead_letter"
        await harness.drain_with_backoff()

        # Exactly one provider send; the reconciler never asked for another.
        assert provider.posts == Counter({command.idempotency_key: 1})
        assert provider.sends == Counter({command.idempotency_key: 1})
        operation = await harness.commands.get(TENANT, command.command_id)
        assert operation.state == "dead_lettered"
        dead_letter = await harness.commands.get_dead_letter(TENANT, command.command_id)
        # Middleware never learned a provider message id, so the outcome is left
        # to an operator. Either reason code is a manual-repair dead letter: the
        # base readback answers NOT_FOUND here, and a pending change answers
        # UNSUPPORTED instead. Neither one re-executes the send.
        assert dead_letter.reason_code in {
            "provider_missing_manual_repair",
            "readback_unsupported",
        }
        assert dead_letter.retry_exhausted is False
    finally:
        await harness.http.aclose()
