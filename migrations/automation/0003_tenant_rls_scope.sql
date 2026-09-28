BEGIN;

-- Progressive RLS scope correction (forward-only; 0002_tenant_rls is unchanged).
-- These tables are read or written by runtime paths that do not bind
-- app.tenant_id for the transaction (app/automation_v2.py store and dispatch paths). With RLS enabled and a
-- NOBYPASSRLS runtime role (PAS-83) those paths see no rows, and the outbox
-- worker's cross-tenant claim returns nothing. RLS is disabled here until
-- each path binds the tenant; a later forward migration re-enables it
-- table by table. Enforced by tests/test_progressive_tenant_rls.py.

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_approvals;
ALTER TABLE middleware_automation_approvals DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_audit;
ALTER TABLE middleware_automation_audit DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_dead_letters;
ALTER TABLE middleware_automation_dead_letters DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_dispatch_outbox;
ALTER TABLE middleware_automation_dispatch_outbox DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_job_steps;
ALTER TABLE middleware_automation_job_steps DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_jobs;
ALTER TABLE middleware_automation_jobs DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_reconciliation_runs;
ALTER TABLE middleware_automation_reconciliation_runs DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_automation_replay_requests;
ALTER TABLE middleware_automation_replay_requests DISABLE ROW LEVEL SECURITY;

INSERT INTO middleware_automation_schema_migrations(version,name)
VALUES (3,'tenant_rls_scope') ON CONFLICT (version) DO UPDATE SET name=EXCLUDED.name;

COMMIT;
