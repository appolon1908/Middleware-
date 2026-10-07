"""Operation journal contracts; production workers must use durable storage."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ContextManager, Protocol

from .errors import ConnectorVersionConflictError
from .models import CommandRequest, CommandResult
from .standards import deep_thaw


def request_fingerprint(request: CommandRequest, manifest_digest: str) -> str:
    """Exclude tracing metadata; bind semantic identity to the authorized actor."""
    value = {
        "connector_id": request.connector_id,
        "command_id": request.command_id,
        "command_type": request.command_type,
        "command_version": request.command_version,
        "tenant_id": request.context.tenant_id,
        "actor_id": request.context.actor_id,
        "payload": deep_thaw(request.payload),
        "manifest_digest": manifest_digest,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    request_sha256: str
    attempts: int = 0
    result: CommandResult | None = None


class OperationLease(Protocol):
    @property
    def snapshot(self) -> OperationSnapshot: ...

    def save(self, result: CommandResult, *, attempts: int) -> None:
        """Commit before submission and after every externally observed outcome."""


class OperationStore(Protocol):
    def acquire(
        self, request: CommandRequest, request_sha256: str
    ) -> ContextManager[OperationLease]:
        """Serialize one tenant/connector/key; reject different request semantics."""


class _MemoryLease:
    def __init__(self, snapshot: OperationSnapshot) -> None:
        self.snapshot = snapshot

    def save(self, result: CommandResult, *, attempts: int) -> None:
        self.snapshot = OperationSnapshot(
            self.snapshot.request_sha256, attempts, result
        )


class InMemoryOperationStore:
    """Development only: replay safety lasts for this process's lifetime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str, str], _MemoryLease] = {}

    @contextmanager
    def acquire(
        self, request: CommandRequest, request_sha256: str
    ) -> Iterator[OperationLease]:
        key = (
            request.context.tenant_id,
            request.connector_id,
            request.context.idempotency_key,
        )
        with self._lock:
            lease = self._entries.setdefault(
                key, _MemoryLease(OperationSnapshot(request_sha256))
            )
            if lease.snapshot.request_sha256 != request_sha256:
                raise ConnectorVersionConflictError(
                    "idempotency key was used with different command semantics"
                )
            yield lease
