from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    INT4RANGE,
    JSONB,
    ExcludeConstraint,
)
from sqlalchemy.dialects.postgresql import (
    UUID as PGUUID,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TelephonyCommandJournal(Base):
    __tablename__ = "telephony_command_journal"
    command_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    command_public_id: Mapped[str] = mapped_column(
        String(144), nullable=False, unique=True, default=lambda: f"CMD-{uuid4().hex}"
    )
    command_type: Mapped[str] = mapped_column(String(96), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_public_id: Mapped[str] = mapped_column(String(144), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    business_unit_public_id: Mapped[str] = mapped_column(String(144), nullable=False)
    campaign_public_id: Mapped[str] = mapped_column(String(144), nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    causation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_decision_id: Mapped[str] = mapped_column(String(144), nullable=False)
    policy_decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        CheckConstraint("aggregate_version >= 1", name="ck_telephony_command_version"),
        CheckConstraint(
            "environment IN ('staging','test','production')",
            name="ck_telephony_command_environment",
        ),
        UniqueConstraint(
            "environment",
            "aggregate_type",
            "aggregate_public_id",
            "aggregate_version",
            name="uq_telephony_command_aggregate_version",
        ),
        Index(
            "ix_telephony_command_aggregate",
            "aggregate_type",
            "aggregate_public_id",
            "aggregate_version",
        ),
        Index("ix_telephony_command_correlation", "correlation_id"),
    )


class TelephonyOperationJournal(Base):
    __tablename__ = "telephony_operation_journal"
    operation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    operation_public_id: Mapped[str] = mapped_column(
        String(144), nullable=False, unique=True, default=lambda: f"OPR-{uuid4().hex}"
    )
    command_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_command_journal.command_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_service_key: Mapped[str] = mapped_column(
        String(144), nullable=False, default="telephony-adapter"
    )
    adapter_operation_id: Mapped[str] = mapped_column(
        String(144), nullable=False, default=""
    )
    target_system: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    target_public_id: Mapped[str] = mapped_column(
        String(144), nullable=False, default=""
    )
    desired_state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    idempotency_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default=lambda: uuid4().hex
    )
    transition_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    endpoint_key: Mapped[str] = mapped_column(String(96), nullable=False)
    readback_endpoint_key: Mapped[str] = mapped_column(String(96), nullable=False)
    target_configuration_checksum: Mapped[str] = mapped_column(
        String(71), nullable=False
    )
    target_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    desired_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_hash: Mapped[str | None] = mapped_column(String(64))
    readback_matches: Mapped[bool | None] = mapped_column(Boolean)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "(readback_matches IS NOT TRUE) OR (actual_hash IS NOT NULL)",
            name="ck_telephony_operation_readback_hash",
        ),
        Index("ix_telephony_operation_correlation", "correlation_id"),
    )


class TelephonyOperationTransition(Base):
    __tablename__ = "telephony_operation_transition"
    transition_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    operation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_operation_journal.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_command_journal.command_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    transition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "sequence", name="uq_telephony_transition_sequence"
        ),
        UniqueConstraint(
            "operation_id",
            "transition_hash",
            name="uq_telephony_transition_hash",
        ),
    )


class TelephonyTerminalResult(Base):
    __tablename__ = "telephony_terminal_result"
    result_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    result_public_id: Mapped[str] = mapped_column(
        String(144), nullable=False, unique=True, default=lambda: f"RES-{uuid4().hex}"
    )
    operation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_operation_journal.operation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    command_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_command_journal.command_id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    application_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    readback_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_system: Mapped[str] = mapped_column(String(32), nullable=False)
    target_resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_public_id: Mapped[str] = mapped_column(String(144), nullable=False)
    requested_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    application_status: Mapped[str] = mapped_column(String(32), nullable=False)
    readback_status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_service_key: Mapped[str] = mapped_column(String(144), nullable=False)
    adapter_configuration_checksum: Mapped[str] = mapped_column(
        String(71), nullable=False
    )
    safe_summary: Mapped[str] = mapped_column(String(512), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    readback_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    odoo_callback_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    immutable_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "result_hash", "operation_id", name="uq_telephony_result_binding"
        ),
        Index("ix_telephony_result_correlation", "correlation_id"),
    )


class TelephonyReconciliationRun(Base):
    __tablename__ = "telephony_reconciliation_run"
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_public_id: Mapped[str] = mapped_column(
        String(144), nullable=False, unique=True, default=lambda: f"REC-{uuid4().hex}"
    )
    command_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("telephony_command_journal.command_id", ondelete="RESTRICT"),
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_public_id: Mapped[str] = mapped_column(String(144), nullable=False)
    target_system: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_telephony_reconciliation_correlation", "correlation_id"),
    )


class IntegrationEvent(Base):
    __tablename__ = "integration_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1.0"
    )
    original_event_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    entity_key: Mapped[str | None] = mapped_column(String(256))
    source_system: Mapped[str] = mapped_column(
        String(50), nullable=False, default="vicidial"
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (Index("ix_integration_event_payload_hash", "payload_hash"),)


class IntegrationDelivery(Base):
    __tablename__ = "integration_delivery"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("event_id", "target", name="uq_delivery_event_target"),
    )


class BroadEventDelivery(Base):
    __tablename__ = "broad_event_delivery"
    delivery_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("integration_event.id"), nullable=False
    )
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    target_environment: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RESERVED")
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(64))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "workflow_id",
            "workflow_version",
            "idempotency_key",
            name="uq_broad_event_delivery_scope",
        ),
    )


class N8nTargetAttestation(Base):
    __tablename__ = "n8n_target_attestation"
    attestation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    target_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    target_environment: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_host: Mapped[str] = mapped_column(String(255), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_nonce: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class N8nExecutionRegistration(Base):
    __tablename__ = "n8n_execution_registration"
    execution_registration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    registration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True
    )
    delivery_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("broad_event_delivery.delivery_id"),
        unique=True,
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="REGISTERED"
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    response_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class N8nExecutionTransition(Base):
    __tablename__ = "n8n_execution_transition"
    transition_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    registration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("n8n_execution_registration.registration_id"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "to_status",
            name="uq_n8n_transition_registration_status",
        ),
    )


