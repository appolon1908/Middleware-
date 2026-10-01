"""The single SQLAlchemy engine of the Middleware process.

The engine is created once from the canonical ``app.core.config.settings``
and shared by every ORM router, worker and script through ``SessionFactory``
and ``get_session``. ``RuntimeContainer`` (``app.core.runtime``) references
this engine rather than creating another one, and disposes it on shutdown.

``configure()`` exists for process bootstrap and tests that need the engine
bound to a different DSN; it rebinds the module-level ``engine`` and
``SessionFactory`` in place so consumers that imported those names keep
working.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as SyncSession

from app.core.config import Settings, runtime_database_sslmode, settings
from app.db.connection import build_database_connection_authority
from app.db.tenant_context import (
    TENANT_CONTEXT_GUC,
    TENANT_CONTEXT_INFO_KEY,
    canonical_tenant_id,
)


@event.listens_for(SyncSession, "after_begin")
def _restore_transaction_tenant_context(session, transaction, connection) -> None:
    """Re-apply bound tenant context whenever SQLAlchemy opens a transaction.

    PostgreSQL clears SET LOCAL state at COMMIT/ROLLBACK, so a session that
    commits and keeps working would otherwise run its next transaction with
    no tenant context. The validated tenant lives only in the session's
    in-memory ``info`` map, never on the pooled connection.
    """
    tenant_id = session.info.get(TENANT_CONTEXT_INFO_KEY)
    if tenant_id:
        connection.execute(
            text("SELECT set_config(:setting_name, :tenant_id, true)"),
            {"setting_name": TENANT_CONTEXT_GUC, "tenant_id": tenant_id},
        )


async def set_transaction_tenant_context(
    session: AsyncSession,
    tenant_id: str | UUID,
) -> str:
    """Bind validated tenant authority to the session and current transaction.

    ``set_config(..., true)`` is PostgreSQL's transaction-local equivalent of
    ``SET LOCAL``. A session is bound to exactly one tenant: rebinding it to a
    different tenant is refused, so stale context can never be reused for
    another tenant.
    """
    normalized = canonical_tenant_id(tenant_id)
    bound = session.info.get(TENANT_CONTEXT_INFO_KEY)
    if bound is not None and bound != normalized:
        raise ValueError("session is already bound to a different tenant")
    session.info[TENANT_CONTEXT_INFO_KEY] = normalized
    await session.execute(
        text("SELECT set_config(:setting_name, :tenant_id, true)"),
        {"setting_name": TENANT_CONTEXT_GUC, "tenant_id": normalized},
    )
    return normalized


@asynccontextmanager
async def tenant_session(tenant_id: str | UUID) -> AsyncIterator[AsyncSession]:
    """Open a process-wide session already bound to one validated tenant."""
    async with SessionFactory() as session:
        await set_transaction_tenant_context(session, tenant_id)
        yield session




def _build_engine(config: Settings, database_url: str | None = None) -> AsyncEngine:
    environment = getattr(config, "app_env", "test")
    runtime_profile = getattr(config, "runtime_profile_id", None)
    profile_sslmode = runtime_database_sslmode(runtime_profile)
    requires_verify_full = (
        environment in {"staging", "production"} and profile_sslmode == "verify-full"
    )
    authority = build_database_connection_authority(
        database_url or config.database_url,
        command_timeout_seconds=config.database_command_timeout_seconds,
        application_name="codestra-middleware/" + (runtime_profile or environment),
        secure_environment=requires_verify_full,
        validate_tls_files=requires_verify_full,
    )
    return create_async_engine(
        authority.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=config.database_pool_size,
        max_overflow=config.database_max_overflow,
        pool_timeout=config.database_pool_timeout_seconds,
        pool_recycle=config.database_pool_recycle_seconds,
        connect_args=authority.connect_args,
    )

engine: AsyncEngine = _build_engine(settings)
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


def get_engine() -> AsyncEngine:
    """Return the process-wide engine."""
    return engine


def configure(config: Settings | None = None, *, database_url: str | None = None) -> AsyncEngine:
    """Rebind the process-wide engine (and ``SessionFactory``) to ``config``.

    The previous engine is left for the caller to dispose; ``RuntimeContainer``
    does so on close. Consumers holding ``SessionFactory`` see the new engine
    because the sessionmaker is reconfigured in place.
    """
    global engine
    resolved = config or settings
    engine = _build_engine(resolved, database_url)
    SessionFactory.configure(bind=engine)
    return engine


async def dispose() -> None:
    """Close every pooled connection of the process-wide engine."""
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
