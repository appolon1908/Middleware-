"""Narrow progressive tenant RLS to tenant-bound access paths.

Revision ID: 0071_tenant_rls_scope
Revises: 0070_agent_provisioning_lifecycle

0069_progressive_tenant_rls enabled RLS on tables that runtime paths still
read or write without binding ``app.tenant_id`` for the transaction
(agent realtime/presence/campaign/queue/tenant reads, activity and session
context, Klyrow intake, social repository/queue/polling, Telnexa ingress).
With a NOBYPASSRLS runtime role (PAS-83) those paths would see no rows.
This forward-only revision disables RLS on those tables; 0069 is unchanged.
A later revision re-enables each table once its paths bind the tenant.
Enforced by tests/test_progressive_tenant_rls.py.
"""

from alembic import op

revision = "0071_tenant_rls_scope"
down_revision = "0070_agent_provisioning_lifecycle"
branch_labels = None
depends_on = None


DEFERRED_TABLES = (
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


def upgrade() -> None:
    for table in DEFERRED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS codestra_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in reversed(DEFERRED_TABLES):
        tenant = "NULLIF(current_setting('app.tenant_id', true), '')"
        if table in UUID_TENANT_TABLES:
            tenant += "::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""CREATE POLICY codestra_tenant_isolation ON {table}
    USING (tenant_id = {tenant})
    WITH CHECK (tenant_id = {tenant})"""
        )
