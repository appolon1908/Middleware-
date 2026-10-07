from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .commands import (
    AUTHENTICATED_CLIENT_ID_KEY,
    CommandConflict,
    CommandEnvelope,
    CommandNotFound,
    PostgresCommandStore,
    authenticated_command_digest,
)
from .klyrow_alert_adapter import KlyrowAlertAdapter, KlyrowAlertAdapterError
from .odoo_provider_adapter import OdooProviderAdapter, OdooProviderAdapterError
from .klyrow_email_adapter import KlyrowEmailAdapter, KlyrowEmailAdapterError
from .postly_social_adapter import (
    PostlySocialAdapter,
    PostlySocialAdapterError,
    PostlySocialUnknownOutcomeError,
)
from .telnexa_provider_adapter import (
    TelnexaProviderAdapterError,
    TelnexaSmsAdapter,
)
from .provider_canary import provider_evidence_digest
from .temporal_workflows import (
    ActivityResult,
    CommandExecutionRequest,
    CommandTransitionRequest,
    DeadLetterReplayRequest,
    DelayedCallbackRequest,
    ProvisioningStepRequest,
    ReconciliationRequest,
)


class ProviderAdapter(Protocol):
    async def execute(self, request: CommandExecutionRequest) -> ActivityResult:
        ...

    async def readback(self, request: CommandExecutionRequest) -> ActivityResult:
        ...


class FailClosedWorkflowActivities:
    """Activity bindings used until a durable command/operation owns execution."""

    @staticmethod
    def _blocked(name: str) -> ApplicationError:
        return ApplicationError(
            f"{name} is not bound to the durable command ledger",
            non_retryable=True,
            type="CapabilityDisabled",
        )

    @activity.defn(name="dispatch_delayed_callback")
    async def dispatch_delayed_callback(self, request: DelayedCallbackRequest) -> Any:
        raise self._blocked("dispatch_delayed_callback")

    @activity.defn(name="provision_identity")
    async def provision_identity(self, request: ProvisioningStepRequest) -> Any:
        raise self._blocked("provision_identity")

    @activity.defn(name="provision_product")
    async def provision_product(self, request: ProvisioningStepRequest) -> Any:
        raise self._blocked("provision_product")

    @activity.defn(name="verify_provisioning")
    async def verify_provisioning(self, request: ProvisioningStepRequest) -> Any:
        raise self._blocked("verify_provisioning")

    @activity.defn(name="compensate_provisioning")
    async def compensate_provisioning(self, request: ProvisioningStepRequest) -> Any:
        raise self._blocked("compensate_provisioning")

    @activity.defn(name="replay_dead_letter")
    async def replay_dead_letter(self, request: DeadLetterReplayRequest) -> Any:
        raise self._blocked("replay_dead_letter")

    def registered(self) -> tuple[Any, ...]:
        return (
            self.dispatch_delayed_callback,
            self.provision_identity,
            self.provision_product,
            self.verify_provisioning,
            self.compensate_provisioning,
            self.replay_dead_letter,
        )


