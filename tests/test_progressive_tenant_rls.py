import re
from pathlib import Path

from app.platform.tenant_inventory import scan_tenant_inventory


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/versions/0069_progressive_tenant_rls.py"
CORE_SQL = ROOT / "migrations/0014_tenant_rls.sql"
RECOVERY_SQL = ROOT / "migrations/0013_command_recovery.sql"
AUTOMATION_SQL = ROOT / "migrations/automation/0002_tenant_rls.sql"


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


SCOPE_CORE_SQL = ROOT / "migrations/0015_tenant_rls_scope.sql"
SCOPE_AUTOMATION_SQL = ROOT / "migrations/automation/0003_tenant_rls_scope.sql"
SCOPE_MIGRATION = ROOT / "migrations/versions/0071_tenant_rls_scope.py"
LIFECYCLE_MIGRATION = ROOT / "migrations/versions/0070_agent_provisioning_lifecycle.py"

# Tables whose RLS is in effect after the scope corrections. Every runtime
# path that touches them binds app.tenant_id (or none touches them). Grow
# this set only with a forward migration that re-enables RLS once a table's
# paths are tenant-bound.
EFFECTIVE_RLS_TABLES = frozenset({
    "agent_provisioning_repair_intent",
    "agent_webrtc_session",
    "social_campaigns",
    "social_media_assets",
})


def _sql_tables(path: Path, verb: str) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(re.findall(rf"ALTER TABLE (\w+) {verb} ROW LEVEL SECURITY", text))


def test_progressive_rls_is_scoped_to_tenant_bound_tables() -> None:
    enabled = (
        set(_migration_namespace()["RLS_TABLES"])
        | {"agent_provisioning_repair_intent", "agent_webrtc_session"}
        | _sql_tables(CORE_SQL, "ENABLE")
        | _sql_tables(RECOVERY_SQL, "ENABLE")
        | _sql_tables(AUTOMATION_SQL, "ENABLE")
    )
    scope: dict[str, object] = {}
    exec(SCOPE_MIGRATION.read_text(encoding="utf-8"), scope)
    disabled = (
        set(scope["DEFERRED_TABLES"])
        | _sql_tables(SCOPE_CORE_SQL, "DISABLE")
        | _sql_tables(SCOPE_AUTOMATION_SQL, "DISABLE")
    )
    assert disabled <= enabled
    assert enabled - disabled == EFFECTIVE_RLS_TABLES
    tenant_tables = _source_tables("migrations/")
    assert EFFECTIVE_RLS_TABLES <= tenant_tables
    assert enabled <= tenant_tables


def test_scope_migrations_are_forward_only_and_receipted() -> None:
    core = SCOPE_CORE_SQL.read_text(encoding="utf-8")
    automation = SCOPE_AUTOMATION_SQL.read_text(encoding="utf-8")
    assert "VALUES (15,'0015_tenant_rls_scope')" in core
    assert "VALUES (3,'tenant_rls_scope')" in automation
    for text in (core, automation):
        assert "ENABLE ROW LEVEL SECURITY" not in text
        assert "BYPASSRLS" in text
    source = SCOPE_MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "0070_agent_provisioning_lifecycle"' in source


def test_rls_is_fail_closed_and_type_correct() -> None:
    namespace = _migration_namespace()
    source = MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "current_setting('app.tenant_id', true)" in source
    assert "WITH CHECK" in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    assert "migration/table owner retains" in source

    tenant_expression = namespace["_tenant_expression"]
    assert tenant_expression("social_accounts").endswith("::uuid")
    assert not tenant_expression("agent_provisioning_request").endswith("::uuid")


def test_sql_rls_migrations_are_forward_only_and_receipted() -> None:
    core = CORE_SQL.read_text(encoding="utf-8")
    automation = AUTOMATION_SQL.read_text(encoding="utf-8")
    assert "VALUES (14,'tenant_rls')" in core
    assert "VALUES (2,'tenant_rls')" in automation
    for text in (core, automation):
        assert "DISABLE ROW LEVEL SECURITY" not in text
        assert "BYPASSRLS" in text
        assert "WITH CHECK" in text


def test_section6_successor_owns_new_rls_tables() -> None:
    source = (ROOT / "migrations/versions/0070_agent_provisioning_lifecycle.py").read_text()
    for table in ("agent_provisioning_repair_intent", "agent_webrtc_session"):
        assert table in source
    assert 'for table in ("agent_provisioning_repair_intent", "agent_webrtc_session")' in source
    assert 'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY' in source
    assert 'CREATE POLICY codestra_tenant_isolation ON {table}' in source
    assert "current_setting('app.tenant_id', true)" in source
