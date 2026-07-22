-- Upgrade early v0.4 databases that recorded 0007 before proxy routing landed.

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
ALTER TABLE acquisition_attempts ADD COLUMN IF NOT EXISTS proxy_connected_ip INET;
REVOKE ALL ON proxy_nodes, proxy_leases FROM PUBLIC;

CREATE OR REPLACE FUNCTION worker_api.task_capabilities(p_task UUID, p_token UUID)
RETURNS TEXT[] LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; capabilities TEXT[];
BEGIN
    worker := worker_api._identity();
    SELECT t.required_capabilities INTO capabilities FROM crawl_tasks t
    WHERE t.id = p_task AND t.state = 'leased' AND t.lease_token = p_token
      AND t.lease_owner = worker.id;
    RETURN capabilities;
END $$;

CREATE OR REPLACE FUNCTION worker_api.assign_proxy(p_task UUID, p_token UUID, p_origin TEXT)
RETURNS TABLE (node_id TEXT, endpoint TEXT)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
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
    ORDER BY (SELECT count(*) FROM proxy_leases l WHERE l.node_id = n.id AND l.expires_at > now()), n.id
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
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
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
    ) OR (family(p_address) = 6 AND (
        NOT (p_address <<= inet '2000::/3') OR p_address <<= inet '2001:db8::/32'
        OR p_address <<= inet 'fc00::/7' OR p_address <<= inet 'fe80::/10'
        OR p_address <<= inet 'ff00::/8'
    )) THEN RETURN FALSE; END IF;
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
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE worker workers; matched BOOLEAN;
BEGIN
    worker := worker_api._identity();
    IF p_outcome NOT IN ('blocked', 'transport') OR p_cooldown_seconds < 1 OR p_offline_after < 1 THEN
        RAISE EXCEPTION 'invalid proxy failure' USING ERRCODE = '22023';
    END IF;
    UPDATE proxy_nodes n SET failure_count = n.failure_count + 1,
        state = CASE WHEN p_outcome = 'blocked' THEN 'cooldown'
                     WHEN n.failure_count + 1 >= p_offline_after THEN 'offline' ELSE n.state END,
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
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
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

REVOKE ALL ON FUNCTION worker_api.task_capabilities(UUID,UUID),
    worker_api.assign_proxy(UUID,UUID,TEXT), worker_api.record_proxy_ip(UUID,UUID,TEXT,INET),
    worker_api.proxy_failure(UUID,UUID,TEXT,TEXT,INTEGER,INTEGER),
    worker_api.release_proxy(UUID,UUID) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
        GRANT EXECUTE ON FUNCTION worker_api.task_capabilities(UUID,UUID),
            worker_api.assign_proxy(UUID,UUID,TEXT), worker_api.record_proxy_ip(UUID,UUID,TEXT,INET),
            worker_api.proxy_failure(UUID,UUID,TEXT,TEXT,INTEGER,INTEGER),
            worker_api.release_proxy(UUID,UUID) TO crawltrove_worker;
    END IF;
END $$;
