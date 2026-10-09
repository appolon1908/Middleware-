"""Mission 3: the agent provisioning saga orchestrator.

Odoo sends exactly one command to ``POST .../requests``; this module owns
every subsequent step. Odoo never calls Keycloak, VICIdial, Klyrow, or
Telnexa directly, and never receives Keycloak admin credentials - it only
ever sees this API's saga state and step history.

Path note: this deliberately does NOT live under the existing
``/platform/v1/provisioning/requests`` path (``app.api.v1.platform``). That
path is already a real, tested, and migrated feature - the canonical
service/infrastructure catalog's review-gated provisioning workflow
(``platform_services`` / ``platform_provisioning_requests`` /
``ProvisioningCreate`` with ``manifest_sha256``/``git_sha``/
``requested_components``) - an entirely different domain (onboarding
*microservices* into the platform, not provisioning *people*). Reusing that
path for this schema would collide with a live feature, so this router is
mounted at ``/platform/v1/agent-provisioning`` instead.

Every mutating saga step is fail-closed behind
``settings.live_identity_provisioning_enabled`` (Keycloak) and
``settings.vicidial_write_enabled``/``settings.live_writes_enabled``
(VICIdial channel adapter), matching this codebase's existing
default-closed posture (see ``app.api.v1.telephony._fail_closed_action``
for the same idiom).

CHANNEL_PROVISIONING provisions phone/webrtc through
``app.adapters.vicidial.mtls_client`` (sync_agent -> reserve or adopt an
extension -> provision_webrtc if requested), email through Klyrow and sms
through Telnexa.

Lifecycle (see ``app.core.agent_provisioning_lifecycle`` for the rules and
``docs/integrations/agent-provisioning-lifecycle.md`` for the contract):

* The saga commits after every step, so a crash loses at most the step in
  flight. A row left in an in-flight state longer than
  ``agent_provisioning_lease_seconds`` is resumed by a same-key retry, by
  ``/reconcile``, or by ``app.workers.agent_provisioning_reconciler``.
* suspend/revoke deprovision for real: revoke the WebRTC credential and
  disable the VICIdial agent, then disable the Keycloak user. Any step that
  is gated or fails leaves ``last_error_code=DEPROVISION_INCOMPLETE`` and
  repeating the action retries only what is still live.
* reactivate re-enables the Keycloak user and re-runs the saga, which
  re-issues exactly the provider operations whose effect was undone.
* Bindings are exclusive: a Keycloak identity (per tenant), a VICIdial user
  id and an extension can belong to only one employee until that
  employee's request is cleanly revoked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, and_, func, not_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.keycloak.lifecycle_client import (
    KeycloakLifecycleAdapter,
    KeycloakLifecycleDisabled,
    KeycloakLifecycleError,
)
from app.adapters.vicidial.mtls_client import VicidialMtlsClient, VicidialMtlsError
from app.core.agent_provisioning_lifecycle import (
    CONFLICTS,
    IN_FLIGHT_STATES,
    INVERSE_OPERATIONS,
    STALE_RECOVERED,
    STALE_RECOVERY_FAILED,
    STEPS,
    TRANSITIONS,
    action_allowed,
    classify_vicidial_error,
    is_retryable,
    lease_active,
)
from app.core.config import settings
from app.klyrow_sender_identity_adapter import (
    KlyrowSenderIdentityAdapter,
    KlyrowSenderIdentityAdapterError,
)
from app.telnexa_sender_profile_adapter import (
    TelnexaSenderProfileAdapter,
    TelnexaSenderProfileAdapterError,
)
from app.core.provisioning_auth import (
    ProvisioningPrincipal,
    require_current_policy_revision,
    require_provisioning_scope,
    require_tenant_match,
)
from app.db.models import (
    AgentProvisioningAudit,
    AgentProvisioningRequest,
    AgentProvisioningStep,
    IdempotencyRecord,
    OutboxEvent,
)
from app.db.session import get_session

router = APIRouter(prefix="/platform/v1/agent-provisioning", tags=["agent-provisioning"])
LOGGER = logging.getLogger("codestra.agent_provisioning")

IDEMPOTENCY_SCOPE = "agent_provisioning"
TERMINAL_STATES = frozenset({"EFFECTIVE", "PARTIAL", "FAILED", "SUSPENDED", "REVOKED"})
TERMINAL_REVOKED_STATES = frozenset({"REVOKED"})
VICIDIAL_BUSINESS_UNITS = frozenset(
    {"MOY", "COD", "SCP", "MBL", "RLP", "FTP", "TRX", "CAL", "TEST"}
)
RECONCILER_PRINCIPAL = ProvisioningPrincipal(
    subject="system:agent-provisioning-reconciler",
    authorized_party="middleware",
    tenant_ids=frozenset(),
)
LIFECYCLE_TOPICS = {
    "suspend": "platform.user.suspended",
    "revoke": "platform.user.revoked",
    "reactivate": "platform.user.reactivated",
}


class CampaignAssignment(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=64)
    # Vicidialer-Codestra's own AgentSpec requires exactly these two
    # fields (user_id ^[A-Z]{3}[0-9]{4,12}$, user_group) and binds an
    # agent to exactly one campaign - so they only make sense on the
    # single campaign the phone/webrtc channels will actually use. Optional
    # because odoo/sms/email-only requests never need a VICIdial agent.
    vicidial_user_id: str | None = Field(default=None, pattern=r"^[A-Z]{3}[0-9]{4,12}$")
    vicidial_user_group: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{2,20}$")
    # Vicidialer-Codestra requires every phone/webrtc mutation's context to
    # exactly match a pre-registered campaign_authority row (tenant_id,
    # business_unit, supervisor_subject) - it does not trust the caller's
    # bare say-so. This campaign must already have been provisioned there
    # (a separate, earlier flow); this field is not itself a provisioning
    # request for it.
    vicidial_supervisor_subject: str | None = Field(default=None, max_length=128)
    # Per-campaign product identities (Milestones 8/9). A human can be
    # Supervisor in one campaign and Agent in another with a different
    # email/sender per campaign, all under the same Keycloak identity -
    # so these live on the campaign assignment, not on IdentitySelection.
    campaign_email: str | None = Field(default=None, min_length=3, max_length=255)
    sms_sender: str | None = Field(default=None, min_length=1, max_length=20)
    sms_sender_type: str = Field(default="alphanumeric", max_length=32)
    sms_countries: list[str] = Field(default_factory=list, max_length=64)


class ChannelSelection(BaseModel):
    odoo: bool = False
    phone: bool = False
    webrtc: bool = False
    sms: bool = False
    email: bool = False


class ProvisioningEntitlements(BaseModel):
    """Approved optional capabilities for this agent.

    These are deliberately separate from transport channels.  A request may
    need an Odoo identity without an agent desktop, and voicemail/recording/
    monitoring are capability grants rather than delivery channels.  Keeping
    them explicit prevents the Odoo approval from being silently discarded at
    the Middleware boundary.
    """

    model_config = ConfigDict(extra="forbid")

    agent_desktop: bool = True
    voicemail: bool = False
    recording_access: bool = False
    monitoring_access: bool = False


class TelephonySelection(BaseModel):
    # Adopt binds an already-existing extension (e.g. 6101) without
    # consuming a pool slot; it is never a free-form dial string.
    existing_extension: str | None = Field(default=None, pattern=r"^[0-9]{2,16}$")
    incoming_allowed: bool = True
    outgoing_allowed: bool = True
    max_webrtc_sessions: int = Field(default=1, ge=1, le=1)
    # One of Vicidialer-Codestra's six named campaign-type pools
    # (transportation/moneybee/web_ai/senior_products/student_repayment/
    # supervisor_qa). Required only when reserving a NEW extension
    # (existing_extension unset) - there is no confirmed mapping from this
    # codebase's campaign/business-unit identifiers onto those pool names,
    # so the caller must state it explicitly rather than have it guessed.
    extension_pool: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")


class IdentitySelection(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    first_name: str = Field(default="", max_length=128)
    last_name: str = Field(default="", max_length=128)


class ProvisioningCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    employee_id: str = Field(min_length=1, max_length=128)
    identity: IdentitySelection
    campaigns: list[CampaignAssignment] = Field(default_factory=list, max_length=32)
    channels: ChannelSelection
    entitlements: ProvisioningEntitlements = Field(default_factory=ProvisioningEntitlements)
    telephony: TelephonySelection = Field(default_factory=TelephonySelection)


class TransitionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _record_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _touch(request: AgentProvisioningRequest) -> None:
    # Always explicit: the column's server-side onupdate would otherwise
    # expire the attribute, and it is the lease clock.
    request.updated_at = _now()


async def _checkpoint(
    session: AsyncSession, request: AgentProvisioningRequest, state: str | None = None,
) -> None:
    """Persist progress so a crash loses at most the step in flight."""
    if state is not None:
        request.state = state
    _touch(request)
    await session.commit()


async def _get_request(
    request_id: UUID, session: AsyncSession, *,
    principal: ProvisioningPrincipal | None = None, for_update: bool = False,
) -> AgentProvisioningRequest:
    """Load one saga row, scoped to the caller's tenants.

    A row in a tenant the caller does not cover is indistinguishable from a
    missing row (404), so request ids cannot be probed across tenants.
    """
    stmt = select(AgentProvisioningRequest).where(AgentProvisioningRequest.id == request_id)
    if principal is not None:
        if not principal.tenant_ids:
            raise HTTPException(403, "tenant claim does not cover the requested tenant")
        stmt = stmt.where(AgentProvisioningRequest.tenant_id.in_(principal.tenant_ids))
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "provisioning request not found")
    return row


async def _append_audit(
    session: AsyncSession, request: AgentProvisioningRequest, *,
    from_state: str, to_state: str, action: str, principal: ProvisioningPrincipal,
) -> None:
    audit_id = uuid4()
    session.add(AgentProvisioningAudit(
        id=audit_id, request_id=request.id, from_state=from_state, to_state=to_state,
        action=action, actor_subject=principal.subject, correlation_id=request.correlation_id,
        record_hash=_record_hash({
            "id": str(audit_id), "request_id": str(request.id), "from": from_state,
            "to": to_state, "action": action, "actor": principal.subject,
            "at": _now().isoformat(),
        }),
    ))


async def _add_step(
    session: AsyncSession, request: AgentProvisioningRequest, *,
    system: str, operation: str, state: str,
    external_reference: str | None = None, readback_state: str | None = None,
    error_code: str | None = None, error_summary: str | None = None,
) -> None:
    attempt = (
        await session.execute(
            select(func.count()).select_from(AgentProvisioningStep).where(
                AgentProvisioningStep.request_id == request.id,
                AgentProvisioningStep.system == system,
                AgentProvisioningStep.operation == operation,
            )
        )
    ).scalar_one() + 1
    now = _now()
    session.add(AgentProvisioningStep(
        id=uuid4(), request_id=request.id, system=system, operation=operation,
        attempt=attempt, state=state, external_reference=external_reference,
        started_at=now, completed_at=now, readback_state=readback_state,
        error_code=error_code,
        error_summary=error_summary[:500] if error_summary else error_summary,
        # Python-side so steps written in one transaction still order.
        created_at=now,
    ))
    STEPS.labels(system=system, operation=operation, state=state).inc()
    LOGGER.info(
        "agent_provisioning_step",
        extra={
            "correlation_id": request.correlation_id, "request_id": request.request_id,
            "system": system, "operation": operation, "attempt": attempt,
            "state": state, "error_code": error_code,
        },
    )


async def _emit_platform_event(
    session: AsyncSession, request: AgentProvisioningRequest, topic: str,
) -> None:
    """Notify n8n (Mission 4E) - and nothing more.

    n8n reacts only after this event lands; it never decides whether a
    user exists or is provisioned - that determination is made entirely
    above, by the saga itself. This reuses the existing generic outbox
    dispatch worker (the same "pending" row + worker pattern every other
    topic in this codebase already uses) rather than calling n8n directly
    from the request path.
    """
    session.add(OutboxEvent(
        id=uuid4(), topic=topic, correlation_id=request.correlation_id,
        status="pending",
        payload={
            "request_id": request.request_id,
            "tenant_id": request.tenant_id,
            "employee_id": request.employee_id,
            "primary_email": request.primary_email,
            "state": request.state,
            "last_error_code": request.last_error_code,
            "last_error_summary": request.last_error_summary,
        },
    ))


StepOutcome = Literal["ok", "gated", "failed"]


async def _lock_resource(session: AsyncSession, key: str) -> None:
    """Serialize binding checks for one provider resource until commit."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key},
    )


