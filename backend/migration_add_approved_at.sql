-- Apply once to an existing PostgreSQL database before deploying this backend.
ALTER TABLE publications
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ NULL;
