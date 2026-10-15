"""Shared PostgreSQL tenant-context primitives.

Tenant identity is validated by the authentication/authorization layer before
these helpers are called. These functions only normalize the already-authorized
identifier and bind it to the *current transaction* using PostgreSQL set_config
with is_local=true. They never create connection-persistent tenant state.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
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


def resolve_tenant_id(
    authorized_tenants: Iterable[str],
    requested_tenant_id: str | None = None,
) -> str:
    """Resolve exactly one tenant from verified authority.

    Multi-tenant callers must name the tenant; wildcards and an implicit
    first-tenant fallback are never accepted. Transport-agnostic so HTTP
    routes and workers share one fail-closed rule.
    """
    tenants = tuple(dict.fromkeys(str(item).strip() for item in authorized_tenants if str(item).strip()))
    if "*" in tenants:
        raise ValueError("wildcard tenant authorization is prohibited")
    if requested_tenant_id is not None:
        requested = canonical_tenant_id(requested_tenant_id)
        if requested not in tenants:
            raise ValueError("tenant authority does not cover requested tenant")
        return requested
    if len(tenants) != 1:
        raise ValueError("explicit tenant_id is required for multi-tenant authority")
    return canonical_tenant_id(tenants[0])


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
