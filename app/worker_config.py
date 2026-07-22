"""Validated configuration for an enrolled remote acquisition worker."""
from __future__ import annotations

import json
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
from types import MappingProxyType


PROTOCOL_VERSION = 1
VALID_CAPABILITIES = frozenset({
    "http", "browser", "proxy", "captcha",
    "firecrawl_scrape", "firecrawl_interact", "brightdata_unlocker",
    "browserbase_session",
})
WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _value(mapping: Mapping[str, Any], camel: str, snake: str | None = None) -> Any:
    if camel in mapping:
        return mapping[camel]
    return mapping.get(snake or camel)


def _private_file(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if mode & 0o077:
        raise ValueError(f"{label} must not be group/world-readable: {path}")


@dataclass(frozen=True)
class WorkerConfig:
    """Enrollment material with transport and identity invariants checked once."""

    worker_id: str
    database_url: str
    capabilities: frozenset[str]
    protocol_version: int
    artifact_prefix: str
    ssl_context: ssl.SSLContext | None
    security_state: str
    artifact_settings: Mapping[str, Any]

    @classmethod
    def from_file(cls, path: str | Path) -> "WorkerConfig":
        bundle = Path(path)
        _private_file(bundle, "enrollment bundle")
        try:
            data = json.loads(bundle.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid enrollment bundle: {bundle}") from exc
        if not isinstance(data, dict):
            raise ValueError("enrollment bundle must be a JSON object")
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "WorkerConfig":
        worker_id = _value(mapping, "workerId", "worker_id")
        if not isinstance(worker_id, str) or not WORKER_ID.fullmatch(worker_id):
            raise ValueError("workerId must contain only letters, digits, underscores, and hyphens")

        database_url = _value(mapping, "databaseUrl", "database_url")
        parsed = urlsplit(str(database_url or ""))
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
            raise ValueError("databaseUrl must be a PostgreSQL URL")
        if parse_qs(parsed.query).get("sslmode", [""])[0].lower() == "disable":
            raise ValueError("databaseUrl must use verified TLS")

        protocol = _value(mapping, "protocolVersion", "protocol_version")
        if protocol is None:
            protocol = PROTOCOL_VERSION
        if protocol != PROTOCOL_VERSION:
            raise ValueError(f"protocolVersion must be {PROTOCOL_VERSION}")

        capabilities = _value(mapping, "capabilities") or []
        if not isinstance(capabilities, (list, tuple, set)) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise ValueError("capabilities must be a list of names")
        capability_set = frozenset(capabilities)
        unknown = capability_set - VALID_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown worker capability: {sorted(unknown)[0]}")

        allow_insecure = os.environ.get("WORKER_ALLOW_INSECURE_DB", "").lower() == "true"
        ca_cert = _value(mapping, "caCert", "ca_cert")
        client_cert = _value(mapping, "clientCert", "client_cert")
        client_key = _value(mapping, "clientKey", "client_key")
        if allow_insecure:
            context = None
            security_state = "degraded"
        else:
            if not all(isinstance(value, str) and value for value in (ca_cert, client_cert, client_key)):
                raise ValueError("remote workers require verified TLS certificates")
            key_path = Path(client_key)
            _private_file(key_path, "client key")
            try:
                context = ssl.create_default_context(cafile=str(ca_cert))
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                context.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
            except (OSError, ssl.SSLError) as exc:
                raise ValueError("verified TLS configuration is invalid") from exc
            security_state = "verified"

        prefix = _value(mapping, "artifactPrefix", "artifact_prefix")
        expected_prefix = f"workers/{worker_id}/"
        if prefix != expected_prefix:
            raise ValueError(f"artifactPrefix must equal {expected_prefix}")

        artifact_settings = {
            key: value for key, value in mapping.items()
            if key.startswith("s3") or key.startswith("S3_")
        }
        return cls(
            worker_id=worker_id,
            database_url=str(database_url),
            capabilities=capability_set,
            protocol_version=protocol,
            artifact_prefix=prefix,
            ssl_context=context,
            security_state=security_state,
            artifact_settings=MappingProxyType(artifact_settings),
        )
