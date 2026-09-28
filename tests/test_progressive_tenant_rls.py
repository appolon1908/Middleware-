from pathlib import Path

from app.platform.tenant_inventory import scan_tenant_inventory


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/versions/0069_progressive_tenant_rls.py"
LIFECYCLE_MIGRATION = ROOT / "migrations/versions/0070_agent_provisioning_lifecycle.py"
# Tables created by 0070 with RLS and the tenant policy from the start.
LIFECYCLE_RLS_TABLES = frozenset({"agent_provisioning_repair_intent", "agent_webrtc_session"})
CORE_SQL = ROOT / "migrations/0014_tenant_rls.sql"
AUTOMATION_SQL = ROOT / "migrations/automation/0002_tenant_rls.sql"

# Tenant-owned tables whose runtime access paths do not yet bind app.tenant_id
# for the transaction (or whose workers claim across tenants). RLS on these
# would make non-owner runtime roles see or write nothing. This set may only
# shrink: move a table into the RLS migrations once its paths are bound.
DEFERRED_RLS_TABLES = frozenset({
    # SQL core: storage/control/kernel/persistence/outbox worker paths.
    "middleware_command_attempts",
    "middleware_command_audit",
    "middleware_command_dead_letters",
    "middleware_command_replays",
    "middleware_commands",
    "middleware_communication_cancellations",
    "middleware_communication_events",
    "middleware_communication_idempotency",
    "middleware_communication_messages",
    "middleware_communication_provider_events",
    "middleware_communication_suppressions",
    "middleware_control_audit",
    "middleware_control_mutations",
    "middleware_event_ledger",
    "middleware_inbox",
    "middleware_observability_incident_audit",
    "middleware_observability_incident_events",
    "middleware_observability_incident_mutations",
    "middleware_observability_incidents",
    "middleware_observability_notification_intents",
    "middleware_operation_mutations",
    "middleware_outbox",
    "middleware_outbox_attempt_events",
    "middleware_reconciliation_audit",
    # Alembic: Klyrow intake and social polling/queue paths.
    "klyrow_delivery_analytics",
    "klyrow_delivery_event_inbox",
    "klyrow_mail_inbound",
    "social_accounts",
    "social_audit_events",
    "social_campaigns",
    "social_idempotency_records",
    "social_media_assets",
    "social_posts",
    "social_provider_events",
    "social_publish_jobs",
})


def _migration_namespace() -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(MIGRATION.read_text(encoding="utf-8"), namespace)
    return namespace


def _source_tables(prefix: str) -> set[str]:
    return {
        row.table
        for row in scan_tenant_inventory(ROOT)
        if row.ownership == "tenant_owned"
        and row.tenant_representation == "tenant_id_not_null"
        and row.source.startswith(prefix)
        and not row.table.startswith("callback_")
    }


def _enabled(sql: str) -> set[str]:
    prefix, suffix = "ALTER TABLE ", " ENABLE ROW LEVEL SECURITY"
    return {
        line.strip()[len(prefix):-len(suffix + ";")]
        for line in sql.splitlines()
        if line.strip().startswith(prefix) and line.strip().endswith(suffix + ";")
    }


def test_progressive_rls_partitions_every_tenant_table_into_covered_or_deferred() -> None:
    lifecycle = LIFECYCLE_MIGRATION.read_text(encoding="utf-8")
    for table in LIFECYCLE_RLS_TABLES:
        assert f'"{table}"' in lifecycle
    assert "ENABLE ROW LEVEL SECURITY" in lifecycle
    assert "WITH CHECK ({tenant_policy})" in lifecycle
    alembic = set(_migration_namespace()["RLS_TABLES"]) | LIFECYCLE_RLS_TABLES
    core = _enabled(CORE_SQL.read_text(encoding="utf-8"))
    automation = _enabled(AUTOMATION_SQL.read_text(encoding="utf-8"))
    expected_alembic = _source_tables("migrations/versions/")
    expected_automation = _source_tables("migrations/automation/")
    expected_core = _source_tables("migrations/") - expected_alembic - expected_automation

    covered = alembic | core | automation
    assert not covered & DEFERRED_RLS_TABLES
    assert alembic | (DEFERRED_RLS_TABLES & expected_alembic) == expected_alembic
    assert core | (DEFERRED_RLS_TABLES & expected_core) == expected_core
    assert automation == expected_automation
    assert DEFERRED_RLS_TABLES <= expected_alembic | expected_core


def test_rls_policies_are_fail_closed_on_the_transaction_tenant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "current_setting('app.tenant_id', true)" in source
    assert "WITH CHECK" in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    assert "migration/table owner retains" in source
    for sql in (CORE_SQL, AUTOMATION_SQL):
        text = sql.read_text(encoding="utf-8")
        for table in _enabled(text):
            assert f"CREATE POLICY codestra_tenant_isolation ON {table}" in text


def test_sql_rls_migrations_are_forward_only_and_receipted() -> None:
    core = CORE_SQL.read_text(encoding="utf-8")
    automation = AUTOMATION_SQL.read_text(encoding="utf-8")
    assert "VALUES (14,'0014_tenant_rls')" in core
    assert "VALUES (2,'tenant_rls')" in automation
    for text in (core, automation):
        assert "DISABLE ROW LEVEL SECURITY" not in text
        assert "BYPASSRLS" in text
        assert "WITH CHECK" in text
