-- Core-only encrypted reusable acquisition profiles.

CREATE TABLE IF NOT EXISTS session_profiles (
    id UUID PRIMARY KEY,
    name TEXT UNIQUE NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    backend TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    allowed_domains TEXT[] NOT NULL,
    ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12),
    key_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON session_profiles FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        REVOKE ALL ON session_profiles FROM crawltrove_worker;
    END IF;
END $$;

-- Human intervention is durable, but its control URLs are deliberately not.
CREATE TABLE IF NOT EXISTS live_sessions (
    id UUID PRIMARY KEY,
    task_id UUID UNIQUE NOT NULL REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    worker_id TEXT REFERENCES workers(id),
    remote_session_id TEXT,
    state TEXT NOT NULL CHECK (state IN
        ('starting','waiting','connected','resuming','closed','expired','cancelled')),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS live_session_tokens (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('view','control','worker')),
    token_hash BYTEA UNIQUE NOT NULL CHECK (octet_length(token_hash) = 32),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS live_session_tokens_active_idx
    ON live_session_tokens (session_id, scope, expires_at)
    WHERE consumed_at IS NULL;

REVOKE ALL ON live_sessions, live_session_tokens FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        REVOKE ALL ON live_sessions, live_session_tokens FROM crawltrove_worker;
    END IF;
END $$;

-- This is the only worker transition into a human-waiting state.  Its fence
-- is the active task lease plus the enrolled database identity.
CREATE OR REPLACE FUNCTION worker_api.start_live_session(
    p_task UUID, p_token UUID, p_backend TEXT, p_worker_id TEXT,
    p_ttl_seconds INTEGER, p_code TEXT DEFAULT 'human_input_required'
) RETURNS TABLE (id UUID, backend TEXT, expires_at TIMESTAMPTZ)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; task crawl_tasks; job crawl_jobs;
    session_id UUID; session_expires TIMESTAMPTZ;
BEGIN
    worker := worker_api._identity();
    IF p_worker_id IS NULL OR p_worker_id <> worker.id
       OR p_backend !~ '^[a-z][a-z0-9_-]{0,63}$'
       OR p_ttl_seconds NOT BETWEEN 300 AND 3600 THEN
        RAISE EXCEPTION 'invalid live session request' USING ERRCODE = '22023';
    END IF;
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL
      AND j.deadline_at > now()
    FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT j.* INTO job FROM crawl_jobs AS j WHERE j.id = task.job_id;
    session_expires := LEAST(now() + make_interval(secs => p_ttl_seconds), job.deadline_at);
    IF session_expires <= now() THEN RETURN; END IF;
    session_id := md5(random()::text || clock_timestamp()::text)::uuid;
    INSERT INTO live_sessions (id, task_id, backend, worker_id, state, expires_at)
    VALUES (session_id, task.id, p_backend, worker.id, 'waiting', session_expires);
    UPDATE crawl_tasks AS t SET state = 'waiting_input', error_code = p_code,
        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
        byte_budget_reserved = 0, artifact_budget_reserved = 0, updated_at = now()
    WHERE t.id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    -- Proxy leasing is optional in installations that have not enabled it yet.
    IF to_regclass('public.proxy_leases') IS NOT NULL THEN
        EXECUTE 'DELETE FROM public.proxy_leases WHERE task_id = $1' USING task.id;
    END IF;
    UPDATE crawl_jobs AS j SET reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved)
    WHERE j.id = task.job_id;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'waiting_input', actual_cost = '{}'
    WHERE task_id = task.id AND attempt_number = task.attempt_count;
    INSERT INTO crawl_events(job_id, task_id, event, metadata)
    VALUES (task.job_id, task.id, 'task_waiting_input',
        jsonb_build_object('session_id', session_id, 'backend', p_backend));
    RETURN QUERY SELECT session_id, p_backend, session_expires;
END $$;

-- Preserve the existing worker protocol while making every wait durable.
CREATE OR REPLACE FUNCTION worker_api.wait_for_input(p_task UUID, p_token UUID, p_code TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; session_id UUID;
BEGIN
    worker := worker_api._identity();
    SELECT started.id INTO session_id FROM worker_api.start_live_session(
        p_task, p_token, 'owned', worker.id, 900, p_code
    ) AS started;
    RETURN session_id IS NOT NULL;
END $$;

REVOKE ALL ON FUNCTION worker_api.start_live_session(UUID,UUID,TEXT,TEXT,INTEGER,TEXT)
    FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        GRANT EXECUTE ON FUNCTION worker_api.start_live_session(UUID,UUID,TEXT,TEXT,INTEGER,TEXT)
            TO crawltrove_worker;
    END IF;
END $$;
