"""Durable worker operation leases with committed pre-submission crash markers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text

from middleware.connector_sdk.errors import (
    CommandNotAllowedError,
    ConnectorVersionConflictError,
)
from middleware.connector_sdk.runtime import _validate_result
from middleware.connector_sdk.models import (
    CommandOutcome,
    CommandRequest,
    CommandResult,
)
from middleware.connector_sdk.operations import OperationLease, OperationSnapshot
from middleware.connector_sdk.standards import deep_thaw

from .api.database import Database


class _PostgresLease:
    def __init__(
        self, database: Database, params: dict, snapshot: OperationSnapshot
    ) -> None:
        self.database = database
        self.params = params
        self.snapshot = snapshot

    def save(self, result: CommandResult, *, attempts: int) -> None:
        _validate_result(result)
        if type(attempts) is not int or not self.snapshot.attempts <= attempts <= 20:
            raise CommandNotAllowedError(
                "journal attempts must be monotonic and bounded"
            )
        value = {
            "outcome": result.outcome.value,
            "operation_id": result.operation_id,
            "provider_reference": result.provider_reference,
            "safe_result": deep_thaw(result.safe_result),
            "retryable": result.retryable,
            "error_code": result.error_code,
        }
        with self.database.session(self.params["tenant_id"]) as session:
            session.execute(
                text("""
                UPDATE connector_sdk.connector_command_journal
                   SET result=CAST(:result AS jsonb), attempts=:attempts, updated_at=now()
                 WHERE tenant_id=:tenant_id AND environment=:environment
                   AND connector_id=:connector_id AND idempotency_key=:key
            """),
                {
                    **self.params,
                    "result": json.dumps(value, allow_nan=False),
                    "attempts": attempts,
                },
            )
        self.snapshot = OperationSnapshot(
            self.snapshot.request_sha256, attempts, result
        )


class PostgresOperationStore:
    """One journal per environment; advisory locks serialize across workers.

    The session lock is held separately from short journal transactions so the
    UNKNOWN marker commits before the provider call. A dead process releases
    its lock but leaves that marker for read-only reconciliation on redelivery.
    No command payload is retained. Workers must redeliver the original request.
    """

    def __init__(self, database: Database, environment: str) -> None:
        if environment not in {"development", "staging", "production"}:
            raise ValueError("invalid connector environment")
        self.database = database
        self.environment = environment

    @contextmanager
    def acquire(
        self, request: CommandRequest, request_sha256: str
    ) -> Iterator[OperationLease]:
        params: dict[str, Any] = {
            "tenant_id": UUID(request.context.tenant_id),
            "environment": self.environment,
            "connector_id": request.connector_id,
            "key": request.context.idempotency_key,
        }
        lock_identity = json.dumps(
            {key: str(value) for key, value in params.items()}, sort_keys=True
        )
        lock_id = int.from_bytes(
            hashlib.sha256(lock_identity.encode()).digest()[:8], "big", signed=True
        )
        # Closing a pooled connection alone does not release session advisory
        # locks. Always unlock explicitly before returning it to the pool.
        with self.database.engine.connect() as connection:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}
            ).scalar_one()
            connection.commit()
            if not acquired:
                raise ConnectorVersionConflictError(
                    "command execution is already in progress"
                )
            try:
                with self.database.session(params["tenant_id"]) as session:
                    session.execute(
                        text("""
                        INSERT INTO connector_sdk.connector_command_journal
                            (tenant_id, environment, connector_id, idempotency_key, request_sha256)
                        VALUES (:tenant_id, :environment, :connector_id, :key, :digest)
                        ON CONFLICT DO NOTHING
                    """),
                        {**params, "digest": request_sha256},
                    )
                    row = (
                        session.execute(
                            text("""
                        SELECT request_sha256, attempts, result
                          FROM connector_sdk.connector_command_journal
                         WHERE tenant_id=:tenant_id AND environment=:environment
                           AND connector_id=:connector_id AND idempotency_key=:key
                    """),
                            params,
                        )
                        .mappings()
                        .one()
                    )
                    if row["request_sha256"] != request_sha256:
                        raise ConnectorVersionConflictError(
                            "idempotency key was used with different command semantics"
                        )
                    raw = row["result"]
                    result = (
                        None
                        if raw is None
                        else CommandResult(
                            outcome=CommandOutcome(raw["outcome"]),
                            operation_id=raw["operation_id"],
                            provider_reference=raw.get("provider_reference"),
                            safe_result=raw.get("safe_result", {}),
                            retryable=raw.get("retryable", False),
                            error_code=raw.get("error_code"),
                        )
                    )
                    snapshot = OperationSnapshot(
                        request_sha256, row["attempts"], result
                    )
                yield _PostgresLease(self.database, params, snapshot)
            finally:
                try:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id}
                    )
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise
