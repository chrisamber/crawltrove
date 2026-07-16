-- 0001_init.sql — persistence foundation (Epic 1).
--
-- Every statement is idempotent (IF NOT EXISTS) so the migration is safe to
-- re-apply after restarts or interrupted deployments, even though
-- run_migrations() already gates on schema_migrations.
--
-- Design notes:
--   * scrape_runs.external_id holds the crawler's ephemeral job_id (or an
--     ad-hoc scrape's storage stem) so GET /api/crawl/{jobId} keeps correlating
--     while DB PKs stay clean. UNIQUE allows many NULLs in Postgres.
--   * scraped_pages.metadata is the full metadata block stored verbatim as
--     JSONB; content_hash is ALSO promoted to its own indexed column so the
--     music-metadata join key (metadata.dedup.content_hash) is addressable both
--     ways.
--   * signals flag, never filter — nothing here drops or rejects a page.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A recurring/named scrape DEFINITION (new concept). NULL schedule = manual only.
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT,
    kind        TEXT NOT NULL DEFAULT 'scrape',
    target_url  TEXT,
    params      JSONB NOT NULL DEFAULT '{}',
    schedule    TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One execution: a crawl jobId, an ad-hoc scrape, or a fired job definition.
CREATE TABLE IF NOT EXISTS scrape_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id    TEXT UNIQUE,                                  -- crawler job_id / scrape stem
    job_id         BIGINT REFERENCES scrape_jobs(id) ON DELETE SET NULL,  -- NULL = ad-hoc
    trigger        TEXT NOT NULL DEFAULT 'manual',
    status         TEXT NOT NULL DEFAULT 'pending',
    engine_used    TEXT,
    pages_count    INT NOT NULL DEFAULT 0,
    error_message  TEXT,
    raw_output_path TEXT,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scraped_pages (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         BIGINT REFERENCES scrape_runs(id) ON DELETE CASCADE,
    url            TEXT,
    status_code    INT,
    engine         TEXT,
    extractor      TEXT,
    content_hash   TEXT,
    extracted_text TEXT,
    raw_json_path  TEXT,
    raw_md_path    TEXT,
    raw_html_path  TEXT,
    metadata       JSONB NOT NULL DEFAULT '{}',                 -- full metadata block, verbatim
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extracted_records (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    page_id      BIGINT REFERENCES scraped_pages(id) ON DELETE CASCADE,
    source_url   TEXT,
    record_type  TEXT NOT NULL DEFAULT 'extract',
    data_json    JSONB,
    content_hash TEXT,
    confidence   REAL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scrape_errors (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     BIGINT REFERENCES scrape_runs(id) ON DELETE CASCADE,
    page_url   TEXT,
    stage      TEXT,
    message    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON scraped_pages (content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_url          ON scraped_pages (url);
CREATE INDEX IF NOT EXISTS idx_pages_run          ON scraped_pages (run_id);
CREATE INDEX IF NOT EXISTS idx_runs_job           ON scrape_runs (job_id);
CREATE INDEX IF NOT EXISTS idx_runs_status        ON scrape_runs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_due           ON scrape_jobs (next_run_at) WHERE enabled;
CREATE INDEX IF NOT EXISTS idx_pages_meta_gin     ON scraped_pages USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_records_page       ON extracted_records (page_id);
