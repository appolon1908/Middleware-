BEGIN;
CREATE TABLE IF NOT EXISTS middleware_command_dead_letters (
 id bigserial PRIMARY KEY, tenant_id text NOT NULL, command_id text NOT NULL,
 attempt_number integer NOT NULL CHECK(attempt_number>=0), reason_code text NOT NULL,
 error_class text NOT NULL, terminal_reason text NOT NULL, correlation_id text NOT NULL,
 principal_id text NOT NULL, capability text NOT NULL, poisoned boolean NOT NULL DEFAULT false,
 retry_exhausted boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,command_id) REFERENCES middleware_commands(tenant_id,command_id) ON DELETE RESTRICT,
 UNIQUE(tenant_id,command_id)
);
CREATE INDEX IF NOT EXISTS middleware_command_dead_letters_tenant_created_idx ON middleware_command_dead_letters(tenant_id,created_at DESC,id DESC);
DROP TRIGGER IF EXISTS middleware_command_dead_letters_immutable ON middleware_command_dead_letters;
CREATE TRIGGER middleware_command_dead_letters_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON middleware_command_dead_letters FOR EACH STATEMENT EXECUTE FUNCTION middleware_reject_immutable_mutation();

ALTER TABLE middleware_command_dead_letters ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_dead_letters;
CREATE POLICY codestra_tenant_isolation ON middleware_command_dead_letters
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
CREATE TABLE IF NOT EXISTS middleware_command_replays (
 replay_id uuid PRIMARY KEY, tenant_id text NOT NULL, original_command_id text NOT NULL,
 replay_command_id uuid, requested_by text NOT NULL, idempotency_key text NOT NULL,
 reason text NOT NULL, state text NOT NULL CHECK(state IN ('requested','scheduled','submitted','cancelled')),
 scheduled_at timestamptz, cancelled_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,original_command_id) REFERENCES middleware_commands(tenant_id,command_id) ON DELETE RESTRICT,
 UNIQUE(tenant_id,original_command_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS middleware_command_replays_history_idx ON middleware_command_replays(tenant_id,original_command_id,created_at,replay_id);

ALTER TABLE middleware_command_replays ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS codestra_tenant_isolation ON middleware_command_replays;
CREATE POLICY codestra_tenant_isolation ON middleware_command_replays
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));
INSERT INTO middleware_schema_migrations(version,name) VALUES(13,'0013_command_recovery') ON CONFLICT(version) DO UPDATE SET name=EXCLUDED.name;
COMMIT;
