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

-- Owned CONNECT nodes are core-managed.  Worker roles receive no direct
-- table grants; each proxy assignment is fenced to its crawl task lease.
CREATE TABLE IF NOT EXISTS proxy_nodes (
    id TEXT PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'),
    endpoint TEXT UNIQUE NOT NULL,
    regions TEXT[] NOT NULL DEFAULT '{}',
    state TEXT NOT NULL CHECK (state IN ('active','cooldown','revoked','offline')),
    credential_expires_at TIMESTAMPTZ NOT NULL,
    cooldown_until TIMESTAMPTZ,
    last_connected_ip INET,
    last_seen_at TIMESTAMPTZ,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proxy_leases (
    node_id TEXT NOT NULL REFERENCES proxy_nodes(id),
    task_id UUID PRIMARY KEY REFERENCES crawl_tasks(id) ON DELETE CASCADE,
    origin_key TEXT NOT NULL,
    lease_token UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS proxy_nodes_select_idx
    ON proxy_nodes (state, credential_expires_at, cooldown_until, id);
CREATE INDEX IF NOT EXISTS proxy_leases_active_idx
    ON proxy_leases (node_id, expires_at);

ALTER TABLE acquisition_attempts
    ADD COLUMN IF NOT EXISTS proxy_connected_ip INET;

REVOKE ALL ON proxy_nodes, proxy_leases FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        REVOKE ALL ON proxy_nodes, proxy_leases FROM crawltrove_worker;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION worker_api.task_capabilities(p_task UUID, p_token UUID)
RETURNS TEXT[]
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; capabilities TEXT[];
BEGIN
    worker := worker_api._identity();
    SELECT t.required_capabilities INTO capabilities FROM crawl_tasks t
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id;
    RETURN capabilities;
END $$;

-- Restricted remote-worker proxy protocol.  Only the core stores endpoints;
-- credentials remain mounted at the worker and are never read from PostgreSQL.
CREATE OR REPLACE FUNCTION worker_api.assign_proxy(
    p_task UUID, p_token UUID, p_origin TEXT
) RETURNS TABLE (node_id TEXT, endpoint TEXT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; task crawl_tasks; selected proxy_nodes;
BEGIN
    worker := worker_api._identity();
    IF NOT ('proxy' = ANY(worker.capabilities)) OR p_origin IS NULL OR p_origin = '' THEN
        RAISE EXCEPTION 'proxy capability denied' USING ERRCODE = '42501';
    END IF;
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL AND j.deadline_at > now()
    FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN; END IF;
    DELETE FROM proxy_leases WHERE expires_at <= now() OR task_id = task.id;
    SELECT n.* INTO selected FROM proxy_nodes n
    WHERE n.state = 'active' AND n.credential_expires_at > now()
      AND (n.cooldown_until IS NULL OR n.cooldown_until <= now())
    ORDER BY (SELECT count(*) FROM proxy_leases l
              WHERE l.node_id = n.id AND l.expires_at > now()), n.id
    FOR UPDATE OF n SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN; END IF;
    INSERT INTO proxy_leases (node_id, task_id, origin_key, lease_token, expires_at)
    VALUES (selected.id, task.id, p_origin, p_token, now() + interval '120 seconds');
    UPDATE acquisition_attempts SET proxy_id = selected.id
    WHERE task_id = task.id AND attempt_number = task.attempt_count AND finished_at IS NULL;
    RETURN QUERY SELECT selected.id, selected.endpoint;
END $$;

CREATE OR REPLACE FUNCTION worker_api.record_proxy_ip(
    p_task UUID, p_token UUID, p_node TEXT, p_address INET
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; matched BOOLEAN;
BEGIN
    worker := worker_api._identity();
    IF p_address IS NULL OR (
        family(p_address) = 4 AND (
            p_address <<= inet '0.0.0.0/8' OR p_address <<= inet '10.0.0.0/8'
            OR p_address <<= inet '100.64.0.0/10' OR p_address <<= inet '127.0.0.0/8'
            OR p_address <<= inet '169.254.0.0/16' OR p_address <<= inet '172.16.0.0/12'
            OR p_address <<= inet '192.0.0.0/24' OR p_address <<= inet '192.0.2.0/24'
            OR p_address <<= inet '192.88.99.0/24' OR p_address <<= inet '192.168.0.0/16'
            OR p_address <<= inet '198.18.0.0/15' OR p_address <<= inet '198.51.100.0/24'
            OR p_address <<= inet '203.0.113.0/24' OR p_address <<= inet '224.0.0.0/3'
        )
    ) OR (
        family(p_address) = 6 AND (
            NOT (p_address <<= inet '2000::/3') OR p_address <<= inet '2001:db8::/32'
            OR p_address <<= inet 'fc00::/7' OR p_address <<= inet 'fe80::/10'
            OR p_address <<= inet 'ff00::/8'
        )
    ) THEN
        RETURN FALSE;
    END IF;
    UPDATE proxy_nodes n SET last_connected_ip = p_address, last_seen_at = now()
    FROM proxy_leases l JOIN crawl_tasks t ON t.id = l.task_id
    WHERE n.id = p_node AND l.node_id = n.id AND l.task_id = p_task
      AND l.lease_token = p_token AND l.expires_at > now() AND t.state = 'leased'
      AND t.lease_token = p_token AND t.lease_owner = worker.id
    RETURNING TRUE INTO matched;
    IF NOT COALESCE(matched, FALSE) THEN RETURN FALSE; END IF;
    UPDATE acquisition_attempts SET proxy_id = p_node, proxy_connected_ip = p_address
    WHERE task_id = p_task
      AND attempt_number = (SELECT attempt_count FROM crawl_tasks WHERE id = p_task)
      AND finished_at IS NULL;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.proxy_failure(
    p_task UUID, p_token UUID, p_node TEXT, p_outcome TEXT,
    p_cooldown_seconds INTEGER DEFAULT 300, p_offline_after INTEGER DEFAULT 3
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; matched BOOLEAN;
BEGIN
    worker := worker_api._identity();
    IF p_outcome NOT IN ('blocked', 'transport')
       OR p_cooldown_seconds < 1 OR p_offline_after < 1 THEN
        RAISE EXCEPTION 'invalid proxy failure' USING ERRCODE = '22023';
    END IF;
    UPDATE proxy_nodes n SET failure_count = n.failure_count + 1,
        state = CASE WHEN p_outcome = 'blocked' THEN 'cooldown'
                     WHEN n.failure_count + 1 >= p_offline_after THEN 'offline'
                     ELSE n.state END,
        cooldown_until = CASE WHEN p_outcome = 'blocked'
                              THEN now() + (p_cooldown_seconds * interval '1 second')
                              ELSE n.cooldown_until END
    FROM proxy_leases l JOIN crawl_tasks t ON t.id = l.task_id
    WHERE n.id = p_node AND l.node_id = n.id AND l.task_id = p_task
      AND l.lease_token = p_token AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id
    RETURNING TRUE INTO matched;
    RETURN COALESCE(matched, FALSE);
END $$;

CREATE OR REPLACE FUNCTION worker_api.release_proxy(p_task UUID, p_token UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; removed BOOLEAN;
BEGIN
    worker := worker_api._identity(TRUE);
    DELETE FROM proxy_leases l USING acquisition_attempts a
    WHERE l.task_id = p_task AND l.lease_token = p_token AND a.task_id = p_task
      AND a.attempt_number = (SELECT attempt_count FROM crawl_tasks WHERE id = p_task)
      AND a.worker_id = worker.id
    RETURNING TRUE INTO removed;
    RETURN COALESCE(removed, FALSE);
END $$;

REVOKE ALL ON FUNCTION worker_api.assign_proxy(UUID,UUID,TEXT),
    worker_api.task_capabilities(UUID,UUID),
    worker_api.record_proxy_ip(UUID,UUID,TEXT,INET),
    worker_api.proxy_failure(UUID,UUID,TEXT,TEXT,INTEGER,INTEGER),
    worker_api.release_proxy(UUID,UUID) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        GRANT EXECUTE ON FUNCTION worker_api.assign_proxy(UUID,UUID,TEXT),
            worker_api.task_capabilities(UUID,UUID),
            worker_api.record_proxy_ip(UUID,UUID,TEXT,INET),
            worker_api.proxy_failure(UUID,UUID,TEXT,TEXT,INTEGER,INTEGER),
            worker_api.release_proxy(UUID,UUID) TO crawltrove_worker;
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
