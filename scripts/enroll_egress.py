#!/usr/bin/env python3
"""Create or revoke the short-lived mTLS credentials for one owned egress node."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path


NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _private_file(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if mode & 0o077:
        raise ValueError(f"{label} must not be group/world-readable: {path}")


def _node_id(value: str) -> str:
    if not NODE_ID.fullmatch(value):
        raise ValueError("node id must be 1-64 letters, digits, underscores, or hyphens")
    return value


def _write_private(path: Path, content: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"refusing to overwrite existing enrollment bundle: {path}") from exc
    with os.fdopen(descriptor, "w") as bundle:
        bundle.write(content)


def _make_certificate(node_id: str, ca_cert: Path, ca_key: Path, output: Path,
                      days: int) -> tuple[Path, Path]:
    key_path = output.with_suffix(".key.pem")
    cert_path = output.with_suffix(".cert.pem")
    csr_path = output.with_suffix(".csr.pem")
    for path in (key_path, cert_path, csr_path):
        if path.exists():
            raise ValueError(f"refusing to overwrite existing credential: {path}")
    try:
        subprocess.run([
            "openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes",
            "-subj", f"/CN=crawltrove-egress-{node_id}", "-keyout", str(key_path),
            "-out", str(csr_path),
        ], check=True)
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(csr_path), "-CA", str(ca_cert),
            "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(cert_path),
            "-days", str(days), "-sha256",
        ], check=True)
    except Exception:
        for path in (key_path, cert_path):
            if path.exists():
                path.unlink()
        raise
    finally:
        if csr_path.exists():
            csr_path.unlink()
    key_path.chmod(0o600)
    cert_path.chmod(0o600)
    return cert_path, key_path


async def _create(args: argparse.Namespace) -> None:
    node_id = _node_id(args.id)
    if not 1 <= args.days <= 90:
        raise ValueError("credential lifetime must be 1-90 days")
    ca_cert, ca_key, output = Path(args.ca_cert), Path(args.ca_key), Path(args.out)
    _private_file(ca_key, "CA key")
    if not ca_cert.is_file():
        raise ValueError(f"cannot read CA certificate: {ca_cert}")
    cert_path, key_path = _make_certificate(node_id, ca_cert, ca_key, output, args.days)
    try:
        bundle = {
            "nodeId": node_id,
            "caCert": str(ca_cert.resolve()),
            "nodeCert": str(cert_path.resolve()),
            "nodeKey": str(key_path.resolve()),
            "allowedPorts": sorted(set(args.allowed_port)),
            "listenHost": args.listen_host,
            "listenPort": args.listen_port,
        }
        if args.crl:
            bundle["crl"] = str(Path(args.crl).resolve())
        _write_private(output, json.dumps(bundle, indent=2) + "\n")
    except Exception:
        for path in (output, cert_path, key_path):
            if path.exists():
                path.unlink()
        raise


async def _revoke(args: argparse.Namespace) -> None:
    node_id = _node_id(args.id)
    certificate = Path(args.certificate)
    if not certificate.is_file():
        raise ValueError(f"cannot read node certificate: {certificate}")
    import asyncpg
    connection = await asyncpg.connect(args.database_admin_dsn)
    try:
        async with connection.transaction():
            changed = await connection.execute(
                "UPDATE proxy_nodes SET state = 'revoked' WHERE id = $1", node_id,
            )
            if changed.endswith("0"):
                raise ValueError(f"egress node is not enrolled: {node_id}")
    finally:
        await connection.close()
    # The CA database is operator-maintained.  Update its revocation record only
    # after the core will no longer select the node.
    subprocess.run(["openssl", "ca", "-config", args.ca_config, "-revoke", str(certificate)], check=True)
    subprocess.run(["openssl", "ca", "-config", args.ca_config, "-gencrl", "-out", args.crl], check=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--ca-cert", required=True)
    create.add_argument("--ca-key", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--days", type=int, default=30)
    create.add_argument("--allowed-port", type=int, choices=(80, 443), action="append", default=[])
    create.add_argument("--listen-host", default="0.0.0.0")
    create.add_argument("--listen-port", type=int, default=9443)
    create.add_argument("--crl")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    revoke.add_argument("--certificate", required=True)
    revoke.add_argument("--database-admin-dsn", required=True)
    revoke.add_argument("--ca-config", required=True)
    revoke.add_argument("--crl", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create" and not args.allowed_port:
        args.allowed_port = [80, 443]
    try:
        asyncio.run(_create(args) if args.command == "create" else _revoke(args))
    except Exception as exc:
        # Command arguments can contain DSNs; never include them in this error.
        message = str(exc) if isinstance(exc, (ValueError, subprocess.CalledProcessError)) else "egress enrollment failed"
        print(f"egress enrollment failed: {message}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
