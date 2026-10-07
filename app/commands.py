from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_contracts import validate_contract
from .provider_canary import provider_evidence_digest

ROOT = Path(__file__).resolve().parents[1]
TEMPORAL_COMMAND_DESTINATION = "temporal-command"
ODOO_COMMAND_DESTINATION = "odoo-command"
AUTHENTICATED_CLIENT_ID_KEY = "_authenticated_client_id"
CommandState = Literal[
    "persisted",
    "queued",
    "dispatching",
    "accepted",
    "readback_pending",
    "completed",
    "failed",
    "reconciliation_required",
    "dead_lettered",
    "cancelled",
]
API_OPERATION_STATES = {
    "persisted": "RECEIVED", "queued": "QUEUED", "dispatching": "SUBMITTED",
    "accepted": "ACCEPTED", "readback_pending": "UNKNOWN", "completed": "COMPLETED",
    "failed": "FAILED", "reconciliation_required": "RECONCILIATION_REQUIRED",
    "dead_lettered": "DEAD_LETTERED", "cancelled": "CANCELLED",
}


class OperationEvent(BaseModel):
    event_id: int
    operation_id: UUID
    previous_state: str | None
    new_state: str
    actor_id: str
    reason: str
    safe_metadata: dict[str, Any]
    created_at: datetime


class OperationAttempt(BaseModel):
    attempt_id: int
    operation_id: UUID
    attempt_number: int
    state: str
    provider_operation_id: str | None = None
    safe_error_code: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
ALLOWED_COMMAND_TRANSITIONS: dict[str, set[str]] = {
    "persisted": {"queued", "cancelled"},
    "queued": {"dispatching", "dead_lettered", "cancelled"},
    "dispatching": {"accepted", "failed", "reconciliation_required"},
    "accepted": {"readback_pending", "reconciliation_required"},
    "readback_pending": {"completed", "failed", "reconciliation_required"},
    "reconciliation_required": {"queued", "dead_lettered"},
    "failed": {"queued", "dead_lettered"},
    "completed": set(),
    "dead_lettered": set(),
    "cancelled": set(),
}


class CommandError(RuntimeError):
    status_code = 400
    code = "command_invalid"
    retryable = False


class CommandCapabilityDisabled(CommandError):
    status_code = 403
    code = "capability_disabled"


class CommandConflict(CommandError):
    status_code = 409
    code = "command_conflict"


class CommandNotFound(CommandError):
    status_code = 404
    code = "command_not_found"


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    command_type: str = Field(
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$",
        max_length=180,
    )
    command_version: Literal["1.0"]
    target: str = Field(
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=100,
    )
    tenant_id: str = Field(min_length=1, max_length=128)
    requested_by: str = Field(min_length=1, max_length=300)
    correlation_id: str = Field(min_length=1, max_length=180)
    idempotency_key: str = Field(min_length=8, max_length=180)
    capability: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,100}$")
    payload: dict[str, Any]

    @field_validator("payload")
    @classmethod
    def bound_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 262_144:
            raise ValueError("command payload exceeds 262144 bytes")
        return value

    @model_validator(mode="after")
    def enforce_canonical_contract(self) -> "CommandEnvelope":
        validate_contract("command", self.model_dump(mode="json"))
        return self


class CommandOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    tenant_id: str
    command_type: str
    command_version: str
    target: str
    requested_by: str
    correlation_id: str
    idempotency_key: str
    capability: str
    state: CommandState
    provider_operation_id: str | None = None
    readback_evidence: dict[str, Any] | None = None
    readback_evidence_sha256: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    resource_version: int = 1
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    reconciliation_requested_at: datetime | None = None
    reconciliation_reason: str | None = None
    duplicate: bool = False

class OperationMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.: -]*$")


