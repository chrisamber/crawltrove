"""In-process scheduler — the durable fix for today's lost-on-restart jobs.

Reuses the crawler's asyncio idiom (no APScheduler/broker). Every poll_interval_s
it asks the database for due, enabled job definitions; claiming + rescheduling
happens atomically in repo.claim_due_jobs() (FOR UPDATE SKIP LOCKED), so schedule
state lives in Postgres and survives the deploy pipeline's container restarts.

Disabled entirely when DATABASE_URL is unset: the loop just sleeps. The startup
hook only launches it when persistence is enabled.
"""
import asyncio
import logging
import os
import time

from app.db.pool import get_pool
from app.db import repo
from app import runner, storage

logger = logging.getLogger("scheduler")

# At most one retention sweep per this interval, regardless of poll cadence.
_PRUNE_EVERY_S = 6 * 3600


def _maybe_prune(last_prune: float) -> float:
    """Run storage.prune at most every few hours when retention is configured.

    Gated on DATA_RETENTION_DAYS (unset => never prune). DATA_KEEP_RUNS caps how
    many most-recent artifacts per kind are always kept. Returns the new
    last-prune timestamp.
    """
    days = os.environ.get("DATA_RETENTION_DAYS")
    if not days:
        return last_prune
    now = time.monotonic()
    if now - last_prune < _PRUNE_EVERY_S:
        return last_prune
    try:
        storage.prune(int(days), keep_runs=int(os.environ.get("DATA_KEEP_RUNS", "50")))
    except Exception as e:
        logger.warning("retention prune failed: %s", e)
    return now


async def scheduler_loop(poll_interval_s: int = 30) -> None:
    """Poll for due jobs forever. Cancels cleanly on shutdown."""
    logger.info("scheduler loop started (poll=%ss)", poll_interval_s)
    last_prune = 0.0
    while True:
        try:
            pool = await get_pool()
            if pool is None:
                # Persistence disabled / DB unreachable — nothing to schedule.
                await asyncio.sleep(poll_interval_s)
                continue
            due = await repo.claim_due_jobs(limit=10)
            for job in due:
                run_id = await runner.launch_job(job, trigger="schedule")
                if run_id is not None:
                    logger.info("scheduled job %s -> run %s", job.get("id"), run_id)
            last_prune = _maybe_prune(last_prune)
        except asyncio.CancelledError:
            logger.info("scheduler loop stopping")
            raise
        except Exception as e:
            # Never let a bad tick kill the loop.
            logger.warning("scheduler tick failed: %s", e)
        await asyncio.sleep(poll_interval_s)
