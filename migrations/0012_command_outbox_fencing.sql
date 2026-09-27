BEGIN;
ALTER TABLE middleware_outbox
  ADD COLUMN IF NOT EXISTS fencing_token bigint NOT NULL DEFAULT 0
  CHECK (fencing_token >= 0);
ALTER TABLE middleware_outbox_attempt_events
  ADD COLUMN IF NOT EXISTS fencing_token bigint;
INSERT INTO middleware_schema_migrations(version,name) VALUES(12,'0012_command_outbox_fencing')
ON CONFLICT(version) DO UPDATE SET name=EXCLUDED.name;
COMMIT;
