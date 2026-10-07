#!/usr/bin/env python3
from __future__ import annotations

import asyncio

import asyncpg
import httpx

from app.config import ConfigurationError, Settings
from app.commands import ODOO_COMMAND_DESTINATION, TEMPORAL_COMMAND_DESTINATION
from app.nats_transport import NatsJetStreamPublisher
from app.odoo_transport import OdooCommandDispatcher
from app.storage import NATS_JETSTREAM_DESTINATION, OutboxRecord, PostgresOutboxStore
from app.temporal_runtime import connect_temporal
from app.temporal_transport import TemporalCommandDispatcher
from app.worker import OutboxWorker


async def _load_reconciliation_command_id(
    pool: asyncpg.Pool,
    record: OutboxRecord,
) -> str | None:
    """Read the trusted command identity from the exact durable outbox row."""

    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT command_id
            FROM middleware_outbox
            WHERE id=$1
              AND tenant_id=$2
              AND destination=$3
              AND event_type=$4
              AND idempotency_key=$5
              AND completed_at IS NULL
              AND cancelled_at IS NULL
              AND dead_lettered_at IS NULL
            """,
            record.id,
            record.tenant_id,
            record.destination,
            record.event_type,
            record.idempotency_key,
        )
    return str(value) if value is not None else None


async def main() -> None:
    settings = Settings.from_env()
    temporal_enabled = settings.temporal_worker_mode != "disabled"
    odoo_enabled = settings.odoo_delivery_enabled
    if not settings.outbox_dispatch_enabled and not temporal_enabled and not odoo_enabled:
        raise ConfigurationError(
            "JetStream, Temporal, and Odoo outbox dispatch are all intentionally "
            "disabled"
        )
    if settings.database_url is None:
        raise ConfigurationError("DATABASE_URL is required for the outbox worker")

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=8,
        command_timeout=10,
    )
    publisher: NatsJetStreamPublisher | None = None
    odoo_client: httpx.AsyncClient | None = None
    try:
        handlers = {}
        if settings.outbox_dispatch_enabled:
            publisher = await NatsJetStreamPublisher.connect(settings)
            handlers[NATS_JETSTREAM_DESTINATION] = publisher.publish
        if temporal_enabled:
            temporal_client = await connect_temporal(settings)

            async def reconciliation_command_id(record: OutboxRecord) -> str | None:
                return await _load_reconciliation_command_id(pool, record)

            temporal_dispatcher = TemporalCommandDispatcher(
                temporal_client,
                settings.temporal_task_queue,
                reconciliation_command_id_lookup=reconciliation_command_id,
            )
            handlers[TEMPORAL_COMMAND_DESTINATION] = temporal_dispatcher.dispatch
        if odoo_enabled:
            # Registered only when ODOO_WRITE is on, so the handler cannot be
            # reached while the capability is closed.
            odoo_client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.odoo_timeout_seconds)
            )
            handlers[ODOO_COMMAND_DESTINATION] = OdooCommandDispatcher(
                client=odoo_client,
                base_url=settings.odoo_base_url or "",
                secrets=dict(settings.odoo_tenant_hmac_secrets),
                source_delivery_enabled=settings.odoo_source_delivery_enabled,
                default_secret=settings.odoo_default_hmac_secret or None,
            ).dispatch
        worker = OutboxWorker(
            PostgresOutboxStore(pool),
            handlers,
        )
        await worker.run_forever()
    finally:
        if publisher is not None:
            await publisher.close()
        if odoo_client is not None:
            await odoo_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
