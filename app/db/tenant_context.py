"""Shared PostgreSQL tenant-context primitives.

Tenant identity is validated by the authentication/authorization layer before
these helpers are called. These functions only normalize the already-authorized
identifier and bind it to the *current transaction* using PostgreSQL set_config
with is_local=true. They never create connection-persistent tenant state.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

TENANT_CONTEXT_GUC = "app.tenant_id"
TENANT_CONTEXT_INFO_KEY = "codestra.tenant_id"
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def canonical_tenant_id(tenant_id: str | UUID) -> str:
    """Validate and normalize the scalar tenant identifier used by PostgreSQL RLS.

    Middleware has both UUID-backed and text-backed tenant columns, so the
    transaction context carries a canonical scalar string; each RLS policy
    owns any table-specific type cast it requires.
    """
    value = str(tenant_id).strip()
    if not value:
        raise ValueError("tenant_id is required")
    if not TENANT_ID_PATTERN.fullmatch(value):
        raise ValueError("tenant_id contains unsupported characters")
    return value


async def set_asyncpg_transaction_tenant_context(
    connection: Any,
    tenant_id: str | UUID,
) -> str:
    """Bind one authorized tenant to an already-open asyncpg transaction."""
    normalized = canonical_tenant_id(tenant_id)
    is_in_transaction = getattr(connection, "is_in_transaction", None)
    if callable(is_in_transaction) and not is_in_transaction():
        raise RuntimeError("tenant context requires an active PostgreSQL transaction")
    await connection.execute(
        "SELECT set_config('app.tenant_id',$1,true)",
        normalized,
    )
    return normalized


@asynccontextmanager
async def asyncpg_tenant_connection(
    pool: Any,
    tenant_id: str | UUID,
) -> AsyncIterator[Any]:
    """Acquire an asyncpg connection and open a tenant-bound transaction."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            await set_asyncpg_transaction_tenant_context(connection, tenant_id)
            yield connection
