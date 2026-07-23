from datetime import datetime, timezone

import httpx
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


def test_owned_session_actions_are_bounded_and_never_accept_code_execution():
    from app.acquisition.owned_session import validate_action

    assert validate_action({"action": "click", "selector": "#continue"}) == {
        "action": "click", "selector": "#continue",
    }
    for action in (
        {"action": "evaluate", "code": "alert(1)"},
        {"action": "fill", "selector": "#x", "text": "x" * 4097},
        {"action": "scroll", "delta": 10_001},
    ):
        with pytest.raises(ValueError):
            validate_action(action)


@requires_db
async def test_job_session_token_exposes_only_same_origin_open_path(db, active_claim):
    from app.acquisition import sessions
    from app.main import app

    handle = await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", ttl_seconds=900, pool=db,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        issued = await client.post(
            f"/api/crawl/{active_claim.job_id}/sessions/{handle.id}/token"
        )
        assert issued.status_code == 200
        opened = issued.json()["url"]
        assert opened.startswith(f"/api/acquisition/sessions/{handle.id}/open?")
        assert not any(value in opened.lower() for value in ("connecturl", "wsurl", "cdp", "vnc"))
        assert (await client.get(opened)).status_code == 200
        assert (await client.get(opened)).status_code == 401


@requires_db
async def test_worker_role_cannot_read_session_rows(db):
    async with db.acquire() as conn:
        can_read = await conn.fetchval(
            "SELECT has_table_privilege('crawltrove_worker', 'live_session_tokens', 'SELECT')"
        )
    assert can_read is False


@requires_db
async def test_local_session_resumes_through_a_new_completion_fence(db, active_claim):
    from app.acquisition import sessions
    from app.crawl import repository
    from app.crawl.types import TaskResult

    async with db.acquire() as conn:
        await conn.execute("DELETE FROM workers WHERE id = 'browser-1'")
    handle = await sessions.wait_for_input_local(
        active_claim, backend="owned", worker_id="browser-1", pool=db,
    )
    assert await sessions.request_resume(handle.id, pool=db)
    resumed = await repository.resume_live_session(handle.id, "browser-1")
    assert resumed is not None and resumed.attempt == active_claim.attempt
    assert await repository.complete_task(
        resumed.id, resumed.lease_token,
        TaskResult("https://example.com/complete", 200, "done", "complete"),
    )
    assert await sessions.close_completed(handle.id, pool=db)
    async with db.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT state, attempt_count FROM crawl_tasks WHERE id = $1", active_claim.id,
        )
        state = await conn.fetchval("SELECT state FROM live_sessions WHERE id = $1", handle.id)
    assert dict(task) == {"state": "succeeded", "attempt_count": active_claim.attempt}
    assert state == "closed"


@requires_db
async def test_worker_shutdown_terminalizes_remote_wait_and_job_counters(db, active_claim):
    from app.acquisition import sessions

    handle = await sessions.wait_for_input(
        active_claim, backend="owned", worker_id="browser-1", pool=db,
    )
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT worker_api.close_live_session($1, 'expired')", handle.id,
        )
        task = await conn.fetchrow(
            "SELECT state, error_code FROM crawl_tasks WHERE id = $1", active_claim.id,
        )
        job = await conn.fetchrow(
            "SELECT terminal_count, failed_count FROM crawl_jobs WHERE id = $1",
            active_claim.job_id,
        )
        session_state = await conn.fetchval(
            "SELECT state FROM live_sessions WHERE id = $1", handle.id,
        )
    assert dict(task) == {
        "state": "permanent_failed", "error_code": "human_input_timeout",
    }
    assert dict(job) == {"terminal_count": 1, "failed_count": 1}
    assert session_state == "expired"


@requires_db
async def test_cancel_and_expiry_terminalize_parked_tasks(db, active_claim):
    from app.acquisition import sessions
    from app.crawl import repository
    from app.crawl.config import CrawlConfig

    cancelled = await sessions.wait_for_input_local(
        active_claim, backend="owned", worker_id="browser-1", pool=db,
    )
    assert await sessions.cancel(cancelled.id, pool=db)
    async with db.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT state, error_code FROM crawl_tasks WHERE id = $1", active_claim.id,
        )
    assert dict(task) == {"state": "cancelled", "error_code": "human_input_cancelled"}

    await repository.submit_job(CrawlConfig(url="https://example.org/second", engine="browser"))
    expiring_claim = await repository.claim_task("browser-1", {"http", "browser"})
    assert expiring_claim is not None
    expiring = await sessions.wait_for_input_local(
        expiring_claim, backend="owned", worker_id="browser-1", pool=db,
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE live_sessions SET expires_at = now() - interval '1 second' WHERE id = $1",
            expiring.id,
        )
    assert await sessions.expire_due(pool=db) == 1
    async with db.acquire() as conn:
        expired_task = await conn.fetchrow(
            "SELECT state, error_code FROM crawl_tasks WHERE id = $1", expiring_claim.id,
        )
    assert dict(expired_task) == {
        "state": "permanent_failed", "error_code": "human_input_timeout",
    }


@requires_db
async def test_expired_session_is_not_reported_as_active_before_maintenance(db, active_claim):
    from app.acquisition import sessions
    from app.crawl import repository

    handle = await sessions.wait_for_input_local(
        active_claim, backend="owned", worker_id="browser-1", pool=db,
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE live_sessions SET expires_at = now() - interval '1 second' WHERE id = $1",
            handle.id,
        )
    job = await repository.get_job(active_claim.job_id)
    assert job is not None and job["activeSession"] is None
