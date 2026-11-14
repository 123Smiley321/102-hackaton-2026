-- Run once on an existing database before deploying this backend version.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS external_id VARCHAR(120);
UPDATE companies SET external_id = 'company-' || id::text WHERE external_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_companies_external_id ON companies (external_id);
ALTER TABLE companies ALTER COLUMN external_id SET NOT NULL;