class N8nAcknowledgement(Base):
    __tablename__ = "n8n_acknowledgement"
    acknowledgement_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True
    )
    registration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("n8n_execution_registration.registration_id"),
        nullable=False,
    )
    delivery_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("broad_event_delivery.delivery_id"),
        unique=True,
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    execution_status: Mapped[str] = mapped_column(String(24), nullable=False)
    result_classification: Mapped[str] = mapped_column(String(64), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class N8nWorkflowRegistry(Base):
    __tablename__ = "n8n_workflow_registry"
    registry_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workflow_code: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    n8n_workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    tenant_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_contract: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    webhook_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "workflow_code", "workflow_version", name="uq_n8n_registry_code_version"
        ),
        UniqueConstraint("n8n_workflow_id", name="uq_n8n_registry_workflow_id"),
    )


class N8nRuntimeExecution(Base):
    __tablename__ = "n8n_runtime_execution"
    execution_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_code: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    n8n_execution_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    causation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    failure_class: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "event_type",
            "source_event_id",
            "workflow_version",
            "idempotency_key_hash",
            name="uq_n8n_runtime_idempotency",
        ),
        Index("ix_n8n_runtime_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_n8n_runtime_tenant_correlation", "tenant_id", "correlation_id"),
    )


class N8nRuntimeResult(Base):
    __tablename__ = "n8n_runtime_result"
    result_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("n8n_runtime_execution.execution_id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_code: Mapped[str] = mapped_column(String(128), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    persisted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "result_hash", name="uq_n8n_runtime_result_hash"
        ),
    )


class N8nRuntimeNonce(Base):
    __tablename__ = "n8n_runtime_nonce"
    identity: Mapped[str] = mapped_column(String(128), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OdooResultDelivery(Base):
    __tablename__ = "odoo_result_delivery"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(acknowledgement_id, runtime_result_id, integration_event_id) = 1",
            name="ck_odoo_result_delivery_one_source",
        ),
        CheckConstraint(
            "(integration_event_id IS NULL) = (standard_result_json IS NULL)",
            name="ck_odoo_result_delivery_standard_payload",
        ),
    )
    result_delivery_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    acknowledgement_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("n8n_acknowledgement.acknowledgement_id"),
        nullable=True,
        unique=True,
    )
    runtime_result_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("n8n_runtime_result.result_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    integration_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("integration_event.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    standard_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_public_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True, default=uuid4
    )
    originating_outbox_public_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    odoo_result_inbox_id: Mapped[str | None] = mapped_column(String(64))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    last_error_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("integration_event.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_idempotency_scope_key"),
    )


class PublisherNonce(Base):
    __tablename__ = "publisher_nonce"
    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    signed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PublisherAcknowledgement(Base):
    __tablename__ = "publisher_acknowledgement"
    acknowledgement_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledgement: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SecurityRejection(Base):
    __tablename__ = "security_rejection"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    claimed_publisher: Mapped[str | None] = mapped_column(String(128))
    authentication_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNVERIFIED"
    )
    key_id: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ip_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "authentication_state = 'UNVERIFIED'",
            name="ck_security_rejection_unverified",
        ),
    )


class InvalidEventQuarantine(Base):
    __tablename__ = "invalid_event_quarantine"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    server_correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_correlation_id: Mapped[str | None] = mapped_column(String(128))
    claimed_source: Mapped[str | None] = mapped_column(String(64))
    claimed_publisher_identity: Mapped[str | None] = mapped_column(String(128))
    authenticated_publisher_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authentication_state: Mapped[str] = mapped_column(String(24), nullable=False)
    authentication_key_id: Mapped[str | None] = mapped_column(String(64))
    original_signature_verification: Mapped[str] = mapped_column(
        String(24), nullable=False
    )
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_version: Mapped[str | None] = mapped_column(String(32))
    sanitized_preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    business_unit: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_REVIEW"
    )
    review_owner: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replayed_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("integration_event.id")
    )
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    record_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint("replay_count >= 0", name="ck_quarantine_replay_count"),
        CheckConstraint("occurrence_count >= 1", name="ck_quarantine_occurrence_count"),
        CheckConstraint(
            "retention_deadline > received_at", name="ck_quarantine_retention"
        ),
        CheckConstraint("record_version >= 1", name="ck_quarantine_record_version"),
        CheckConstraint(
            "(review_owner IS NULL) = (reviewed_at IS NULL)",
            name="ck_quarantine_review_consistency",
        ),
        CheckConstraint(
            "(resolved_by IS NULL) = (resolved_at IS NULL)",
            name="ck_quarantine_resolution_consistency",
        ),
        CheckConstraint(
            "replayed_event_id IS NULL OR (authentication_state = 'VERIFIED' AND "
            "status = 'REPLAYED' AND resolved_at IS NOT NULL)",
            name="ck_quarantine_replay_eligibility",
        ),
        CheckConstraint(
            "status IN ('PENDING_REVIEW','UNDER_REVIEW','CORRECTABLE',"
            "'REPLAY_APPROVED','REPLAYING','REPLAYED','RESOLVED_NO_REPLAY',"
            "'EXPIRED','REJECTED')",
            name="ck_quarantine_state",
        ),
        CheckConstraint(
            "authentication_state = 'VERIFIED' AND "
            "original_signature_verification = 'VERIFIED'",
            name="ck_quarantine_verified_auth",
        ),
        CheckConstraint(
            "(encrypted_payload IS NULL AND encryption_nonce IS NULL AND "
            "encryption_key_version IS NULL) OR "
            "(encrypted_payload IS NOT NULL AND encryption_nonce IS NOT NULL AND "
            "encryption_key_version IS NOT NULL)",
            name="ck_quarantine_encryption_fields",
        ),
        Index("ix_quarantine_status_received", "status", "received_at"),
        Index(
            "ix_quarantine_publisher_received",
            "authenticated_publisher_id",
            "received_at",
        ),
        Index("ix_quarantine_correlation", "server_correlation_id"),
        Index(
            "ix_quarantine_retention_active",
            "retention_deadline",
            postgresql_where=text("legal_hold = false"),
        ),
        Index("ix_quarantine_fingerprint", "payload_fingerprint"),
    )


class QuarantineCorrection(Base):
    __tablename__ = "quarantine_correction"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    quarantine_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("invalid_event_quarantine.id", ondelete="RESTRICT"),
        nullable=False,
    )
    correction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    correction_reason: Mapped[str] = mapped_column(String(512), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    derived_correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sanitized_diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "quarantine_id",
            "correction_version",
            name="uq_quarantine_correction_version",
        ),
        CheckConstraint(
            "correction_version >= 1", name="ck_quarantine_correction_version"
        ),
    )


