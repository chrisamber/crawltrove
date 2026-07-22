"""Exercise the durable queue against a disposable, loopback-only PostgreSQL DB.

This is intentionally a release load driver, not a pytest test.  It inserts a
large fixture directly so the public 100-page API limit remains enforced.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import os
import resource
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATABASE = "crawltrove_v040_load_test"
WORKERS = 8
FIXTURE_ORIGINS = tuple(f"http://127.0.0.1:{18080 + number}" for number in range(WORKERS))
FORBIDDEN_METRIC_LABELS = {
    "url", "origin", "job_id", "task_id", "worker_id", "session_id", "exception",
}


def _is_local_dsn(dsn: str) -> bool:
    parsed = urlsplit(dsn)
    return parsed.scheme in {"postgres", "postgresql"} and parsed.hostname in {
        "localhost", "127.0.0.1", "::1",
    }


def _database_dsn(admin_dsn: str) -> str:
    parsed = urlsplit(admin_dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{DATABASE}", "", ""))


async def _migrate(dsn: str) -> None:
    import asyncpg

    migrations = sorted((ROOT / "app" / "db" / "migrations").glob("*.sql"))
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for path in migrations:
            version = path.stem
            if await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = $1", version):
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
    finally:
        await conn.close()


async def _create_database(admin_dsn: str) -> str:
    import asyncpg

    if not _is_local_dsn(admin_dsn):
        raise ValueError("--admin-dsn must point to localhost, 127.0.0.1, or ::1")
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}"')
        await admin.execute(f'CREATE DATABASE "{DATABASE}"')
    finally:
        await admin.close()
    dsn = _database_dsn(admin_dsn)
    await _migrate(dsn)
    return dsn


async def _drop_database(admin_dsn: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DATABASE}"')
    finally:
        await admin.close()


def _config() -> dict:
    return {
        "url": FIXTURE_ORIGINS[0] + "/http/0",
        "limit": 100,
        "maxDepth": 0,
        "onlyMainContent": True,
        "engine": "auto",
        "useSitemap": False,
        "screenshots": False,
        "screenshotMaxWidth": 1,
        "screenshotMaxHeight": 1,
        "respectRobots": False,
        "robotsFailOpen": False,
        "minDelayMs": 0,
        "maxBrowserPages": 100,
        "maxOrigins": 100,
        "maxFailures": 100,
        "maxBytes": 1024 ** 3,
        "maxArtifactBytes": 2 * 1024 ** 3,
        "timeoutSeconds": 21600,
        "acquisition": {"provider": "auto", "maxAttempts": 2, "creditBudgets": {},
                        "allowHumanIntervention": False},
    }


def _fixture_url(index: int) -> tuple[str, list[str]]:
    origin = FIXTURE_ORIGINS[index % len(FIXTURE_ORIGINS)]
    kind = ("http", "retry", "document.pdf", "browser")[index % 4]
    capability = ["http", "browser"] if kind == "browser" else ["http"]
    return f"{origin}/{kind}/{index}", capability


async def _seed(dsn: str, task_count: int) -> uuid.UUID:
    import asyncpg

    job_id = uuid.uuid4()
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            run_id = await conn.fetchval(
                "INSERT INTO scrape_runs (external_id, trigger, status) VALUES ($1, 'load', 'pending') "
                "RETURNING id", str(job_id),
            )
            await conn.execute(
                """INSERT INTO crawl_jobs
                   (id, run_id, state, config, max_pages, max_bytes, max_artifact_bytes,
                    discovered_count, deadline_at)
                   VALUES ($1, $2, 'pending', $3::jsonb, 100, $4, $5, $6,
                           now() + interval '1 hour')""",
                job_id, run_id, json.dumps(_config()), 1024 ** 3, 2 * 1024 ** 3, task_count,
            )
            await conn.executemany(
                "INSERT INTO crawl_origins (origin_key) VALUES ($1)",
                [(origin,) for origin in FIXTURE_ORIGINS],
            )
        columns = (
            "id", "job_id", "original_url", "normalized_url", "url_hash", "origin_key",
            "depth", "discovery_seq", "state", "max_attempts", "required_capabilities",
        )
        for start in range(0, task_count, 1000):
            records = []
            for index in range(start, min(start + 1000, task_count)):
                url, capability = _fixture_url(index)
                records.append((
                    uuid.uuid4(), job_id, url, url, hashlib.sha256(url.encode()).digest(),
                    FIXTURE_ORIGINS[index % len(FIXTURE_ORIGINS)], 0, index, "pending", 2,
                    capability,
                ))
            await conn.copy_records_to_table("crawl_tasks", records=records, columns=columns)
    finally:
        await conn.close()
    return job_id


async def _worker(dsn: str, worker_id: str, result_queue) -> None:
    # The child explicitly sets its one disposable DSN; it never inherits an
    # ambient deployment DATABASE_URL.
    os.environ["DATABASE_URL"] = dsn
    from app.crawl import repository
    from app.crawl.classify import FailureDecision
    from app.crawl.types import TaskResult
    from app.db import pool

    claimed = completed = retried = 0
    latencies: list[float] = []
    idle_since = time.monotonic()
    try:
        while time.monotonic() - idle_since < 5:
            started = time.perf_counter()
            task = await repository.claim_task(worker_id, {"http", "browser"})
            elapsed = (time.perf_counter() - started) * 1000
            if task is None:
                await asyncio.sleep(0.002)
                continue
            idle_since = time.monotonic()
            claimed += 1
            if claimed % 25 == 1:
                latencies.append(elapsed)
            if "/retry/" in task.url and task.attempt == 1:
                ok = await repository.retry_task(
                    task.id, task.lease_token,
                    # Exercise the retry state machine without intentionally
                    # opening the per-origin transport circuit. Circuit-breaker
                    # behavior has dedicated regression coverage.
                    FailureDecision(True, "extraction", "load_retry"),
                    datetime.now(timezone.utc), {"downloaded_bytes": 0},
                )
                if not ok:
                    raise RuntimeError("fenced retry was rejected")
                retried += 1
                continue
            ok = await repository.complete_task(
                task.id, task.lease_token,
                TaskResult(task.url, 200, "fixture", "ok", {"downloaded_bytes": 2}),
            )
            if not ok:
                raise RuntimeError("duplicate or over-budget result was rejected")
            completed += 1
    finally:
        await pool.reset_pool()
    result_queue.put({
        "worker": worker_id, "claimed": claimed, "completed": completed, "retried": retried,
        "claim_latency_ms": latencies,
        "rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        ),
    })


def _worker_entry(dsn: str, worker_id: str, result_queue) -> None:
    try:
        asyncio.run(_worker(dsn, worker_id, result_queue))
    except BaseException as exc:
        result_queue.put({"worker": worker_id, "error": f"{type(exc).__name__}: {exc}"})
        raise


async def _sample(dsn: str, job_id: uuid.UUID) -> dict:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT state, count(*) AS count FROM crawl_tasks WHERE job_id = $1 GROUP BY state", job_id)
        duplicate_origin_leases = await conn.fetchval(
            "SELECT count(*) FROM (SELECT origin_key FROM crawl_origin_leases "
            "GROUP BY origin_key HAVING count(*) > 1) AS duplicate_leases"
        )
        job = await conn.fetchrow(
            "SELECT max_bytes, downloaded_bytes, reserved_bytes, max_artifact_bytes, "
            "artifact_bytes, reserved_artifact_bytes FROM crawl_jobs WHERE id = $1", job_id,
        )
        return {
            "states": {row["state"]: row["count"] for row in rows},
            "duplicate_origin_leases": duplicate_origin_leases,
            "bytes_safe": job["downloaded_bytes"] + job["reserved_bytes"] <= job["max_bytes"],
            "artifacts_safe": job["artifact_bytes"] + job["reserved_artifact_bytes"] <= job["max_artifact_bytes"],
        }
    finally:
        await conn.close()


def _metric_labels_safe() -> bool:
    from app.metrics import METRIC_LABELS

    return not FORBIDDEN_METRIC_LABELS.intersection(
        label for labels in METRIC_LABELS.values() for label in labels
    )


async def run(args: argparse.Namespace) -> dict:
    dsn = None
    processes = []
    try:
        dsn = await _create_database(args.admin_dsn)
        started = time.monotonic()
        job_id = await _seed(dsn, args.tasks)
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        processes = [context.Process(
            target=_worker_entry, args=(dsn, f"load-{number + 1}", result_queue),
        ) for number in range(args.workers)]
        for process in processes:
            process.start()

        samples = []
        while any(process.is_alive() for process in processes):
            await asyncio.sleep(1)
            sample = await _sample(dsn, job_id)
            sample["elapsed_seconds"] = round(time.monotonic() - started, 3)
            samples.append(sample)
            if sample["duplicate_origin_leases"] or not sample["bytes_safe"] or not sample["artifacts_safe"]:
                raise RuntimeError("origin concurrency or byte-cap safety check failed")
            if time.monotonic() - started > args.timeout_seconds:
                raise RuntimeError("queue did not complete before the bounded timeout")

        for process in processes:
            process.join(timeout=1)
            if process.exitcode:
                raise RuntimeError(f"worker process {process.pid} exited with {process.exitcode}")
        results = [result_queue.get(timeout=5) for _ in processes]
        errors = [result["error"] for result in results if "error" in result]
        if errors:
            raise RuntimeError("; ".join(errors))
        final = await _sample(dsn, job_id)
        succeeded = final["states"].get("succeeded", 0)
        remaining = sum(count for state, count in final["states"].items() if state not in {"succeeded"})
        if succeeded != args.tasks or remaining:
            raise RuntimeError(f"unreconstructable queue state: {final['states']}")
        if not _metric_labels_safe():
            raise RuntimeError("metric labels include an unbounded identifier")
        latencies = sorted(value for result in results for value in result["claim_latency_ms"])
        rss = [result["rss_bytes"] for result in results]
        elapsed = time.monotonic() - started
        return {
            "tasks": args.tasks, "workers": args.workers,
            "fixture": "synthetic queue capability mix (HTTP/retry/PDF/browser)",
            "elapsed_seconds": round(elapsed, 3), "completion_rate_per_second": round(args.tasks / elapsed, 2),
            "claim_latency_ms": {"p50": round(statistics.median(latencies), 3),
                                 "p95": round(latencies[int(len(latencies) * .95) - 1], 3)},
            "worker_rss_bytes": {"max": max(rss), "limit": args.max_worker_rss_bytes},
            "browser_rss": "not_applicable (queue fixtures do not launch a browser)",
            "retries": sum(result["retried"] for result in results),
            "final": final, "samples": samples,
        }
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
        if dsn is not None:
            await _drop_database(args.admin_dsn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--admin-dsn",
        default=os.getenv("V040_LOAD_ADMIN_DSN", "postgresql://localhost:5432/postgres"),
    )
    parser.add_argument("--tasks", type=int, default=100_000)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-worker-rss-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--report", type=Path, default=ROOT / "tmp" / "v040-load-report.json")
    args = parser.parse_args()
    if args.tasks < 1 or args.workers < WORKERS or args.timeout_seconds < 1:
        parser.error("--tasks and --timeout-seconds must be positive; --workers must be at least 8")
    return args


if __name__ == "__main__":
    options = parse_args()
    report = asyncio.run(run(options))
    if report["worker_rss_bytes"]["max"] > options.max_worker_rss_bytes:
        raise SystemExit("worker RSS exceeded the configured safety limit")
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "samples"}
    summary["sample_count"] = len(report["samples"])
    summary["report"] = str(options.report)
    print(json.dumps(summary, indent=2))
