-- MAX company-scoped destination and encrypted bot token.
-- Apply once to an existing database after migration_task17_18_security_publishing.sql.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS max_chat_id VARCHAR(80) NULL,
    ADD COLUMN IF NOT EXISTS max_access_token TEXT NULL;
