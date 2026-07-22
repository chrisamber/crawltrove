-- Remote browser workers may attach only to their own active live session.
CREATE OR REPLACE FUNCTION worker_api.issue_live_session_token(p_session UUID)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; session live_sessions; raw BYTEA; token TEXT;
BEGIN
    worker := worker_api._identity();
    SELECT * INTO session FROM live_sessions WHERE id = p_session
      AND worker_id = worker.id AND state IN ('waiting','connected') AND expires_at > now()
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    raw := gen_random_bytes(32);
    token := translate(trim(trailing '=' FROM encode(raw, 'base64')), '+/', '-_');
    INSERT INTO live_session_tokens (id, session_id, scope, token_hash, expires_at)
    VALUES (md5(random()::text || clock_timestamp()::text)::uuid, session.id, 'worker',
            digest(raw, 'sha256'), LEAST(session.expires_at, now() + interval '60 seconds'));
    RETURN token;
END $$;

CREATE OR REPLACE FUNCTION worker_api.touch_live_session(p_session UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; ok BOOLEAN;
BEGIN
    worker := worker_api._identity();
    UPDATE live_sessions SET state = 'connected', last_seen_at = now()
    WHERE id = p_session AND worker_id = worker.id
      AND state IN ('waiting','connected') AND expires_at > now()
    RETURNING TRUE INTO ok;
    RETURN COALESCE(ok, FALSE);
END $$;

CREATE OR REPLACE FUNCTION worker_api.close_live_session(p_session UUID, p_reason TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; session live_sessions; task crawl_tasks; terminal TEXT;
    error_kind TEXT; failure_code TEXT; ok BOOLEAN;
BEGIN
    worker := worker_api._identity(TRUE);
    IF p_reason NOT IN ('cancelled','expired') THEN
        RAISE EXCEPTION 'invalid session close reason' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO session FROM live_sessions WHERE id = p_session AND worker_id = worker.id
      AND state NOT IN ('closed','expired','cancelled') FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    SELECT * INTO task FROM crawl_tasks WHERE id = session.task_id FOR UPDATE;
    IF FOUND AND task.state = 'succeeded' THEN
        UPDATE live_sessions SET state = 'closed', closed_at = now(), last_seen_at = now()
        WHERE id = session.id RETURNING TRUE INTO ok;
        RETURN COALESCE(ok, FALSE);
    END IF;
    terminal := CASE WHEN p_reason = 'cancelled' THEN 'cancelled' ELSE 'expired' END;
    error_kind := CASE WHEN p_reason = 'cancelled' THEN 'policy' ELSE 'transport' END;
    failure_code := CASE WHEN p_reason = 'cancelled'
                         THEN 'human_input_cancelled' ELSE 'human_input_timeout' END;
    IF FOUND AND (
        task.state = 'waiting_input'
        OR (task.state = 'leased' AND task.lease_owner = worker.id)
    ) THEN
        IF task.state = 'leased' THEN
            DELETE FROM crawl_origin_leases WHERE task_id = task.id;
            DELETE FROM proxy_leases WHERE task_id = task.id;
            UPDATE crawl_jobs SET
                reserved_bytes = GREATEST(0, reserved_bytes - task.byte_budget_reserved),
                reserved_artifact_bytes = GREATEST(
                    0, reserved_artifact_bytes - task.artifact_budget_reserved
                )
            WHERE id = task.job_id;
        END IF;
        UPDATE crawl_tasks SET
            state = CASE WHEN p_reason = 'cancelled' THEN 'cancelled' ELSE 'permanent_failed' END,
            error_class = error_kind, error_code = failure_code,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            byte_budget_reserved = 0, artifact_budget_reserved = 0,
            finished_at = now(), updated_at = now()
        WHERE id = task.id;
        UPDATE crawl_jobs SET terminal_count = terminal_count + 1,
            failed_count = failed_count + CASE WHEN p_reason = 'expired' THEN 1 ELSE 0 END
        WHERE id = task.job_id;
        UPDATE acquisition_attempts SET finished_at = COALESCE(finished_at, now()),
            outcome = CASE WHEN p_reason = 'cancelled' THEN 'cancelled' ELSE 'failed' END,
            error_code = failure_code
        WHERE task_id = task.id AND attempt_number = task.attempt_count;
        INSERT INTO crawl_events(job_id, task_id, event, metadata)
        VALUES (
            task.job_id, task.id,
            CASE WHEN p_reason = 'cancelled' THEN 'task_cancelled'
                 ELSE 'task_permanent_failed' END,
            jsonb_build_object('reason', failure_code)
        );
    END IF;
    UPDATE live_sessions SET state = terminal, closed_at = now(), last_seen_at = now()
    WHERE id = session.id RETURNING TRUE INTO ok;
    RETURN COALESCE(ok, FALSE);
END $$;

CREATE OR REPLACE FUNCTION worker_api.inspect_live_session(p_session UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; session live_sessions;
BEGIN
    worker := worker_api._identity(TRUE);
    SELECT * INTO session FROM live_sessions
    WHERE id = p_session AND worker_id = worker.id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN jsonb_build_object(
        'state', CASE WHEN session.expires_at <= now()
                      AND session.state IN ('starting','waiting','connected','resuming')
                      THEN 'expired' ELSE session.state END,
        'expires_at', session.expires_at
    );
END $$;

CREATE OR REPLACE FUNCTION worker_api.resume_live_session(p_session UUID)
RETURNS TABLE (id UUID, job_id UUID, url TEXT, normalized_url TEXT, origin_key TEXT,
    depth INTEGER, attempt INTEGER, lease_token UUID, deadline_at TIMESTAMPTZ,
    config JSONB, byte_allowance BIGINT, artifact_allowance BIGINT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; session live_sessions; task crawl_tasks; job crawl_jobs;
    token UUID; bytes BIGINT; artifacts BIGINT;
BEGIN
    worker := worker_api._identity();
    SELECT * INTO session FROM live_sessions
    WHERE live_sessions.id = p_session AND worker_id = worker.id
      AND state = 'resuming' AND expires_at > now() FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO task FROM crawl_tasks
    WHERE crawl_tasks.id = session.task_id AND state = 'waiting_input' FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO job FROM crawl_jobs
    WHERE crawl_jobs.id = task.job_id AND cancel_requested_at IS NULL
      AND deadline_at > now() FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    PERFORM 1 FROM crawl_origins WHERE origin_key = task.origin_key FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM crawl_origin_leases WHERE crawl_origin_leases.origin_key = task.origin_key
    ) THEN RETURN; END IF;
    bytes := LEAST(job.max_bytes - job.downloaded_bytes - job.reserved_bytes, 10485760);
    artifacts := LEAST(
        job.max_artifact_bytes - job.artifact_bytes - job.reserved_artifact_bytes,
        31457280
    );
    IF bytes <= 0 OR artifacts <= 0 THEN RETURN; END IF;
    token := md5(random()::text || clock_timestamp()::text)::uuid;
    UPDATE crawl_jobs SET reserved_bytes = reserved_bytes + bytes,
        reserved_artifact_bytes = reserved_artifact_bytes + artifacts WHERE crawl_jobs.id = job.id;
    UPDATE crawl_tasks SET state = 'leased', lease_owner = worker.id, lease_token = token,
        lease_expires_at = now() + interval '120 seconds', byte_budget_reserved = bytes,
        artifact_budget_reserved = artifacts, updated_at = now() WHERE crawl_tasks.id = task.id;
    INSERT INTO crawl_origin_leases (origin_key, task_id, lease_token, expires_at)
    VALUES (task.origin_key, task.id, token, now() + interval '120 seconds');
    UPDATE live_sessions SET last_seen_at = now() WHERE live_sessions.id = session.id;
    RETURN QUERY SELECT task.id, task.job_id, task.original_url, task.normalized_url,
        task.origin_key, task.depth, task.attempt_count, token, job.deadline_at,
        job.config, bytes, artifacts;
END $$;

CREATE OR REPLACE FUNCTION worker_api.finish_live_session(p_session UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; ok BOOLEAN;
BEGIN
    worker := worker_api._identity(TRUE);
    UPDATE live_sessions s SET state = 'closed', closed_at = now(), last_seen_at = now()
    WHERE s.id = p_session AND s.worker_id = worker.id AND s.state = 'resuming'
      AND EXISTS (SELECT 1 FROM crawl_tasks t
                  WHERE t.id = s.task_id AND t.state = 'succeeded')
    RETURNING TRUE INTO ok;
    RETURN COALESCE(ok, FALSE);
END $$;

REVOKE ALL ON FUNCTION worker_api.issue_live_session_token(UUID),
    worker_api.touch_live_session(UUID), worker_api.close_live_session(UUID,TEXT),
    worker_api.inspect_live_session(UUID), worker_api.resume_live_session(UUID),
    worker_api.finish_live_session(UUID) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        GRANT EXECUTE ON FUNCTION worker_api.issue_live_session_token(UUID),
            worker_api.touch_live_session(UUID), worker_api.close_live_session(UUID,TEXT),
            worker_api.inspect_live_session(UUID), worker_api.resume_live_session(UUID),
            worker_api.finish_live_session(UUID)
            TO crawltrove_worker;
    END IF;
END $$;