class EventInbox(Base):
    __tablename__ = "event_inbox"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    topic: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SyncJob(Base):
    __tablename__ = "sync_job"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    __tablename__ = "webhook_delivery"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    target: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    action: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(128))
    correlation_id: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(32))
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PolicyDecision(Base):
    __tablename__ = "policy_decision"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    policy: Mapped[str] = mapped_column(String(128))
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(128))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrchestrationRequest(Base):
    __tablename__ = "orchestration_request"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    request_uid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    business_unit: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    department_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    team_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    supervisor_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_references: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    requested_resources: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="disabled")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CredentialGrant(Base):
    __tablename__ = "credential_grant"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    orchestration_request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orchestration_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    vault_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadSyncRequest(Base):
    __tablename__ = "lead_sync_request"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    business_unit: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    campaign_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    list_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReconciliationCheckpoint(Base):
    __tablename__ = "reconciliation_checkpoint"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source: Mapped[str] = mapped_column(String(64), unique=True)
    cursor: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(24), default="idle")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransferPolicyDecision(Base):
    __tablename__ = "transfer_policy_decision"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    transfer_id: Mapped[str] = mapped_column(String(128))
    allowed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SystemHealthSnapshot(Base):
    __tablename__ = "system_health_snapshot"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    component: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TelephonyExtensionPool(Base):
    __tablename__ = "telephony_extension_pool"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    business_unit: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role_class: Mapped[str] = mapped_column(String(32), nullable=False)
    range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    range_end: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        CheckConstraint("range_start >= 6100", name="ck_telephony_pool_start"),
        CheckConstraint("range_end <= 9999", name="ck_telephony_pool_end"),
        CheckConstraint("range_start <= range_end", name="ck_telephony_pool_order"),
    )


class CampaignExtensionAllocation(Base):
    """Authoritative immutable campaign extension-block ledger."""

    __tablename__ = "campaign_extension_allocation"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    campaign_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    allocation_public_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    extension_start: Mapped[int] = mapped_column(Integer, nullable=False)
    extension_end: Mapped[int] = mapped_column(Integer, nullable=False)
    extension_range: Mapped[Any] = mapped_column(
        INT4RANGE,
        Computed(
            "int4range(extension_start, extension_end, '[]')",
            persisted=True,
        ),
        nullable=False,
    )
    allocation_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="PROPOSED"
    )
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_change_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "extension_start >= 6100",
            name="ck_campaign_extension_allocation_start",
        ),
        CheckConstraint(
            "extension_end <= 9999",
            name="ck_campaign_extension_allocation_end",
        ),
        CheckConstraint(
            "extension_start <= extension_end",
            name="ck_campaign_extension_allocation_order",
        ),
        CheckConstraint(
            "campaign_number > 0 AND campaign_number % 100 = 0",
            name="ck_campaign_extension_allocation_number",
        ),
        CheckConstraint(
            "allocation_status IN "
            "('PROPOSED','RESERVED_DISABLED','ACTIVE','PAUSED','RETIRED')",
            name="ck_campaign_extension_allocation_status",
        ),
        ExcludeConstraint(
            ("extension_range", "&&"),
            using="gist",
            name="ex_campaign_extension_allocation_no_overlap",
        ),
    )


