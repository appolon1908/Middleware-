BEGIN;

-- Forward-only PAS-88 safety correction.
-- These SQL-managed runtime paths are not all transaction-tenant-bound on
-- this release lane. A non-owner runtime role without BYPASSRLS would make
-- cross-tenant queues/recovery work invisible, so defer RLS until each path
-- binds app.tenant_id before touching the table.

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_dead_letters;
ALTER TABLE middleware_command_dead_letters DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_replays;
ALTER TABLE middleware_command_replays DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_attempts;
ALTER TABLE middleware_command_attempts DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_audit;
ALTER TABLE middleware_command_audit DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_commands;
ALTER TABLE middleware_commands DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_cancellations;
ALTER TABLE middleware_communication_cancellations DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_events;
ALTER TABLE middleware_communication_events DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_idempotency;
ALTER TABLE middleware_communication_idempotency DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_messages;
ALTER TABLE middleware_communication_messages DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_provider_events;
ALTER TABLE middleware_communication_provider_events DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_communication_suppressions;
ALTER TABLE middleware_communication_suppressions DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_control_audit;
ALTER TABLE middleware_control_audit DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_control_mutations;
ALTER TABLE middleware_control_mutations DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_event_ledger;
ALTER TABLE middleware_event_ledger DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_inbox;
ALTER TABLE middleware_inbox DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_observability_incident_audit;
ALTER TABLE middleware_observability_incident_audit DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_observability_incident_events;
ALTER TABLE middleware_observability_incident_events DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_observability_incident_mutations;
ALTER TABLE middleware_observability_incident_mutations DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_observability_incidents;
ALTER TABLE middleware_observability_incidents DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_observability_notification_intents;
ALTER TABLE middleware_observability_notification_intents DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_operation_mutations;
ALTER TABLE middleware_operation_mutations DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_outbox;
ALTER TABLE middleware_outbox DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_outbox_attempt_events;
ALTER TABLE middleware_outbox_attempt_events DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_realtime_events;
ALTER TABLE middleware_realtime_events DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_realtime_tickets;
ALTER TABLE middleware_realtime_tickets DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_reconciliation_audit;
ALTER TABLE middleware_reconciliation_audit DISABLE ROW LEVEL SECURITY;

INSERT INTO middleware_schema_migrations(version,name)
VALUES (15,'defer_unbound_tenant_rls') ON CONFLICT (version) DO UPDATE SET name=EXCLUDED.name;

COMMIT;
