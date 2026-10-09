"""Disabled-by-default crash recovery for the agent provisioning saga.

The saga commits after every provider step. A runner that dies mid-saga
leaves its row in an in-flight state; once the row's lease
(``agent_provisioning_lease_seconds``) expires this sweep resumes it
through the same step functions the API uses, so every provider effect
that already succeeded is skipped rather than repeated. It never starts
new sagas and never retries settled FAILED/PARTIAL rows - that remains an
explicit operator ``/reconcile``. All provider effects stay behind their
own kill switches.

It runs as one more bounded pass of the ``middleware-reconciler`` process
(``app.entrypoints.reconciliation_worker``), so it inherits that process's
canonical startup validation, cadence and health surface.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.agent_provisioning import resume_stale_sagas
from app.core.config import settings
from app.db.session import SessionFactory


logger = logging.getLogger("codestra.agent_provisioning")


async def reconcile_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> dict[str, object]:
    if not settings.agent_provisioning_reconciler_enabled:
        return {"status": "disabled", "resumed": 0}
    resumed = await resume_stale_sagas(session_factory)
    if resumed:
        logger.info("agent provisioning sagas resumed count=%s", len(resumed))
    return {"status": "ok", "resumed": len(resumed)}
