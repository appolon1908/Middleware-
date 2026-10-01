from __future__ import annotations

from uuid import uuid4

import pytest

from app.db.session import (
    TENANT_CONTEXT_GUC,
    _restore_transaction_tenant_context,
    set_transaction_tenant_context,
)
from app.db.tenant_context import TENANT_CONTEXT_INFO_KEY


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.info: dict[str, str] = {}

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))


class _SyncSession:
    def __init__(self, info: dict[str, str]) -> None:
        self.info = info


@pytest.mark.asyncio
async def test_sets_transaction_local_postgres_context() -> None:
    session = _RecordingSession()
    tenant_id = str(uuid4())

    normalized = await set_transaction_tenant_context(session, tenant_id)  # type: ignore[arg-type]

    assert normalized == tenant_id
    assert len(session.calls) == 1
    sql, params = session.calls[0]
    assert "set_config" in sql
    assert params == {"setting_name": TENANT_CONTEXT_GUC, "tenant_id": tenant_id}
    assert session.info[TENANT_CONTEXT_INFO_KEY] == tenant_id


@pytest.mark.asyncio
async def test_rejects_missing_or_invalid_tenant_context_before_sql() -> None:
    session = _RecordingSession()

    for invalid in ("", "   ", "bad tenant", "/tenant", "\x00", "x" * 129, "tenant\nother"):
        with pytest.raises(ValueError):
            await set_transaction_tenant_context(session, invalid)  # type: ignore[arg-type]

    assert session.calls == []
    assert TENANT_CONTEXT_INFO_KEY not in session.info


@pytest.mark.asyncio
async def test_accepts_existing_text_tenant_identifier() -> None:
    session = _RecordingSession()
    normalized = await set_transaction_tenant_context(session, "COD")  # type: ignore[arg-type]
    assert normalized == "COD"
    assert session.calls[-1][1]["tenant_id"] == "COD"


@pytest.mark.asyncio
async def test_session_bound_to_one_tenant_cannot_switch_tenants() -> None:
    session = _RecordingSession()
    await set_transaction_tenant_context(session, "tenant-a")  # type: ignore[arg-type]
    await set_transaction_tenant_context(session, "tenant-a")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="different tenant"):
        await set_transaction_tenant_context(session, "tenant-b")  # type: ignore[arg-type]

    assert session.info[TENANT_CONTEXT_INFO_KEY] == "tenant-a"
    assert [params["tenant_id"] for _, params in session.calls] == ["tenant-a", "tenant-a"]


def test_every_new_transaction_restores_the_bound_tenant() -> None:
    connection = _RecordingConnection()
    session = _SyncSession({TENANT_CONTEXT_INFO_KEY: "tenant-a"})

    _restore_transaction_tenant_context(session, None, connection)

    assert connection.calls == [
        (
            "SELECT set_config(:setting_name, :tenant_id, true)",
            {"setting_name": TENANT_CONTEXT_GUC, "tenant_id": "tenant-a"},
        )
    ]


def test_unbound_session_transactions_carry_no_tenant_context() -> None:
    connection = _RecordingConnection()

    _restore_transaction_tenant_context(_SyncSession({}), None, connection)

    assert connection.calls == []
