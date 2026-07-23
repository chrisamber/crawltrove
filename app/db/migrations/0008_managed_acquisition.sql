-- Native provider budgets are job-scoped.  Reservations prevent parallel
-- workers from spending the same provider credit twice.
CREATE TABLE IF NOT EXISTS crawl_provider_usage (
    job_id UUID NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    meter TEXT NOT NULL,
    limit_value NUMERIC NOT NULL CHECK (limit_value >= 0),
    reserved_value NUMERIC NOT NULL DEFAULT 0 CHECK (reserved_value >= 0),
    consumed_value NUMERIC NOT NULL DEFAULT 0 CHECK (consumed_value >= 0),
    PRIMARY KEY (job_id, provider, meter),
    CHECK (reserved_value + consumed_value <= limit_value)
);

ALTER TABLE acquisition_attempts
    ADD COLUMN IF NOT EXISTS remote_id TEXT,
    ADD COLUMN IF NOT EXISTS lease_token UUID,
    ADD COLUMN IF NOT EXISTS cost_estimated BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS acquisition_attempts_provider_route_idx
    ON acquisition_attempts (task_id, route, lease_token);
