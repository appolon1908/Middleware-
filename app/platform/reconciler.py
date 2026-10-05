"""The reconciliation authority for adapter-executed commands.

An operation whose provider outcome is unknown sits in
``reconciliation_required`` with its outbox row quarantined. The reconciler
(``middleware-reconciler`` process, or the same code invoked in tests):

1. claims the quarantined outbox row with a lease (``FOR UPDATE SKIP LOCKED``),
2. asks the owning adapter to read the provider state back (``reconcile``),
3. normalises the answer,
4. compares expected vs actual,
5. transitions the ledger — ``MATCHED`` → ``completed``; ``NOT_FOUND`` (the
   effect provably never happened) → ``queued`` + a bounded retry of the same
   outbox row; ``MISMATCH`` → stays parked with the evidence, dead-lettered
   after the bounded reconciliation budget; ``UNAVAILABLE`` → stays parked
   and is retried on the next cycle, dead-lettered after the budget;
   ``UNSUPPORTED`` (the adapter has no read surface for this command — a
   deterministic answer that no retry changes) → dead-lettered at once with
   the reason recorded, so an acknowledged but unverifiable write never sits
   in the backlog burning the budget,
6. appends the immutable audit rows (command audit + outbox reconciliation audit).

It never issues a provider write merely because a readback failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.commands import CommandConflict, CommandNotFound, CommandService, redact_metadata
from app.core.config import Settings
from app.platform.adapter import AdapterContext, ReadbackResult, ReadbackStatus
from app.platform.metrics import KernelMetrics
from app.platform.registry import AdapterRegistry
from app.platform.bus import status_readback, worker_identity

logger = logging.getLogger("codestra.platform.reconciler")

DEFAULT_RECONCILIATION_BUDGET = 6


@dataclass(frozen=True)
class ReconciliationClaim:
    outbox_id: int
    tenant_id: str
    command_id: UUID
    reconciliation_attempts: int


class ReconciliationSource(Protocol):
    """Where quarantined adapter-command rows come from (Postgres outbox or memory)."""

    async def claim(self, *, reconciler_id: str, lease_seconds: float) -> ReconciliationClaim | None: ...

    async def resolve(self, claim: ReconciliationClaim, *, reconciler_id: str, action: str, reason: str) -> None: ...

    async def release(self, claim: ReconciliationClaim, *, reconciler_id: str, reason: str) -> None: ...

    async def backlog(self) -> int: ...


@dataclass(frozen=True)
class ReconciliationDecision:
    command_id: UUID
    adapter_id: str | None
    readback: ReadbackResult | None
    action: str
    final_state: str
    drift_class: str | None = None


class Reconciler:
    def __init__(
        self,
        *,
        settings: Settings,
        commands: CommandService,
        registry: AdapterRegistry,
        source: ReconciliationSource,
        metrics: KernelMetrics,
        http: Any = None,
        budget: int = DEFAULT_RECONCILIATION_BUDGET,
        lease_seconds: float = 60.0,
        timeout_seconds: float = 30.0,
        reconciler_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.commands = commands
        self.registry = registry
        self.source = source
        self.metrics = metrics
        self.http = http
        self.budget = budget
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.reconciler_id = reconciler_id or worker_identity("reconciler")

    async def run_once(self) -> ReconciliationDecision | None:
        claim = await self.source.claim(reconciler_id=self.reconciler_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        try:
            return await self._reconcile(claim)
        finally:
            try:
                self.metrics.reconciliation_backlog.set(await self.source.backlog())
            except Exception:  # metrics must not break the loop
                logger.debug("reconciliation_backlog_probe_failed", exc_info=True)

    async def run_forever(self, *, poll_seconds: float = 2.0) -> None:
        while True:
            decision = await self.run_once()
            if decision is None:
                await asyncio.sleep(poll_seconds)

    async def _fenced_reconcile(self, claim: ReconciliationClaim, operation: Any, **kwargs: Any) -> bool:
        """Record a read-back verdict only against the version that was read.

        The adapter read-back runs outside any lock; if a worker, operator or
        another reconciler changed the operation meanwhile, the verdict is
        stale. Release the claim instead so the next pass re-reads it.
        """
        try:
            await self.commands.reconcile(
                claim.tenant_id, claim.command_id, expected_version=operation.resource_version, **kwargs,
            )
        except CommandConflict:
            await self.source.release(
                claim, reconciler_id=self.reconciler_id, reason="operation changed during read-back; verdict discarded",
            )
            return False
        return True

    async def _reconcile(self, claim: ReconciliationClaim) -> ReconciliationDecision:
        try:
            operation = await self.commands.get(claim.tenant_id, claim.command_id)
        except CommandNotFound:
            await self.source.resolve(claim, reconciler_id=self.reconciler_id, action="dead_letter", reason="command row missing")
            return ReconciliationDecision(claim.command_id, None, None, "dead_letter", "missing")

        if operation.state == "completed":
            await self.source.resolve(claim, reconciler_id=self.reconciler_id, action="complete", reason="operation already completed")
            return ReconciliationDecision(claim.command_id, None, None, "complete", "completed")
        if operation.state in {"failed", "dead_lettered", "cancelled"}:
            await self.source.resolve(claim, reconciler_id=self.reconciler_id, action="dead_letter", reason=f"operation is terminal ({operation.state})")
            return ReconciliationDecision(claim.command_id, None, None, "dead_letter", operation.state)
        if operation.state in {"persisted", "queued"}:
            # The ledger holds no in-flight attempt (a known-safe failure was
            # re-queued, or the effect never started): the quarantined row goes
            # back to the claimable pool instead of parking forever.
            await self.source.resolve(claim, reconciler_id=self.reconciler_id, action="retry", reason=f"ledger shows no in-flight attempt ({operation.state}); intent returned to the queue")
            return ReconciliationDecision(claim.command_id, None, None, "retry", operation.state)
        if operation.state in {"dispatching", "accepted", "readback_pending"}:
            # This row was claimed only because its worker lease expired: the
            # worker died while the provider may or may not have acted. Park
            # the operation for reconciliation (fenced to its open attempt) and
            # read the provider state back below; never resend blindly.
            attempt = await self.commands.latest_attempt(claim.tenant_id, claim.command_id)
            self.metrics.lease_expirations.inc()
            operation = await self.commands.transition(
                claim.tenant_id, claim.command_id, new_state="reconciliation_required", actor_id=self.reconciler_id,
                reason=f"worker lease expired while {operation.state}; provider state must be read back",
                expected_attempt=attempt,
            )
        if operation.state != "reconciliation_required":
            await self.source.release(claim, reconciler_id=self.reconciler_id, reason=f"operation is {operation.state}; not awaiting reconciliation")
            return ReconciliationDecision(claim.command_id, None, None, "release", operation.state)

        ownership = self.registry.ownership(operation.command_type)
        if ownership is None:
            await self.source.release(claim, reconciler_id=self.reconciler_id, reason="no adapter owns this command in this process")
            return ReconciliationDecision(claim.command_id, None, None, "release", operation.state)
        adapter = self.registry.adapter(ownership.adapter_id)
        advertised = self.registry.advertised(ownership.adapter_id)
        attempt = await self.commands.latest_attempt(claim.tenant_id, claim.command_id)
        envelope = await self.commands.load_envelope(claim.tenant_id, claim.command_id)
        if envelope.target not in adapter.capabilities().connector_ids:
            reason = "connector unavailable during reconciliation"
            await self.commands.transition(claim.tenant_id, claim.command_id, new_state="dead_lettered", actor_id=self.reconciler_id, reason=reason)
            await self.source.resolve(claim, reconciler_id=self.reconciler_id, action="dead_letter", reason=reason)
            return ReconciliationDecision(claim.command_id, adapter.adapter_id, None, "dead_letter", "dead_lettered")
        context = AdapterContext(
            tenant_id=operation.tenant_id,
            command_id=str(operation.command_id),
            correlation_id=operation.correlation_id,
            attempt=attempt,
            timeout_seconds=self.timeout_seconds,
            environment=self.settings.app_env,
            deployment_sha=self.settings.source_sha,
            http=self.http,
            payload=envelope.payload,
        )
        self.metrics.adapter_requests.labels(adapter=adapter.adapter_id, operation="reconcile").inc()
        started = time.perf_counter()
        try:
            # The provider status surface is the cheapest proof of a failed
            # asynchronous operation; otherwise the connector's reconcile hook
            # performs the deeper lookup.
            readback = await status_readback(adapter, operation, context, pending_is_unavailable=False)
            if readback is None:
                readback = await asyncio.wait_for(adapter.reconcile(operation, context), timeout=self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            readback = ReadbackResult(
                ReadbackStatus.UNAVAILABLE, provider_operation_id=operation.provider_operation_id,
                evidence={"retry_hint": "reconcile"}, safe_error_code=type(exc).__name__,
            )
        finally:
            self.metrics.adapter_latency.labels(adapter=adapter.adapter_id, operation="reconcile").observe(time.perf_counter() - started)

        evidence = {
            "schema_version": "1.0",
            "status": readback.status.value.lower(),
            "provider_operation_id": readback.provider_operation_id or operation.provider_operation_id,
            **redact_metadata(dict(readback.evidence)),
        }
        actor = self.reconciler_id
        family = operation.command_type.split(".", 1)[0]
        exhausted = claim.reconciliation_attempts >= self.budget or readback.status is ReadbackStatus.UNSUPPORTED

        if readback.status is ReadbackStatus.MATCHED:
            if not await self._fenced_reconcile(
                claim, operation, matched=True, actor_id=actor,
                reason="reconciliation read-back matched", provider_operation_id=readback.provider_operation_id, evidence=evidence,
            ):
                return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "release", operation.state, "operation_changed_during_readback")
            await self.source.resolve(claim, reconciler_id=actor, action="complete", reason="reconciliation read-back matched")
            self.metrics.reconciliation_decisions.labels(adapter=adapter.adapter_id, result="completed").inc()
            self.metrics.commands_completed.labels(command_family=family, adapter=adapter.adapter_id).inc()
            return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "complete", "completed")

        if readback.status is ReadbackStatus.NOT_FOUND and not exhausted and advertised.safe_reexecution:
            # Absence is repairable only when the adapter explicitly advertises
            # that repeating the provider effect is safe.
            await self.commands.transition(
                claim.tenant_id, claim.command_id, new_state="queued", actor_id=actor,
                reason="reconciliation proved no provider effect; re-queued", expected_attempt=attempt,
            )
            await self.source.resolve(claim, reconciler_id=actor, action="retry", reason="reconciliation proved no provider effect")
            self.metrics.reconciliation_decisions.labels(adapter=adapter.adapter_id, result="requeued").inc()
            return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "retry", "queued", "provider_missing_repairable")

        if readback.status is ReadbackStatus.NOT_FOUND and not advertised.safe_reexecution:
            if not await self._fenced_reconcile(
                claim, operation, matched=False, actor_id=actor,
                reason="provider state missing; automatic repair is not safe",
                provider_operation_id=readback.provider_operation_id, evidence=evidence,
            ):
                return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "release", operation.state, "operation_changed_during_readback")
            reason = "provider state missing and adapter does not permit safe automatic re-execution"
            await self.commands.dead_letter(
                claim.tenant_id, claim.command_id, actor_id=actor, reason=reason,
                reason_code="provider_missing_manual_repair", error_class="not_found",
                terminal_reason=reason, retry_exhausted=False,
            )
            await self.source.resolve(claim, reconciler_id=actor, action="dead_letter", reason=reason)
            self.metrics.reconciliation_decisions.labels(adapter=adapter.adapter_id, result="missing_unsafe").inc()
            self.metrics.commands_failed.labels(command_family=family, adapter=adapter.adapter_id, result="dead_lettered").inc()
            return ReconciliationDecision(
                claim.command_id, adapter.adapter_id, readback, "dead_letter", "dead_lettered",
                "provider_missing_manual_repair",
            )

        if readback.status is ReadbackStatus.MISMATCH:
            if not await self._fenced_reconcile(
                claim, operation, matched=False, actor_id=actor,
                reason="reconciliation read-back mismatch", provider_operation_id=readback.provider_operation_id, evidence=evidence,
            ):
                return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "release", operation.state, "operation_changed_during_readback")
            self.metrics.reconciliation_decisions.labels(adapter=adapter.adapter_id, result="mismatch").inc()
        else:
            self.metrics.reconciliation_decisions.labels(adapter=adapter.adapter_id, result=readback.status.value.lower()).inc()

        if exhausted:
            if readback.status is ReadbackStatus.UNSUPPORTED:
                reason = f"provider read-back unsupported ({readback.safe_error_code or 'no read surface'}); operator verification required"
            else:
                reason = f"reconciliation budget exhausted after {readback.status.value.lower()}"
            await self.commands.dead_letter(
                claim.tenant_id, claim.command_id, actor_id=actor, reason=reason,
                reason_code="reconciliation_exhausted" if readback.status is not ReadbackStatus.UNSUPPORTED else "readback_unsupported",
                error_class=readback.status.value.lower(), terminal_reason=reason,
                retry_exhausted=readback.status is not ReadbackStatus.UNSUPPORTED,
            )
            await self.source.resolve(claim, reconciler_id=actor, action="dead_letter", reason=reason)
            self.metrics.retry_exhaustions.inc()
            self.metrics.commands_failed.labels(command_family=family, adapter=adapter.adapter_id, result="dead_lettered").inc()
            return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "dead_letter", "dead_lettered")

        await self.source.release(claim, reconciler_id=actor, reason=f"read-back {readback.status.value.lower()}; will retry")
        return ReconciliationDecision(claim.command_id, adapter.adapter_id, readback, "release", "reconciliation_required")
