-- Durable crawl state. Statements remain re-runnable like the earlier migrations.

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id UUID PRIMARY KEY,
    run_id BIGINT UNIQUE REFERENCES scrape_runs(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (state IN
        ('pending','running','completed','partial','failed','cancelled','timed_out')),
    config JSONB NOT NULL,
    max_pages INTEGER NOT NULL CHECK (max_pages BETWEEN 1 AND 100),
    max_bytes BIGINT NOT NULL CHECK (max_bytes > 0),
    max_artifact_bytes BIGINT NOT NULL CHECK (max_artifact_bytes > 0),
    discovered_count INTEGER NOT NULL DEFAULT 0,
    terminal_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_bytes BIGINT NOT NULL DEFAULT 0,
    artifact_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_artifact_bytes BIGINT NOT NULL DEFAULT 0,
    browser_page_count INTEGER NOT NULL DEFAULT 0,
    next_discovery_seq BIGINT NOT NULL DEFAULT 1,
    terminal_reason TEXT,
    deadline_at TIMESTAMPTZ NOT NULL,
    cancel_requested_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE,
    last_error_code TEXT,
    last_error_message TEXT,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS crawl_tasks (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    original_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    url_hash BYTEA NOT NULL,
    origin_key TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 5),
    discovery_seq BIGINT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    discovered_from_task_id UUID REFERENCES crawl_tasks(id),
    state TEXT NOT NULL CHECK (state IN
        ('pending','leased','retry_wait','succeeded','http_error','blocked_robots',
         'extraction_failed','permanent_failed','cancelled','waiting_input')),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 4),
    required_capabilities TEXT[] NOT NULL DEFAULT ARRAY['http']::TEXT[],
    byte_budget_reserved BIGINT NOT NULL DEFAULT 0,
    artifact_budget_reserved BIGINT NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    http_status INTEGER,
    error_class TEXT,
    error_code TEXT,
    error_message TEXT,
    retry_after_at TIMESTAMPTZ,
    result_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CONSTRAINT crawl_tasks_job_url_unique UNIQUE (job_id, url_hash)
);

CREATE TABLE IF NOT EXISTS crawl_results (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    final_url TEXT NOT NULL,
    status_code INTEGER,
    title TEXT NOT NULL DEFAULT '',
    markdown TEXT,
    markdown_ref TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL,
    downloaded_bytes BIGINT NOT NULL,
    artifact_bytes BIGINT NOT NULL,
    scraped_page_id BIGINT REFERENCES scraped_pages(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (markdown IS NOT NULL OR markdown_ref IS NOT NULL),
    CONSTRAINT crawl_results_one_per_task UNIQUE (task_id)
);

CREATE TABLE IF NOT EXISTS crawl_origins (
    origin_key TEXT PRIMARY KEY,
    next_request_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cooldown_until TIMESTAMPTZ,
    robots_body TEXT,
    robots_status INTEGER,
    robots_fetched_at TIMESTAMPTZ,
    robots_expires_at TIMESTAMPTZ,
    robots_etag TEXT,
    robots_last_modified TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_state TEXT NOT NULL DEFAULT 'closed'
        CHECK (circuit_state IN ('closed','open','half_open')),
    circuit_open_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crawl_origin_leases (
    origin_key TEXT NOT NULL REFERENCES crawl_origins(origin_key) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    lease_token UUID NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (origin_key, task_id),
    UNIQUE (task_id)
);

CREATE TABLE IF NOT EXISTS crawl_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    task_id UUID REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acquisition_attempts (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    route TEXT NOT NULL,
    provider TEXT,
    worker_id TEXT,
    proxy_id TEXT,
    session_profile_id UUID,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    duration_ms BIGINT,
    reserved_cost JSONB NOT NULL DEFAULT '{}',
    actual_cost JSONB NOT NULL DEFAULT '{}',
    outcome TEXT,
    block_reason TEXT,
    error_code TEXT,
    UNIQUE (task_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS crawl_tasks_claim_idx
    ON crawl_tasks (state, available_at, priority, discovery_seq);
CREATE INDEX IF NOT EXISTS crawl_tasks_lease_recovery_idx
    ON crawl_tasks (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS crawl_tasks_origin_idx ON crawl_tasks (origin_key, state);
CREATE INDEX IF NOT EXISTS crawl_events_job_idx ON crawl_events (job_id, id);
CREATE INDEX IF NOT EXISTS acquisition_attempts_task_idx ON acquisition_attempts (task_id, started_at);
