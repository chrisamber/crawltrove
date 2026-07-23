-- Fenced native-provider accounting for enrolled remote acquisition workers.
-- The protocol accepts only route names and native meter totals; provider
-- credentials, remote session identifiers, headers, and live URLs never enter
-- PostgreSQL.

-- Remote completion discovers children inside worker_api.complete.  Keep those
-- children on the same explicit managed-provider route as the seed task.
CREATE OR REPLACE FUNCTION _managed_provider_task_capability()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE provider TEXT;
BEGIN
    SELECT config#>>'{acquisition,provider}' INTO provider
    FROM crawl_jobs WHERE id = NEW.job_id;
    NEW.required_capabilities := CASE provider
        WHEN 'firecrawl' THEN ARRAY['firecrawl_scrape']::TEXT[]
        WHEN 'brightdata' THEN ARRAY['brightdata_unlocker']::TEXT[]
        WHEN 'browserbase' THEN ARRAY['browserbase_session']::TEXT[]
        ELSE NEW.required_capabilities END;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS crawl_tasks_managed_provider_capability ON crawl_tasks;
CREATE TRIGGER crawl_tasks_managed_provider_capability
BEFORE INSERT ON crawl_tasks
FOR EACH ROW EXECUTE FUNCTION _managed_provider_task_capability();
REVOKE ALL ON FUNCTION _managed_provider_task_capability() FROM PUBLIC;

CREATE OR REPLACE FUNCTION worker_api.reserve_provider_attempt(
    p_task UUID, p_token UUID, p_route TEXT, p_reserved JSONB
) RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; task crawl_tasks; job crawl_jobs; native_provider TEXT;
    meter_name TEXT; usage crawl_provider_usage; attempt_id UUID; total_attempts INTEGER;
    route_attempts INTEGER;
BEGIN
    worker := worker_api._identity();
    native_provider := CASE p_route
        WHEN 'local_http' THEN 'local'
        WHEN 'owned_proxy_http' THEN 'local'
        WHEN 'local_browser' THEN 'local'
        WHEN 'firecrawl_scrape' THEN 'firecrawl'
        WHEN 'firecrawl_interact' THEN 'firecrawl'
        WHEN 'brightdata_unlocker' THEN 'brightdata'
        WHEN 'browserbase_session' THEN 'browserbase'
        ELSE NULL END;
    IF native_provider IS NULL OR jsonb_typeof(p_reserved) <> 'object' THEN
        RAISE EXCEPTION 'invalid provider route or native cost' USING ERRCODE = '22023';
    END IF;
    IF p_route IN ('local_http','owned_proxy_http','local_browser')
       AND p_reserved <> '{}'::jsonb THEN
        RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
    END IF;
    IF p_route IN ('firecrawl_scrape','firecrawl_interact') THEN
        IF NOT (p_reserved ? 'credits') OR p_reserved - 'credits' <> '{}'::jsonb
           OR jsonb_typeof(p_reserved->'credits') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
        IF (p_reserved->>'credits')::NUMERIC < 0 THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
    ELSIF p_route = 'brightdata_unlocker' THEN
        IF NOT (p_reserved ? 'requests') OR p_reserved - 'requests' <> '{}'::jsonb
           OR jsonb_typeof(p_reserved->'requests') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
        IF (p_reserved->>'requests')::NUMERIC < 0 THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
    ELSIF p_route = 'browserbase_session' THEN
        IF NOT (p_reserved ? 'browserMinutes') OR NOT (p_reserved ? 'proxyBytes')
           OR p_reserved - 'browserMinutes' - 'proxyBytes' <> '{}'::jsonb
           OR jsonb_typeof(p_reserved->'browserMinutes') IS DISTINCT FROM 'number'
           OR jsonb_typeof(p_reserved->'proxyBytes') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
        IF (p_reserved->>'browserMinutes')::NUMERIC < 0
           OR (p_reserved->>'proxyBytes')::NUMERIC <> 0 THEN
            RAISE EXCEPTION 'invalid provider native cost' USING ERRCODE = '22023';
        END IF;
    END IF;
    IF (p_route = 'local_http' AND NOT ('http' = ANY(worker.capabilities)))
       OR (p_route = 'local_browser' AND NOT ('browser' = ANY(worker.capabilities)))
       OR (p_route = 'owned_proxy_http' AND NOT ('proxy' = ANY(worker.capabilities)))
       OR (native_provider <> 'local' AND NOT (p_route = ANY(worker.capabilities))) THEN
        RAISE EXCEPTION 'provider route capability denied' USING ERRCODE = '42501';
    END IF;
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL AND j.deadline_at > now()
    FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO job FROM crawl_jobs WHERE id = task.job_id FOR UPDATE;
    SELECT count(*) INTO total_attempts FROM acquisition_attempts
    WHERE task_id = task.id AND lease_token IS NOT NULL;
    SELECT count(*) INTO route_attempts FROM acquisition_attempts
    WHERE task_id = task.id AND route = p_route AND lease_token IS NOT NULL;
    IF total_attempts >= LEAST(4, COALESCE((job.config->'acquisition'->>'maxAttempts')::INTEGER, 4))
       OR route_attempts >= LEAST(2, LEAST(4, COALESCE((job.config->'acquisition'->>'maxAttempts')::INTEGER, 4))) THEN
        RETURN NULL;
    END IF;
    IF native_provider <> 'local' THEN
        FOR meter_name IN SELECT jsonb_object_keys(p_reserved) LOOP
            SELECT * INTO usage FROM crawl_provider_usage
            WHERE job_id = task.job_id AND provider = native_provider AND meter = meter_name FOR UPDATE;
            IF NOT FOUND OR (p_reserved->>meter_name)::NUMERIC
                    > usage.limit_value - usage.reserved_value - usage.consumed_value THEN
                RETURN NULL;
            END IF;
        END LOOP;
        FOR meter_name IN SELECT jsonb_object_keys(p_reserved) LOOP
            UPDATE crawl_provider_usage SET reserved_value = reserved_value + (p_reserved->>meter_name)::NUMERIC
            WHERE job_id = task.job_id AND provider = native_provider AND meter = meter_name;
        END LOOP;
    END IF;
    attempt_id := md5(random()::text || clock_timestamp()::text)::UUID;
    INSERT INTO acquisition_attempts
        (id, job_id, task_id, attempt_number, route, provider, worker_id, lease_token, reserved_cost)
    VALUES (attempt_id, task.job_id, task.id, task.attempt_count * 1000 + total_attempts + 1,
            p_route, native_provider, worker.id, p_token, p_reserved);
    RETURN attempt_id;
