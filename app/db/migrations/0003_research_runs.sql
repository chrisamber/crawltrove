-- Research runs: a thin queryable index of deep-research jobs.
-- Files remain the source of truth (data/research/ artifacts + checkpoints);
-- this table is never read for resume — it exists for ops queries only.
CREATE TABLE IF NOT EXISTS research_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id        TEXT UNIQUE NOT NULL,
    query         TEXT,
    status        TEXT NOT NULL,
    rounds_run    INT,
    pages_scraped INT,
    llm_calls     INT,
    artifact_stem TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_research_status ON research_runs (status);
