from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.platform import router
from app.db.session import get_session


def client_for_rows(rows):
    db = AsyncMock()
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    db.execute.return_value = result
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: db
    # Isolate pagination from the independently tested JWT/role authority.
    for route in router.routes:
        original = getattr(route, "original_router", None)
        if original:
            continue
        if (
            getattr(route, "path", "") == "/platform/v1/services"
            and "GET" in route.methods
        ):
            for dependency in route.dependencies:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app), db


def test_services_page_uses_bounded_query_and_lookahead():
    client, db = client_for_rows(
        [{"service_id": "a"}, {"service_id": "b"}, {"service_id": "c"}]
    )
    with client:
        response = client.get(
            "/platform/v1/services?limit=2&offset=4&state=active&owner=platform"
        )
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"service_id": "a"}, {"service_id": "b"}],
        "limit": 2,
        "offset": 4,
        "next_offset": 6,
    }
    sql, values = db.execute.call_args.args
    assert "ORDER BY service_id" in str(sql)
    assert "LIMIT :limit OFFSET :offset" in str(sql)
    assert values == {"limit": 3, "offset": 4, "state": "active", "owner": "platform"}
    assert "active" not in str(sql)


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=501", "offset=-1", "state=unknown", "owner="]
)
def test_services_rejects_invalid_pagination_before_database(query):
    client, db = client_for_rows([])
    with client:
        response = client.get("/platform/v1/services?" + query)
    assert response.status_code == 422
    db.execute.assert_not_awaited()


def test_services_final_page_has_no_next_offset():
    client, _ = client_for_rows([])
    with client:
        response = client.get("/platform/v1/services?limit=2")
    assert response.json()["next_offset"] is None
