"""The agent provisioning crash-recovery sweep and its reconciler wiring.

Database-free: the saga sweep itself is exercised against PostgreSQL in
tests/test_agent_provisioning_lifecycle.py; these pin that the sweep is
off by default and runs as one pass of the middleware-reconciler cycle.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.core.config import settings
from app.entrypoints import reconciliation_worker
from app.workers import agent_provisioning_reconciler


@pytest.mark.asyncio
async def test_sweep_is_disabled_by_default_and_never_touches_sagas(monkeypatch):
    assert settings.agent_provisioning_reconciler_enabled is False

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("disabled sweep resumed sagas")

    monkeypatch.setattr(agent_provisioning_reconciler, "resume_stale_sagas", must_not_run)
    assert await agent_provisioning_reconciler.reconcile_once() == {
        "status": "disabled", "resumed": 0,
    }


@pytest.mark.asyncio
async def test_enabled_sweep_resumes_through_the_given_session_factory(monkeypatch):
    monkeypatch.setattr(settings, "agent_provisioning_reconciler_enabled", True)
    seen: list[object] = []
    factory = object()

    async def resume(session_factory):
        seen.append(session_factory)
        return ["req_a", "req_b"]

    monkeypatch.setattr(agent_provisioning_reconciler, "resume_stale_sagas", resume)
    result = await agent_provisioning_reconciler.reconcile_once(factory)
    assert result == {"status": "ok", "resumed": 2}
    assert seen == [factory]


@pytest.mark.asyncio
async def test_reconciler_cycle_runs_the_agent_provisioning_sweep(monkeypatch):
    @asynccontextmanager
    async def session_factory():
        yield object()

    async def kernel():
        return {"status": "ok", "decisions": 0}

    async def outbox(_session):
        return {"checked": 0}

    async def cleanup(_session):
        return {"deleted": 0}

    async def sweep():
        return {"status": "ok", "resumed": 1}

    monkeypatch.setattr(reconciliation_worker, "reconcile_kernel", kernel)
    monkeypatch.setattr(reconciliation_worker, "SessionFactory", session_factory)
    monkeypatch.setattr(reconciliation_worker, "reconcile_internal_outbox", outbox)
    monkeypatch.setattr(reconciliation_worker, "cleanup_expired", cleanup)
    monkeypatch.setattr(reconciliation_worker, "reconcile_agent_provisioning", sweep)

    result = await reconciliation_worker.cycle()
    assert result["agent_provisioning"] == {"status": "ok", "resumed": 1}
    assert result["kernel"] == {"status": "ok", "decisions": 0}