def _holds_bindings() -> ColumnElement[bool]:
    """A request keeps its bindings until it is cleanly revoked.

    A revoke whose deprovision steps did not all succeed leaves
    ``last_error_code`` set, so the provider resources stay reserved to this
    employee rather than being handed to someone else while still live.
    """
    return not_(and_(
        AgentProvisioningRequest.state == "REVOKED",
        AgentProvisioningRequest.last_error_code.is_(None),
    ))


def _other_employee(request: AgentProvisioningRequest) -> ColumnElement[bool]:
    return not_(and_(
        AgentProvisioningRequest.tenant_id == request.tenant_id,
        AgentProvisioningRequest.employee_id == request.employee_id,
    ))


async def _vicidial_binding_holder(
    session: AsyncSession, request: AgentProvisioningRequest, *,
    operations: tuple[str, ...], reference: str,
) -> AgentProvisioningRequest | None:
    """Another employee's request that holds this VICIdial user/extension.

    VICIdial user ids and extensions are global to the dialer, so this is
    deliberately not tenant-scoped.
    """
    stmt = (
        select(AgentProvisioningRequest)
        .join(AgentProvisioningStep, AgentProvisioningStep.request_id == AgentProvisioningRequest.id)
        .where(
            AgentProvisioningStep.system == "vicidial",
            AgentProvisioningStep.operation.in_(operations),
            AgentProvisioningStep.state == "succeeded",
            AgentProvisioningStep.external_reference == reference,
            _other_employee(request),
            _holds_bindings(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _identity_binding_holder(
    session: AsyncSession, request: AgentProvisioningRequest, keycloak_subject: str,
) -> AgentProvisioningRequest | None:
    stmt = (
        select(AgentProvisioningRequest)
        .where(
            AgentProvisioningRequest.tenant_id == request.tenant_id,
            AgentProvisioningRequest.keycloak_subject == keycloak_subject,
            _other_employee(request),
            _holds_bindings(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _resource_version(
    session: AsyncSession, request: AgentProvisioningRequest, operations: tuple[str, ...],
) -> int:
    """Vicidialer-Codestra's optimistic ``expected_version`` for a resource.

    Each succeeded mutation of one resource key advances its version by
    one, and a binding keeps the key with one employee, so the count of
    that employee's succeeded mutations is the version the provider holds.
    The provider stays the authority: a mismatch is rejected there and
    surfaces here as ``VICIDIAL_RESOURCE_CONFLICT``.
    """
    stmt = (
        select(func.count())
        .select_from(AgentProvisioningStep)
        .join(AgentProvisioningRequest, AgentProvisioningStep.request_id == AgentProvisioningRequest.id)
        .where(
            AgentProvisioningRequest.tenant_id == request.tenant_id,
            AgentProvisioningRequest.employee_id == request.employee_id,
            AgentProvisioningStep.system == "vicidial",
            AgentProvisioningStep.operation.in_(operations),
            AgentProvisioningStep.state == "succeeded",
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def _run_identity_step(
    session: AsyncSession, request: AgentProvisioningRequest,
) -> StepOutcome:
    """IDENTITY: query-then-create the Keycloak user.

    "gated" (kill switch closed) is deliberately not the same outcome as
    "failed" (a real adapter/API error) - the saga's terminal state must
    tell an operator whether something is broken or simply not yet
    switched on.
    """
    if not settings.live_identity_provisioning_enabled:
        await _add_step(
            session, request, system="keycloak", operation="create_user",
            state="skipped", error_code="KILL_SWITCH_CLOSED",
            error_summary="live_identity_provisioning_enabled is false",
        )
        return "gated"
    adapter = KeycloakLifecycleAdapter(settings)
    identity = request.channels_json.get("_identity", {})
    try:
        existing = await adapter.query_user_by_email(request.primary_email)
        if existing is None:
            record = await adapter.create_user(
                request.primary_email,
                identity.get("first_name", ""), identity.get("last_name", ""),
            )
        else:
            record = existing
    except (KeycloakLifecycleError, KeycloakLifecycleDisabled) as exc:
        await _add_step(
            session, request, system="keycloak", operation="create_user",
            state="failed", error_code="KEYCLOAK_ADAPTER_ERROR", error_summary=str(exc),
        )
        return "failed"

    await _lock_resource(session, f"keycloak:{request.tenant_id}:{record.keycloak_subject}")
    if await _identity_binding_holder(session, request, record.keycloak_subject) is not None:
        CONFLICTS.labels(kind="identity_binding").inc()
        await _add_step(
            session, request, system="keycloak", operation="create_user",
            state="failed", error_code="IDENTITY_BINDING_CONFLICT",
            error_summary="this Keycloak identity is already bound to another employee",
        )
        return "failed"
    request.keycloak_subject = record.keycloak_subject
    await _add_step(
        session, request, system="keycloak", operation="create_user",
        state="succeeded", external_reference=record.keycloak_subject,
        readback_state="enabled" if record.enabled else "disabled",
    )
    return "ok"


async def _run_entitlements_step(
    session: AsyncSession, request: AgentProvisioningRequest,
) -> StepOutcome:
    """ENTITLEMENTS: apply the approved desktop and optional capabilities.

    Keycloak role assignment is the only implemented external entitlement
    adapter in this saga.  Voicemail, recording access, and monitoring access
    are still represented as explicit, durable Odoo-owned gates until their
    adapters are introduced; they must never be treated as provisioned merely
    because Odoo requested them.
    """
    entitlements = request.channels_json.get("_entitlements", {})
    outcomes: list[StepOutcome] = []
    agent_desktop = bool(entitlements.get("agent_desktop", True))

    if request.campaigns_json:
        if not agent_desktop:
            await _add_step(
                session, request, system="keycloak", operation="assign_approved_roles",
                state="skipped", error_code="ENTITLEMENT_DISABLED",
                error_summary="agent_desktop entitlement is disabled by the approved request",
            )
        elif not settings.live_identity_provisioning_enabled or not request.keycloak_subject:
            await _add_step(
                session, request, system="keycloak", operation="assign_approved_roles",
                state="skipped", error_code="KILL_SWITCH_CLOSED",
                error_summary=(
                    "live_identity_provisioning_enabled is false or no keycloak_subject"
                ),
            )
            outcomes.append("gated")
        else:
            adapter = KeycloakLifecycleAdapter(settings)
            role_names = sorted({entry["role"] for entry in request.campaigns_json})
            try:
                await adapter.assign_approved_roles(request.keycloak_subject, role_names)
                await _add_step(
                    session, request, system="keycloak", operation="assign_approved_roles",
                    state="succeeded", external_reference=",".join(role_names),
                )
            except KeycloakLifecycleError as exc:
                await _add_step(
                    session, request, system="keycloak", operation="assign_approved_roles",
                    state="failed", error_code="KEYCLOAK_ADAPTER_ERROR", error_summary=str(exc),
                )
                outcomes.append("failed")

    # The AgentProvisioningStep system constraint intentionally permits
    # "odoo" as the durable control-plane record for capabilities whose
    # provider adapter has not been implemented yet.  This makes the gap
    # visible to Odoo's existing mandatory-step/readback machinery and keeps
    # the saga PARTIAL without inventing a false success.
    unavailable = (
        ("voicemail", "provision_voicemail", "Voicemail adapter is not configured."),
        (
            "recording_access", "grant_recording_access",
            "Recording-access adapter is not configured.",
        ),
        (
            "monitoring_access", "grant_monitoring_access",
            "Monitoring-access adapter is not configured.",
        ),
    )
    for capability, operation, summary in unavailable:
        if not entitlements.get(capability):
            continue
        await _add_step(
            session, request, system="odoo", operation=operation, state="blocked",
            error_code="CAPABILITY_ADAPTER_NOT_CONFIGURED", error_summary=summary,
        )
        outcomes.append("gated")

    if "failed" in outcomes:
        return "failed"
    return "gated" if outcomes else "ok"

async def _prior_succeeded_step(
    session: AsyncSession, request: AgentProvisioningRequest, operations: tuple[str, ...],
    *, system: str = "vicidial",
) -> AgentProvisioningStep | None:
    """This request's latest succeeded step for ``operations`` - unless a
    later succeeded inverse operation (see ``INVERSE_OPERATIONS``) undid
    its effect, in which case the provisioning call must be re-issued."""
    inverses = tuple(
        inverse
        for operation in operations
        for inverse in INVERSE_OPERATIONS.get((system, operation), ())
    )
    stmt = (
        select(AgentProvisioningStep)
        .where(
            AgentProvisioningStep.request_id == request.id,
            AgentProvisioningStep.system == system,
            AgentProvisioningStep.operation.in_(operations + inverses),
            AgentProvisioningStep.state == "succeeded",
        )
        .order_by(AgentProvisioningStep.created_at.desc())
    )
    latest = (await session.execute(stmt)).scalars().first()
    if latest is None or latest.operation not in operations:
        return None
    return latest


def _vicidial_identifiers(request: AgentProvisioningRequest) -> dict[str, str] | None:
    campaign = request.campaigns_json[0] if request.campaigns_json else {}
    user_id = campaign.get("vicidial_user_id")
    user_group = campaign.get("vicidial_user_group")
    supervisor = campaign.get("vicidial_supervisor_subject")
    business_unit = user_id[:3] if user_id else None
    if (
        not campaign or not user_id or not user_group or not supervisor
        or business_unit not in VICIDIAL_BUSINESS_UNITS
    ):
        return None
    return {
        "campaign_id": campaign["campaign_id"], "user_id": user_id,
        "user_group": user_group, "supervisor_subject": supervisor,
        "business_unit": str(business_unit),
    }


def _vicidial_context(
    request: AgentProvisioningRequest, ids: dict[str, str], *,
    expected_version: int, reason: str,
) -> dict[str, Any]:
    return {
        "correlation_id": request.correlation_id, "actor": request.requested_by,
        "tenant_id": request.tenant_id, "business_unit": ids["business_unit"],
        "campaign_id": ids["campaign_id"],
        "supervisor_subject": ids["supervisor_subject"],
        "expected_version": expected_version, "reason": reason,
        "requested_at": _now().isoformat(),
    }


async def _call_vicidial(
    adapter: VicidialMtlsClient, operation: str, payload: dict[str, Any],
    request: AgentProvisioningRequest,
) -> dict[str, Any]:
    """Run one blocking mTLS call off the event loop."""
    result = await asyncio.to_thread(
        getattr(adapter, operation), payload,
        correlation_id=request.correlation_id,
        request_id=f"{request.request_id}:{operation}",
    )
    if not isinstance(result, dict):
        raise VicidialMtlsError("VICIdial response must be a JSON object")
    return result


async def _open_vicidial(
    session: AsyncSession, request: AgentProvisioningRequest, operation: str,
) -> VicidialMtlsClient | None:
    try:
        return VicidialMtlsClient(settings)
    except VicidialMtlsError as exc:
        await _add_step(
            session, request, system="vicidial", operation=operation, state="failed",
            error_code="VICIDIAL_NOT_CONFIGURED", error_summary=str(exc),
        )
        return None


async def _vicidial_failure(
    session: AsyncSession, request: AgentProvisioningRequest, operation: str,
    exc: VicidialMtlsError, *, conflict_code: str = "VICIDIAL_RESOURCE_CONFLICT",
) -> None:
    await _add_step(
        session, request, system="vicidial", operation=operation, state="failed",
        error_code=classify_vicidial_error(exc, conflict_code=conflict_code),
        error_summary=str(exc),
    )


async def _binding_conflict(
    session: AsyncSession, request: AgentProvisioningRequest, operation: str,
    error_code: str, summary: str, *, external_reference: str | None = None,
) -> StepOutcome:
    CONFLICTS.labels(kind=error_code.lower()).inc()
    await _add_step(
        session, request, system="vicidial", operation=operation, state="failed",
        external_reference=external_reference, error_code=error_code, error_summary=summary,
    )
    return "failed"


async def _provision_vicidial(
    session: AsyncSession, request: AgentProvisioningRequest, ids: dict[str, str],
) -> StepOutcome:
    """sync_agent -> reserve/adopt extension -> provision_webrtc.

    Every call is skipped when this request's step history already shows
    its effect live (see ``_prior_succeeded_step``), so /reconcile resumes
    past the first failure instead of re-issuing succeeded calls, and an
    extension is never reserved twice. Each succeeded call is committed
    before the next one starts.
    """
    channels = request.channels_json
    telephony = channels.get("_telephony", {})
    identity = channels.get("_identity", {})
    user_id = ids["user_id"]
    adapter = await _open_vicidial(session, request, "provision_phone")
    if adapter is None:
        return "failed"
    try:
        if await _prior_succeeded_step(session, request, ("sync_agent",)) is None:
            await _lock_resource(session, f"vicidial-user:{user_id}")
            if await _vicidial_binding_holder(
                session, request, operations=("sync_agent",), reference=user_id,
            ) is not None:
                return await _binding_conflict(
                    session, request, "sync_agent", "AGENT_BINDING_CONFLICT",
                    "this VICIdial user id is bound to another employee",
                )
            version = await _resource_version(session, request, ("sync_agent", "disable_agent"))
            try:
                result = await _call_vicidial(adapter, "sync_agent", {
                    "context": _vicidial_context(
                        request, ids, expected_version=version,
                        reason="agent provisioning saga",
                    ),
                    "agent": {
                        "user_id": user_id,
                        "full_name": (
                            f"{identity.get('first_name', '')} {identity.get('last_name', '')}"
                        ).strip() or user_id,
                        "user_group": ids["user_group"],
                        "campaigns": [ids["campaign_id"]],
                        "inbound_groups": [], "active": False,
                    },
                }, request)
            except VicidialMtlsError as exc:
                await _vicidial_failure(session, request, "sync_agent", exc)
                return "failed"
            confirmed = (result.get("actual") or {}).get("user_id")
            if confirmed is not None and confirmed != user_id:
                await _add_step(
                    session, request, system="vicidial", operation="sync_agent",
                    state="failed", error_code="READBACK_MISMATCH",
                    error_summary="provider confirmed a different VICIdial user id",
                )
                return "failed"
            await _add_step(
                session, request, system="vicidial", operation="sync_agent",
                state="succeeded", external_reference=user_id, readback_state="agent_synced",
            )
            await _checkpoint(session, request)

        extension: str | None
        prior_extension = await _prior_succeeded_step(
            session, request, ("reserve_extension", "adopt_extension"))
        if prior_extension is not None:
            extension = prior_extension.external_reference
        else:
            requested = telephony.get("existing_extension")
            if requested:
                operation = "adopt_extension"
                await _lock_resource(session, f"extension:{requested}")
                if await _vicidial_binding_holder(
                    session, request, operations=("reserve_extension", "adopt_extension"),
                    reference=requested,
                ) is not None:
                    return await _binding_conflict(
                        session, request, operation, "EXTENSION_BINDING_CONFLICT",
                        "this extension is bound to another employee",
                    )
                payload: dict[str, Any] = {"adoption": {
                    "user_id": user_id, "extension": requested,
                    "webrtc_enabled": bool(channels.get("webrtc")),
                    "incoming_allowed": telephony.get("incoming_allowed", True),
                    "outgoing_allowed": telephony.get("outgoing_allowed", True),
                }}
            elif telephony.get("extension_pool"):
                operation = "reserve_extension"
                payload = {"reservation": {
                    "user_id": user_id, "pool": telephony["extension_pool"],
                    "webrtc_enabled": bool(channels.get("webrtc")),
                    "incoming_allowed": telephony.get("incoming_allowed", True),
                    "outgoing_allowed": telephony.get("outgoing_allowed", True),
                }}
            else:
                await _add_step(
                    session, request, system="vicidial", operation="provision_phone",
                    state="blocked", error_code="CHANNEL_CONFIGURATION_INCOMPLETE",
                    error_summary="neither existing_extension nor extension_pool was supplied",
                )
                return "gated"
            payload["context"] = _vicidial_context(
                request, ids, expected_version=0, reason="agent provisioning saga",
            )
            try:
                result = await _call_vicidial(adapter, operation, payload, request)
            except VicidialMtlsError as exc:
                await _vicidial_failure(
                    session, request, operation, exc, conflict_code="EXTENSION_CONFLICT",
                )
                return "failed"
            extension = (result.get("actual") or {}).get("extension")
            if not isinstance(extension, str) or not extension.isdigit() or (
                requested and extension != requested
            ):
                await _add_step(
                    session, request, system="vicidial", operation=operation,
                    state="failed", error_code="READBACK_MISMATCH",
                    error_summary="provider did not confirm the bound extension",
                )
                return "failed"
            if not requested:
                await _lock_resource(session, f"extension:{extension}")
                if await _vicidial_binding_holder(
                    session, request, operations=("reserve_extension", "adopt_extension"),
                    reference=extension,
                ) is not None:
                    return await _binding_conflict(
                        session, request, operation, "EXTENSION_BINDING_CONFLICT",
                        "provider reserved an extension bound to another employee",
                        external_reference=extension,
                    )
            await _add_step(
                session, request, system="vicidial", operation=operation,
                state="succeeded", external_reference=extension,
                readback_state="phone_active",
            )
            await _checkpoint(session, request)

        if channels.get("webrtc") and await _prior_succeeded_step(
            session, request, ("provision_webrtc",),
        ) is None:
            version = await _resource_version(
                session, request, ("provision_webrtc", "revoke_webrtc"),
            )
            try:
                result = await _call_vicidial(adapter, "provision_webrtc", {
                    "context": _vicidial_context(
                        request, ids, expected_version=version,
                        reason="agent provisioning saga",
                    ),
                    "webrtc": {"user_id": user_id},
                }, request)
            except VicidialMtlsError as exc:
                code = classify_vicidial_error(
                    exc, conflict_code="WEBRTC_SESSION_ALREADY_ACTIVE",
                )
                if code != "WEBRTC_SESSION_ALREADY_ACTIVE":
                    await _vicidial_failure(session, request, "provision_webrtc", exc)
                    return "failed"
                # The edge reports an already-registered session for this
                # user: WebRTC is provisioned, it is just not a new ticket.
                await _add_step(
                    session, request, system="vicidial", operation="provision_webrtc",
                    state="succeeded", external_reference=extension,
                    readback_state="webrtc_session_active",
                )
                return "ok"
            # The response carries a one-time registration credential. It is
            # validated and dropped here - never stored, logged, or returned.
            issued_for = result.get("extension")
            del result
            if issued_for is not None and issued_for != extension:
                await _add_step(
                    session, request, system="vicidial", operation="provision_webrtc",
                    state="failed", error_code="READBACK_MISMATCH",
                    error_summary="WebRTC credential was issued for a different extension",
                )
                return "failed"
            await _add_step(
                session, request, system="vicidial", operation="provision_webrtc",
                state="succeeded", external_reference=extension,
                readback_state="webrtc_session_issued",
            )
    finally:
        adapter.close()
    return "ok"


async def _run_channel_provisioning_step(
    session: AsyncSession, request: AgentProvisioningRequest,
) -> StepOutcome:
    """CHANNEL_PROVISIONING provisions all four channels for real:
    phone/webrtc through the Vicidialer-Codestra adapter (Mission 6:
    sync_agent -> reserve or adopt an extension -> provision_webrtc if
    requested); email through Klyrow's service-authenticated
    /v1/internal/sender-identities (Mission 8, KlyrowSenderIdentityAdapter);
    sms through Telnexa's existing tenant-scoped /api/v1/senders (Mission 9,
    TelnexaSenderProfileAdapter). Email/sms are gated on
    campaigns[0].campaign_email / campaigns[0].sms_sender being supplied
    and on their own klyrow_write_enabled/telnexa_write_enabled switches
    (both still additionally require live_writes_enabled), independent of
    phone/webrtc's vicidial_write_enabled gate.

    Reconciliation: before calling any provider operation, check whether a
    "succeeded" step for that exact operation already exists for this
    request and is still live, and skip the call if so - so /reconcile
    resumes past whichever step first failed instead of re-failing every
    step that already succeeded (see ``_provision_vicidial``).
    """
    channels = request.channels_json
    outcomes: list[StepOutcome] = []
    early_campaign = request.campaigns_json[0] if request.campaigns_json else {}
    early_identity = channels.get("_identity", {})

    if channels.get("email"):
        if not (settings.klyrow_write_enabled and settings.live_writes_enabled):
            await _add_step(
                session, request, system="klyrow", operation="provision_sender_identity",
                state="skipped", error_code="KILL_SWITCH_CLOSED",
                error_summary="klyrow_write_enabled/live_writes_enabled is false",
            )
            outcomes.append("gated")
        elif not early_campaign.get("campaign_email") or not settings.klyrow_default_domain_claim_id:
            await _add_step(
                session, request, system="klyrow", operation="provision_sender_identity",
                state="blocked", error_code="CHANNEL_CONFIGURATION_INCOMPLETE",
                error_summary=(
                    "email requested but campaigns[0].campaign_email or "
                    "settings.klyrow_default_domain_claim_id was not supplied"
                ),
            )
            outcomes.append("gated")
        else:
            prior_email = await _prior_succeeded_step(
                session, request, ("provision_sender_identity",), system="klyrow",
            )
            if prior_email is None:
                try:
                    klyrow_adapter = KlyrowSenderIdentityAdapter(settings)
                    identity_result = await klyrow_adapter.provision_sender_identity(
                        tenant_id=request.tenant_id,
                        domain_claim_id=settings.klyrow_default_domain_claim_id,
                        email=early_campaign["campaign_email"],
                        display_name=(
                            f"{early_identity.get('first_name', '')} "
                            f"{early_identity.get('last_name', '')}"
                        ).strip() or early_campaign["campaign_email"],
                        correlation_id=request.correlation_id,
                    )
                    if str(identity_result.get("status", "")).upper() == "ACTIVE":
                        await _add_step(
                            session, request, system="klyrow",
                            operation="provision_sender_identity", state="succeeded",
                            external_reference=identity_result.get("id"),
                            readback_state="sender_identity_active",
                        )
                    else:
                        await _add_step(
                            session, request, system="klyrow",
                            operation="provision_sender_identity", state="failed",
                            external_reference=identity_result.get("id"),
                            error_code="SENDER_IDENTITY_NOT_ACTIVE",
                            error_summary=f"status={identity_result.get('status')!r}",
                        )
                        return "failed"
                except KlyrowSenderIdentityAdapterError as exc:
                    await _add_step(
                        session, request, system="klyrow",
                        operation="provision_sender_identity", state="failed",
                        error_code="KLYROW_ADAPTER_ERROR", error_summary=str(exc),
                    )
                    return "failed"

    if channels.get("sms"):
        if not (settings.telnexa_write_enabled and settings.live_writes_enabled):
            await _add_step(
                session, request, system="telnexa", operation="provision_sender_profile",
                state="skipped", error_code="KILL_SWITCH_CLOSED",
                error_summary="telnexa_write_enabled/live_writes_enabled is false",
            )
            outcomes.append("gated")
        elif not early_campaign.get("sms_sender"):
            await _add_step(
                session, request, system="telnexa", operation="provision_sender_profile",
                state="blocked", error_code="CHANNEL_CONFIGURATION_INCOMPLETE",
                error_summary="sms requested but campaigns[0].sms_sender was not supplied",
            )
            outcomes.append("gated")
        else:
            prior_sms = await _prior_succeeded_step(
                session, request, ("provision_sender_profile",), system="telnexa",
            )
            if prior_sms is None:
                try:
                    telnexa_adapter = TelnexaSenderProfileAdapter(settings)
                    profile_result = await telnexa_adapter.provision_sender_profile(
                        tenant_id=request.tenant_id,
                        sender=early_campaign["sms_sender"],
                        sender_type=early_campaign.get("sms_sender_type", "alphanumeric"),
                        countries=early_campaign.get("sms_countries") or [],
                    )
                    status_value = str(profile_result.get("status", "")).lower()
                    if status_value in {"requested", "approved"}:
                        await _add_step(
                            session, request, system="telnexa",
                            operation="provision_sender_profile", state="succeeded",
                            external_reference=profile_result.get("id"),
                            readback_state=f"sender_profile_{status_value}",
                        )
                    else:
                        await _add_step(
                            session, request, system="telnexa",
                            operation="provision_sender_profile", state="failed",
                            external_reference=profile_result.get("id"),
                            error_code="SENDER_PROFILE_REJECTED",
                            error_summary=f"status={profile_result.get('status')!r}",
                        )
                        return "failed"
                except TelnexaSenderProfileAdapterError as exc:
                    await _add_step(
                        session, request, system="telnexa",
                        operation="provision_sender_profile", state="failed",
                        error_code="TELNEXA_ADAPTER_ERROR", error_summary=str(exc),
                    )
                    return "failed"

    if not (channels.get("phone") or channels.get("webrtc")):
        return "ok" if not outcomes else "gated"

    if not (settings.vicidial_write_enabled and settings.live_writes_enabled):
        for name in ("phone", "webrtc"):
            if channels.get(name):
                await _add_step(
                    session, request, system="vicidial", operation=f"provision_{name}",
                    state="skipped", error_code="KILL_SWITCH_CLOSED",
                    error_summary="vicidial_write_enabled/live_writes_enabled is false",
                )
        return "gated"

    ids = _vicidial_identifiers(request)
    if ids is None:
        await _add_step(
            session, request, system="vicidial", operation="provision_phone",
            state="blocked", error_code="CHANNEL_CONFIGURATION_INCOMPLETE",
            error_summary=(
                "phone/webrtc requested but campaigns[0].vicidial_user_id/"
                "vicidial_user_group/vicidial_supervisor_subject were not "
                "supplied, or vicidial_user_id's business-unit prefix is "
                "not a recognized code"
            ),
        )
        return "gated"

    # Email/sms are committed before any VICIdial call starts.
    await _checkpoint(session, request)
    vicidial_outcome = await _provision_vicidial(session, request, ids)
    if vicidial_outcome != "ok":
        return vicidial_outcome
    return "gated" if outcomes else "ok"


async def _run_readback_step(
    session: AsyncSession, request: AgentProvisioningRequest, identity_outcome: StepOutcome,
) -> StepOutcome:
    if identity_outcome != "ok" or not request.keycloak_subject:
        return "gated"
    if not settings.live_identity_provisioning_enabled:
        await _add_step(
            session, request, system="keycloak", operation="readback",
            state="skipped", error_code="KILL_SWITCH_CLOSED",
        )
        return "gated"
    adapter = KeycloakLifecycleAdapter(settings)
    try:
        record = await adapter.query_user_by_email(request.primary_email)
    except KeycloakLifecycleError as exc:
        await _add_step(
            session, request, system="keycloak", operation="readback",
            state="failed", error_code="KEYCLOAK_ADAPTER_ERROR", error_summary=str(exc),
        )
        return "failed"
    if record is not None and record.keycloak_subject != request.keycloak_subject:
        await _add_step(
            session, request, system="keycloak", operation="readback",
            state="failed", readback_state="subject_mismatch",
            error_code="READBACK_MISMATCH",
            error_summary="the email now resolves to a different Keycloak subject",
        )
        return "failed"
    ok = record is not None and record.enabled
    await _add_step(
        session, request, system="keycloak", operation="readback",
        state="succeeded" if ok else "failed",
        readback_state="enabled" if ok else "not_confirmed",
    )
    return "ok" if ok else "failed"


def _record_transition(
    action: str, from_state: str, request: AgentProvisioningRequest,
) -> None:
    TRANSITIONS.labels(action=action, from_state=from_state, to_state=request.state).inc()
    LOGGER.info(
        "agent_provisioning_transition",
        extra={
            "correlation_id": request.correlation_id, "request_id": request.request_id,
            "action": action, "from_state": from_state, "to_state": request.state,
            "error_code": request.last_error_code,
        },
    )


async def _advance_saga(
    session: AsyncSession, request: AgentProvisioningRequest, principal: ProvisioningPrincipal,
    *, action: str = "advance", from_state: str | None = None,
) -> None:
    """Run REQUESTED -> VALIDATING -> IDENTITY -> ENTITLEMENTS ->
    CHANNEL_PROVISIONING -> READBACK -> {EFFECTIVE, PARTIAL, FAILED},
    committing after every phase. Every external effect inside each step is
    itself fail-closed (see the step functions above), so running this
    synchronously never risks a slow or unbounded external call by default.

    The terminal state distinguishes a real error from a designed gate:
    any step outcome of "failed" (an adapter/API genuinely errored) makes
    the whole saga FAILED; if nothing failed but something was "gated"
    (a kill switch closed, or a channel adapter that does not exist yet)
    the saga is PARTIAL; only when every step reports "ok" is it EFFECTIVE.
    The caller commits the terminal state.
    """
    from_state = from_state or request.state
    request.last_error_code = None
    request.last_error_summary = None
    await _checkpoint(session, request, "IDENTITY")
    identity_outcome = await _run_identity_step(session, request)
    await _checkpoint(session, request, "ENTITLEMENTS")
    entitlements_outcome = await _run_entitlements_step(session, request)
    await _checkpoint(session, request, "CHANNEL_PROVISIONING")
    channels_outcome = await _run_channel_provisioning_step(session, request)
    await _checkpoint(session, request, "READBACK")
    readback_outcome = await _run_readback_step(session, request, identity_outcome)

    outcomes = (identity_outcome, entitlements_outcome, channels_outcome, readback_outcome)
    if "failed" in outcomes:
        request.state = "FAILED"
        request.last_error_code = "SAGA_STEP_FAILED"
        request.last_error_summary = "At least one provisioning step returned a real adapter error."
        await _emit_platform_event(session, request, "platform.user.provision_failed")
    elif all(outcome == "ok" for outcome in outcomes):
        request.state = "EFFECTIVE"
        await _emit_platform_event(session, request, "platform.user.provisioned")
    else:
        request.state = "PARTIAL"

    request.version += 1
    _touch(request)
    await _append_audit(
        session, request, from_state=from_state, to_state=request.state,
        action=action, principal=principal,
    )
    _record_transition(action, from_state, request)


async def _run_deprovision(
    session: AsyncSession, request: AgentProvisioningRequest, action: str,
) -> list[StepOutcome]:
    """Undo every provider effect that is still live for this request.

    Order: revoke the WebRTC credential (live media access) first, then
    disable the VICIdial agent, then disable the Keycloak user. Every
    operation is attempted even when an earlier one fails, because each
    one independently removes access. Klyrow/Telnexa sender identities are
    tenant/campaign-shared resources, not personal access, and are left in
    place; sending through them requires the Keycloak identity disabled here.
    """
    outcomes: list[StepOutcome] = []
    live_webrtc = await _prior_succeeded_step(session, request, ("provision_webrtc",))
    live_agent = await _prior_succeeded_step(session, request, ("sync_agent",))
    pending = [
        (operation, live)
        for operation, live in (("revoke_webrtc", live_webrtc), ("disable_agent", live_agent))
        if live is not None
    ]
    vicidial_live = settings.vicidial_write_enabled and settings.live_writes_enabled
    ids = _vicidial_identifiers(request)
    if pending and (not vicidial_live or ids is None):
        for operation, _ in pending:
            await _add_step(
                session, request, system="vicidial", operation=operation, state="skipped",
                error_code="KILL_SWITCH_CLOSED" if not vicidial_live
                else "CHANNEL_CONFIGURATION_INCOMPLETE",
                error_summary="vicidial_write_enabled/live_writes_enabled is false"
                if not vicidial_live else "VICIdial identifiers are missing from the request",
            )
            outcomes.append("gated")
    elif pending and ids is not None:
        adapter = await _open_vicidial(session, request, pending[0][0])
        if adapter is None:
            outcomes.append("failed")
        else:
            try:
                for operation, live in pending:
                    versioned = (
                        ("provision_webrtc", "revoke_webrtc") if operation == "revoke_webrtc"
                        else ("sync_agent", "disable_agent")
                    )
                    version = await _resource_version(session, request, versioned)
                    try:
                        await _call_vicidial(adapter, operation, {
                            "context": _vicidial_context(
                                request, ids, expected_version=version,
                                reason=f"agent provisioning {action}",
                            ),
                            "user_id": ids["user_id"],
                        }, request)
                    except VicidialMtlsError as exc:
                        await _vicidial_failure(session, request, operation, exc)
                        outcomes.append("failed")
                        continue
                    await _add_step(
                        session, request, system="vicidial", operation=operation,
                        state="succeeded", external_reference=live.external_reference,
                        readback_state="webrtc_revoked" if operation == "revoke_webrtc"
                        else "agent_disabled",
                    )
            finally:
                adapter.close()

    if request.keycloak_subject and await _prior_succeeded_step(
        session, request, ("disable_user",), system="keycloak",
    ) is None:
        if not settings.live_identity_provisioning_enabled:
            await _add_step(
                session, request, system="keycloak", operation="disable_user",
                state="skipped", error_code="KILL_SWITCH_CLOSED",
                error_summary="live_identity_provisioning_enabled is false",
            )
            outcomes.append("gated")
        else:
            try:
                await KeycloakLifecycleAdapter(settings).disable_user(request.keycloak_subject)
                await _add_step(
                    session, request, system="keycloak", operation="disable_user",
                    state="succeeded", external_reference=request.keycloak_subject,
                    readback_state="disabled",
                )
            except KeycloakLifecycleError as exc:
                await _add_step(
                    session, request, system="keycloak", operation="disable_user",
                    state="failed", error_code="KEYCLOAK_ADAPTER_ERROR", error_summary=str(exc),
                )
                outcomes.append("failed")
    return outcomes


async def _reactivate_identity(
    session: AsyncSession, request: AgentProvisioningRequest,
) -> StepOutcome:
    if not request.keycloak_subject or await _prior_succeeded_step(
        session, request, ("disable_user",), system="keycloak",
    ) is None:
        return "ok"
    if not settings.live_identity_provisioning_enabled:
        await _add_step(
            session, request, system="keycloak", operation="enable_user",
            state="skipped", error_code="KILL_SWITCH_CLOSED",
            error_summary="live_identity_provisioning_enabled is false",
        )
        return "gated"
    try:
        await KeycloakLifecycleAdapter(settings).enable_user(request.keycloak_subject)
    except KeycloakLifecycleError as exc:
        await _add_step(
            session, request, system="keycloak", operation="enable_user",
            state="failed", error_code="KEYCLOAK_ADAPTER_ERROR", error_summary=str(exc),
        )
        return "failed"
    await _add_step(
        session, request, system="keycloak", operation="enable_user",
        state="succeeded", external_reference=request.keycloak_subject,
        readback_state="enabled",
    )
    return "ok"


def _binding_view(
    request: AgentProvisioningRequest, steps: list[AgentProvisioningStep],
) -> dict[str, Any]:
    """The agent/user binding as the provider systems currently hold it.

    Derived from succeeded steps only (``steps`` is oldest-first), so it
    never reports a binding that a provider did not confirm.
    """
    def latest(operations: tuple[str, ...]) -> AgentProvisioningStep | None:
        matching = [
            step for step in steps
            if step.system == "vicidial" and step.operation in operations
            and step.state == "succeeded"
        ]
        return matching[-1] if matching else None

    agent = latest(("sync_agent", "disable_agent"))
    extension = latest(("reserve_extension", "adopt_extension"))
    webrtc = latest(("provision_webrtc", "revoke_webrtc"))
    return {
        "keycloak_subject": request.keycloak_subject,
        "vicidial_user_id": agent.external_reference if agent else None,
        "agent_state": None if agent is None
        else ("synced" if agent.operation == "sync_agent" else "disabled"),
        "extension": extension.external_reference if extension else None,
        "extension_mode": None if extension is None
        else ("reserved" if extension.operation == "reserve_extension" else "adopted"),
        "webrtc_state": None if webrtc is None
        else ("provisioned" if webrtc.operation == "provision_webrtc" else "revoked"),
    }


def _public_view(request: AgentProvisioningRequest, steps: list[AgentProvisioningStep]) -> dict:
    return {
        "middleware_request_id": str(request.id),
        "request_id": request.request_id,
        "tenant_id": request.tenant_id,
        "employee_id": request.employee_id,
        "state": request.state,
        "correlation_id": request.correlation_id,
        "keycloak_subject": request.keycloak_subject,
        "last_error_code": request.last_error_code,
        "last_error_summary": request.last_error_summary,
        "entitlements": request.channels_json.get("_entitlements", {}),
        "binding": _binding_view(request, steps),
        "version": request.version,
        "steps": [
            {
                "system": step.system, "operation": step.operation, "attempt": step.attempt,
                "state": step.state, "external_reference": step.external_reference,
                "readback_state": step.readback_state, "error_code": step.error_code,
                "error_summary": step.error_summary,
                "retryable": step.state == "failed" and is_retryable(step.error_code),
                "started_at": step.started_at, "completed_at": step.completed_at,
            }
            for step in steps
        ],
    }


async def _steps_for(session: AsyncSession, request: AgentProvisioningRequest) -> list[AgentProvisioningStep]:
    rows = (
        await session.execute(
            select(AgentProvisioningStep)
            .where(AgentProvisioningStep.request_id == request.id)
            .order_by(AgentProvisioningStep.created_at, AgentProvisioningStep.attempt)
        )
    ).scalars().all()
    return list(rows)


def _lease_is_active(request: AgentProvisioningRequest) -> bool:
    return lease_active(
        request.state, request.updated_at, now=_now(),
        lease_seconds=settings.agent_provisioning_lease_seconds,
    )


async def _stored_response(
    session: AsyncSession, key_hash: str, request_hash: str,
) -> dict[str, Any] | None:
    """The response already recorded for this Idempotency-Key, if any."""
    record = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == IDEMPOTENCY_SCOPE,
                IdempotencyRecord.key_hash == key_hash,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    if record.request_hash != request_hash:
        CONFLICTS.labels(kind="idempotency").inc()
        raise HTTPException(409, "Idempotency-Key reused with a different request body")
    return record.response


async def _resume_create(
    session: AsyncSession, key_hash: str, request_hash: str, principal: ProvisioningPrincipal,
) -> AgentProvisioningRequest | None:
    """Same Idempotency-Key, no stored response: the first attempt is still
    running, or crashed after committing some steps. Resume a crashed one."""
    row = (
        await session.execute(
            select(AgentProvisioningRequest)
            .where(AgentProvisioningRequest.idempotency_hash == key_hash)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise HTTPException(409, "Idempotency-Key reused with a different request body")
    require_tenant_match(principal, row.tenant_id)
    if _lease_is_active(row):
        CONFLICTS.labels(kind="in_flight").inc()
        await session.rollback()
        raise HTTPException(409, "agent provisioning saga is in progress; retry later")
    if row.state in IN_FLIGHT_STATES:
        from_state = row.state
        await _checkpoint(session, row, "RECONCILING")
        STALE_RECOVERED.inc()
        await _advance_saga(session, row, principal, action="recover", from_state=from_state)
    return row


@router.post("/requests", status_code=status.HTTP_202_ACCEPTED)
async def create_provisioning_request(
    body: ProvisioningCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=256),
    x_correlation_id: str = Header("", alias="X-Correlation-ID"),
    x_policy_revision: str = Header(..., alias="X-Policy-Revision"),
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    require_tenant_match(principal, body.tenant_id)
    require_current_policy_revision(x_policy_revision)

    correlation_id = x_correlation_id or str(uuid4())
    request_payload = body.model_dump(mode="json")
    request_hash = _hash(json.dumps(request_payload, sort_keys=True))
    key_hash = _hash(f"{IDEMPOTENCY_SCOPE}:{idempotency_key}")

    stored = await _stored_response(session, key_hash, request_hash)
    if stored is not None:
        return stored

    row = await _resume_create(session, key_hash, request_hash, principal)
    created = False
    if row is None:
        row = AgentProvisioningRequest(
            id=uuid4(), request_id=body.request_id, tenant_id=body.tenant_id,
            employee_id=body.employee_id, primary_email=body.identity.email,
            campaigns_json=[c.model_dump() for c in body.campaigns],
            channels_json={
                **body.channels.model_dump(),
                "_identity": body.identity.model_dump(),
                "_entitlements": body.entitlements.model_dump(),
                "_telephony": body.telephony.model_dump(),
            },
            telephony_json=body.telephony.model_dump(),
            state="REQUESTED", policy_revision=x_policy_revision,
            idempotency_hash=key_hash, request_hash=request_hash,
            correlation_id=correlation_id, requested_by=principal.subject,
            updated_at=_now(),
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            # A concurrent attempt with the same key won the insert.
            resumed = await _resume_create(session, key_hash, request_hash, principal)
            if resumed is None:
                CONFLICTS.labels(kind="request_id").inc()
                raise HTTPException(409, "request_id already exists") from exc
            row = resumed
        else:
            created = True
            await _append_audit(
                session, row, from_state="", to_state="REQUESTED", action="create",
                principal=principal,
            )
            await _checkpoint(session, row, "VALIDATING")
            await _advance_saga(session, row, principal, from_state="REQUESTED")
    if not created:
        # A same-key attempt that was still running when this one first
        # looked may have finished and stored its response while
        # _resume_create waited on the row lock: replay it rather than
        # colliding on the idempotency record.
        stored = await _stored_response(session, key_hash, request_hash)
        if stored is not None:
            await session.rollback()
            return stored

    steps = await _steps_for(session, row)
    response = jsonable_encoder(_public_view(row, steps))
    session.add(IdempotencyRecord(
        id=uuid4(), scope=IDEMPOTENCY_SCOPE, key_hash=key_hash, request_hash=request_hash,
        response=response, status_code=202,
    ))
    await session.commit()
    return response


@router.get("/requests/{request_id}")
async def get_provisioning_request(
    request_id: UUID,
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    request = await _get_request(request_id, session, principal=principal)
    steps = await _steps_for(session, request)
    return _public_view(request, steps)


Action = Literal["reconcile", "suspend", "reactivate", "revoke"]


async def _transition(
    request_id: UUID, body: TransitionRequest, action: Action,
    principal: ProvisioningPrincipal, session: AsyncSession,
    *, policy_revision: str | None = None,
) -> dict:
    if policy_revision is not None:
        require_current_policy_revision(policy_revision)
    request = await _get_request(request_id, session, principal=principal, for_update=True)
    leased = _lease_is_active(request)
    if not action_allowed(action, request.state, lease_is_active=leased):
        CONFLICTS.labels(kind="in_flight" if leased else "transition").inc()
        state = request.state
        await session.rollback()
        if leased:
            raise HTTPException(409, "agent provisioning saga is in progress; retry later")
        raise HTTPException(409, f"cannot {action} a request in state {state}")
    from_state = request.state

    if action == "reconcile":
        await _checkpoint(session, request, "RECONCILING")
        await _advance_saga(session, request, principal, action="reconcile", from_state=from_state)
    elif action in ("suspend", "revoke"):
        target = "SUSPENDED" if action == "suspend" else "REVOKED"
        steps_before = len(await _steps_for(session, request))
        outcomes = await _run_deprovision(session, request, action)
        settled_before = from_state == target and request.last_error_code is None
        if settled_before and len(await _steps_for(session, request)) == steps_before:
            # Construct the idempotent response while the ORM row is still
            # loaded; rollback expires attributes and a later lazy refresh
            # would perform IO outside async greenlet context.
            response = _public_view(request, await _steps_for(session, request))
            await session.rollback()
            return response
        request.state = target
        if outcomes:
            request.last_error_code = "DEPROVISION_INCOMPLETE"
            request.last_error_summary = (
                f"{action} is recorded but at least one provider still holds live "
                f"access; repeat {action} to retry the remaining steps."
            )
        else:
            request.last_error_code = None
            request.last_error_summary = None
        request.version += 1
        _touch(request)
        await _append_audit(
            session, request, from_state=from_state, to_state=target, action=action,
            principal=principal,
        )
        await _emit_platform_event(session, request, LIFECYCLE_TOPICS[action])
        _record_transition(action, from_state, request)
    elif action == "reactivate":
        identity_outcome = await _reactivate_identity(session, request)
        if identity_outcome != "ok":
            request.last_error_code = (
                "REACTIVATION_FAILED" if identity_outcome == "failed" else "REACTIVATION_GATED"
            )
            request.last_error_summary = "the Keycloak identity could not be re-enabled"
            request.version += 1
            _touch(request)
            await _append_audit(
                session, request, from_state=from_state, to_state=request.state,
                action=action, principal=principal,
            )
            _record_transition(action, from_state, request)
        else:
            await _checkpoint(session, request, "RECONCILING")
            await _advance_saga(
                session, request, principal, action="reactivate", from_state=from_state,
            )
            await _emit_platform_event(session, request, LIFECYCLE_TOPICS["reactivate"])

    await session.commit()
    steps = await _steps_for(session, request)
    return _public_view(request, steps)


@router.post("/requests/{request_id}/reconcile")
async def reconcile_provisioning_request(
    request_id: UUID, body: TransitionRequest,
    x_policy_revision: str | None = Header(None, alias="X-Policy-Revision"),
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    return await _transition(
        request_id, body, "reconcile", principal, session, policy_revision=x_policy_revision,
    )


@router.post("/requests/{request_id}/suspend")
async def suspend_provisioning_request(
    request_id: UUID, body: TransitionRequest,
    x_policy_revision: str | None = Header(None, alias="X-Policy-Revision"),
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    return await _transition(
        request_id, body, "suspend", principal, session, policy_revision=x_policy_revision,
    )


@router.post("/requests/{request_id}/reactivate")
async def reactivate_provisioning_request(
    request_id: UUID, body: TransitionRequest,
    x_policy_revision: str | None = Header(None, alias="X-Policy-Revision"),
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    return await _transition(
        request_id, body, "reactivate", principal, session, policy_revision=x_policy_revision,
    )


@router.post("/requests/{request_id}/revoke")
async def revoke_provisioning_request(
    request_id: UUID, body: TransitionRequest,
    x_policy_revision: str | None = Header(None, alias="X-Policy-Revision"),
    principal: ProvisioningPrincipal = Depends(require_provisioning_scope("identity.request")),
    session: AsyncSession = Depends(get_session),
):
    return await _transition(
        request_id, body, "revoke", principal, session, policy_revision=x_policy_revision,
    )


async def resume_stale_sagas(
    session_factory: async_sessionmaker[AsyncSession], *, limit: int | None = None,
) -> list[str]:
    """Resume sagas whose runner crashed mid-flight (lease expired).

    Each row is claimed under ``FOR UPDATE SKIP LOCKED`` and re-checked, so
    concurrent reconcilers and API calls never run the same saga twice.
    Returns the resumed ``request_id`` values.
    """
    batch = limit or settings.agent_provisioning_reconciler_batch_size
    cutoff = _now() - timedelta(seconds=settings.agent_provisioning_lease_seconds)
    async with session_factory() as session:
        candidates = list((
            await session.execute(
                select(AgentProvisioningRequest.id)
                .where(
                    AgentProvisioningRequest.state.in_(IN_FLIGHT_STATES),
                    AgentProvisioningRequest.updated_at < cutoff,
                )
                .order_by(AgentProvisioningRequest.updated_at)
                .limit(batch)
            )
        ).scalars().all())

    resumed: list[str] = []
    for candidate in candidates:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(AgentProvisioningRequest)
                    .where(AgentProvisioningRequest.id == candidate)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None or row.state not in IN_FLIGHT_STATES or _lease_is_active(row):
                await session.rollback()
                continue
            from_state = row.state
            request_id = row.request_id
            try:
                await _checkpoint(session, row, "RECONCILING")
                await _advance_saga(
                    session, row, RECONCILER_PRINCIPAL, action="recover", from_state=from_state,
                )
                await session.commit()
            except Exception:
                # One poisoned row must not starve the rest of the batch. Any
                # checkpoint it already committed refreshed its lease, which
                # also delays its next attempt.
                await session.rollback()
                STALE_RECOVERY_FAILED.inc()
                LOGGER.exception(
                    "agent_provisioning_recovery_failed", extra={"request_id": request_id},
                )
                continue
            STALE_RECOVERED.inc()
            resumed.append(request_id)
    return resumed