END $$;

CREATE OR REPLACE FUNCTION worker_api.finish_provider_attempt(
    p_attempt UUID, p_token UUID, p_outcome TEXT, p_actual JSONB, p_estimated BOOLEAN DEFAULT FALSE
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; attempt acquisition_attempts; task crawl_tasks; job crawl_jobs;
    meter_name TEXT; usage crawl_provider_usage;
BEGIN
    worker := worker_api._identity();
    IF p_outcome NOT IN ('succeeded','retryable_failure','failed')
       OR jsonb_typeof(p_actual) <> 'object' THEN
        RAISE EXCEPTION 'invalid provider completion' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO attempt FROM acquisition_attempts
    WHERE id = p_attempt AND lease_token = p_token AND worker_id = worker.id FOR UPDATE;
    IF NOT FOUND OR attempt.finished_at IS NOT NULL THEN RETURN FALSE; END IF;
    FOR meter_name IN SELECT jsonb_object_keys(p_actual) LOOP
        IF NOT attempt.reserved_cost ? meter_name THEN
            RAISE EXCEPTION 'provider native meter mismatch' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    SELECT t.* INTO task FROM crawl_tasks t JOIN crawl_jobs j ON j.id = t.job_id
    WHERE t.id = attempt.task_id AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id AND j.cancel_requested_at IS NULL AND j.deadline_at > now()
    FOR UPDATE OF t, j;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    SELECT * INTO job FROM crawl_jobs WHERE id = task.job_id FOR UPDATE;
    FOR meter_name IN SELECT jsonb_object_keys(attempt.reserved_cost) LOOP
        IF NOT (p_actual ? meter_name)
           OR jsonb_typeof(p_actual->meter_name) IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'provider actual cost is invalid' USING ERRCODE = '22023';
        END IF;
        IF (p_actual->>meter_name)::NUMERIC < 0
           OR (p_actual->>meter_name)::NUMERIC > (attempt.reserved_cost->>meter_name)::NUMERIC THEN
            RAISE EXCEPTION 'provider actual cost is invalid' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    IF attempt.provider <> 'local' THEN
        FOR meter_name IN SELECT jsonb_object_keys(attempt.reserved_cost) LOOP
            SELECT * INTO usage FROM crawl_provider_usage
            WHERE job_id = task.job_id AND provider = attempt.provider AND meter = meter_name FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'provider usage meter is missing' USING ERRCODE = '22023'; END IF;
            UPDATE crawl_provider_usage
            SET reserved_value = reserved_value - (attempt.reserved_cost->>meter_name)::NUMERIC,
                consumed_value = consumed_value + (p_actual->>meter_name)::NUMERIC
            WHERE job_id = task.job_id AND provider = attempt.provider AND meter = meter_name;
        END LOOP;
    END IF;
    UPDATE acquisition_attempts SET finished_at = now(),
        duration_ms = EXTRACT(EPOCH FROM now() - started_at) * 1000,
        actual_cost = p_actual, outcome = p_outcome, cost_estimated = p_estimated
    WHERE id = attempt.id;
    RETURN TRUE;
END $$;

REVOKE ALL ON FUNCTION worker_api.reserve_provider_attempt(UUID,UUID,TEXT,JSONB),
    worker_api.finish_provider_attempt(UUID,UUID,TEXT,JSONB,BOOLEAN) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        GRANT EXECUTE ON FUNCTION worker_api.reserve_provider_attempt(UUID,UUID,TEXT,JSONB),
            worker_api.finish_provider_attempt(UUID,UUID,TEXT,JSONB,BOOLEAN) TO crawltrove_worker;
    END IF;
END $$;
