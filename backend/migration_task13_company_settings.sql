-- Apply once to an existing PostgreSQL database before deploying task #13.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS brand_guidelines TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS enabled_platforms JSON NOT NULL DEFAULT '["vk", "telegram"]',
    ADD COLUMN IF NOT EXISTS approval_required BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS approval_mode VARCHAR(30) NOT NULL DEFAULT 'per_platform';
