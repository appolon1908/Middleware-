from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace

from .storage import (
    DEFAULT_MAX_OUTBOX_ATTEMPTS,
    ActiveLease,
    LeaseLostError,
    OutboxRecord,
    PostgresOutboxStore,
    ReconciliationError,
    StorageError,
)


Handler = Callable[[OutboxRecord], Awaitable[None]]
EffectGate = Callable[[OutboxRecord], Awaitable[bool] | bool]
log = logging.getLogger(__name__)


class KnownSafeRetryError(RuntimeError):
    """Handler-provided proof that no external effect could have committed.

    Provider adapters may raise this only when they can establish that the
    operation is safe to retry, for example because dispatch was rejected before
    any provider write was attempted. Ambiguous transport/provider errors must
    not use this exception; they remain quarantined for reconciliation instead.
    """


class OutboxWorker:
    """Generic bounded lease/retry/reconciliation worker.

    No provider handlers are registered on intake-runtime-v1. Immediately before
    any future provider handler is invoked, the claimed row is durably moved into
    the reconciliation-required state and its active worker lease is refreshed for
    a full lease window. A background heartbeat continues renewing that ownership
    until the provider task actually terminates, including any time spent waiting
    for a cancellation-suppressing coroutine to finish after the timeout.

    The handler executes under the lease it was given (``record.lease``): when
    the heartbeat proves ownership or fencing was lost, the lease is marked lost,
    a handler that has not yet reached the provider must not call it, and this
    worker resolves nothing — the row belongs to its new owner or the reconciler.

    Timeout is sticky: once the configured deadline is crossed, a later normal
    return or KnownSafeRetryError from a cancellation-suppressing handler cannot
    turn that unknown outcome into an automatic complete or retry transition.
    Provider dispatch remains disabled by Settings on this branch.
    """

    def __init__(
        self,
        store: PostgresOutboxStore,
        handlers: Mapping[str, Handler],
        *,
        poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
        handler_timeout_seconds: float = 45.0,
        max_attempts: int = DEFAULT_MAX_OUTBOX_ATTEMPTS,
        effect_gate: EffectGate | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if handler_timeout_seconds <= 0 or handler_timeout_seconds >= lease_seconds:
            raise ValueError("handler timeout must be positive and strictly below lease")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.handlers = handlers
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.handler_timeout_seconds = handler_timeout_seconds
        self.max_attempts = max_attempts
        self.effect_gate = effect_gate
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    async def _heartbeat_active_dispatch(
        self,
        record_id: int,
        fencing_token: int,
        stop: asyncio.Event,
        lease: ActiveLease,
    ) -> None:
        interval = max(0.01, min(5.0, self.lease_seconds / 3.0))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self.store.renew_active_dispatch(
                    record_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    fencing_token=fencing_token,
                )
            except StorageError:
                # Ownership or fencing was lost: another actor owns this row.
                # The handler must not reach the provider from here on, and
                # this worker must not resolve the row's outcome.
                lease.lost = True
                log.error(
                    "active dispatch lease lost during heartbeat; handler outcome will not be resolved by this worker",
                    extra={"outbox_id": record_id},
                )
                return
            except Exception:
                # Keep retrying while provider code is alive. The row remains
                # reconciliation-required and therefore excluded from claims even
                # if PostgreSQL is temporarily unavailable.
                log.exception(
                    "active dispatch lease heartbeat failed; will retry",
                    extra={"outbox_id": record_id},
                )

    async def _resolve(self, record: OutboxRecord, *, action: str, reason: str) -> None:
        """Resolve the quarantined row as its owner; a refusal leaves the row
        quarantined for the reconciler instead of stopping the worker loop."""
        try:
            await self.store.resolve_reconciliation(
                record.id,
                operator_id=f"worker:{self.worker_id}",
                action=action,  # type: ignore[arg-type]
                reason=reason,
                max_attempts=self.max_attempts,
                worker_id=self.worker_id,
                fencing_token=record.fencing_token,
            )
        except ReconciliationError:
            log.exception(
                "outbox outcome could not be resolved by the worker; reconciliation quarantine retained",
                extra={"outbox_id": record.id, "result": action},
            )

    async def run_once(self) -> bool:
        record = await self.store.claim(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if record is None:
            return False

        handler = self.handlers.get(record.destination)
        if handler is None:
            await self.store.fail(
                record.id,
                worker_id=self.worker_id,
                error=f"no handler registered for destination {record.destination}",
                max_attempts=self.max_attempts,
                fencing_token=record.fencing_token,
            )
            return True

        # External effects fail closed unless the runtime supplies its
        # authoritative gate; a denial is recorded before any provider code.
        allowed = False
        if self.effect_gate is not None:
            decision = self.effect_gate(record)
            allowed = await decision if isinstance(decision, Awaitable) else decision
        if not allowed:
            await self.store.fail(
                record.id,
                worker_id=self.worker_id,
                error="effect gate denied dispatch",
                max_attempts=self.max_attempts,
                fencing_token=record.fencing_token,
            )
            return True

        # This final pre-provider transaction refreshes the lease from database
        # time. If it fails, the exception propagates and provider code is never run.
        await self.store.quarantine_unknown_outcome(
            record.id,
            worker_id=self.worker_id,
            error=(
                "provider dispatch reserved before handler invocation; external outcome "
                "must be explicitly confirmed before automatic release"
            ),
            lease_seconds=self.lease_seconds,
            fencing_token=record.fencing_token,
        )

        lease = ActiveLease(owner=self.worker_id, fencing_token=record.fencing_token)
        record = replace(record, lease_owner=self.worker_id, lease=lease)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_active_dispatch(
                record.id, record.fencing_token, heartbeat_stop, lease
            )
        )
        handler_task = asyncio.ensure_future(handler(record))
        timed_out = False
        try:
            try:
                done, _ = await asyncio.wait(
                    {handler_task},
                    timeout=self.handler_timeout_seconds,
                )
                if handler_task not in done:
                    timed_out = True
                    handler_task.cancel()

                try:
                    await handler_task
                except asyncio.CancelledError:
                    if not timed_out:
                        raise
                except LeaseLostError:
                    log.error(
                        "outbox handler stopped before the provider effect because the lease was lost; "
                        "row left to its owner or the reconciler",
                        extra={"outbox_id": record.id},
                    )
                except KnownSafeRetryError as exc:
                    if timed_out:
                        log.error(
                            "outbox handler crossed timeout then reported safe retry; "
                            "unknown outcome remains quarantined",
                            extra={"outbox_id": record.id},
                        )
                    elif lease.lost:
                        log.error(
                            "outbox handler reported safe retry after the lease was lost; "
                            "not resolved by this worker",
                            extra={"outbox_id": record.id},
                        )
                    elif record.attempt_count >= self.max_attempts:
                        # The last permitted attempt failed without an effect:
                        # the row is terminal, not retried into a budget it no
                        # longer has.
                        log.warning(
                            "outbox handler certified failure as safe to retry on the final attempt; dead-lettering",
                            extra={"outbox_id": record.id},
                        )
                        await self._resolve(
                            record,
                            action="dead_letter",
                            reason=f"retry budget exhausted after known-safe failure: {exc}",
                        )
                    else:
                        log.warning(
                            "outbox handler certified failure as safe to retry",
                            extra={"outbox_id": record.id},
                        )
                        await self._resolve(
                            record,
                            action="retry",
                            reason=f"handler certified known-safe retry: {exc}",
                        )
                except Exception:
                    if timed_out:
                        log.exception(
                            "outbox handler crossed timeout and later raised; "
                            "unknown outcome remains quarantined",
                            extra={"outbox_id": record.id},
                        )
                    else:
                        log.exception(
                            "outbox handler raised; reconciliation quarantine retained",
                            extra={"outbox_id": record.id},
                        )
                else:
                    if timed_out:
                        log.error(
                            "outbox handler crossed timeout and later returned; "
                            "unknown outcome remains quarantined",
                            extra={"outbox_id": record.id},
                        )
                    elif lease.lost:
                        log.error(
                            "outbox handler returned after the lease was lost; "
                            "outcome not resolved by this worker",
                            extra={"outbox_id": record.id},
                        )
                    else:
                        await self._resolve(
                            record,
                            action="complete",
                            reason="handler returned successfully and confirmed delivery outcome",
                        )
            except asyncio.CancelledError:
                # A worker shutdown must not orphan live provider code while the
                # lease heartbeat is stopped. Cancel the provider task and keep
                # renewing ownership until that task actually terminates.
                if not handler_task.done():
                    handler_task.cancel()
                try:
                    await handler_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
        finally:
            heartbeat_stop.set()
            await heartbeat_task
        return True

    async def run_forever(self) -> None:
        while True:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self.poll_seconds)