class CampaignRegistry(Base):
    __tablename__ = "campaign_registry"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    campaign_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    campaign_code: Mapped[str] = mapped_column(String(3), nullable=False, unique=True)
    campaign_public_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    vicidial_campaign_id: Mapped[str] = mapped_column(
        String(8), nullable=False, unique=True
    )
    agent_group: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    dialplan_context: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True
    )
    parent_campaign_number: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("campaign_registry.campaign_number")
    )
    extension_allocation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("campaign_extension_allocation.id"),
        nullable=False,
        unique=True,
    )
    registry_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="PROPOSED_DISABLED"
    )
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_change_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CampaignObjectIdentity(Base):
    __tablename__ = "campaign_object_identity"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    campaign_number: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaign_registry.campaign_number"), nullable=False
    )
    identity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    sequence_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    public_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    full_alias: Mapped[str | None] = mapped_column(String(112), unique=True)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="ID_ASSIGNED"
    )
    dialing_state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="NOT_ELIGIBLE"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __talz{béq¶»§q«^w]¶÷†òµëçVÆÆ&ÆSÔfÇ6R¢FW7F–æF–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢F–ÇÆåö6öçFW‡C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢2&VfW&Væ6RöæÇ’Âæ÷B6÷’ÒöFöò†6öFW7G&öÖ–FFÆWv&Uö'&–FvR’&VÖ–ç0¢2F†R7—7FVÒöb&V6÷&Bf÷"F†RÆVBö7W7FöÖW"×&öf–ÆR&V6÷&B—G6VÆbâ6W@¢2öæ6RB÷&–v–æFRF–ÖRg&öÒ÷&–v–æFT6ÆÅ&WVW7BæÆVEöÖöFVÂöÆVEö–C°¢2çVÆÂf÷"6ÆÇ2v—F‚æò5$ÒÆ–æ¶vR†Rærâ–çFW&æÂ6ÆÇ2’à¢ÆVEöÖöFVÃ¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢ÆVEö–C¢ÖVE¶–çBÂæöæUÒÒÖVEö6öÇVÖâ„–çFVvW"¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’À¢6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’À¢öçWFFSÖgVæ2ææ÷r‚’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢&Æ–fV7–6ÆU÷7FFR”â‚u5D%DTBrÂt4ôääT5DTBrÂtTäDTBr’"À¢æÖSÒ&6µ÷FVÆW†öç•ö6ÆÅöÆ–fV7–6ÆU÷7FFR"À¢’À¢6†V6´6öç7G&–çB€¢&f–æU÷7FFR”â‚ ¢"w&WVW7FVBrÂv66WFVBrÂwVWVVBrÂvF–Æ–ærrÂw&–æv–ærrÂvç7vW&VBrÂ ¢"v6öææV7FVBrÂv6ö×ÆWFVBrÂvf–ÆVBrÂv'W7’rÂvæõöç7vW"rÂv6æ6VÆVBrÂ ¢"w&V¦V7FVBr’"À¢æÖSÒ&6µ÷FVÆW†öç•ö6ÆÅöf–æU÷7FFR"À¢’À¢6†V6´6öç7G&–çB€¢&†æwWöÆVr•2åTÄÂõ"†æwWöÆVr”â‚vvVçEöÆVrrÂwVW%öÆVrr’"À¢æÖSÒ&6µ÷FVÆW†öç•ö6ÆÅö†æwWöÆVr"À¢’À¢  ¦6Æ72FVÆW†öç”6ÆÄÆ–fV7–6ÆTWfVçB„&6R“ ¢õ÷F&ÆVæÖUõòÒ'FVÆW†öç•ö6ÆÅöÆ–fV7–6ÆUöWfVçB ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6ÆÅö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚'FVÆW†öç•ö6ÆÅöÆ–fV7–6ÆRæ–B"ÂöæFVÆWFSÒ$444DR"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢–çFVw&F–öåöWfVçEö–C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ€¢&–t–çFVvW"À¢f÷&V–vä¶W’‚&–çFVw&F–öåöWfVçBæ–B"ÂöæFVÆWFSÒ$444DR"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢Væ—VSÕG'VRÀ¢¢÷&–v–æÅöWfVçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VP¢¢Væ—VUö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6†ææVÃ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6R¢–æ6öÖ–æu÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢&Wf–÷W5÷7FFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒb’¢&W7VÇF–æu÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢G&ç6—F–öåöÆ–VC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6R¢ö67W'&VEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6P¢¢&V6÷&FVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢  ¦6Æ72vVçD6ÆÅ7FFR„&6R“ ¢õ÷F&ÆVæÖUõòÒ&vVçEö6ÆÅ÷7FFR ¢6ÆÅö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’Â&–Ö'•ö¶W“ÕG'VR¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢'W6–æW75÷Væ—Eö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–våö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢vVçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢W‡FVç6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VP¢¢7FW&—6µ÷Væ—VV–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢Æ–æ¶VF–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢WfVçE÷G—S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢7FFU÷&æ³¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢6WVVæ6S¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„&–t–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢WfVçE÷F–ÖW7F×¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6P¢¢6öçFW‡Eö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’À¢6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’À¢öçWFFSÖgVæ2ææ÷r‚’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&W‡FVç6–öâ"Â'6WVVæ6R"ÂæÖSÒ'WövVçEö6ÆÅ÷7FFUöW‡FVç6–öå÷6WVVæ6R ¢’À¢6†V6´6öç7G&–çB‚'6WVVæ6RãÒ"ÂæÖSÒ&6µövVçEö6ÆÅ÷7FFU÷6WVVæ6R"’À¢  ¦6Æ72vVçD6ÆÄWfVçB„&6R“ ¢õ÷F&ÆVæÖUõòÒ&vVçEö6ÆÅöWfVçB ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢66†VÖ÷fW'6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢WfVçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VR¢WfVçE÷G—S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢–FV×÷FVæ7•ö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VP¢¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6ÆÅö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢'W6–æW75÷Væ—Eö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–våö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢vVçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢W‡FVç6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢7FW&—6µ÷Væ—VV–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢Æ–æ¶VF–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6WVVæ6S¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„&–t–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢WfVçE÷F–ÖW7F×¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6P¢¢–ÆöEö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢G&ç6—F–öåöÆ–VC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6R¢&V6÷&FVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&6ÆÅö–B"Â'6WVVæ6R"ÂæÖSÒ'WövVçEö6ÆÅöWfVçEö6ÆÅ÷6WVVæ6R ¢’À¢6†V6´6öç7G&–çB‚'6WVVæ6RãÒ"ÂæÖSÒ&6µövVçEö6ÆÅöWfVçE÷6WVVæ6R"’À¢  ¦6Æ72–çFVw&F–öå6W'f–6R„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öå÷6W'f–6R ¢6W'f–6Uö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6W'f–6Uö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VR¢F—7Æ•öæÖS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢Væ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6R¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢  ¦6Æ72–çFVw&F–öä7&VFVçF–Å&VfW&Væ6R„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öåö7&VFVçF–Å÷&VfW&Væ6R ¢7&VFVçF–Å÷&VfW&Væ6Uö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢&VfW&Væ6Uö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VR¢&÷f–FW#¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢Væ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6R¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢  ¦6Æ72–çFVw&F–öäVæGö–çB„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öåöVæGö–çB ¢VæGö–çEö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6W'f–6Uö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&–çFVw&F–öå÷6W'f–6Rç6W'f–6Uö–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢VæGö–çEö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ“b’ÂçVÆÆ&ÆSÔfÇ6R¢•÷fW'6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ'c"¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢'6W'f–6Uö–B"Â&VæGö–çEö¶W’"Â&•÷fW'6–öâ"ÂæÖSÒ'WöVæGö–çEö–FVçF—G’ ¢’À¢  ¦6Æ72–çFVw&F–öäVæGö–çEfW'6–öâ„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öåöVæGö–çE÷fW'6–öâ ¢VæGö–çE÷fW'6–öåö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢VæGö–çEö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&–çFVw&F–öåöVæGö–çBæVæGö–çEö–B"ÂöæFVÆWFSÒ$444DR"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢6öæf–wW&F–öå÷fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢&6U÷W&Ã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒS"’ÂçVÆÆ&ÆSÔfÇ6R¢F…÷FV×ÆFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒS"’ÂçVÆÆ&ÆSÔfÇ6R¢‡GGöÖWF†öC¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ’ÂçVÆÆ&ÆSÔfÇ6R¢6öçFVçE÷G—S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ&Æ–6F–öâö§6öâ ¢¢WF†VçF–6F–öåöÖöFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢&WV—&VEöVF–Væ6S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢&WV—&VE÷66÷W3¢ÖVE¶Æ—7E·7G%ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖÆ—7@¢¢7&VFVçF–Å÷&VfW&Væ6Uö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6R¢FÇ5÷&öf–ÆUö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢F–ÖV÷WEö×3¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢6öææV7F–öå÷F–ÖV÷WEö×3¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ€¢–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ3 ¢¢&FUöÆ–Ö—E÷W%öÖ–çWFS¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ€¢–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ ¢¢6öæ7W'&Væ7•öÆ–Ö—C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢–FV×÷FVæ7•÷&WV—&VC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÕG'VP¢¢&WG'•ö6Æ73¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ$äõõ$UE%’ ¢¢&WG'•öÆ–Ö—C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢&VF—&V7G5öÆÆ÷vVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6P¢¢F&vWEöGFW7FF–öå÷&WV—&VC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÕG'VP¢¢7FÆU÷&VE÷6fS¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6P¢¢Væ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6R¢¶–ÆÅ÷7v—F6ƒ¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÕG'VR¢6öæf–wW&F–öåö6†V6·7VÓ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒs’ÂçVÆÆ&ÆSÔfÇ6R¢VffV7F—fUöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6P¢¢W‡—&W5öC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢7&VFVEö'“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢&÷fVEö'“¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&VæGö–çEö–B"À¢&6öæf–wW&F–öå÷fW'6–öâ"À¢æÖSÒ'WöVæGö–çEö6öæf–wW&F–öå÷fW'6–öâ"À¢’À¢6†V6´6öç7G&–çB‚&6öæf–wW&F–öå÷fW'6–öâãÒ"ÂæÖSÒ&6µöVæGö–çE÷fW'6–öâ"’À¢6†V6´6öç7G&–çB‚'F–ÖV÷WEö×2â"ÂæÖSÒ&6µöVæGö–çE÷F–ÖV÷WB"’À¢6†V6´6öç7G&–çB€¢&6öææV7F–öå÷F–ÖV÷WEö×2â"ÂæÖSÒ&6µöVæGö–çEö6öææV7F–öå÷F–ÖV÷WB ¢’À¢6†V6´6öç7G&–çB‚'&WG'•öÆ–Ö—BãÒ"ÂæÖSÒ&6µöVæGö–çE÷&WG'•öÆ–Ö—B"’À¢6†V6´6öç7G&–çB€¢'&WG'•ö6Æ72”â‚täõõ$UE%’rÂt$õTäDTEõE$å4”TåEõ$UE%’rÂtÔåTÅõ$UÄ•ôôäÅ’r’"À¢æÖSÒ&6µöVæGö–çE÷&WG'•ö6Æ72"À¢’À¢  ¦6Æ72–çFVw&F–öå&÷WFT&–æF–ær„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öå÷&÷WFUö&–æF–ær ¢&–æF–æuö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢VæGö–çE÷fW'6–öåö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&–çFVw&F–öåöVæGö–çE÷fW'6–öâæVæGö–çE÷fW'6–öåö–B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢Vçf—&öæÖVçC¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢÷&væ—¦F–öå÷66÷S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ" ¢¢'W6–æW75÷Væ—E÷66÷S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ" ¢¢6×–vå÷66÷S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ""¢v÷&¶fÆ÷u÷66÷S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ""¢WfVçE÷G—U÷66÷S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ" ¢¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&Vçf—&öæÖVçB"À¢&VæGö–çE÷fW'6–öåö–B"À¢&÷&væ—¦F–öå÷66÷R"À¢&'W6–æW75÷Væ—E÷66÷R"À¢&6×–vå÷66÷R"À¢'v÷&¶fÆ÷u÷66÷R"À¢&WfVçE÷G—U÷66÷R"À¢æÖSÒ'W÷&÷WFUö&–æF–æu÷66÷R"À¢’À¢–æFW‚€¢&—…÷&÷WFUö&–æF–æuöÆöö·W"À¢&Vçf—&öæÖVçB"À¢&÷&væ—¦F–öå÷66÷R"À¢&'W6–æW75÷Væ—E÷66÷R"À¢&6×–vå÷66÷R"À¢’À¢  ¦6Æ72–çFVw&F–öå66†VÖfW'6–öâ„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öå÷66†VÖ÷fW'6–öâ ¢66†VÖ÷fW'6–öåö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6W'f–6Uö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢VæGö–çEö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ“b’ÂçVÆÆ&ÆSÔfÇ6R¢•÷fW'6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢66†VÖ÷&VfW&Væ6S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒS"’ÂçVÆÆ&ÆSÔfÇ6R¢6†V6·7VÓ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒs’ÂçVÆÆ&ÆSÔfÇ6R¢Væ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ„&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÔfÇ6R¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢'6W'f–6Uö¶W’"À¢&VæGö–çEö¶W’"À¢&•÷fW'6–öâ"À¢æÖSÒ'Wö–çFVw&F–öå÷66†VÖö¶W’"À¢’À¢  ¦6Æ72–çFVw&F–öäVæGö–çDVF—B„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öåöVæGö–çEöVF—B ¢VF—Eö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢VæGö–çEö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’ÂçVÆÆ&ÆSÔfÇ6R¢7F–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢7F÷#¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢&Wf–÷W5ö6†V6·7VÓ¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒs’¢æWuö6†V6·7VÓ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒs’ÂçVÆÆ&ÆSÔfÇ6R¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢  ¦6Æ72–çFVw&F–öå&Vv—7G'”vVæW&F–öâ„&6R“ ¢õ÷F&ÆVæÖUõòÒ&–çFVw&F–öå÷&Vv—7G'•övVæW&F–öâ ¢Vçf—&öæÖVçC¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’Â&–Ö'•ö¶W“ÕG'VR¢vVæW&F–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„&–t–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢6öæf–wW&F–öåö6†V6·7VÓ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒs’ÂçVÆÆ&ÆSÔfÇ6R¢V&Æ—6†VEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢V&Æ—6†VEö'“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R  ¦6Æ726ÆÆ&6µ&V6÷&B„&6R“ ¢""$6æöæ–6Â6ÆÆ&6²6öçG&öÂ7FFS²7W7FöÖW"5$ÒFF&VÖ–ç2–âöFöòâ""  ¢õ÷F&ÆVæÖUõòÒ&6ÆÆ&6µ÷&V6÷&B ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–våö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6öçF7Eö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢ÆVEö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢÷÷'GVæ—G•ö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢÷&–v–æÅö6ÆÅö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢÷&–v–æÅöÆ–æ¶VF–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢76–væVEövVçEö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢76–væVE÷W6W%ö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢76–væVE÷FVÕö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢7WW'f—6÷%ö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢†öæUöçVÖ&W#¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢æ÷&ÖÆ—¦VE÷†öæS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢66†VGVÆVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6P¢¢7W7FöÖW%÷F–ÖW¦öæS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢&–÷&—G“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ$äõ$ÔÂ"¢&V6öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#Sb’ÂçVÆÆ&ÆSÔfÇ6R¢æ÷FW3¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…FW‡BÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ""¢7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%44„TETÄTB"¢FW6—&VE÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%44„TETÄTB ¢¢7GVÅ÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%44„TETÄTB ¢¢&VÖ–æFW%öVÖ–ÅöVæ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÕG'VP¢¢&VÖ–æFW%÷÷WöVæ&ÆVC¢ÖVE¶&ööÅÒÒÖVEö6öÇVÖâ€¢&ööÆVâÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÕG'VP¢¢VÖ–Å÷&VÖ–æFW%óöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR¢¢VÖ–Å÷&VÖ–æFW%ó%öC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR¢¢÷W÷&VÖ–æFW%öC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢GFV×Eö6÷VçC¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢Ö…öGFV×G3¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ2¢Æ7EöGFV×EöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢æW‡EöGFV×EöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢6ö×ÆWFVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢6æ6VÆÆVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢6ö×ÆWF–öåöF—7÷6—F–öã¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢6ö×ÆWF–öåöæ÷FW3¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…FW‡B¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢–FV×÷FVæ7•ö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢&WVW7Eö†6ƒ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢7–æ5÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%TäD”är ¢¢6ö×Æ–æ6Uö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢6öçFW‡Eö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢7&VFVEö'“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’À¢6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’À¢öçWFFSÖgVæ2ææ÷r‚’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢&76–væVEövVçEö–B•2äõBåTÄÂõ"76–væVE÷FVÕö–B•2äõBåTÄÂ"À¢æÖSÒ&6µö6ÆÆ&6µö÷væW""À¢’À¢6†V6´6öç7G&–çB€¢'fW'6–öâãÒäBGFV×Eö6÷VçBãÒäBÖ…öGFV×G2ãÒ"À¢æÖSÒ&6µö6ÆÆ&6µö6÷VçFW'2"À¢’À¢Væ—VT6öç7G&–çB€¢'FVæçEö–B"Â&–FV×÷FVæ7•ö¶W’"ÂæÖSÒ'Wö6ÆÆ&6µ÷FVæçEö–FV×÷FVæ7’ ¢’À¢–æFW‚‚&—…ö6ÆÆ&6µöGVUö6Æ–Ò"Â'7FFR"Â'66†VGVÆVEöB"’À¢–æFW‚€¢&—…ö6ÆÆ&6µövVçE÷VWVR"À¢'FVæçEö–B"À¢&6×–våö–B"À¢&76–væVEövVçEö–B"À¢'66†VGVÆVEöB"À¢’À¢–æFW‚‚&—…ö6ÆÆ&6µ÷†öæR"Â'FVæçEö–B"Â&æ÷&ÖÆ—¦VE÷†öæR"’À¢–æFW‚‚&—…ö6ÆÆ&6µö6÷'&VÆF–öâ"Â&6÷'&VÆF–öåö–B"’À¢  ¦6Æ726ÆÆ&6´WfVçB„&6R“ ¢õ÷F&ÆVæÖUõòÒ&6ÆÆ&6µöWfVçB ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6ÆÆ&6µö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&6ÆÆ&6µ÷&V6÷&Bæ–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–våö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢WfVçE÷G—S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢–FV×÷FVæ7•ö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢7F÷%ö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢–ÆöEö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢V&Æ—6†VEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢ö67W'&VEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&6ÆÆ&6µö–B"À¢'fW'6–öâ"À¢&WfVçE÷G—R"À¢æÖSÒ'Wö6ÆÆ&6µöWfVçE÷fW'6–öå÷G—R"À¢’À¢Væ—VT6öç7G&–çB€¢'FVæçEö–B"Â&–FV×÷FVæ7•ö¶W’"ÂæÖSÒ'Wö6ÆÆ&6µöWfVçEö–FV×÷FVæ7’ ¢’À¢–æFW‚‚&—…ö6ÆÆ&6µöWfVçEö÷WF&÷‚"Â'V&Æ—6†VEöB"Â&ö67W'&VEöB"’À¢  ¦6Æ726ÆÆ&6´FVÆ—fW'’„&6R“ ¢õ÷F&ÆVæÖUõòÒ&6ÆÆ&6µöFVÆ—fW'’ ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢6ÆÆ&6µö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&6ÆÆ&6µ÷&V6÷&Bæ–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢6ÆÆ&6µ÷fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢6†ææVÃ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢7FvS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢–FV×÷FVæ7•ö¶W“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VP¢¢7FGW3¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%TUTTB"¢ÖW76vUö–C¢ÖVEµUT”BÂæöæUÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’¢&÷f–FW%öÖW76vUö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢GFV×Eö6÷VçC¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢Æ7EöW'&÷%ö6öFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢æW‡EöGFV×EöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’À¢6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’À¢öçWFFSÖgVæ2ææ÷r‚’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢õ÷F&ÆUö&w5õòÒ€¢Væ—VT6öç7G&–çB€¢&6ÆÆ&6µö–B"À¢&6ÆÆ&6µ÷fW'6–öâ"À¢&6†ææVÂ"À¢'7FvR"À¢æÖSÒ'Wö6ÆÆ&6µöFVÆ—fW'•÷7FvR"À¢’À¢–æFW‚‚&—…ö6ÆÆ&6µöFVÆ—fW'•÷&WG'’"Â'7FGW2"Â&æW‡EöGFV×EöB"’À¢  ¤tTåEõ$õd•4”ôä”äuõ5DDU2Ò€¢%$UTU5DTB"À¢%dÄ”DD”är"À¢$”DTåD•E’"À¢$TåD•DÄTÔTåE2"À¢$4„ääTÅõ$õd•4”ôä”är"À¢%$TD$4²"À¢$TddT5D•dR"À¢%%D”Â"À¢$d”ÄTB"À¢%$T4ôä4”Ä”är"À¢%5U5TäDTB"À¢%$Udô´TB"À¢  ¦6Æ72vVçE&÷f—6–öæ–æu&WVW7B„&6R“ ¢""$öæRGW&&ÆR6vW"öFöò%&÷f—6–öâ"6Æ–6²„Ö—76–öâ2’à ¢öFöò6VæG2W†7FÇ’öæR6öÖÖæB†W&RæBöÆÇ2öö'6W'fW2F†—2&÷r†æB—G0¢vVçE&÷f—6–öæ–æu7FW6†–ÆG&Vâ’f÷"&öw&W73²Ö–FFÆWv&RÆöæRFV6–FW0¢†÷rFò&V6‚TddT5D•dR7&÷72¶W–6Æö²Âd”4–F–ÂÂ¶Ç—&÷rÂæBFVÆæW†à¢æò&÷f–FW"6V7&WB÷"&r7&VFVçF–Â—2WfW"7F÷&VB–âF†—2F&ÆR÷"–à¢vVçE&÷f—6–öæ–æu7FWÒ6VR¶W–6Æöµ÷7V&¦V7B&VÆ÷rÂv†–6‚—2â÷VP¢–FVçF–f–W"ÂæWfW"Fö¶Vâà¢""  ¢õ÷F&ÆVæÖUõòÒ&vVçE÷&÷f—6–öæ–æu÷&WVW7B ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢&WVW7Eö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VR¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢V×Æ÷–VUö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢&–Ö'•öVÖ–Ã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6R¢6×–vç5ö§6öã¢ÖVE¶Æ—7E´ç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖÆ—7@¢¢6†ææVÇ5ö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢FVÆW†öç•ö§6öã¢ÖVE¶F–7E·7G"Âç•ÕÒÒÖVEö6öÇVÖâ€¢¥4ôä"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÖF–7@¢¢7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#B’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%$UTU5DTB"¢¶W–6Æöµ÷7V&¦V7C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢öÆ–7•÷&Wf—6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢–FV×÷FVæ7•ö†6ƒ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ€¢7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VP¢¢&WVW7Eö†6ƒ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢&WVW7FVEö'“¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6R¢Æ7EöW'&÷%ö6öFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢Æ7EöW'&÷%÷7VÖÖ'“¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒS’¢fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’À¢6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’À¢öçWFFSÖgVæ2ææ÷r‚’À¢çVÆÆ&ÆSÔfÇ6RÀ¢¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢'7FFR”â‚r"²"rÂr"æ¦ö–â„tTåEõ$õd•4”ôä”äuõ5DDU2’²"r’"À¢æÖSÒ&6µövVçE÷&÷f—6–öæ–æu÷7FFR"À¢’À¢6†V6´6öç7G&–çB‚'fW'6–öâãÒ"ÂæÖSÒ&6µövVçE÷&÷f—6–öæ–æu÷fW'6–öâ"’À¢–æFW‚‚&—…övVçE÷&÷f—6–öæ–æu÷FVæçE÷7FFR"Â'FVæçEö–B"Â'7FFR"’À¢  ¦6Æ72vVçE&÷f—6–öæ–æu7FW„&6R“ ¢""%W"ÖW‡FW&æÂÖ÷W&F–öâ6vÆörÂöæR&÷rW"GFV×Bà ¢FVÆ–&W&FVÇ’Ö—'&÷'26öFW7G&ç&÷f—6–öæ–ærç7FWw2f–VÆB6†RöâF†P¢öFöò6–FR†öÆöã“‚ôöFöòÂ6öFW7G&ö–FVçF—G•÷&÷f—6–öæ–ær’6òF†P¢Gvò7—7FV×2FW67&–&RF†R6ÖR6v–âF†R6ÖRfö6'VÆ'“ ¢7—7FVÒö÷W&F–öâöGFV×B÷7FFRöW‡FW&æÅ÷&VfW&Væ6R÷7F'FVEöBð¢6ö×ÆWFVEöB÷&VF&6µ÷7FFRöW'&÷%ö6öFRöW'&÷%÷7VÖÖ'’âæWfW"7F÷&W0¢&÷f–FW"6V7&WG2÷"&rFö¶Vç2à¢""  ¢õ÷F&ÆVæÖUõòÒ&vVçE÷&÷f—6–öæ–æu÷7FW ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢&WVW7Eö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&vVçE÷&÷f—6–öæ–æu÷&WVW7Bæ–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢–æFWƒÕG'VRÀ¢¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢7—7FVÓ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢÷W&F–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢GFV×C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ'VæF–ær"¢W‡FW&æÅ÷&VfW&Væ6S¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’¢7F'FVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢6ö×ÆWFVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢&VF&6µ÷7FFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’¢W'&÷%ö6öFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢W'&÷%÷7VÖÖ'“¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒS’¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢'7—7FVÒ”â‚v¶W–6Æö²rÂwf–6–F–ÂrÂv¶Ç—&÷rrÂwFVÆæW†rÂvöFöòr’"À¢æÖSÒ&6µövVçE÷&÷f—6–öæ–æu÷7FW÷7—7FVÒ"À¢’À¢–æFW‚‚&—…övVçE÷&÷f—6–öæ–æu÷7FW÷&WVW7B"Â'&WVW7Eö–B"Â'7—7FVÒ"Â&GFV×B"’À¢  ¦6Æ72vVçE&÷f—6–öæ–ætVF—B„&6R“ ¢""$VæBÖöæÇ’7FFR×G&ç6—F–öâÆVFvW"âæòWFFRöFVÆWFRF‚W†—7G2â""  ¢õ÷F&ÆVæÖUõòÒ&vVçE÷&÷f—6–öæ–æuöVF—B ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢&WVW7Eö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’À¢f÷&V–vä¶W’‚&vVçE÷&÷f—6–öæ–æu÷&WVW7Bæ–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢–æFWƒÕG'VRÀ¢¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢g&öÕ÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#B’ÂçVÆÆ&ÆSÔfÇ6R¢Fõ÷7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#B’ÂçVÆÆ&ÆSÔfÇ6R¢7F–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢7F÷%÷7V&¦V7C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’ÂçVÆÆ&ÆSÔfÇ6R¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢&V6÷&Eö†6ƒ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂVæ—VSÕG'VR¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢  ¦6Æ72öFöô6×–vå6v„&6R“ ¢""$öæR6vW"66WFVBöFöò6×–vâÖ6öçG&öÂ÷WF&÷‚WfVçBà ¢F†RVæ—VR–çFVw&F–öåöWfVçEö–F—2F†R&W†7FÇ’öæR6v&÷rW ¢WfVçB"wV&çFVS²6öæ7W'&VçB6VÆV7F÷'2Æ÷6RöâF†R6öç7G&–çBÂæ÷Böâ¢&6Râ6v7FGW2†F—7F6‚Æ–fV7–6ÆR’æBVffV7F—fU÷7FFV‡v†BF†P¢FFW"ö'6W'fVBæBöFöòv2FöÆB’&RFVÆ–&W&FVÇ’6W&FR6öÇVÖç2à¢""  ¢õ÷F&ÆVæÖUõòÒ&öFöõö6×–vå÷6v ¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢'7FGW2”â‚uTäD”ärrÂu$U4U%dTBrÂu$UE%’rÂt4ôÕÄUDTBrÂtDTEôÄUEDU"r’"À¢æÖSÒ&6µööFöõö6×–vå÷6v÷7FGW2"À¢’À¢6†V6´6öç7G&–çB€¢&VffV7F—fU÷7FFR•2åTÄÂõ"VffV7F—fU÷7FFR”â ¢"‚wVæ¶æ÷vârÂv'6VçBrÂw&÷f—6–öæVEöF—6&ÆVBrÂw7–çF†WF–5÷FW7FVBrÂv7F—fRrÂvF—6&ÆVBr’"À¢æÖSÒ&6µööFöõö6×–vå÷6vöVffV7F—fU÷7FFR"À¢’À¢–æFW‚‚&—…ööFöõö6×–vå÷6vö6Æ–Ò"Â'7FGW2"Â&æW‡EöGFV×EöB"’À¢¢6vö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ€¢uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–C@¢¢–çFVw&F–öåöWfVçEö–C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ€¢&–t–çFVvW"À¢f÷&V–vä¶W’‚&–çFVw&F–öåöWfVçBæ–B"ÂöæFVÆWFSÒ%$U5E$”5B"’À¢çVÆÆ&ÆSÔfÇ6RÀ¢Væ—VSÕG'VRÀ¢¢WfVçE÷WV–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢WfVçE÷G—S¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ’ÂçVÆÆ&ÆSÔfÇ6R¢÷W&F–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6R¢6öÖÖæEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢÷&væ—¦F–öå÷V&Æ–5ö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢'W6–æW75÷Væ—E÷V&Æ–5ö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–vå÷V&Æ–5ö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6öæf–wW&F–öå÷fW'6–öã¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6R¢Öæ–fW7E÷&Vc¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#Sb’ÂçVÆÆ&ÆSÔfÇ6R¢Öæ–fW7Eö†6ƒ¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒƒ’ÂçVÆÆ&ÆSÔfÇ6R¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢7FGW3¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%TäD”är"¢GFV×G3¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢&W6W'fVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢ÆV6UöW‡—&W5öC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢æW‡EöGFV×EöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢Æ7EöW'&÷%ö6Æ73¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢VffV7F—fU÷7FFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’¢Wf–FVæ6Uö§6öã¢ÖVE¶F–7E·7G"Âç•ÒÂæöæUÒÒÖVEö6öÇVÖâ„¥4ôä"¢ö'6W'fVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢&VF&6µö–FV×÷FVæ7•ö¶W“¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒc’¢&VF&6µöGFV×C¢ÖVE¶–çEÒÒÖVEö6öÇVÖâ„–çFVvW"ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÓ¢&VF&6µö–C¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’¢6ö×ÆWFVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢¢WFFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ€¢FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6P¢ ¦6Æ72vVçE&÷f—6–öæ–æu&W—$–çFVçB„&6R“ ¢õ÷F&ÆVæÖUõòÒ&vVçE÷&÷f—6–öæ–æu÷&W—%ö–çFVçB ¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢'7FFR”â‚u$õõ4TBrÂtUD„õ$•¤TBrÂtU„T5UD”ärrÂu5T44TTDTBrÂtd”ÄTBrÂt4ä4TÄÄTBr’"À¢æÖSÒ&6µövVçE÷&W—%÷7FFR"À¢’À¢f÷&V–vä¶W”6öç7G&–çB€¢²'FVæçEö–B"Â'&WVW7Eö–B%ÒÀ¢²&vVçE÷&÷f—6–öæ–æu÷&WVW7BçFVæçEö–B"Â&vVçE÷&÷f—6–öæ–æu÷&WVW7Bæ–B%ÒÀ¢æÖSÒ&fµövVçE÷&W—%÷FVæçE÷&WVW7B"À¢öæFVÆWFSÒ%$U5E$”5B"À¢’À¢–æFW‚€¢&—…övVçE÷&W—%÷FVæçE÷&WVW7B"À¢'FVæçEö–B"À¢'&WVW7Eö–B"À¢FW‡B‚&7&VFVEöBDU42"’À¢’À¢ ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–CB¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢&WVW7Eö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’ÂçVÆÆ&ÆSÔfÇ6R¢G&–gEö6Æ73¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒC’ÂçVÆÆ&ÆSÔfÇ6R¢&÷÷6VEö7F–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#B’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ%$õõ4TB"¢VffV7Eö6Æ73¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ3"’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ'&÷f–FW%ö×WFF–öâ"¢WF†÷&—¦VEö'“¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’¢&W7VÇEö6öFS¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’¢7&VFVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6R¢W†V7WFVEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’  ¦6Æ72vVçEvV''F56W76–öâ„&6R“ ¢õ÷F&ÆVæÖUõòÒ&vVçE÷vV''F5÷6W76–öâ ¢õ÷F&ÆUö&w5õòÒ€¢6†V6´6öç7G&–çB€¢'7FFR”â‚t•55TTBrÂu$Tt•5DU$”ärrÂu$Tt•5DU$TBrÂtU…•$TBrÂu$Udô´TBrÂtd”ÄTBr’"À¢æÖSÒ&6µövVçE÷vV''F5÷7FFR"À¢’À¢f÷&V–vä¶W”6öç7G&–çB€¢²'FVæçEö–B"Â'&WVW7Eö–B%ÒÀ¢²&vVçE÷&÷f—6–öæ–æu÷&WVW7BçFVæçEö–B"Â&vVçE÷&÷f—6–öæ–æu÷&WVW7Bæ–B%ÒÀ¢æÖSÒ&fµövVçE÷vV''F5÷FVæçE÷&WVW7B"À¢öæFVÆWFSÒ%$U5E$”5B"À¢’À¢–æFW‚€¢'WövVçE÷vV''F5ö7F—fUöFWf–6R"À¢'FVæçEö–B"À¢&V×Æ÷–VUö–B"À¢Væ—VSÕG'VRÀ¢÷7Fw&W7Å÷v†W&S×FW‡B‚'7FFR”â‚t•55TTBrÂu$Tt•5DU$”ärrÂu$Tt•5DU$TBr’"’À¢’À¢ ¢–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’Â&–Ö'•ö¶W“ÕG'VRÂFVfVÇC×WV–CB¢FVæçEö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢&WVW7Eö–C¢ÖVEµUT”EÒÒÖVEö6öÇVÖâ…uUT”B†5÷WV–CÕG'VR’ÂçVÆÆ&ÆSÔfÇ6RÂ–æFWƒÕG'VR¢V×Æ÷–VUö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R¢6×–våö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒcB’ÂçVÆÆ&ÆSÔfÇ6R¢W‡FVç6–öã¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒb’ÂçVÆÆ&ÆSÔfÇ6R¢&÷f–FW%÷&VfW&Væ6S¢ÖVE·7G"ÂæöæUÒÒÖVEö6öÇVÖâ…7G&–ærƒ#SR’¢7FFS¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#B’ÂçVÆÆ&ÆSÔfÇ6RÂFVfVÇCÒ$•55TTB"¢—77VVEöC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’Â6W'fW%öFVfVÇCÖgVæ2ææ÷r‚’ÂçVÆÆ&ÆSÔfÇ6R¢W‡—&W5öC¢ÖVE¶FFWF–ÖUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’ÂçVÆÆ&ÆSÔfÇ6R¢&Wfö¶VEöC¢ÖVE¶FFWF–ÖRÂæöæUÒÒÖVEö6öÇVÖâ„FFUF–ÖR‡F–ÖW¦öæSÕG'VR’¢6÷'&VÆF–öåö–C¢ÖVE·7G%ÒÒÖVEö6öÇVÖâ…7G&–ærƒ#‚’ÂçVÆÆ&ÆSÔfÇ6R