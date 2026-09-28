from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENABLE_ALEMBIC = ROOT / "migrations/versions/0069_progressive_tenant_rls.py"
DEFER_ALEMBIC = ROOT / "migrations/versions/0071_defer_unbound_tenant_rls.py"
SECTION6_ALEMBIC = ROOT / "migrations/versions/0070_agent_provisioning_lifecycle.py"
CORE_ENABLE_SQL = ROOT / "migrations/0014_tenant_rls.sql"
CORE_RECOVERY_SQL = ROOT / "migrations/0013_command_recovery.sql"
CORE_DEFER_SQL = ROOT / "migrations/0015_defer_unbound_tenant_rls.sql"
AUTOMATION_ENABLE_SQL = ROOT / "migrations/automation/0002_tenant_rls.sql"
AUTOMATION_DEFER_SQL = ROOT / "migrations/automation/0003_defer_unbound_tenant_rls.sql"

SAFE_ALEMBIC_RLS = {"social_campaigns", "social_media_assets"}
SECTION6_RLS = {"agent_provisioning_repair_intent", "agent_webrtc_session"}


def _namespace(path: Path) -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(path.read_text(encoding="utf-8"), namespace)
    return namespace


def _enabled_tables(path: Path) -> set[str]:
    import re

    return set(
        re.findall(
            r"ALTER TABLE ([A-Za-z0-9_]+) ENABLE ROW LEVEL SECURITY",
            path.read_text(encoding="utf-8"),
        )
    )


def _disabled_tables(path: Path) -> set[str]:
    import re

    return set(
        re.findall(
            r"ALTER TABLE ([A-Za-z0-9_]+) DISABLE ROW LEVEL SECURITY",
            path.read_text(encoding="utf-8"),
        )
    )


def test_progressive_rls_effective_scope_is_explicit() -> None:
    enabled = _namespace(ENABLE_ALEMBIC)
    deferred = _namespace(DEFER_ALEMBIC)
    initially_enabled = set(enabled["RLS_TABLES"])
    deferred_tables = set(deferred["DEFERRED_RLS_TABLES"])

    assert initially_enabled == SAFE_ALEMBIC_RLS | deferred_tables
    assert SAFE_ALEMBIC_RLS.isdisjoint(deferred_tables)
    assert deferred["down_revision"] == "0070_agent_provisioning_lifecycle"

    section6 = SECTION6_ALEMBIC.read_text(encoding="utf-8")
    for table in SECTION6_RLS:
        assert table in section6
        assert "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in section6


def test_sql_managed_unbound_paths_are_deferred_forward_only() -> None:
    initially_core = _enabled_tables(CORE_ENABLE_SQL) | _enabled_tables(CORE_RECOVERY_SQL)
    deferred_core = _disabled_tables(CORE_DEFER_SQL)
    assert initially_core == deferred_core

    initially_automation = _enabled_tables(AUTOMATION_ENABLE_SQL)
    deferred_automation = _disabled_tables(AUTOMATION_DEFER_SQL)
    assert initially_automation == deferred_automation

    core_enable = CORE_ENABLE_SQL.read_text(encoding="utf-8")
    core_defer = CORE_DEFER_SQL.read_text(encoding="utf-8")
    auto_enable = AUTOMATION_ENABLE_SQL.read_text(encoding="utf-8")
    auto_defer = AUTOMATION_DEFER_SQL.read_text(encoding="utf-8")
    assert "VALUES (14,'tenant_rls')" in core_enable
    assert "VALUES (15,'defer_unbound_tenant_rls')" in core_defer
    assert "VALUES (2,'tenant_rls')" in auto_enable
    assert "VALUES (3,'defer_unbound_tenant_rls')" in auto_defer


def test_safe_rls_paths_still_fail_closed_and_type_correct() -> None:
    enabled = _namespace(ENABLE_ALEMBIC)
    source = ENABLE_ALEMBIC.read_text(encoding="utf-8")
    assert "current_setting('app.tenant_id', true)" in source
    assert "WITH CHECK" in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    tenant_expression = enabled["_tenant_expression"]
    assert tenant_expression("social_campaigns").endswith("::uuid")
    assert tenant_expression("social_media_assets").endswith("::uuid")

    section6 = SECTION6_ALEMBIC.read_text(encoding="utf-8")
    assert "current_setting('app.tenant_id', true)" in section6
    assert "WITH CHECK" in section6


def test_deferred_worker_tables_include_cross_tenant_queues() -> None:
    core_deferred = _disabled_tables(CORE_DEFER_SQL)
    for table in (
        "middleware_outbox",
        "middleware_inbox",
        "middleware_commands",
        "middleware_event_ledger",
        "middleware_command_dead_letters",
        "middleware_command_replays",
    ):
        assert table in core_deferred

    alembic_deferred = set(_namespace(DEFER_ALEMBIC)["DEFERRED_RLS_TABLES"])
    for table in (
        "agent_call_state",
        "klyrow_mail_inbound",
        "social_publish_jobs",
        "telnexa_delivery_event_inbox",
    ):
        assert table in alembic_deferred


def test_every_rls_covered_table_is_tenant_bound_per_inventory() -> None:
    """Every table RLS was ever enabled on must be a tenant-owned table whose
    tenant_id is NOT NULL in the tenant inventory, and the tables still covered
    after the forward deferral are exactly the safe ones."""
    from app.platform.tenant_inventory import scan_tenant_inventory

    tenant_tables = {
        row.table
        for row in scan_tenant_inventory(ROOT)
        if row.ownership == "tenant_owned"
        and row.tenant_representation == "tenant_id_not_null"
        and row.source.startswith("migrations/")
        and not row.table.startswith("callback_")
    }
    enabled = (
        set(_namespace(ENABLE_ALEMBIC)["RLS_TABLES"])
        | SECTION6_RLS
        | _enabled_tables(CORE_ENABLE_SQL)
        | _enabled_tables(CORE_RECOVERY_SQL)
        | _enabled_tables(AUTOMATION_ENABLE_SQL)
    )
    deferred = (
        set(_namespace(DEFER_ALEMBIC)["DEFERRED_RLS_TABLES"])
        | _disabled_tables(CORE_DEFER_SQL)
        | _disabled_tables(AUTOMATION_DEFER_SQL)
    )
    assert deferred <= enabled
    assert enabled - deferred == SAFE_ALEMBIC_RLS | SECTION6_RLS
    assert enabled <= tenant_tables