def decode_readback_evidence(value: object) -> dict[str, Any] | None:
    """Normalize asyncpg's default jsonb text codec into a JSON object."""

    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise RuntimeError("persisted read-back evidence is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("persisted read-back evidence must be a JSON object")
    return dict(value)


def verify_readback_evidence_digest(
    value: object,
    persisted_digest: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    evidence = decode_readback_evidence(value)
    if evidence is None:
        if persisted_digest is not None:
            raise RuntimeError("read-back evidence digest has no evidence payload")
        return None, None
    if persisted_digest is None:
        return evidence, None
    if provider_evidence_digest(evidence) != persisted_digest:
        raise RuntimeError("persisted read-back evidence digest does not match payload")
    return evidence, persisted_digest


@dataclass(frozen=True)
class CommandPolicy:
    prefix: str
    target: str
    capability: str
    readback_required: bool


class CommandPolicyRegistry:
    def __init__(
        self,
        policies: tuple[CommandPolicy, ...],
        capabilities: Mapping[str, bool],
    ) -> None:
        self.policies = tuple(
            sorted(policies, key=lambda item: len(item.prefix), reverse=True)
        )
        self.capabilities = dict(capabilities)

    @classmethod
    def load(cls) -> "CommandPolicyRegistry":
        command_path = ROOT / "connectors" / "generated" / "command-registry.v1.json"
        capability_path = ROOT / "config" / "capabilities.v2.json"
        raw_commands = json.loads(command_path.read_text(encoding="utf-8"))
        raw_capabilities = json.loads(capability_path.read_text(encoding="utf-8"))
        policies = tuple(
            CommandPolicy(
                prefix=item["prefix"],
                target=item["connector_id"],
                capability=item["required_capability"],
                readback_required=item["readback_required"] is True,
            )
            for item in raw_commands["commands"]
        )
        capabilities = raw_capabilities["capabilities"]
        if not isinstance(capabilities, dict) or not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in capabilities.items()
        ):
            raise ValueError("capability registry must contain boolean values")
        return cls(policies, capabilities)

    def authorize(self, command: CommandEnvelope) -> CommandPolicy:
        matching = [
            policy
            for policy in self.policies
            if command.command_type.startswith(policy.prefix)
        ]
        if len(matching) != 1:
            raise CommandCapabilityDisabled(
                "command type does not have exactly one owning policy"
            )
        policy = matching[0]
        if command.target != policy.target:
            raise CommandCapabilityDisabled(
                "command target does not own the command type"
            )
        if command.capability != policy.capability:
            raise CommandCapabilityDisabled(
                "command capability does not match the owning policy"
            )
        if self.capabilities.get(policy.capability) is not True:
            raise CommandCapabilityDisabled(
                f"capability {policy.capability} is disabled"
            )
        if not policy.readback_required:
            raise CommandCapabilityDisabled(
                "effectful command policy must require provider read-back"
            )
        return policy


def command_digest(command: CommandEnvelope) -> str:
    canonical = json.dumps(
        command.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def authenticated_command_payload(
    command: CommandEnvelope,
    authenticated_client_id: str,
) -> dict[str, Any]:
    """Build the internal durable envelope without changing the public command."""

    payload = command.model_dump(mode="json")
    payload[AUTHENTICATED_CLIENT_ID_KEY] = authenticated_client_id
    return payload


def authenticated_command_digest(
    command: CommandEnvelope,
    authenticated_client_id: str,
) -> str:
    canonical = json.dumps(
        authenticated_command_payload(command, authenticated_client_id),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def command_digest_matches(
    persisted_digest: str,
    command: CommandEnvelope,
    authenticated_client_id: str,
) -> bool:
    """Accept current provenance-bound digests and pre-provenance legacy rows.

    A legacy match preserves retry idempotency only. Its durable payload remains
    unchanged and therefore still lacks authenticated provenance, so execution
    continues to fail closed at the provider adapter.
    """

    return persisted_digest in {
        authenticated_command_digest(command, authenticated_client_id),
        command_digest(command),
    }


_SENSITIVE_METADATA_PARTS = ("authorization", "token", "password", "secret", "credential", "private_key", "api_key", "access_token", "refresh_token")


def redact_metadata(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: return {}
    if not isinstance(value, Mapping): return {}
    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_METADATA_PARTS) else clean(child) for key, child in item.items()}
        if isinstance(item, list): return [clean(child) for child in item]
        return item
    return dict(clean(value))


class CommandStore(Protocol):
    async def submit(
        self,
        command: CommandEnvelope,
        *,
        authenticated_client_id: str,
    ) -> CommandOperation:
        ...

    async def get(self, tenant_id: str, command_id: UUID) -> CommandOperation:
        ...

    async def list_operations(self, tenant_id: str, *, limit: int, position: tuple[datetime, UUID] | None = None, state: str | None = None, command_type: str | None = None) -> list[CommandOperation]: ...
    async def list_events(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[datetime, int] | None = None) -> list[OperationEvent]: ...
    async def list_attempts(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[int, int] | None = None) -> list[OperationAttempt]: ...
    async def mutate_operation(self, tenant_id: str, command_id: UUID, *, action: Literal["cancel", "reconcile", "retry"], actor_id: str, idempotency_key: str, expected_version: int, reason: str) -> CommandOperation: ...

    async def ready(self) -> bool:
        ...

    async def transition(
        self,
        tenant_id: str,
        command_id: UUID,
        *,
        new_state: CommandState,
        actor_id: str,
        reason: str,
        provider_operation_id: str | None = None,
        readback_evidence: Mapping[str, Any] | None = None,
    ) -> CommandOperation:
        ...

    async def close(self) -> None:
        ...


class MemoryCommandStore:
    def __init__(self) -> None:
        self._commands: dict[tuple[str, UUID], tuple[str, CommandOperation]] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, CommandOperation]] = {}
        self._events: dict[tuple[str, UUID], list[OperationEvent]] = {}
        self._attempts: dict[tuple[str, UUID], list[OperationAttempt]] = {}
        self._mutations: dict[tuple[str, UUID, str, str, str], tuple[str, CommandOperation]] = {}

    async def submit(
        self,
        command: CommandEnvelope,
        *,
        authenticated_client_id: str,
    ) -> CommandOperation:
        digest = authenticated_command_digest(command, authenticated_client_id)
        command_key = (command.tenant_id, command.command_id)
        idempotency_key = (command.tenant_id, command.idempotency_key)
        existing_by_id = self._commands.get(command_key)
        existing_by_idempotency = self._idempotency.get(idempotency_key)
        if (
            existing_by_id is not None
            and existing_by_idempotency is not None
            and existing_by_id[1].command_id != existing_by_idempotency[1].command_id
        ):
            raise CommandConflict(
                "command and idempotency identities refer to different operations"
            )
        existing = existing_by_id or existing_by_idempotency
        if existing is not None:
            if not command_digest_matches(
                existing[0], command, authenticated_client_id
            ):
                raise CommandConflict(
                    "command identity was reused with different content"
                )
            return existing[1].model_copy(update={"duplicate": True})

        now = datetime.now().astimezone()
        operation = CommandOperation(
            **command.model_dump(exclude={"payload"}),
            state="persisted",
            created_at=now,
            updated_at=now,
        )
        entry = (digest, operation)
        self._commands[command_key] = entry
        self._idempotency[idempotency_key] = entry
        self._events[command_key] = [OperationEvent(event_id=1, operation_id=command.command_id, previous_state=None, new_state="persisted", actor_id=command.requested_by, reason="validated command and persisted delivery intent", safe_metadata={"authenticated_client_id": authenticated_client_id}, created_at=now)]
        self._attempts[command_key] = []
        return operation

    async def get(self, tenant_id: str, command_id: UUID) -> CommandOperation:
        entry = self._commands.get((tenant_id, command_id))
        if entry is None:
            raise CommandNotFound("command operation was not found")
        return entry[1]

    async def list_operations(self, tenant_id: str, *, limit: int, position: tuple[datetime, UUID] | None = None, state: str | None = None, command_type: str | None = None) -> list[CommandOperation]:
        rows = [entry[1] for (row_tenant, _), entry in self._commands.items() if row_tenant == tenant_id]
        if state is not None: rows = [row for row in rows if row.state == state]
        if command_type is not None: rows = [row for row in rows if row.command_type == command_type]
        rows.sort(key=lambda row: (row.created_at, row.command_id.int), reverse=True)
        if position is not None: rows = [row for row in rows if (row.created_at, row.command_id.int) < (position[0], position[1].int)]
        return rows[:limit]

    async def list_events(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[datetime, int] | None = None) -> list[OperationEvent]:
        await self.get(tenant_id, command_id)
        rows = list(self._events.get((tenant_id, command_id), []))
        if position is not None: rows = [row for row in rows if (row.created_at, row.event_id) > position]
        return [row.model_copy(update={"safe_metadata": redact_metadata(row.safe_metadata)}) for row in rows[:limit]]

    async def list_attempts(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[int, int] | None = None) -> list[OperationAttempt]:
        await self.get(tenant_id, command_id)
        rows = list(self._attempts.get((tenant_id, command_id), []))
        if position is not None: rows = [row for row in rows if (row.attempt_number, row.attempt_id) > position]
        return rows[:limit]

    async def mutate_operation(self, tenant_id: str, command_id: UUID, *, action: Literal["cancel", "reconcile", "retry"], actor_id: str, idempotency_key: str, expected_version: int, reason: str) -> CommandOperation:
        key = (tenant_id, command_id)
        entry = self._commands.get(key)
        if entry is None: raise CommandNotFound("command operation was not found")
        request_digest = hashlib.sha256(json.dumps({"expected_version": expected_version, "reason": reason}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mutation_key = (tenant_id, command_id, action, actor_id, idempotency_key)
        replay = self._mutations.get(mutation_key)
        if replay:
            if replay[0] != request_digest: raise CommandConflict("idempotency key was reused with different mutation content")
            return replay[1].model_copy(update={"duplicate": True})
        digest, operation = entry
        if operation.resource_version != expected_version: raise CommandConflict("expected_version is stale")
        if action == "cancel":
            if operation.state in {"completed", "failed", "reconciliation_required", "dead_lettered"}: raise CommandConflict("operation is not cancellable")
            state = "cancelled" if operation.state in {"persisted", "queued"} else "reconciliation_required"
            updates = {"state": state, "cancelled_at": datetime.now().astimezone() if state == "cancelled" else None, "cancellation_reason": reason}
        elif action == "reconcile":
            if operation.state not in {"dispatching", "accepted", "readback_pending", "reconciliation_required"}: raise CommandConflict("operation is not reconcilable")
            updates = {"state": "reconciliation_required", "reconciliation_requested_at": datetime.now().astimezone(), "reconciliation_reason": reason}
        else:
            if operation.state != "failed": raise CommandConflict("operation is not safely retryable")
            updates = {"state": "queued", "last_error": None}
        now = datetime.now().astimezone()
        updated = operation.model_copy(update={**updates, "resource_version": operation.resource_version + 1, "updated_at": now})
        self._commands[key] = (digest, updated)
        events = self._events[key]
        events.append(OperationEvent(event_id=len(events) + 1, operation_id=command_id, previous_state=operation.state, new_state=updated.state, actor_id=actor_id, reason=reason, safe_metadata={"action": action, "resource_version": updated.resource_version}, created_at=now))
        self._mutations[mutation_key] = (request_digest, updated)
        return updated

    async def ready(self) -> bool:
        return True

    async def transition(
        self,
        tenant_id: str,
        command_id: UUID,
        *,
        new_state: CommandState,
        actor_id: str,
        reason: str,
        provider_operation_id: str | None = None,
        readback_evidence: Mapping[str, Any] | None = None,
    ) -> CommandOperation:
        if readback_evidence is not None and new_state != "completed":
            raise CommandConflict("read-back evidence may be persisted only on completion")
        key = (tenant_id, command_id)
        entry = self._commands.get(key)
        if entry is None:
            raise CommandNotFound("command operation was not found")
        digest, operation = entry
        if new_state not in ALLOWED_COMMAND_TRANSITIONS[operation.state]:
            raise CommandConflict(
                f"invalid command transition {operation.state} -> {new_state}"
            )
        now = datetime.now().astimezone()
        updated = operation.model_copy(
            update={
                "state": new_state,
                "provider_operation_id": (
                    provider_operation_id or operation.provider_operation_id
                ),
                "readback_evidence": (
                    dict(readback_evidence)
                    if readback_evidence is not None
                    else operation.readback_evidence
                ),
                "readback_evidence_sha256": (
                    provider_evidence_digest(readback_evidence)
                    if readback_evidence is not None
                    else operation.readback_evidence_sha256
                ),
                "last_error": reason if new_state in {"failed", "reconciliation_required"} else None,
                "updated_at": now,
            }
        )
        self._commands[key] = (digest, updated)
        self._idempotency[(tenant_id, updated.idempotency_key)] = (digest, updated)
        events = self._events[key]
        events.append(OperationEvent(event_id=len(events) + 1, operation_id=command_id, previous_state=operation.state, new_state=new_state, actor_id=actor_id, reason=reason[:2048], safe_metadata={"provider_operation_id": provider_operation_id}, created_at=now))
        attempts = self._attempts[key]
        if new_state == "dispatching":
            attempts.append(OperationAttempt(attempt_id=len(attempts) + 1, operation_id=command_id, attempt_number=len(attempts) + 1, state=new_state, provider_operation_id=provider_operation_id, started_at=now))
        elif attempts:
            attempts[-1] = attempts[-1].model_copy(update={"state": new_state, "provider_operation_id": provider_operation_id or attempts[-1].provider_operation_id, "safe_error_code": "operation_failed" if new_state in {"failed", "reconciliation_required"} else None, "finished_at": now if new_state in {"completed", "failed", "reconciliation_required"} else None})
        return updated

    async def close(self) -> None:
        return None


class PostgresCommandStore:
    REQUIRED_COLUMNS = {
        "middleware_commands": {
            "command_id",
            "tenant_id",
            "command_type",
            "command_version",
            "target",
            "requested_by",
            "correlation_id",
            "idempotency_key",
            "capability",
            "payload",
            "payload_sha256",
            "state",
            "provider_operation_id",
            "last_error",
            "created_at",
            "updated_at",
            "queued_at",
            "accepted_at",
            "completed_at",
            "failed_at",
            "resource_version", "cancelled_at", "cancellation_reason",
            "reconciliation_requested_at", "reconciliation_reason",
        },
        "middleware_command_attempts": {
            "id",
            "tenant_id",
            "command_id",
            "attempt_number",
            "state",
            "provider_operation_id",
            "result_payload",
            "error_code",
            "error_detail",
            "started_at",
            "finished_at",
        },
        "middleware_command_audit": {
            "id",
            "tenant_id",
            "command_id",
            "previous_state",
            "new_state",
            "actor_id",
            "reason",
            "metadata",
            "created_at",
        },
        "middleware_operation_mutations": {"id", "tenant_id", "command_id", "action", "actor_id", "idempotency_key", "request_sha256", "response_status", "response_payload", "created_at"},
    }
    REQUIRED_KEYS = {
        ("middleware_commands", "PRIMARY KEY", ("tenant_id", "command_id")),
        (
            "middleware_commands",
            "UNIQUE",
            ("tenant_id", "idempotency_key"),
        ),
        ("middleware_command_attempts", "PRIMARY KEY", ("id",)),
        (
            "middleware_command_attempts",
            "UNIQUE",
            ("tenant_id", "command_id", "attempt_number"),
        ),
        ("middleware_command_audit", "PRIMARY KEY", ("id",)),
        ("middleware_operation_mutations", "PRIMARY KEY", ("id",)),
        ("middleware_operation_mutations", "UNIQUE", ("tenant_id", "command_id", "action", "actor_id", "idempotency_key")),
    }
    REQUIRED_TRIGGERS = {"middleware_command_audit_immutable", "middleware_operation_mutations_immutable"}

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "PostgresCommandStore":
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=10,
        )
        store = cls(pool)
        try:
            if not await store.ready():
                raise RuntimeError("command ledger schema is not ready")
        except Exception:
            await pool.close()
            raise
        return store

    @staticmethod
    def _operation(
        row: asyncpg.Record,
        *,
        duplicate: bool = False,
        readback_evidence: object = None,
        readback_evidence_sha256: str | None = None,
    ) -> CommandOperation:
        normalized_evidence, persisted_digest = verify_readback_evidence_digest(
            readback_evidence,
            readback_evidence_sha256,
        )
        return CommandOperation(
            command_id=row["command_id"],
            tenant_id=row["tenant_id"],
            command_type=row["command_type"],
            command_version=row["command_version"],
            target=row["target"],
            requested_by=row["requested_by"],
            correlation_id=row["correlation_id"],
            idempotency_key=row["idempotency_key"],
            capability=row["capability"],
            state=row["state"],
            provider_operation_id=row["provider_operation_id"],
            readback_evidence=normalized_evidence,
            readback_evidence_sha256=persisted_digest,
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resource_version=row["resource_version"],
            cancelled_at=row["cancelled_at"],
            cancellation_reason=row["cancellation_reason"],
            reconciliation_requested_at=row["reconciliation_requested_at"],
            reconciliation_reason=row["reconciliation_reason"],
            duplicate=duplicate,
        )

    async def submit(
        self,
        command: CommandEnvelope,
        *,
        authenticated_client_id: str,
    ) -> CommandOperation:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                return await self.submit_on_connection(
                    conn,
                    command,
                    authenticated_client_id=authenticated_client_id,
                )

    async def submit_on_connection(
        self,
        conn: asyncpg.Connection,
        command: CommandEnvelope,
        *,
        authenticated_client_id: str,
        next_attempt_at: datetime | None = None,
    ) -> CommandOperation:
        """Persist a command using the caller's transaction.

        Incident ingestion uses this primitive so the incident projection,
        timeline, audit row, command, and notification outbox intent commit or
        roll back together.
        """

        digest = authenticated_command_digest(command, authenticated_client_id)
        payload = authenticated_command_payload(command, authenticated_client_id)
        row = await conn.fetchrow(
            """
            INSERT INTO middleware_commands (
                command_id, tenant_id, command_type, command_version,
                target, requested_by, correlation_id, idempotency_key,
                capability, payload, payload_sha256, state
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,'persisted')
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            str(command.command_id),
            command.tenant_id,
            command.command_type,
            command.command_version,
            command.target,
            command.requested_by,
            command.correlation_id,
            command.idempotency_key,
            command.capability,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            digest,
        )
        if row is not None:
            await conn.execute(
                """
                INSERT INTO middleware_command_audit (
                    tenant_id, command_id, previous_state, new_state,
                    actor_id, reason, metadata
                ) VALUES ($1,$2,NULL,'persisted',$3,$4,$5::jsonb)
                """,
                command.tenant_id,
                str(command.command_id),
                command.requested_by,
                "validated command and persisted delivery intent",
                json.dumps(
                    {"authenticated_client_id": authenticated_client_id},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            await conn.execute(
                """
                INSERT INTO middleware_outbox (
                    tenant_id, command_id, destination, event_type, payload,
                    idempotency_key, next_attempt_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,COALESCE($7,now()))
                """,
                command.tenant_id,
                str(command.command_id),
                TEMPORAL_COMMAND_DESTINATION,
                command.command_type,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                command.idempotency_key,
                next_attempt_at,
            )
            return self._operation(row)

        existing_rows = await conn.fetch(
            """
            SELECT * FROM middleware_commands
            WHERE (tenant_id=$1 AND command_id=$2)
               OR (tenant_id=$1 AND idempotency_key=$3)
            ORDER BY created_at ASC
            """,
            command.tenant_id,
            str(command.command_id),
            command.idempotency_key,
        )
        if not existing_rows:
            raise CommandConflict("command conflict could not be reconciled")
        identities = {
            (item["command_id"], item["idempotency_key"])
            for item in existing_rows
        }
        if len(identities) != 1 or not command_digest_matches(
            existing_rows[0]["payload_sha256"],
            command,
            authenticated_client_id,
        ):
            raise CommandConflict(
                "command identity was reused with different content"
            )
        return self._operation(existing_rows[0], duplicate=True)

    async def get(self, tenant_id: str, command_id: UUID) -> CommandOperation:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM middleware_commands WHERE tenant_id=$1 AND command_id=$2",
                tenant_id,
                str(command_id),
            )
            persisted = await conn.fetchrow(
                """
                SELECT
                    (
                        SELECT result_payload FROM middleware_command_attempts
                        WHERE tenant_id=$1 AND command_id=$2
                        ORDER BY attempt_number DESC LIMIT 1
                    ) AS result_payload,
                    (
                        SELECT metadata->>'readback_evidence_sha256'
                        FROM middleware_command_audit
                        WHERE tenant_id=$1 AND command_id=$2
                          AND new_state='completed'
                        ORDER BY id DESC LIMIT 1
                    ) AS readback_evidence_sha256
                """,
                tenant_id,
                str(command_id),
            )
        if row is None:
            raise CommandNotFound("command operation was not found")
        assert persisted is not None
        return self._operation(
            row,
            readback_evidence=persisted["result_payload"],
            readback_evidence_sha256=persisted["readback_evidence_sha256"],
        )

    async def list_operations(self, tenant_id: str, *, limit: int, position: tuple[datetime, UUID] | None = None, state: str | None = None, command_type: str | None = None) -> list[CommandOperation]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM middleware_commands WHERE tenant_id=$1
                   AND ($2::text IS NULL OR state=$2)
                   AND ($3::text IS NULL OR command_type=$3)
                   AND ($4::timestamptz IS NULL OR (created_at, command_id) < ($4, $5))
                   ORDER BY created_at DESC, command_id DESC LIMIT $6""",
                tenant_id, state, command_type, position[0] if position else None,
                str(position[1]) if position else None, limit,
            )
        return [self._operation(row) for row in rows]

    async def list_events(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[datetime, int] | None = None) -> list[OperationEvent]:
        await self.get(tenant_id, command_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, command_id, previous_state, new_state, actor_id, reason, metadata, created_at
                   FROM middleware_command_audit WHERE tenant_id=$1 AND command_id=$2
                     AND ($3::timestamptz IS NULL OR (created_at, id) > ($3, $4))
                   ORDER BY created_at ASC, id ASC LIMIT $5""",
                tenant_id, str(command_id), position[0] if position else None, position[1] if position else None, limit,
            )
        return [OperationEvent(event_id=row["id"], operation_id=row["command_id"], previous_state=row["previous_state"], new_state=row["new_state"], actor_id=row["actor_id"], reason=row["reason"], safe_metadata=redact_metadata(row["metadata"]), created_at=row["created_at"]) for row in rows]

    async def list_attempts(self, tenant_id: str, command_id: UUID, *, limit: int, position: tuple[int, int] | None = None) -> list[OperationAttempt]:
        await self.get(tenant_id, command_id)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, command_id, attempt_number, state, provider_operation_id, error_code, started_at, finished_at
                   FROM middleware_command_attempts WHERE tenant_id=$1 AND command_id=$2
                     AND ($3::integer IS NULL OR (attempt_number, id) > ($3, $4))
                   ORDER BY attempt_number ASC, id ASC LIMIT $5""",
                tenant_id, str(command_id), position[0] if position else None, position[1] if position else None, limit,
            )
        return [OperationAttempt(attempt_id=row["id"], operation_id=row["command_id"], attempt_number=row["attempt_number"], state=row["state"], provider_operation_id=row["provider_operation_id"], safe_error_code=row["error_code"], started_at=row["started_at"], finished_at=row["finished_at"]) for row in rows]

    async def mutate_operation(self, tenant_id: str, command_id: UUID, *, action: Literal["cancel", "reconcile", "retry"], actor_id: str, idempotency_key: str, expected_version: int, reason: str) -> CommandOperation:
        request_digest = hashlib.sha256(json.dumps({"expected_version": expected_version, "reason": reason}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow("SELECT * FROM middleware_commands WHERE tenant_id=$1 AND command_id=$2 FOR UPDATE", tenant_id, str(command_id))
                if current is None: raise CommandNotFound("command operation was not found")
                replay = await conn.fetchrow("""SELECT request_sha256, response_payload FROM middleware_operation_mutations
                    WHERE tenant_id=$1 AND command_id=$2 AND action=$3 AND actor_id=$4 AND idempotency_key=$5""", tenant_id, str(command_id), action, actor_id, idempotency_key)
                if replay:
                    if replay["request_sha256"] != request_digest: raise CommandConflict("idempotency key was reused with different mutation content")
                    payload = json.loads(replay["response_payload"]) if isinstance(replay["response_payload"], str) else dict(replay["response_payload"])
                    return CommandOperation.model_validate(payload).model_copy(update={"duplicate": True})
                if current["resource_version"] != expected_version: raise CommandConflict("expected_version is stale")
                previous = current["state"]
                new_state = previous
                if action == "cancel":
                    if previous in {"completed", "failed", "reconciliation_required", "dead_lettered", "cancelled"}: raise CommandConflict("operation is not cancellable")
                    if previous in {"persisted", "queued"}:
                        active_lease = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM middleware_outbox WHERE tenant_id=$1 AND command_id=$2 AND lease_owner IS NOT NULL AND lease_until > now() AND completed_at IS NULL)""", tenant_id, str(command_id))
                        new_state = "reconciliation_required" if active_lease else "cancelled"
                    else: new_state = "reconciliation_required"
                    row = await conn.fetchrow("""UPDATE middleware_commands SET state=$3, resource_version=resource_version+1,
                        cancelled_at=CASE WHEN $3='cancelled' THEN now() ELSE cancelled_at END,
                        cancellation_reason=$4, updated_at=now() WHERE tenant_id=$1 AND command_id=$2 RETURNING *""", tenant_id, str(command_id), new_state, reason)
                    if new_state == "cancelled":
                        await conn.execute("""UPDATE middleware_outbox SET cancelled_at=now(), lease_owner=NULL, lease_until=NULL
                            WHERE tenant_id=$1 AND command_id=$2 AND completed_at IS NULL AND lease_owner IS NULL""", tenant_id, str(command_id))
                elif action == "reconcile":
                    if previous not in {"dispatching", "accepted", "readback_pending", "reconciliation_required"}: raise CommandConflict("operation is not reconcilable")
                    new_state = "reconciliation_required"
                    row = await conn.fetchrow("""UPDATE middleware_commands SET state=$3, resource_version=resource_version+1,
                        reconciliation_requested_at=now(), reconciliation_reason=$4, updated_at=now()
                        WHERE tenant_id=$1 AND command_id=$2 RETURNING *""", tenant_id, str(command_id), new_state, reason)
                    work_key = "operation-reconcile:" + hashlib.sha256(f"{tenant_id}:{command_id}:{actor_id}:{idempotency_key}".encode()).hexdigest()
                    await conn.execute("""INSERT INTO middleware_outbox (tenant_id, command_id, destination, event_type, payload, idempotency_key)
                        VALUES ($1,$2,$3,'operation.reconcile.v1',$4::jsonb,$5) ON CONFLICT DO NOTHING""", tenant_id, str(command_id), TEMPORAL_COMMAND_DESTINATION, json.dumps({"command_id": str(command_id), "action": "reconcile", "reason": reason}), work_key)
                else:
                    if previous != "failed": raise CommandConflict("operation is not safely retryable")
                    new_state = "queued"
                    work_key = "operation-retry:" + hashlib.sha256(f"{tenant_id}:{command_id}:{actor_id}:{idempotency_key}".encode()).hexdigest()
                    retry_envelope = json.loads(current["payload"]) if isinstance(current["payload"], str) else dict(current["payload"])
                    retry_envelope["idempotency_key"] = work_key
                    row = await conn.fetchrow("""UPDATE middleware_commands SET state='queued', resource_version=resource_version+1,
                        last_error=NULL, queued_at=now(), updated_at=now() WHERE tenant_id=$1 AND command_id=$2 RETURNING *""", tenant_id, str(command_id))
                    await conn.execute("""INSERT INTO middleware_outbox (tenant_id, command_id, destination, event_type, payload, idempotency_key)
                        VALUES ($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT DO NOTHING""", tenant_id, str(command_id), TEMPORAL_COMMAND_DESTINATION, current["command_type"], json.dumps(retry_envelope), work_key)
                assert row is not None
                await conn.execute("""INSERT INTO middleware_command_audit (tenant_id, command_id, previous_state, new_state, actor_id, reason, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)""", tenant_id, str(command_id), previous, new_state, actor_id, reason, json.dumps({"action": action, "resource_version": row["resource_version"]}))
                operation = self._operation(row)
                payload = operation.model_dump(mode="json")
                payload["state"] = operation.state
                await conn.execute("""INSERT INTO middleware_operation_mutations (tenant_id, command_id, action, actor_id, idempotency_key, request_sha256, response_status, response_payload)
                    VALUES ($1,$2,$3,$4,$5,$6,200,$7::jsonb)""", tenant_id, str(command_id), action, actor_id, idempotency_key, request_digest, json.dumps(payload, separators=(",", ":"), sort_keys=True))
                return operation

    async def transition(
        self,
        tenant_id: str,
        command_id: UUID,
        *,
        new_state: CommandState,
        actor_id: str,
        reason: str,
        provider_operation_id: str | None = None,
        readback_evidence: Mapping[str, Any] | None = None,
    ) -> CommandOperation:
        if readback_evidence is not None and new_state != "completed":
            raise CommandConflict("read-back evidence may be persisted only on completion")
        safe_reason = reason[:2048]
        safe_readback = (
            dict(readback_evidence) if readback_evidence is not None else None
        )
        safe_readback_digest = (
            provider_evidence_digest(safe_readback)
            if safe_readback is not None
            else None
        )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    """
                    SELECT * FROM middleware_commands
                    WHERE tenant_id=$1 AND command_id=$2
                    FOR UPDATE
                    """,
                    tenant_id,
                    str(command_id),
                )
                if current is None:
                    raise CommandNotFound("command operation was not found")
                previous_state = current["state"]
                if new_state not in ALLOWED_COMMAND_TRANSITIONS[previous_state]:
                    raise CommandConflict(
                        f"invalid command transition {previous_state} -> {new_state}"
                    )
                row = await conn.fetchrow(
                    """
                    UPDATE middleware_commands
                    SET state=$3,
                        provider_operation_id=COALESCE($4, provider_operation_id),
                        last_error=CASE
                            WHEN $3 IN ('failed','reconciliation_required') THEN $5
                            ELSE NULL
                        END,
                        updated_at=now(),
                        queued_at=CASE WHEN $3='queued' THEN now() ELSE queued_at END,
                        accepted_at=CASE WHEN $3='accepted' THEN now() ELSE accepted_at END,
                        completed_at=CASE WHEN $3='completed' THEN now() ELSE completed_at END,
                        failed_at=CASE WHEN $3='failed' THEN now() ELSE failed_at END
                    WHERE tenant_id=$1 AND command_id=$2
                    RETURNING *
                    """,
                    tenant_id,
                    str(command_id),
                    new_state,
                    provider_operation_id,
                    safe_reason,
                )
                await conn.execute(
                    """
                    INSERT INTO middleware_command_audit (
                        tenant_id, command_id, previous_state, new_state,
                        actor_id, reason, metadata
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                    """,
                    tenant_id,
                    str(command_id),
                    previous_state,
                    new_state,
                    actor_id,
                    safe_reason,
                    json.dumps(
                        {
                            "provider_operation_id": provider_operation_id,
                            "readback_evidence_sha256": safe_readback_digest,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
                if new_state == "dispatching":
                    attempt_number = await conn.fetchval(
                        """
                        SELECT COALESCE(max(attempt_number), 0) + 1
                        FROM middleware_command_attempts
                        WHERE tenant_id=$1 AND command_id=$2
                        """,
                        tenant_id,
                        str(command_id),
                    )
                    await conn.execute(
                        """
                        INSERT INTO middleware_command_attempts (
                            tenant_id, command_id, attempt_number, state
                        ) VALUES ($1,$2,$3,'dispatching')
                        """,
                        tenant_id,
                        str(command_id),
                        attempt_number,
                    )
                elif new_state in {
                    "accepted",
                    "readback_pending",
                    "completed",
                    "failed",
                    "reconciliation_required",
                }:
                    await conn.execute(
                        """
                        UPDATE middleware_command_attempts
                        SET state=$3,
                            provider_operation_id=COALESCE($4, provider_operation_id),
                            result_payload=COALESCE($6::jsonb, result_payload),
                            error_detail=CASE
                                WHEN $3 IN ('failed','reconciliation_required') THEN $5
                                ELSE error_detail
                            END,
                            finished_at=CASE
                                WHEN $3 IN ('completed','failed','reconciliation_required')
                                THEN now() ELSE finished_at
                            END
                        WHERE id=(
                            SELECT id FROM middleware_command_attempts
                            WHERE tenant_id=$1 AND command_id=$2
                            ORDER BY attempt_number DESC LIMIT 1
                        )
                        """,
                        tenant_id,
                        str(command_id),
                        new_state,
                        provider_operation_id,
                        safe_reason,
                        (
                            json.dumps(
                                safe_readback,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            if safe_readback is not None
                            else None
                        ),
                    )
        assert row is not None
        if safe_readback is None:
            async with self.pool.acquire() as conn:
                persisted = await conn.fetchrow(
                    """
                    SELECT
                        (
                            SELECT result_payload FROM middleware_command_attempts
                            WHERE tenant_id=$1 AND command_id=$2
                            ORDER BY attempt_number DESC LIMIT 1
                        ) AS result_payload,
                        (
                            SELECT metadata->>'readback_evidence_sha256'
                            FROM middleware_command_audit
                            WHERE tenant_id=$1 AND command_id=$2
                              AND new_state='completed'
                            ORDER BY id DESC LIMIT 1
                        ) AS readback_evidence_sha256
                    """,
                    tenant_id,
                    str(command_id),
                )
            assert persisted is not None
            safe_readback = persisted["result_payload"]
            safe_readback_digest = persisted["readback_evidence_sha256"]
        return self._operation(
            row,
            readback_evidence=safe_readback,
            readback_evidence_sha256=safe_readback_digest,
        )

    async def ready(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                head = await conn.fetchval(
                    "SELECT max(version) FROM middleware_schema_migrations"
                )
                column_rows = await conn.fetch(
                    """
                    SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=ANY($1::text[])
                    """,
                    list(self.REQUIRED_COLUMNS),
                )
                key_rows = await conn.fetch(
                    """
                    SELECT tc.table_name,
                           tc.constraint_type,
                           array_agg(
                               kcu.column_name ORDER BY kcu.ordinal_position
                           ) AS columns
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_schema = kcu.constraint_schema
                     AND tc.constraint_name = kcu.constraint_name
                     AND tc.table_name = kcu.table_name
                    WHERE tc.table_schema='public'
                      AND tc.table_name=ANY($1::text[])
                      AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                    GROUP BY tc.table_name, tc.constraint_type, tc.constraint_name
                    """,
                    list(self.REQUIRED_COLUMNS),
                )
                trigger_rows = await conn.fetch(
                    """
                    SELECT tgname, tgenabled::text AS tgenabled FROM pg_trigger
                    WHERE NOT tgisinternal AND tgname=ANY($1::text[])
                    """,
                    list(self.REQUIRED_TRIGGERS),
                )
            if head != 9:
                return False
            observed_columns = {
                table: set() for table in self.REQUIRED_COLUMNS
            }
            for row in column_rows:
                observed_columns[row["table_name"]].add(row["column_name"])
            if any(
                required - observed_columns[table]
                for table, required in self.REQUIRED_COLUMNS.items()
            ):
                return False
            observed_keys = {
                (
                    row["table_name"],
                    row["constraint_type"],
                    tuple(row["columns"]),
                )
                for row in key_rows
            }
            enabled_triggers = {
                row["tgname"]
                for row in trigger_rows
                if row["tgenabled"] == "O"
            }
            return (
                self.REQUIRED_KEYS <= observed_keys
                and enabled_triggers == self.REQUIRED_TRIGGERS
            )
        except Exception:
            return False

    async def close(self) -> None:
        await self.pool.close()


@dataclass(frozen=True)
class CommandService:
    store: CommandStore
    policies: CommandPolicyRegistry

    async def submit(
        self,
        command: CommandEnvelope,
        *,
        authenticated_subject: str,
        authenticated_client_id: str,
    ) -> CommandOperation:
        self.validate_submission(
            command,
            authenticated_subject=authenticated_subject,
            authenticated_client_id=authenticated_client_id,
        )
        return await self.store.submit(
            command,
            authenticated_client_id=authenticated_client_id,
        )

    def validate_submission(
        self,
        command: CommandEnvelope,
        *,
        authenticated_subject: str,
        authenticated_client_id: str,
    ) -> None:
        if command.requested_by != authenticated_subject:
            raise CommandCapabilityDisabled(
                "requested_by must equal the authenticated token subject"
            )
        if not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*",
            authenticated_client_id,
        ) or len(authenticated_client_id) > 100:
            raise CommandCapabilityDisabled(
                "authenticated client ID is invalid"
            )
        self.policies.authorize(command)

    async def get(self, tenant_id: str, command_id: UUID) -> CommandOperation:
        return await self.store.get(tenant_id, command_id)

    async def list_operations(self, *args: Any, **kwargs: Any) -> list[CommandOperation]: return await self.store.list_operations(*args, **kwargs)
    async def list_events(self, *args: Any, **kwargs: Any) -> list[OperationEvent]: return await self.store.list_events(*args, **kwargs)
    async def list_attempts(self, *args: Any, **kwargs: Any) -> list[OperationAttempt]: return await self.store.list_attempts(*args, **kwargs)
    async def mutate_operation(self, *args: Any, **kwargs: Any) -> CommandOperation: return await self.store.mutate_operation(*args, **kwargs)
