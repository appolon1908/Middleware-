"""Durable tenant-scoped command replay and recovery journal.

Revision ID: 20261007_0005
Revises: 20260828_0004
"""

from alembic import op

revision = "20261007_0005"
down_revision = "20260828_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE connector_sdk.connector_command_journal (
            tenant_id uuid NOT NULL,
            environment text NOT NULL CHECK (environment IN ('development', 'staging', 'production')),
            connector_id text NOT NULL CHECK (connector_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
            idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[!-~]{8,180}$'),
            request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
            result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, environment, connector_id, idempotency_key)
        );
        ALTER TABLE connector_sdk.connector_command_journal ENABLE ROW LEVEL SECURITY;
        ALTER TABLE connector_sdk.connector_command_journal FORCE ROW LEVEL SECURITY;
        CREATE POLICY connector_command_journal_tenant_policy
            ON connector_sdk.connector_command_journal
            USING (tenant_id = NULLIF(current_setting('codestra.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('codestra.tenant_id', true), '')::uuid);
        COMMENT ON TABLE connector_sdk.connector_command_journal IS
            'Middleware worker journal: digest and redacted outcomes only; no payloads or credentials; no automatic expiry';
    """)


def downgrade() -> None:
    op.execute("DROP TABLE connector_sdk.connector_command_journal")
