"""Restricted database client used by enrolled remote workers."""
import hashlib
import json
from typing import Any, Optional
from uuid import UUID

from app.crawl.types import ClaimedTask, TaskResult
from app import normalize


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


class RemoteRepository:
    """Calls only worker_api functions; this module deliberately names no tables."""

    def __init__(self, pool: Any, capabilities: set[str]):
        self.pool = pool
        self.capabilities = capabilities

    async def register(self, protocol_version: int, capabilities: set[str]) -> str:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT worker_api.register($1,$2::TEXT[])", protocol_version,
                sorted(capabilities),
            )

    async def drain(self) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT worker_api.drain()"))

    async def claim_task(self, worker_id: str, capabilities: set[str]) -> Optional[ClaimedTask]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM worker_api.claim($1::TEXT[])", sorted(capabilities)
            )
        if row is None:
            return None
        return ClaimedTask(
            id=row["id"], job_id=row["job_id"], url=row["url"],
            normalized_url=row["normalized_url"], origin_key=row["origin_key"],
            depth=row["depth"], attempt=row["attempt"], lease_token=row["lease_token"],
            deadline_at=row["deadline_at"], config=_loads(row["config"]),
            byte_allowance=row["byte_allowance"], artifact_allowance=row["artifact_allowance"],
        )

    async def heartbeat(self, task_id: UUID, lease_token: UUID) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT worker_api.heartbeat($1, $2)", task_id, lease_token))

    async def reserve_browser_navigation(self, task_id: UUID, lease_token: UUID) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.reserve_browser_navigation($1,$2)",
                task_id, lease_token,
            ))

    async def robots_cache(self, origin_key: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("SELECT worker_api.robots_cache($1)", origin_key)
        return _loads(value) if value is not None else None

    async def store_robots(self, origin_key: str, body: str, status: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.fetchval(
                "SELECT worker_api.store_robots($1,$2,$3)", origin_key, body, status,
            )

    async def block_robots(self, task_id: UUID, lease_token: UUID, code: str) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.block_robots($1,$2,$3)", task_id, lease_token, code,
            ))

    async def complete_task(self, task_id: UUID, lease_token: UUID, result: TaskResult,
                            artifact_ref: Optional[dict] = None) -> bool:
        if artifact_ref is None:
            raise ValueError("remote completion requires an artifact reference")
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.complete($1,$2,$3::jsonb,$4::jsonb)", task_id,
                lease_token, json.dumps(artifact_ref), json.dumps({
                    **dict(result.metadata), "final_url": result.final_url,
                    "status_code": result.status_code, "title": result.title,
                    "discovered_urls": [
                        {
                            "url": url,
                            "origin_key": normalize.origin_key(url),
                            "sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                        }
                        for url in result.discovered_urls
                    ],
                }),
            ))

    async def fail_task(self, task_id: UUID, lease_token: UUID, decision: Any, metadata: Optional[dict] = None) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.fail($1,$2,$3,$4,$5::jsonb)", task_id, lease_token,
                decision.error_class, decision.error_code, json.dumps(metadata or {}),
            ))

    async def retry_task(self, task_id: UUID, lease_token: UUID, decision: Any,
                         available_at: Any, metadata: Optional[dict] = None) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.retry($1,$2,$3,$4,$5,$6::jsonb)", task_id, lease_token,
                decision.error_class, decision.error_code, available_at, json.dumps(metadata or {}),
            ))

    async def release(self, task_id: UUID, lease_token: UUID) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.release($1,$2)", task_id, lease_token
            ))

    async def release_current(self, task_id: UUID, lease_token: UUID) -> bool:
        return await self.release(task_id, lease_token)

    async def wait_for_input(self, task_id: UUID, lease_token: UUID, code: str) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT worker_api.wait_for_input($1,$2,$3)", task_id, lease_token, code
            ))
