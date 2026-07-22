"""Explicit, operator-invoked maintenance commands for durable crawling."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.artifacts import ArtifactRef, FilesystemArtifactStore, S3ArtifactStore, artifact_store
from app.crawl import repository
from app.db import migrate


COMMANDS = frozenset({
    "reconcile-job", "reap-leases", "list-failures", "validate-artifacts",
    "cleanup-temporary", "purge-job", "compatibility",
})


def validate_purge_confirmation(job_id: str, confirmation: str) -> UUID:
    """Reject every purge request that is not an exact UUID confirmation."""
    try:
        parsed = UUID(job_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("purge-job requires one explicit job UUID") from exc
    if confirmation != str(parsed):
        raise ValueError("purge-job requires --confirm with the identical job UUID")
    return parsed


async def list_failures(job_id: UUID | None = None, limit: int = 100) -> list[dict]:
    """Return durable failed task metadata without URLs, secrets, or raw captures."""
    pool = await repository.require_pool()
    query = """
        SELECT id, job_id, state, error_class, error_code, attempt_count, finished_at
        FROM crawl_tasks
        WHERE state IN ('http_error', 'extraction_failed', 'permanent_failed')
          AND ($1::UUID IS NULL OR job_id = $1)
        ORDER BY finished_at DESC NULLS LAST
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, job_id, max(1, min(limit, 500)))
    return [dict(row) for row in rows]


async def validate_artifacts(job_id: UUID | None = None) -> dict[str, int]:
    """Verify referenced immutable artifacts without changing them."""
    pool = await repository.require_pool()
    query = """
        SELECT r.markdown_ref, r.content_sha256, r.artifact_bytes, r.metadata,
               attempt.worker_id
        FROM crawl_results r JOIN crawl_tasks t ON t.id = r.task_id
        LEFT JOIN LATERAL (
            SELECT worker_id FROM acquisition_attempts
            WHERE task_id = r.task_id AND worker_id IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
        ) AS attempt ON TRUE
        WHERE r.markdown_ref IS NOT NULL AND ($1::UUID IS NULL OR t.job_id = $1)
    """
    checked = invalid = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, job_id)
    for row in rows:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        ref = ArtifactRef(
            uri=row["markdown_ref"], size=row["artifact_bytes"],
            sha256=row["content_sha256"],
            media_type=(metadata or {}).get("media_type", "text/markdown"),
        )
        checked += 1
        try:
            store = artifact_store(row["worker_id"])
            valid = await store.verify(ref)
        except Exception:
            valid = False
        if not valid:
            invalid += 1
    return {"checked": checked, "invalid": invalid}


async def cleanup_temporary(now: datetime | None = None) -> int:
    """Remove only artifact-store temporary objects older than 24 hours."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    store = artifact_store()
    if isinstance(store, FilesystemArtifactStore):
        temporary = store.root / "tmp"
        if not temporary.is_dir():
            return 0
        removed = 0
        for candidate in temporary.iterdir():
            if candidate.is_file() and datetime.fromtimestamp(
                candidate.stat().st_mtime, tz=timezone.utc,
            ) < cutoff:
                candidate.unlink()
                removed += 1
        return removed
    if isinstance(store, S3ArtifactStore):
        prefix = f"{store.prefix}tmp/"
        removed = 0
        continuation_token = None
        while True:
            request = {"Bucket": store.bucket, "Prefix": prefix}
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            response = await asyncio.to_thread(store.client.list_objects_v2, **request)
            for item in response.get("Contents", []):
                if item["LastModified"] < cutoff:
                    await asyncio.to_thread(
                        store.client.delete_object, Bucket=store.bucket, Key=item["Key"],
                    )
                    removed += 1
            continuation_token = response.get("NextContinuationToken")
            if not response.get("IsTruncated"):
                break
        return removed
    raise RuntimeError("unsupported artifact store")


async def purge_job(job_id: str, confirmation: str) -> bool:
    """Delete one durable job only after an exact, explicit confirmation."""
    parsed = validate_purge_confirmation(job_id, confirmation)
    pool = await repository.require_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM crawl_jobs WHERE id = $1 RETURNING TRUE", parsed,
        )
    return bool(deleted)


async def compatibility() -> dict[str, int]:
    """Apply only pending forward migrations and report the count."""
    applied = await migrate.run_migrations()
    return {"migrationsApplied": applied}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile-job")
    reconcile.add_argument("job_id")
    commands.add_parser("reap-leases")
    failures = commands.add_parser("list-failures")
    failures.add_argument("--job-id")
    failures.add_argument("--limit", type=int, default=100)
    validate = commands.add_parser("validate-artifacts")
    validate.add_argument("--job-id")
    commands.add_parser("cleanup-temporary")
    purge = commands.add_parser("purge-job")
    purge.add_argument("job_id")
    purge.add_argument("--confirm", required=True)
    commands.add_parser("compatibility")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict | list[dict]:
    if args.command == "reconcile-job":
        return {"reconciled": await repository.reconcile_job(UUID(args.job_id))}
    if args.command == "reap-leases":
        return {"reaped": await repository.reap_expired_leases()}
    if args.command == "list-failures":
        return await list_failures(UUID(args.job_id) if args.job_id else None, args.limit)
    if args.command == "validate-artifacts":
        return await validate_artifacts(UUID(args.job_id) if args.job_id else None)
    if args.command == "cleanup-temporary":
        return {"removed": await cleanup_temporary()}
    if args.command == "purge-job":
        return {"purged": await purge_job(args.job_id, args.confirm)}
    if args.command == "compatibility":
        return await compatibility()
    raise ValueError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), default=str))
