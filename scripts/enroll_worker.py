#!/usr/bin/env python3
"""Enroll or revoke one least-privilege remote crawl worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,52}$")


def _role_name(worker_id: str) -> str:
    if not WORKER_ID.fullmatch(worker_id):
        raise ValueError("worker id must be 1-53 letters, digits, underscores, or hyphens")
    return "ct_worker_" + worker_id.replace("-", "_")


def _identifier(value: str) -> str:
    # IDs are already constrained, but quote identifiers so the command remains
    # correct for the mixed-case IDs accepted by the enrollment format.
    return '"' + value.replace('"', '""') + '"'


def _secret_file(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        value = path.read_text().strip()
    except OSError as exc:
        raise ValueError(f"cannot read credential file: {path}") from exc
    if mode & 0o077:
        raise ValueError(f"credential file must not be group/world-readable: {path}")
    if not value:
        raise ValueError(f"credential file is empty: {path}")
    return value


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)


def _worker_database_url(admin_dsn: str, role: str) -> str:
    parsed = urlsplit(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise ValueError("database admin DSN must be a PostgreSQL URL")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    query = "sslmode=verify-full"
    return urlunsplit(("postgresql", f"{quote(role, safe='')}@{host}", parsed.path, query, ""))


def _make_certificate(role: str, ca_cert: Path, ca_key: Path, output: Path) -> tuple[Path, Path]:
    key_path = output.with_suffix(".key.pem")
    cert_path = output.with_suffix(".cert.pem")
    csr_path = output.with_suffix(".csr.pem")
    subprocess.run([
        "openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes",
        "-subj", f"/CN={role}", "-keyout", str(key_path), "-out", str(csr_path),
    ], check=True)
    try:
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(csr_path), "-CA", str(ca_cert),
            "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(cert_path),
            "-days", "365", "-sha256",
        ], check=True)
    finally:
        if csr_path.exists():
            csr_path.unlink()
    key_path.chmod(0o600)
    cert_path.chmod(0o600)
    return cert_path, key_path


async def _create(args: argparse.Namespace) -> None:
    role = _role_name(args.id)
    output = Path(args.out)
    if output.exists():
        raise ValueError(f"refusing to overwrite existing enrollment bundle: {output}")
    capabilities = sorted(set(args.capability))
    allowed = {"http", "browser", "proxy", "captcha"}
    if not set(capabilities) <= allowed:
        raise ValueError("unsupported capability")

    ca_cert, ca_key = Path(args.ca_cert), Path(args.ca_key)
    try:
        if ca_key.stat().st_mode & 0o077:
            raise ValueError(f"CA key must not be group/world-readable: {ca_key}")
    except OSError as exc:
        raise ValueError(f"cannot read CA key: {ca_key}") from exc
    cert_path, key_path = _make_certificate(role, ca_cert, ca_key, output)
    try:
        _write_private(output, json.dumps({
            "workerId": args.id,
            "databaseUrl": args.database_url or _worker_database_url(args.database_admin_dsn, role),
            "caCert": str(ca_cert.resolve()),
            "clientCert": str(cert_path.resolve()),
            "clientKey": str(key_path.resolve()),
            "capabilities": capabilities,
            "protocolVersion": 1,
            "artifactPrefix": f"workers/{args.id}/",
            "s3Bucket": args.s3_bucket,
            "s3AccessKey": _secret_file(Path(args.s3_access_key_file)),
            "s3SecretKey": _secret_file(Path(args.s3_secret_key_file)),
        }, indent=2) + "\n")
        import asyncpg
        conn = await asyncpg.connect(dsn=args.database_admin_dsn)
        try:
            async with conn.transaction():
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawltrove_worker') THEN
                            CREATE ROLE crawltrove_worker NOLOGIN;
                        END IF;
                    END $$
                """)
                await conn.execute("GRANT USAGE ON SCHEMA worker_api TO crawltrove_worker")
                await conn.execute(
                    "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA worker_api TO crawltrove_worker"
                )
                await conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM crawltrove_worker")
                await conn.execute(f"CREATE ROLE {_identifier(role)} LOGIN INHERIT")
                await conn.execute(f"GRANT crawltrove_worker TO {_identifier(role)}")
                await conn.execute(
                    "INSERT INTO workers (id, db_role, capabilities, protocol_version, state, artifact_bucket, artifact_prefix) "
                    "VALUES ($1, $2::name, $3::text[], 1, 'active', $4, $5)",
                    args.id, role, capabilities, args.s3_bucket, f"workers/{args.id}/",
                )
        finally:
            await conn.close()
    except Exception:
        # Do not leave reusable private credentials for a DB identity that was
        # not successfully enrolled.
        for path in (output, cert_path, key_path):
            if path.exists():
                path.unlink()
        raise


async def _revoke(args: argparse.Namespace) -> None:
    role = _role_name(args.id)
    import asyncpg
    conn = await asyncpg.connect(dsn=args.database_admin_dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE workers SET state = 'revoked', revoked_at = now() WHERE id = $1",
                args.id,
            )
            await conn.execute(f"ALTER ROLE {_identifier(role)} NOLOGIN")
    finally:
        await conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    create = command.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--capability", action="append", required=True)
    create.add_argument("--database-admin-dsn", required=True)
    create.add_argument("--database-url")
    create.add_argument("--ca-cert", required=True)
    create.add_argument("--ca-key", required=True)
    create.add_argument("--s3-access-key-file", required=True)
    create.add_argument("--s3-secret-key-file", required=True)
    create.add_argument("--s3-bucket", required=True)
    create.add_argument("--out", required=True)
    revoke = command.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    revoke.add_argument("--database-admin-dsn", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(_create(args) if args.command == "create" else _revoke(args))
    except Exception as exc:
        # Intentionally omit command arguments: they can contain a DSN.
        message = str(exc) if isinstance(exc, (ValueError, subprocess.CalledProcessError)) else "database enrollment failed"
        print(f"enrollment failed: {message}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
