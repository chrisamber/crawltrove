-- Remote workers may execute only this narrowly scoped function protocol.

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    db_role NAME UNIQUE NOT NULL,
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    protocol_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','draining','revoked','incompatible')),
    artifact_bucket TEXT NOT NULL,
    artifact_prefix TEXT UNIQUE NOT NULL,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE SCHEMA IF NOT EXISTS worker_api;
REVOKE ALL ON workers FROM PUBLIC;
REVOKE ALL ON SCHEMA worker_api FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        CREATE ROLE crawltrove_worker NOLOGIN;
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END $$;

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END $$;

CREATE OR REPLACE FUNCTION worker_api._identity(p_allow_draining BOOLEAN DEFAULT FALSE)
RETURNS workers
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers;
BEGIN
    SELECT * INTO worker FROM workers
    WHERE db_role = session_user AND protocol_version = 1
      AND (state = 'active' OR (p_allow_draining AND state = 'draining'));
    IF NOT FOUND THEN
        RAISE EXCEPTION 'worker identity is not active and protocol-compatible'
            USING ERRCODE = '42501';
    END IF;
    UPDATE workers SET last_seen_at = now() WHERE id = worker.id;
    RETURN worker;
END $$;

CREATE OR REPLACE FUNCTION worker_api.claim(p_capabilities TEXT[])
RETURNS TABLE (id UUID, job_id UUID, url TEXT, normalized_url TEXT, origin_key TEXT,
    depth INTEGER, attempt INTEGER, lease_token UUID, deadline_at TIMESTAMPTZ,
    config JSONB, byte_allowance BIGINT, artifact_allowance BIGINT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; task crawl_tasks; job crawl_jobs; origin crawl_origins;
    token UUID; bytes BIGINT; artifacts BIGINT;
BEGIN
    worker := worker_api._identity();
    IF NOT (p_capabilities <@ worker.capabilities) THEN
        RAISE EXCEPTION 'capability denied' USING ERRCODE = '42501';
    END IF;
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.state IN ('pending','retry_wait') AND t.available_at <= now()
      AND t.required_capabilities <@ p_capabilities AND j.cancel_requested_at IS NULL
      AND j.deadline_at > now()
    ORDER BY t.priority, t.available_at, t.discovery_seq
    FOR UPDATE OF t SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT j.* INTO job FROM crawl_jobs j WHERE j.id = task.job_id FOR UPDATE;
    SELECT o.* INTO origin FROM crawl_origins o
    WHERE o.origin_key = task.origin_key FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF origin.circuit_state = 'open'
       AND GREATEST(origin.cooldown_until, origin.circuit_open_until) > now() THEN
        UPDATE crawl_tasks SET available_at = GREATEST(
            origin.cooldown_until, origin.circuit_open_until
        ), updated_at = now() WHERE crawl_tasks.id = task.id;
        RETURN;
    END IF;
    IF origin.circuit_state = 'open' THEN
        UPDATE crawl_origins SET circuit_state = 'half_open', updated_at = now()
        WHERE origin_key = task.origin_key;
    END IF;
    IF origin.next_request_at > now() OR origin.cooldown_until > now()
       OR EXISTS (SELECT 1 FROM crawl_origin_leases l
                  WHERE l.origin_key = task.origin_key) THEN
        RETURN;
    END IF;
    bytes := LEAST(job.max_bytes - job.downloaded_bytes - job.reserved_bytes, 10485760);
    artifacts := LEAST(job.max_artifact_bytes - job.artifact_bytes - job.reserved_artifact_bytes, 31457280);
    IF bytes <= 0 OR artifacts <= 0 THEN RETURN; END IF;
    token := md5(random()::text || clock_timestamp()::text)::uuid;
    UPDATE crawl_jobs AS j SET reserved_bytes = j.reserved_bytes + bytes,
        reserved_artifact_bytes = reserved_artifact_bytes + artifacts,
        state = CASE WHEN state = 'pending' THEN 'running' ELSE state END,
        started_at = COALESCE(started_at, now()) WHERE j.id = job.id;
    UPDATE crawl_tasks AS t SET state = 'leased', lease_owner = worker.id, lease_token = token,
        lease_expires_at = now() + interval '120 seconds', attempt_count = attempt_count + 1,
        byte_budget_reserved = bytes, artifact_budget_reserved = artifacts, updated_at = now()
    WHERE t.id = task.id RETURNING t.* INTO task;
    INSERT INTO crawl_origin_leases (origin_key, task_id, lease_token, expires_at)
    VALUES (task.origin_key, task.id, token, now() + interval '120 seconds');
    UPDATE crawl_origins AS o SET next_request_at = now() +
        ((job.config->>'minDelayMs')::integer * interval '1 millisecond'), updated_at = now()
    WHERE o.origin_key = task.origin_key;
    INSERT INTO acquisition_attempts (id, job_id, task_id, attempt_number, route, provider, worker_id, reserved_cost)
    VALUES (md5(random()::text || clock_timestamp()::text)::uuid, job.id, task.id,
        task.attempt_count, COALESCE(job.config->>'engine','auto'),
        job.config#>>'{acquisition,provider}', worker.id,
        jsonb_build_object('downloaded_bytes', bytes, 'artifact_bytes', artifacts));
    RETURN QUERY SELECT task.id, task.job_id, task.original_url, task.normalized_url,
        task.origin_key, task.depth, task.attempt_count, token, job.deadline_at,
        job.config, bytes, artifacts;
END $$;

CREATE OR REPLACE FUNCTION worker_api.heartbeat(p_task UUID, p_token UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; ok BOOLEAN;
BEGIN
    worker := worker_api._identity();
    UPDATE crawl_tasks t SET lease_expires_at = now() + interval '120 seconds', updated_at = now()
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id
      AND EXISTS (SELECT 1 FROM crawl_jobs j WHERE j.id = t.job_id
                  AND j.cancel_requested_at IS NULL AND j.deadline_at > now())
    RETURNING TRUE INTO ok;
    IF NOT COALESCE(ok, FALSE) THEN RETURN FALSE; END IF;
    UPDATE crawl_origin_leases SET expires_at = now() + interval '120 seconds'
    WHERE task_id = p_task AND lease_token = p_token;
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION worker_api.reserve_browser_navigation(p_task UUID, p_token UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; reserved UUID;
BEGIN
    worker := worker_api._identity();
    UPDATE crawl_jobs j SET browser_page_count = browser_page_count + 1
    FROM crawl_tasks t
    WHERE t.id = p_task AND t.job_id = j.id AND t.state = 'leased'
      AND t.lease_token = p_token AND t.lease_owner = worker.id
      AND j.cancel_requested_at IS NULL AND j.deadline_at > now()
      AND j.browser_page_count < (j.config->>'maxBrowserPages')::INTEGER
    RETURNING j.id INTO reserved;
    RETURN reserved IS NOT NULL;
END $$;

CREATE OR REPLACE FUNCTION worker_api.robots_cache(p_origin TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; cached JSONB;
BEGIN
    worker := worker_api._identity();
    SELECT jsonb_build_object(
        'robots_body', o.robots_body, 'robots_status', o.robots_status,
        'robots_fetched_at', o.robots_fetched_at,
        'robots_expires_at', o.robots_expires_at
    ) INTO cached FROM crawl_origins o
    WHERE o.origin_key = p_origin
      AND EXISTS (SELECT 1 FROM crawl_tasks t
          WHERE t.origin_key = p_origin AND t.state = 'leased'
            AND t.lease_owner = worker.id);
    RETURN cached;
END $$;

CREATE OR REPLACE FUNCTION worker_api.store_robots(
    p_origin TEXT, p_body TEXT, p_status INTEGER
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; changed TEXT;
BEGIN
    worker := worker_api._identity();
    UPDATE crawl_origins o SET robots_body = p_body, robots_status = p_status,
        robots_fetched_at = now(), robots_expires_at = now() + interval '24 hours',
        updated_at = now()
    WHERE o.origin_key = p_origin
      AND EXISTS (SELECT 1 FROM crawl_tasks t
          WHERE t.origin_key = p_origin AND t.state = 'leased'
            AND t.lease_owner = worker.id)
    RETURNING o.origin_key INTO changed;
    RETURN changed IS NOT NULL;
END $$;

CREATE OR REPLACE FUNCTION worker_api.block_robots(
    p_task UUID, p_token UUID, p_code TEXT
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks;
BEGIN
    worker := worker_api._identity();
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL
      AND j.deadline_at > now() FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    UPDATE crawl_tasks SET state = 'blocked_robots', error_class = 'policy',
        error_code = p_code, lease_owner = NULL, lease_token = NULL,
        lease_expires_at = NULL, byte_budget_reserved = 0,
        artifact_budget_reserved = 0, finished_at = now(), updated_at = now()
    WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET
        reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved),
        terminal_count = terminal_count + 1, blocked_count = blocked_count + 1
    WHERE id = task.job_id;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'blocked_robots',
        error_code = p_code, actual_cost = '{}'
    WHERE task_id = task.id AND attempt_number = task.attempt_count;
    INSERT INTO crawl_events(job_id, task_id, event, metadata)
    VALUES (task.job_id, task.id, 'task_blocked_robots', jsonb_build_object('error_code', p_code));
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.complete(
    p_task UUID, p_token UUID, p_artifact JSONB, p_metadata JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE worker workers; task crawl_tasks; job crawl_jobs; used BIGINT; size BIGINT;
    uri TEXT; sha TEXT; media TEXT; reported BIGINT; discovered JSONB;
    discovered_url TEXT; discovered_hash TEXT; discovered_origin TEXT; inserted UUID;
BEGIN
    worker := worker_api._identity();
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL AND j.deadline_at > now()
    FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    SELECT * INTO job FROM crawl_jobs WHERE id = task.job_id;
    uri := p_artifact->>'uri'; sha := p_artifact->>'sha256'; media := p_artifact->>'media_type';
    size := COALESCE((p_artifact->>'size')::BIGINT, -1);
    reported := GREATEST(0, COALESCE((p_metadata->>'downloaded_bytes')::BIGINT, 0));
    used := reported;
    IF uri IS NULL OR worker.artifact_bucket IS NULL
       OR uri NOT LIKE 's3://' || worker.artifact_bucket || '/' || worker.artifact_prefix || '%'
       OR sha !~ '^[0-9a-fA-F]{64}$' OR media IS NULL OR media = ''
       OR media <> 'text/markdown' OR size < 0 OR size > task.artifact_budget_reserved
       OR reported > task.byte_budget_reserved THEN
        RETURN FALSE;
    END IF;
    INSERT INTO crawl_results (id, task_id, final_url, status_code, title, markdown,
        markdown_ref, metadata, content_sha256, downloaded_bytes, artifact_bytes)
    VALUES (task.id, task.id, COALESCE(p_metadata->>'final_url', task.normalized_url),
        NULLIF(p_metadata->>'status_code','')::INTEGER, COALESCE(p_metadata->>'title',''),
        NULL, uri, p_metadata || jsonb_build_object('media_type', media), lower(sha), used, size)
    ON CONFLICT (task_id) DO NOTHING;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    UPDATE crawl_tasks SET state = 'succeeded', result_id = task.id, lease_owner = NULL,
        lease_token = NULL, lease_expires_at = NULL, byte_budget_reserved = 0,
        artifact_budget_reserved = 0, finished_at = now(), updated_at = now() WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved),
        downloaded_bytes = downloaded_bytes + used, artifact_bytes = artifact_bytes + size,
        terminal_count = terminal_count + 1, succeeded_count = succeeded_count + 1 WHERE id = job.id;
    UPDATE acquisition_attempts SET finished_at = now(),
        duration_ms = EXTRACT(EPOCH FROM now() - started_at) * 1000,
        actual_cost = jsonb_build_object('downloaded_bytes', used, 'artifact_bytes', size),
        outcome = 'succeeded' WHERE task_id = task.id AND attempt_number = task.attempt_count;
    UPDATE crawl_origins SET consecutive_failures = 0, circuit_state = 'closed',
        circuit_open_until = NULL, cooldown_until = NULL, updated_at = now()
    WHERE origin_key = task.origin_key;
    FOR discovered IN SELECT value FROM jsonb_array_elements(
        COALESCE(p_metadata->'discovered_urls', '[]'::jsonb)
    ) LOOP
        discovered_url := discovered->>'url';
        discovered_hash := discovered->>'sha256';
        discovered_origin := discovered->>'origin_key';
        IF task.depth + 1 <= (job.config->>'maxDepth')::INTEGER
           AND discovered_origin = task.origin_key
           AND discovered_hash ~ '^[0-9a-f]{64}$' THEN
            INSERT INTO crawl_tasks (id, job_id, original_url, normalized_url, url_hash,
                origin_key, depth, discovery_seq, discovered_from_task_id, state,
                max_attempts, required_capabilities)
            SELECT md5(random()::text || clock_timestamp()::text)::uuid, task.job_id,
                discovered_url, discovered_url, decode(discovered_hash, 'hex'), task.origin_key,
                task.depth + 1, job.next_discovery_seq, task.id, 'pending', task.max_attempts,
                CASE WHEN job.config->>'engine' = 'browser'
                     THEN ARRAY['browser']::TEXT[] ELSE ARRAY['http']::TEXT[] END
            WHERE job.discovered_count < job.max_pages
            ON CONFLICT (job_id, url_hash) DO NOTHING RETURNING id INTO inserted;
            IF inserted IS NOT NULL THEN
                UPDATE crawl_jobs SET discovered_count = discovered_count + 1,
                    next_discovery_seq = next_discovery_seq + 1 WHERE id = job.id
                RETURNING * INTO job;
                inserted := NULL;
            END IF;
        END IF;
    END LOOP;
    INSERT INTO crawl_events(job_id, task_id, event, metadata)
    VALUES (task.job_id, task.id, 'task_succeeded', jsonb_build_object('remote', true));
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.fail(p_task UUID, p_token UUID, p_error_class TEXT, p_error_code TEXT, p_metadata JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks; used BIGINT;
BEGIN
    worker := worker_api._identity();
    SELECT * INTO task FROM crawl_tasks WHERE id = p_task AND state = 'leased'
      AND lease_token = p_token AND lease_owner = worker.id FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    used := LEAST(task.byte_budget_reserved, GREATEST(0, COALESCE((p_metadata->>'downloaded_bytes')::BIGINT,0)));
    UPDATE crawl_tasks SET state = 'permanent_failed', error_class = p_error_class,
        error_code = p_error_code, lease_owner = NULL, lease_token = NULL,
        lease_expires_at = NULL, byte_budget_reserved = 0, artifact_budget_reserved = 0,
        finished_at = now(), updated_at = now() WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved),
        downloaded_bytes = downloaded_bytes + used, terminal_count = terminal_count + 1, failed_count = failed_count + 1 WHERE id = task.job_id;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'failed', error_code = p_error_code,
        actual_cost = jsonb_build_object('downloaded_bytes',used)
    WHERE task_id = task.id AND attempt_number = task.attempt_count;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.retry(
    p_task UUID, p_token UUID, p_error_class TEXT, p_error_code TEXT, p_available_at TIMESTAMPTZ, p_metadata JSONB
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks; used BIGINT;
BEGIN
    worker := worker_api._identity();
    SELECT * INTO task FROM crawl_tasks WHERE id = p_task AND state = 'leased'
      AND lease_token = p_token AND lease_owner = worker.id FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    used := LEAST(task.byte_budget_reserved, GREATEST(0, COALESCE((p_metadata->>'downloaded_bytes')::BIGINT,0)));
    IF task.attempt_count >= task.max_attempts THEN
        UPDATE crawl_tasks SET state = 'permanent_failed', error_class = p_error_class,
            error_code = p_error_code, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, byte_budget_reserved = 0,
            artifact_budget_reserved = 0, finished_at = now(), updated_at = now()
        WHERE id = task.id;
        DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
        UPDATE crawl_jobs SET
            reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
            reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved),
            downloaded_bytes = downloaded_bytes + used,
            terminal_count = terminal_count + 1, failed_count = failed_count + 1
        WHERE id = task.job_id;
        UPDATE acquisition_attempts SET finished_at = now(), outcome = 'failed',
            error_code = p_error_code,
            actual_cost = jsonb_build_object('downloaded_bytes',used)
        WHERE task_id = task.id AND attempt_number = task.attempt_count;
        INSERT INTO crawl_events(job_id, task_id, event, metadata)
        VALUES (task.job_id, task.id, 'task_permanent_failed',
            jsonb_build_object('error_code', p_error_code));
        RETURN TRUE;
    END IF;
    UPDATE crawl_tasks SET state = 'retry_wait', available_at = p_available_at,
        error_class = p_error_class, error_code = p_error_code, lease_owner = NULL,
        lease_token = NULL, lease_expires_at = NULL, byte_budget_reserved = 0,
        artifact_budget_reserved = 0, updated_at = now() WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved), downloaded_bytes = downloaded_bytes + used
    WHERE id = task.job_id;
    UPDATE crawl_origins SET next_request_at = GREATEST(next_request_at, p_available_at), updated_at = now()
        , consecutive_failures = consecutive_failures +
            CASE WHEN p_error_class IN ('transport','http') THEN 1 ELSE 0 END
        , circuit_state = CASE
            WHEN consecutive_failures + CASE WHEN p_error_class IN ('transport','http') THEN 1 ELSE 0 END >= 5
            THEN 'open' ELSE circuit_state END
        , circuit_open_until = CASE
            WHEN consecutive_failures + CASE WHEN p_error_class IN ('transport','http') THEN 1 ELSE 0 END >= 5
            THEN now() + interval '300 seconds' ELSE circuit_open_until END
    WHERE origin_key = task.origin_key;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'retry', error_code = p_error_code,
        actual_cost = jsonb_build_object('downloaded_bytes',used) WHERE task_id = task.id AND attempt_number = task.attempt_count;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.release(p_task UUID, p_token UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks;
BEGIN
    worker := worker_api._identity(TRUE);
    SELECT * INTO task FROM crawl_tasks WHERE id = p_task AND state = 'leased'
      AND lease_token = p_token AND lease_owner = worker.id FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    UPDATE crawl_tasks SET state = 'retry_wait', available_at = now(), error_class = 'transport',
        error_code = 'worker_released', lease_owner = NULL, lease_token = NULL,
        lease_expires_at = NULL, byte_budget_reserved = 0, artifact_budget_reserved = 0,
        updated_at = now() WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0, reserved_artifact_bytes - task.artifact_budget_reserved)
    WHERE id = task.job_id;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'released', error_code = 'worker_released',
        actual_cost = '{}' WHERE task_id = task.id AND attempt_number = task.attempt_count;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.wait_for_input(p_task UUID, p_token UUID, p_code TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks;
BEGIN
    worker := worker_api._identity();
    SELECT * INTO task FROM crawl_tasks WHERE id = p_task AND state = 'leased'
      AND lease_token = p_token AND lease_owner = worker.id FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    UPDATE crawl_tasks SET state = 'waiting_input', error_code = p_code, lease_owner = NULL,
        lease_token = NULL, lease_expires_at = NULL, byte_budget_reserved = 0,
        artifact_budget_reserved = 0, updated_at = now()
    WHERE id = task.id;
    DELETE FROM crawl_origin_leases WHERE task_id = task.id AND lease_token = p_token;
    UPDATE crawl_jobs SET reserved_bytes = GREATEST(0,reserved_bytes-task.byte_budget_reserved),
        reserved_artifact_bytes = GREATEST(0,reserved_artifact_bytes-task.artifact_budget_reserved)
    WHERE id = task.job_id;
    UPDATE acquisition_attempts SET finished_at = now(), outcome = 'waiting_input', actual_cost = '{}'
    WHERE task_id = task.id AND attempt_number = task.attempt_count;
    RETURN TRUE;
END $$;

CREATE OR REPLACE FUNCTION worker_api.register(p_protocol INTEGER, p_capabilities TEXT[])
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE worker workers;
BEGIN
    SELECT * INTO worker FROM workers WHERE db_role = session_user FOR UPDATE;
    IF NOT FOUND OR worker.state = 'revoked' THEN RETURN COALESCE(worker.state,'revoked'); END IF;
    IF worker.protocol_version <> p_protocol OR NOT (p_capabilities <@ worker.capabilities) THEN
        UPDATE workers SET state = 'incompatible', last_seen_at = now() WHERE id = worker.id;
        RETURN 'incompatible';
    END IF;
    UPDATE workers SET state = 'active', last_seen_at = now() WHERE id = worker.id;
    RETURN 'active';
END $$;

CREATE OR REPLACE FUNCTION worker_api.drain()
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    UPDATE workers SET state = 'draining', last_seen_at = now()
    WHERE db_role = session_user AND state = 'active' AND protocol_version = 1;
    RETURN FOUND;
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA worker_api FROM PUBLIC;
DO $$
BEGIN
    GRANT USAGE ON SCHEMA worker_api TO crawltrove_worker;
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM crawltrove_worker;
    GRANT EXECUTE ON FUNCTION worker_api.claim(TEXT[]), worker_api.heartbeat(UUID,UUID),
        worker_api.reserve_browser_navigation(UUID,UUID), worker_api.robots_cache(TEXT),
        worker_api.store_robots(TEXT,TEXT,INTEGER), worker_api.block_robots(UUID,UUID,TEXT),
        worker_api.complete(UUID,UUID,JSONB,JSONB), worker_api.fail(UUID,UUID,TEXT,TEXT,JSONB),
        worker_api.retry(UUID,UUID,TEXT,TEXT,TIMESTAMPTZ,JSONB), worker_api.release(UUID,UUID),
        worker_api.wait_for_input(UUID,UUID,TEXT), worker_api.register(INTEGER,TEXT[]),
        worker_api.drain() TO crawltrove_worker;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
    NULL;
END $$;
