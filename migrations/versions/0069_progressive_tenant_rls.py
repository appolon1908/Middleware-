"""Enable progressive tenant RLS on canonical tenant-owned tables.

Revision ID: 0069_progressive_tenant_rls
Revises: 0068_explicit_tenant_child_columns

Runtime roles are grant-only and have no BYPASSRLS (PAS-83).  We therefore
ENABLE RLS here without FORCE so the protected migration/table owner retains
an explicit maintenance path.  Application roles remain subject to policy.

Progressive scope: only tables whose every runtime access path already binds
``app.tenant_id`` for the transaction are covered.  Klyrow and social tables
are deferred: ``app/api/internal/klyrow_mail.py``,
``app/workers/klyrow_mail_odoo.py``, ``app/social/sql_repository.py``,
``app/social/queue.py`` and ``app/workers/postly_polling.py`` read or write
them without a tenant-bound transaction, and the polling/dispatch workers scan
across tenants.  They join this list once those paths bind tenant context.
"""

from alembic import op

revision = "0069_progressive_tenant_rls"
down_revision = "0068_explicit_tenant_child_columns"
branch_labels = None
depends_on = None


# Callback tables are intentionally excluded: migration 0052 already applies
# stronger tenant + campaign/role policies to that family. SQL-managed core
# and automation tables are owned by migrations/0014_tenant_rls.sql and
# migrations/automation/0002_tenant_rls.sql respectively.
RLS_TABLES = (
    "agent_call_event",
    "agent_call_state",
    "agent_provisioning_audit",
    "agent_provisioning_request",
    "agent_provisioning_step",
    "telnexa_delivery_analytics",
    "telnexa_delivery_event_inbox",
)


def _policy_sql(table: str) -> str:
    tenant = "NULLIF(current_setting('app.tenant_id', true), '')"
    return f"""CREATE POLICY codestra_tenant_isolation ON {table}
    USING (tenant_id = {tenant})
    WITH CHECK (tenant_id = {tenant})"""


def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS codestra_tenant_isolation ON {table}")
        op.execute(_policy_sql(table))


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS codestra_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
