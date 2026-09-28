BEGIN;

-- Forward-only tenant RLS scope correction for automation-v2.
-- Re-enable per table only after every call path binds app.tenant_id.

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
VALUES (3,'defer_unbound_tenant_rls') ON CONFLICT (version) DO UPDATE SET name=EXCLUDED.name;

COMMIT;
