from datetime import datetime, timezone

import pytest

from tests.conftest import requires_db


@pytest.fixture
async def active_claim(db):
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    await repository.submit_job(CrawlConfig(url="https://example.com", engine="browser"))
    claim = await repository.claim_task("browser-1", {"http", "browser"})
    assert claim is not None
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM workers WHERE db_role = session_user"
        )
        await conn.execute(
            """INSERT INTO workers
               (id, db_role, capabilities, protocol_version, state, artifact_bucket, artifact_prefix)
               VALUES ('browser-1', session_user, ARRAY['http','browser'], 1, 'active',
                       'bucket', 'workers/browser-1/')"""
        )
    return claim


@requires_db
async def test_waiting_input_releases_task_lease_and_clamps_expiry(db, active_claim):
    from app.acquisition import sessions

    handle = await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", ttl_seconds=900, pool=db,
    )
    async with db.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT state, lease_token, lease_owner, attempt_count FROM crawl_tasks WHERE id = $1",
            active_claim.id,
        )
        reserved = await conn.fetchrow(
            "SELECT reserved_bytes, reserved_artifact_bytes, deadline_at FROM crawl_jobs WHERE id = $1",
            active_claim.job_id,
        )
        leases = await conn.fetchval(
            "SELECT count(*) FROM crawl_origin_leases WHERE task_id = $1", active_claim.id
        )
        session = await conn.fetchrow(
            "SELECT worker_id, state FROM live_sessions WHERE id = $1", handle.id
        )
    assert task["state"] == "waiting_input"
    assert task["lease_token"] is None and task["lease_owner"] is None
    assert task["attempt_count"] == active_claim.attempt
    assert reserved["reserved_bytes"] == 0 and reserved["reserved_artifact_bytes"] == 0
    assert leases == 0
    assert session["worker_id"] == "browser-1" and session["state"] == "waiting"
    assert handle.expires_at <= reserved["deadline_at"]


@requires_db
async def test_session_token_is_single_use_and_only_a_hash_is_stored(db, active_claim):
    from app.acquisition import sessions

    handle = await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", ttl_seconds=900, pool=db,
    )
    token = await sessions.issue_token(handle.id, "control", ttl_seconds=60, pool=db)
    assert await sessions.consume_token(handle.id, token, "control", pool=db) is True
    assert await sessions.consume_token(handle.id, token, "control", pool=db) is False
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token_hash, consumed_at FROM live_session_tokens WHERE session_id = $1", handle.id
        )
    assert token.encode() not in row["token_hash"]
    assert len(row["token_hash"]) == 32 and row["consumed_at"] is not None


@requires_db
async def test_session_token_rejects_non_base64_aliases(db, active_claim):
    from app.acquisition import sessions

    handle = await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", ttl_seconds=900, pool=db,
    )
    token = await sessions.issue_token(handle.id, "control", ttl_seconds=60, pool=db)

    assert await sessions.consume_token(handle.id, token + "!", "control", pool=db) is False
    assert await sessions.consume_token(handle.id, token, "control", pool=db) is True


@requires_db
async def test_stale_or_wrong_worker_cannot_create_a_waiting_session(db, active_claim):
    from app.acquisition import sessions

    with pytest.raises(sessions.SessionStateError):
        await sessions.wait_for_input(
            active_claim, backend="owned", worker_id="other-worker", ttl_seconds=900, pool=db,
        )
    assert await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", ttl_seconds=900, pool=db,
    )


def test_session_surface_cannot_contain_raw_remote_control_urls():
    from app.acquisition.sessions import SessionSnapshot

    snapshot = SessionSnapshot(
        status="waiting", expires_at=datetime.now(timezone.utc), usage={"actions": 0}
    )
    assert not {"connectUrl", "wsUrl", "cdpUrl", "vncUrl"} & set(snapshot.__dict__)


@requires_db
async def test_worker_role_cannot_read_session_rows(db):
    async with db.acquire() as conn:
        can_read = await conn.fetchval(
            "SELECT has_table_privilege('crawltrove_worker', 'live_session_tokens', 'SELECT')"
        )
    assert can_read is False
