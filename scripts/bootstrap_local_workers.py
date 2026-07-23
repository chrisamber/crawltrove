#!/usr/bin/env python3
"""Provision the private-network workers used by the local Compose stack."""

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


WORKERS = (
    ("standard", ("http",)),
    ("browser", ("browser", "http")),
    ("captcha", ("browser", "captcha", "http")),
)
SAFE_SECRET = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _role_name(worker_id: str) -> str:
    return "ct_worker_" + worker_id.replace("-", "_")


def _worker_dsn(admin_dsn: str, role: str, password: str) -> str:
    parsed = urlsplit(admin_dsn)
    host = parsed.hostname or "db"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit(("postgresql", netloc, parsed.path, "", ""))


async def _role_statement(conn, template: str, role: str, password: str | None = None):
    if password is None:
        return await conn.fetchval("SELECT format($1, $2::text)", template, role)
    return await conn.fetchval(
        "SELECT format($1, $2::text, $3::text)", template, role, password
    )


async def bootstrap() -> None:
    if os.environ.get("CRAWLTROVE_LOCAL_BOOTSTRAP") != "true":
        raise RuntimeError("local worker bootstrap is disabled")
    admin_dsn = _required("DATABASE_ADMIN_URL")
    if (urlsplit(admin_dsn).hostname or "") != "db":
        raise RuntimeError("local worker bootstrap may connect only to Compose service db")

    output_dir = Path(os.environ.get("WORKER_ENROLLMENT_DIR", "/enrollments"))
    output_dir.mkdir(parents=True, exist_ok=True)
    worker_uid = int(os.environ.get("WORKER_BUNDLE_UID", "1000"))
    worker_gid = int(os.environ.get("WORKER_BUNDLE_GID", "1000"))
    artifact_bucket = _required("S3_BUCKET")

    import asyncpg

    bundles = []
    conn = await asyncpg.connect(admin_dsn)
    try:
        async with conn.transaction():
            if not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='crawltrove_worker')"
            ):
                await conn.execute("CREATE ROLE crawltrove_worker NOLOGIN")
            for worker_id, capabilities in WORKERS:
                role = _role_name(worker_id)
                password = _required(f"{worker_id.upper()}_DB_PASSWORD")
                if not SAFE_SECRET.fullmatch(password):
                    raise RuntimeError(f"{worker_id} local DB password is invalid")
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=$1)", role
                )
                template = (
                    "ALTER ROLE %I LOGIN PASSWORD %L"
                    if exists
                    else "CREATE ROLE %I LOGIN INHERIT PASSWORD %L"
                )
                await conn.execute(await _role_statement(conn, template, role, password))
                await conn.execute(
                    await _role_statement(conn, "GRANT crawltrove_worker TO %I", role)
                )
                await conn.execute(
                    """INSERT INTO workers
                       (id, db_role, capabilities, protocol_version, state,
                        artifact_bucket, artifact_prefix)
                       VALUES ($1, $2::name, $3::text[], 1, 'active', $4, $5)
                       ON CONFLICT (id) DO UPDATE SET db_role=EXCLUDED.db_role,
                           capabilities=EXCLUDED.capabilities, protocol_version=1,
                           artifact_bucket=EXCLUDED.artifact_bucket,
                           state='active', artifact_prefix=EXCLUDED.artifact_prefix,
                           revoked_at=NULL""",
                    worker_id,
                    role,
                    list(capabilities),
                    artifact_bucket,
                    f"workers/{worker_id}/",
                )
                bundles.append((worker_id, {
                    "workerId": worker_id,
                    "databaseUrl": _worker_dsn(admin_dsn, role, password),
                    "capabilities": list(capabilities),
                    "protocolVersion": 1,
                    "artifactPrefix": f"workers/{worker_id}/",
                    "s3Bucket": artifact_bucket,
                    "s3AccessKey": _required(f"{worker_id.upper()}_S3_ACCESS_KEY"),
                    "s3SecretKey": _required(f"{worker_id.upper()}_S3_SECRET_KEY"),
                }))
    finally:
        await conn.close()

    for worker_id, bundle in bundles:
        destination = output_dir / f"{worker_id}.json"
        temporary = output_dir / f".{worker_id}.json.tmp"
        temporary.write_text(json.dumps(bundle, separators=(",", ":")) + "\n")
        temporary.chmod(0o600)
        os.chown(temporary, worker_uid, worker_gid)
        os.replace(temporary, destination)


if __name__ == "__main__":
    asyncio.run(bootstrap())
