"""Defer tenant RLS on access paths that are not yet tenant-bound.

Revision ID: 0071_defer_unbound_tenant_rls
Revises: 0070_agent_provisioning_lifecycle

This forward migration narrows PAS-88 RLS to paths proven safe on this exact
release lane. Runtime roles do not have BYPASSRLS, so a policy on an unbound
cross-tenant worker would hide work rather than isolate it.
"""

from alembic import op

revision = "0071_defer_unbound_tenant_rls"
down_revision = "0070_agent_provisioning_lifecycle"
branch_labels = None
depends_on = None

DEFERRED_RLS_TABLES = (
    "agent_call_event",
    "agent_call_state",
    "agent_provisioning_audit",
    "agent_provisioning_request",
    "agent_provisioning_step",
    "klyrow_delivery_analytics",
    "klyrow_delivery_event_inbox",
    "klyrow_mail_inbound",
    "social_accounts",
    "social_audit_events",
    "social_idempotency_records",
    "social_posts",
    "social_provider_events",
    "social_publish_jobs",
    "telnexa_delivery_analytics",
    "telnexa_delivery_event_inbox",
)

UUID_TENANT_TABLES = frozenset({
    "social_accounts",
    "social_audit_events",
    "social_idempotency_records",
    "social_posts",
    "social_provider_events",
    "social_publish_jobs",
})


def _tenant_expression(table: str) -> str:
    setting = "NULLIF(current_setting('app.tenant_id', true), '')"
    if table in UUID_TENANT_TABLES:
        return f"{setting}::uuid"
    return setting


def upgrade() -> None:
    for table in DEFERRED_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS codestra_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in reversed(DEFERRED_RLS_TABLES):
        tenant = _tenant_expression(table)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""CREATE POLICY codestra_tenant_isolation ON {table}
            USING (tenant_id = {tenant})
            WITH CHECK (tenant_id = {tenant})"""
        )
