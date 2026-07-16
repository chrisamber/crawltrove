-- 0002_fts.sql — full-text search over scraped pages (Epic 2, E2.S2).
--
-- A STORED generated tsvector column kept in lock-step with the row by Postgres
-- (no triggers to maintain), indexed with GIN. The 2-arg to_tsvector('english',
-- …) form is IMMUTABLE (a literal regconfig), which a generated column requires;
-- the 1-arg form is only STABLE (depends on default_text_search_config) and
-- would be rejected here. coalesce() guards the NULLs.
--
-- Search weights the page text, its URL, and the metadata title. This is a
-- ranking/recall signal and is complementary to — not a replacement for — the
-- exact/near-dup index (sha256 + MinHash) under data/index/.
--
-- Idempotent (IF NOT EXISTS) so it is safe after restarts or interrupted
-- deployments.

ALTER TABLE scraped_pages
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(extracted_text, '') || ' ' ||
            coalesce(url, '') || ' ' ||
            coalesce(metadata->>'title', '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_pages_search ON scraped_pages USING GIN (search_tsv);