class CommandLedgerWorkflowActivities:
    def __init__(
        self,
        store: PostgresCommandStore,
        odoo: OdooProviderAdapter | None = None,
        klyrow_alert: KlyrowAlertAdapter | None = None,
        telnexa_sms: TelnexaSmsAdapter | None = None,
        klyrow_email: KlyrowEmailAdapter | None = None,
        postly_social: PostlySocialAdapter | None = None,
    ) -> None:
        self.store = store
        self.odoo = odoo
        self.klyrow_alert = klyrow_alert
        self.telnexa_sms = telnexa_sms
        self.klyrow_email = klyrow_email
        self.postly_social = postly_social

    @activity.defn(name="record_command_transition")
    async def record_command_transition(
        self,
        request: CommandTransitionRequest,
    ) -> ActivityResult:
        try:
            operation = await self.store.transition(
                request.tenant_id,
                UUID(request.command_id),
                new_state=request.new_state,  # type: ignore[arg-type]
                actor_id=request.actor_id,
                reason=request.reason,
                provider_operation_id=request.provider_operation_id,
                readback_evidence=request.readback_evidence,
            )
        except (ValueError, CommandConflict, CommandNotFound) as exc:
            raise ApplicationError(
                str(exc),
                non_retryable=True,
                type="CommandTransitionRejected",
            ) from exc
        return ActivityResult(
            status=operation.state,
            detail=request.reason,
            provider_operation_id=operation.provider_operation_id,
            readback_evidence=operation.readback_evidence,
        )

    def _adapter(self, request: CommandExecutionRequest) -> ProviderAdapter:
        if request.target == "odoo-19" and self.odoo is not None:
            return self.odoo
        if request.target == "klyrow-alert-email" and self.klyrow_alert is not None:
            return self.klyrow_alert
        if request.target == "telnexa-sms" and self.telnexa_sms is not None:
            return self.telnexa_sms
        if request.target == "klyrow-email" and self.klyrow_email is not None:
            return self.klyrow_email
        if request.target == "postly-social" and self.postly_social is not None:
            return self.postly_social
        raise ApplicationError(
            "no production provider adapter is activated for this command",
            non_retryable=True,
            type="CapabilityDisabled",
        )

    @activity.defn(name="execute_command")
    async def execute_command(
        self,
        request: CommandExecutionRequest,
    ) -> ActivityResult:
        adapter = self._adapter(request)
        try:
            return await adapter.execute(request)
        except PostlySocialUnknownOutcomeError as exc:
            # Postly has no idempotency key. Retrying an ambiguous publish
            # could put a second post on a real account, so this outcome must
            # reach an operator instead of the retry policy.
            raise ApplicationError(
                str(exc),
                non_retryable=True,
                type="ProviderOutcomeUnknown",
            ) from exc
        except (
            OdooProviderAdapterError,
            KlyrowAlertAdapterError,
            TelnexaProviderAdapterError,
            KlyrowEmailAdapterError,
            PostlySocialAdapterError,
        ) as exc:
            raise ApplicationError(
                str(exc),
                type="ProviderAdapterError",
            ) from exc

    @activity.defn(name="readback_command")
    async def readback_command(
        self,
        request: CommandExecutionRequest,
    ) -> ActivityResult:
        adapter = self._adapter(request)
        try:
            return await adapter.readback(request)
        except (
            OdooProviderAdapterError,
            KlyrowAlertAdapterError,
            TelnexaProviderAdapterError,
            KlyrowEmailAdapterError,
            PostlySocialAdapterError,
        ) as exc:
            raise ApplicationError(
                str(exc),
                type="ProviderReadbackError",
            ) from exc

    @staticmethod
    def _validated_reconciliation_command(
        row: Mapping[str, Any],
        request: ReconciliationRequest,
        operation_id: UUID,
    ) -> tuple[CommandExecutionRequest, str]:
        durable = row["payload"]
        if isinstance(durable, str):
            try:
                durable = json.loads(durable)
            except ValueError as exc:
                raise ApplicationError(
                    "durable command payload is invalid JSON",
                    non_retryable=True,
                    type="ReconciliationRejected",
                ) from exc
        if not isinstance(durable, Mapping):
            raise ApplicationError(
                "durable command payload is malformed",
                non_retryable=True,
                type="ReconciliationRejected",
            )
        value = dict(durable)
        authenticated_client_id = value.pop(AUTHENTICATED_CLIENT_ID_KEY, None)
        if not isinstance(authenticated_client_id, str) or not authenticated_client_id:
            raise ApplicationError(
                "durable command is missing authenticated client provenance",
                non_retryable=True,
                type="ReconciliationRejected",
            )
        try:
            command = CommandEnvelope.model_validate(value)
        except Exception as exc:
            raise ApplicationError(
                "durable command does not match the canonical envelope",
                non_retryable=True,
                type="ReconciliationRejected",
            ) from exc
        if command.command_id != operation_id or command.tenant_id != request.tenant_id:
            raise ApplicationError(
                "reconciliation identity does not match the durable command",
                non_retryable=True,
                type="ReconciliationRejected",
            )

        expected_digest = authenticated_command_digest(
            command,
            authenticated_client_id,
        )
        persisted_digest = row.get("payload_sha256")
        if (
            not isinstance(persisted_digest, str)
            or not hmac.compare_digest(persisted_digest, expected_digest)
        ):
            raise ApplicationError(
                "durable command payload digest does not match submission evidence",
                non_retryable=True,
                type="ReconciliationRejected",
            )

        return (
            CommandExecutionRequest(
                **command.model_dump(mode="json"),
                authenticated_client_id=authenticated_client_id,
            ),
            expected_digest,
        )

    async def _load_reconciliation_command(
        self,
        request: ReconciliationRequest,
    ) -> tuple[CommandExecutionRequest | None, ActivityResult | None, str | None]:
        try:
            operation_id = UUID(request.operation_id)
        except ValueError as exc:
            raise ApplicationError(
                "reconciliation operation_id is invalid",
                non_retryable=True,
                type="ReconciliationRejected",
            ) from exc

        async with self.store.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM middleware_commands
                WHERE tenant_id=$1 AND command_id=$2
                """,
                request.tenant_id,
                str(operation_id),
            )
        if row is None:
            raise ApplicationError(
                "reconciliation operation was not found",
                non_retryable=True,
                type="ReconciliationRejected",
            )
        if row["state"] == "completed":
            operation = await self.store.get(request.tenant_id, operation_id)
            return None, ActivityResult(
                status="completed",
                detail="provider read-back was already durably reconciled",
                provider_operation_id=operation.provider_operation_id,
                readback_evidence=operation.readback_evidence,
            ), None
        if row["state"] != "reconciliation_required":
            raise ApplicationError(
                "operation is not awaiting reconciliation",
                non_retryable=True,
                type="ReconciliationRejected",
            )

        command, payload_digest = self._validated_reconciliation_command(
            row,
            request,
            operation_id,
        )
        return command, None, payload_digest

    async def _persist_reconciliation_result(
        self,
        request: ReconciliationRequest,
        result: ActivityResult,
        expected_payload_sha256: str,
    ) -> ActivityResult:
        if result.status not in {"matched", "mismatch"}:
            raise ApplicationError(
                "provider read-back returned an unsupported status",
                non_retryable=True,
                type="ProviderReadbackContractError",
            )
        safe_detail = result.detail[:2048]
        provider_operation_id = result.provider_operation_id or request.operation_id
        evidence = result.readback_evidence or {
            "schema_version": "1.0",
            "status": result.status,
            "provider_operation_id": provider_operation_id,
        }
        evidence_digest = provider_evidence_digest(evidence)
        actor = "temporal:codestra.reconciliation.v1"
        matched = result.status == "matched"

        async with self.store.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    """
                    SELECT * FROM middleware_commands
                    WHERE tenant_id=$1 AND command_id=$2
                    FOR UPDATE
                    """,
                    request.tenant_id,
                    request.operation_id,
                )
                if current is None:
                    raise ApplicationError(
                        "reconciliation operation was not found",
                        non_retryable=True,
                        type="ReconciliationRejected",
                    )
                if current["state"] == "completed" and matched:
                    return ActivityResult(
                        status="completed",
                        detail="provider read-back was already durably reconciled",
                        provider_operation_id=current["provider_operation_id"],
                        readback_evidence=evidence,
                    )
                if current["state"] != "reconciliation_required":
                    raise ApplicationError(
                        "operation is no longer awaiting reconciliation",
                        non_retryable=True,
                        type="ReconciliationRejected",
                    )

                try:
                    operation_id = UUID(request.operation_id)
                except ValueError as exc:
                    raise ApplicationError(
                        "reconciliation operation_id is invalid",
                        non_retryable=True,
                        type="ReconciliationRejected",
                    ) from exc
                _, current_payload_sha256 = self._validated_reconciliation_command(
                    current,
                    request,
                    operation_id,
                )
                if not hmac.compare_digest(
                    current_payload_sha256,
                    expected_payload_sha256,
                ):
                    raise ApplicationError(
                        "durable command changed during provider reconciliation",
                        non_retryable=True,
                        type="ReconciliationRejected",
                    )

                next_state = "completed" if matched else "reconciliation_required"
                if matched:
                    row = await conn.fetchrow(
                        """
                        UPDATE middleware_commands
                        SET state='completed',
                            provider_operation_id=COALESCE($3, provider_operation_id),
                            last_error=NULL,
                            completed_at=now(),
                            updated_at=now(),
                            resource_version=resource_version+1
                        WHERE tenant_id=$1 AND command_id=$2
                        RETURNING *
                        """,
                        request.tenant_id,
                        request.operation_id,
                        provider_operation_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE middleware_commands
                        SET provider_operation_id=COALESCE($3, provider_operation_id),
                            last_error=$4,
                            reconciliation_reason=$4,
                            updated_at=now(),
                            resource_version=resource_version+1
                        WHERE tenant_id=$1 AND command_id=$2
                        RETURNING *
                        """,
                        request.tenant_id,
                        request.operation_id,
                        provider_operation_id,
                        safe_detail,
                    )
                assert row is not None
                metadata = json.dumps(
                    {
                        "provider_operation_id": provider_operation_id,
                        "readback_evidence_sha256": evidence_digest,
                        "reconciliation_status": result.status,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                await conn.execute(
                    """
                    INSERT INTO middleware_command_audit (
                        tenant_id, command_id, previous_state, new_state,
                        actor_id, reason, metadata
                    ) VALUES ($1,$2,'reconciliation_required',$3,$4,$5,$6::jsonb)
                    """,
                    request.tenant_id,
                    request.operation_id,
                    next_state,
                    actor,
                    safe_detail,
                    metadata,
                )
                await conn.execute(
                    """
                    UPDATE middleware_command_attempts
                    SET state=$3,
                        provider_operation_id=COALESCE($4, provider_operation_id),
                        result_payload=$5::jsonb,
                        error_code=CASE
                            WHEN $3='reconciliation_required'
                            THEN 'provider_readback_mismatch' ELSE NULL
                        END,
                        error_detail=CASE
                            WHEN $3='reconciliation_required' THEN $6 ELSE NULL
                        END,
                        finished_at=now()
                    WHERE id=(
                        SELECT id FROM middleware_command_attempts
                        WHERE tenant_id=$1 AND command_id=$2
                        ORDER BY attempt_number DESC LIMIT 1
                    )
                    """,
                    request.tenant_id,
                    request.operation_id,
                    next_state,
                    provider_operation_id,
                    json.dumps(evidence, separators=(",", ":"), sort_keys=True),
                    safe_detail,
                )
        return ActivityResult(
            status=next_state,
            detail=safe_detail,
            provider_operation_id=provider_operation_id,
            readback_evidence=evidence,
        )

    @activity.defn(name="reconcile_operation")
    async def reconcile_operation(
        self,
        request: ReconciliationRequest,
    ) -> ActivityResult:
        command, completed, payload_sha256 = await self._load_reconciliation_command(
            request
        )
        if completed is not None:
            return completed
        assert command is not None
        assert payload_sha256 is not None
        result = await self.readback_command(command)
        return await self._persist_reconciliation_result(
            request,
            result,
            payload_sha256,
        )

    def registered(self) -> tuple[Any, ...]:
        return (
            self.record_command_transition,
            self.execute_command,
            self.readback_command,
            self.reconcile_operation,
        )
