-- Preserve exact artifact-bucket validation for databases that may have
-- applied an earlier development revision of the remote-worker migration.

ALTER TABLE workers ADD COLUMN IF NOT EXISTS artifact_bucket TEXT;

CREATE OR REPLACE FUNCTION worker_api._validate_result_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE bucket TEXT; prefix TEXT;
BEGIN
    IF NEW.markdown_ref IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT w.artifact_bucket, w.artifact_prefix INTO bucket, prefix
    FROM crawl_tasks t JOIN workers w ON w.id = t.lease_owner
    WHERE t.id = NEW.task_id;
    IF bucket IS NULL OR prefix IS NULL
       OR NEW.markdown_ref NOT LIKE 's3://' || bucket || '/' || prefix || '%' THEN
        RAISE EXCEPTION 'artifact reference is outside the enrolled worker bucket and prefix'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS validate_remote_result_artifact ON crawl_results;
CREATE TRIGGER validate_remote_result_artifact
BEFORE INSERT OR UPDATE OF markdown_ref ON crawl_results
FOR EACH ROW WHEN (NEW.markdown_ref IS NOT NULL)
EXECUTE FUNCTION worker_api._validate_result_artifact();

REVOKE ALL ON FUNCTION worker_api._validate_result_artifact() FROM PUBLIC;
