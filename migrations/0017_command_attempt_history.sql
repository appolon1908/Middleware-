BEGIN;

-- Every dispatch attempt records the outbox lease it executed under, so the
-- worker identity and fencing token that produced a provider effect are part
-- of the immutable execution history rather than a log line.
ALTER TABLE middleware_command_attempts
    ADD COLUMN IF NOT EXISTS worker_id text,
    ADD COLUMN IF NOT EXISTS fencing_token bigint;
ALTER TABLE middleware_command_attempts
    DROP CONSTRAINT IF EXISTS middleware_command_attempts_fencing_token_check;
ALTER TABLE middleware_command_attempts
    ADD CONSTRAINT middleware_command_attempts_fencing_token_check
    CHECK (fencing_token IS NULL OR fencing_token >= 0);

-- An attempt is written once when it opens and updated only while it is
-- still in flight. Once it is terminal (completed/failed) nothing may rewrite
-- it, its identity and lease lineage never change, and rows are never
-- deleted: reconciliation must add evidence, not rewrite history.
CREATE OR REPLACE FUNCTION middleware_reject_attempt_history_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.state IN ('completed', 'failed') THEN
            RAISE EXCEPTION 'middleware_command_attempts: attempt % of command % is terminal and immutable',
                OLD.attempt_number, OLD.command_id USING ERRCODE = '55000';
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.command_id IS DISTINCT FROM OLD.command_id
           OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
           OR NEW.started_at IS DISTINCT FROM OLD.started_at
           OR (OLD.worker_id IS NOT NULL AND NEW.worker_id IS DISTINCT FROM OLD.worker_id)
           OR (OLD.fencing_token IS NOT NULL AND NEW.fencing_token IS DISTINCT FROM OLD.fencing_token) THEN
            RAISE EXCEPTION 'middleware_command_attempts: attempt identity and lease lineage are immutable'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% is append-only; % is prohibited', TG_TABLE_NAME, TG_OP
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS middleware_command_attempts_immutable_history ON middleware_command_attempts;
CREATE TRIGGER middleware_command_attempts_immutable_history
BEFORE UPDATE OR DELETE ON middleware_command_attempts
FOR EACH ROW EXECUTE FUNCTION middleware_reject_attempt_history_rewrite();

DROP TRIGGER IF EXISTS middleware_command_attempts_no_truncate ON middleware_command_attempts;
CREATE TRIGGER middleware_command_attempts_no_truncate
BEFORE TRUNCATE ON middleware_command_attempts
FOR EACH STATEMENT EXECUTE FUNCTION middleware_reject_immutable_mutation();

INSERT INTO middleware_schema_migrations(version,name)
VALUES (17,'0017_command_attempt_history') ON CONFLICT (version) DO UPDATE SET name=EXCLUDED.name;

COMMIT;
