-- Apply once to an existing PostgreSQL database before deploying task #16.
ALTER TABLE publications
    ADD COLUMN IF NOT EXISTS revision_comment TEXT NULL,
    ADD COLUMN IF NOT EXISTS approval_version INTEGER NOT NULL DEFAULT 1;

-- The publications worker MUST claim only status='approved'.
-- 'approved_waiting', 'revision_requested', 'pending_approval' and 'rejected'
-- are intentionally non-publishable states.
CREATE INDEX IF NOT EXISTS ix_publications_approval_queue
    ON publications (status, created_at);
