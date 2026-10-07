from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.communications import CommunicationMessage, PostgresCommunicationsStore


pytestmark = pytest.mark.skipif(
    os.getenv("RUNTIME_INTEGRATION_TESTS") != "1",
    reason="requires disposable PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def migrated_schema() -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for path in sorted(Path("migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")):
                await conn.execute(path.read_text(encoding="utf-8"))
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_communication_projection_survives_restart() -> None:
    database_url = os.environ["DATABASE_URL"]
    tenant_id = f"tenant-communications-{uuid.uuid4()}"
    message_id = uuid.uuid4()
    now = datetime.now(UTC)
    store = await PostgresCommunicationsStore.connect(database_url)
    store.messages[(tenant_id, message_id)] = CommunicationMessage(
        messageId=message_id,
        tenantId=tenant_id,
        channel="email",
        direction="outbound",
        status="queued",
        correlationId="correlation-restart",
        idempotencyKey="idempotency-restart",
        provider="klyrow",
        createdAt=now,
        updatedAt=now,
    )
    store.add_event(
        tenant_id,
        message_id,
        event_type="queued",
        status="queued",
        provider="klyrow",
    )
    await store.persist()
    await store.close()

    reopened = await PostgresCommunicationsStore.connect(database_url)
    try:
        assert reopened.messages[(tenant_id, message_id)].status == "queued"
        assert [event.type for event in reopened.events[(tenant_id, message_id)]] == [
            "queued"
        ]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_communication_event_ledger_is_immutable() -> None:
    store = await PostgresCommunicationsStore.connect(os.environ["DATABASE_URL"])
    try:
        with pytest.raises(Exception, match="append-only"):
            await store.pool.execute("DELETE FROM middleware_communication_events")
    finally:
        await store.close()
