-- Task 17: publication status, remote identifiers and safe duplicate handling.
ALTER TABLE publications
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS publish_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS publish_error TEXT NULL;

CREATE INDEX IF NOT EXISTS ix_publications_due_approved
    ON publications (status, scheduled_at, id);

-- Task 18: company-scoped integration credentials. Raw API keys are never stored.
CREATE TABLE IF NOT EXISTS company_api_keys (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name VARCHAR(120) NOT NULL,
    key_prefix VARCHAR(16) NOT NULL,
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    scopes JSON NOT NULL DEFAULT '[]'::json,
    created_by VARCHAR(100) NOT NULL DEFAULT 'admin',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ NULL,
    last_used_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_company_api_keys_company_id ON company_api_keys(company_id);
CREATE INDEX IF NOT EXISTS ix_company_api_keys_key_prefix ON company_api_keys(key_prefix);

-- Separate company knowledge stores. Existing chat history remains company_id-scoped.
CREATE TABLE IF NOT EXISTS company_knowledge_items (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title VARCHAR(300) NOT NULL,
    content TEXT NOT NULL,
    source VARCHAR(500) NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_company_knowledge_items_company_id
    ON company_knowledge_items(company_id);
