-- Keep durable queue claims bounded as terminal rows accumulate.
--
-- The original state-leading index still helps state-specific maintenance,
-- but it cannot satisfy the queue's priority ordering without repeatedly
-- sorting the remaining pending rows.  A partial ordered index keeps only
-- claimable states and lets `FOR UPDATE SKIP LOCKED ... LIMIT 1` stop early.
CREATE INDEX IF NOT EXISTS crawl_tasks_ready_idx
    ON crawl_tasks (priority, available_at, discovery_seq)
    WHERE state IN ('pending', 'retry_wait');
