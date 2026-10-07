"""Durable command recovery and isolation against disposable PostgreSQL."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from codestra_connector_runtime.api.config import RuntimeSettings
from codestra_connector_runtime.api.database import Database
from middleware.connector_sdk import (
    CommandOutcome,
    ConnectorRuntime,
    StaticCapabilityProvider,
)
from middleware.connector_sdk.operations import request_fingerprint
from tests.test_connector_sdk_runtime_authority import fixture_runtime
from tests.test_connector_sdk_v1 import FakeAdapter

pytestmark = pytest.mark.postgres


def database():
    settings = RuntimeSettings(
        database_url=os.environ["ADMIN_DATABASE_URL"],
        cursor_hmac_key="x" * 48,
        body_encryption_key_file="/tmp/unused-mw03-key",
    )
    return Database.create(settings)


def test_durable_replay_after_worker_restart():
    from codestra_connector_runtime.command_journal import PostgresOperationStore

    class CountingAdapter(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return super().execute_command(request)

    fixture, _, request = fixture_runtime(CountingAdapter)
    request = replace(
        request,
        context=replace(request.context, idempotency_key="durable-" + uuid4().hex),
    )
    db = database()
    capabilities = StaticCapabilityProvider(
        {(request.context.tenant_id, "EMAIL_DELIVERY"): True}
    )
    try:
        first = ConnectorRuntime(
            fixture.registry,
            capabilities,
            operations=PostgresOperationStore(db, "development"),
        ).execute(request)
        second = ConnectorRuntime(
            fixture.registry,
            capabilities,
            operations=PostgresOperationStore(db, "development"),
        ).execute(request)
        assert first == second
        assert CountingAdapter.submits == 1
    finally:
        db.dispose()


def test_crash_marker_recovers_via_reconciliation_only():
    from codestra_connector_runtime.command_journal import PostgresOperationStore
    from middleware.connector_sdk import CommandResult

    class RecoveryAdapter(FakeAdapter):
        def execute_command(self, request):
            pytest.fail("crashed submission must never be sent again")

    fixture, _, request = fixture_runtime(RecoveryAdapter)
    request = replace(
        request,
        context=replace(request.context, idempotency_key="crash-" + uuid4().hex),
    )
    db = database()
    try:
        store = PostgresOperationStore(db, "development")
        digest = request_fingerprint(
            request, fixture.registry.get(request.connector_id).manifest_digest
        )
        with store.acquire(request, digest) as lease:
            lease.save(
                CommandResult(CommandOutcome.UNKNOWN, request.command_id), attempts=1
            )
        recovered = ConnectorRuntime(
            fixture.registry,
            StaticCapabilityProvider(
                {(request.context.tenant_id, "EMAIL_DELIVERY"): True}
            ),
            operations=PostgresOperationStore(db, "development"),
        ).execute(request)
        assert recovered.outcome is CommandOutcome.COMPLETED
    finally:
        db.dispose()


def test_journal_forces_tenant_rls():
    db = database()
    try:
        with db.session() as session:
            row = session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='connector_sdk.connector_command_journal'::regclass"
                )
            ).one()
            assert row == (True, True)
    finally:
        db.dispose()


def test_parallel_workers_cannot_submit_the_same_command():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from codestra_connector_runtime.command_journal import PostgresOperationStore
    from middleware.connector_sdk import ConnectorVersionConflictError

    started, release = Event(), Event()

    class BlockingAdapter(FakeAdapter):
        def execute_command(self, request):
            started.set()
            assert release.wait(10)
            return super().execute_command(request)

    fixture, _, request = fixture_runtime(BlockingAdapter)
    request = replace(
        request,
        context=replace(request.context, idempotency_key="parallel-" + uuid4().hex),
    )
    db = database()
    try:
        capabilities = StaticCapabilityProvider(
            {(request.context.tenant_id, "EMAIL_DELIVERY"): True}
        )
        first = ConnectorRuntime(
            fixture.registry,
            capabilities,
            operations=PostgresOperationStore(db, "development"),
        )
        second = ConnectorRuntime(
            fixture.registry,
            capabilities,
            operations=PostgresOperationStore(db, "development"),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(first.execute, request)
            assert started.wait(10)
            try:
                with pytest.raises(ConnectorVersionConflictError):
                    second.execute(request)
            finally:
                release.set()
            assert pending.result().outcome is CommandOutcome.COMPLETED
    finally:
        db.dispose()


def test_non_owner_role_cannot_read_another_tenants_journal():
    from uuid import UUID
    from codestra_connector_runtime.command_journal import PostgresOperationStore

    _, _, request = fixture_runtime()
    request = replace(
        request, context=replace(request.context, idempotency_key="rls-" + uuid4().hex)
    )
    db = database()
    try:
        with PostgresOperationStore(db, "development").acquire(request, "a" * 64):
            pass
        with db.session(UUID(request.context.tenant_id)) as session:
            session.execute(text("SET LOCAL ROLE connector_app_test"))
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM connector_sdk.connector_command_journal WHERE idempotency_key=:key"
                    ),
                    {"key": request.context.idempotency_key},
                ).scalar_one()
                == 1
            )
        with db.session(uuid4()) as session:
            session.execute(text("SET LOCAL ROLE connector_app_test"))
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM connector_sdk.connector_command_journal WHERE idempotency_key=:key"
                    ),
                    {"key": request.context.idempotency_key},
                ).scalar_one()
                == 0
            )
    finally:
        db.dispose()


def test_worker_factory_requires_authority_and_disabled_effects_win():
    from codestra_connector_runtime.worker_runtime import build_worker_runtime
    from middleware.connector_sdk import CapabilityDisabledError

    fixture, _, request = fixture_runtime()
    db = database()
    try:
        settings = RuntimeSettings(
            database_url=os.environ["ADMIN_DATABASE_URL"],
            cursor_hmac_key="x" * 48,
            body_encryption_key_file="/tmp/unused",
        )
        runtime = build_worker_runtime(
            settings=settings,
            database=db,
            registry=fixture.registry,
            capabilities=StaticCapabilityProvider(
                {(request.context.tenant_id, "EMAIL_DELIVERY"): True}
            ),
            authorize=lambda _: True,
        )
        with pytest.raises(CapabilityDisabledError):
            runtime.execute(request)
    finally:
        db.dispose()


def test_journal_rejects_sensitive_result_before_persistence():
    from codestra_connector_runtime.command_journal import PostgresOperationStore
    from middleware.connector_sdk import CommandNotAllowedError, CommandResult

    _, _, request = fixture_runtime()
    request = replace(
        request,
        context=replace(request.context, idempotency_key="redaction-" + uuid4().hex),
    )
    db = database()
    try:
        with PostgresOperationStore(db, "development").acquire(
            request, "b" * 64
        ) as lease:
            with pytest.raises(CommandNotAllowedError):
                lease.save(
                    CommandResult(
                        CommandOutcome.COMPLETED,
                        request.command_id,
                        safe_result={"password": "forbidden"},
                    ),
                    attempts=1,
                )
            assert lease.snapshot.result is None
    finally:
        db.dispose()


@pytest.mark.parametrize("key", ["short", "x" * 181, "bad key 0001", "bad\nkey0001"])
def test_database_rejects_invalid_journal_keys(key):
    from sqlalchemy.exc import IntegrityError
    from codestra_connector_runtime.command_journal import PostgresOperationStore

    _, _, request = fixture_runtime()
    request = replace(request, context=replace(request.context, idempotency_key=key))
    db = database()
    try:
        with pytest.raises(IntegrityError):
            with PostgresOperationStore(db, "development").acquire(request, "a" * 64):
                pass
    finally:
        db.dispose()


def test_journal_rejects_changed_actor_and_isolates_environment():
    from codestra_connector_runtime.command_journal import PostgresOperationStore
    from middleware.connector_sdk import ConnectorVersionConflictError, CommandResult

    fixture, _, request = fixture_runtime()
    request = replace(
        request,
        context=replace(request.context, idempotency_key="identity-" + uuid4().hex),
    )
    digest = fixture.registry.get(request.connector_id).manifest_digest
    db = database()
    try:
        store = PostgresOperationStore(db, "development")
        with store.acquire(request, request_fingerprint(request, digest)) as lease:
            lease.save(
                CommandResult(CommandOutcome.UNKNOWN, request.command_id), attempts=1
            )
        changed = replace(
            request, context=replace(request.context, actor_id="another-actor")
        )
        with pytest.raises(ConnectorVersionConflictError):
            with store.acquire(changed, request_fingerprint(changed, digest)):
                pass
        with PostgresOperationStore(db, "staging").acquire(
            request, request_fingerprint(request, digest)
        ) as lease:
            assert lease.snapshot.result is None
            assert lease.snapshot.attempts == 0
    finally:
        db.dispose()


def test_worker_uses_durable_journal_and_emits_only_safe_fields():
    from structlog.testing import capture_logs
    from codestra_connector_runtime.worker_runtime import build_worker_runtime

    class CountingAdapter(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return super().execute_command(request)

    fixture, _, request = fixture_runtime(CountingAdapter)
    request = replace(
        request,
        context=replace(request.context, idempotency_key="worker-" + uuid4().hex),
    )
    db = database()
    settings = RuntimeSettings(
        database_url=os.environ["ADMIN_DATABASE_URL"],
        cursor_hmac_key="x" * 48,
        body_encryption_key_file="/tmp/unused",
        external_effects_enabled=True,
    )
    try:
        with capture_logs() as events:
            for _ in range(2):
                runtime = build_worker_runtime(
                    settings=settings,
                    database=db,
                    registry=fixture.registry,
                    capabilities=StaticCapabilityProvider(
                        {(request.context.tenant_id, "EMAIL_DELIVERY"): True}
                    ),
                    authorize=lambda _: True,
                )
                assert runtime.execute(request).outcome is CommandOutcome.COMPLETED
        assert CountingAdapter.submits == 1
        assert [item["event"] for item in events] == [
            "connector_command_submitted",
            "connector_command_settled",
            "connector_command_replayed",
        ]
        assert all(
            set(item)
            == {
                "event",
                "log_level",
                "connector_id",
                "command_id",
                "correlation_id",
                "outcome",
                "error_code",
                "attempts",
            }
            for item in events
        )
    finally:
        db.dispose()
