"""Encrypted, allowlisted Playwright storage-state profiles."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from publicsuffix2 import get_tld


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ProfileEnvelope:
    """Ciphertext and metadata persisted for one reusable profile."""

    ciphertext: bytes
    nonce: bytes
    key_version: str


def _normal_host(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("host must be a non-empty hostname")
    if any(part in value for part in ("://", "/", "@", ":")):
        raise ValueError("host must be a hostname, not a URL or address")
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("host is not valid IDNA") from exc
    if not host or len(host) > 253 or any(
        not _HOST_LABEL.fullmatch(label) for label in host.split(".")
    ):
        raise ValueError("host is not a valid hostname")
    return host


def _validate_pattern(pattern: str) -> tuple[bool, str]:
    if not isinstance(pattern, str) or not pattern or pattern != pattern.strip():
        raise ValueError("profile allowlist entries must be non-empty hostnames")
    wildcard = pattern.startswith("*.")
    if "*" in pattern and not wildcard:
        raise ValueError("only leading subdomain wildcards are supported")
    domain = _normal_host(pattern[2:] if wildcard else pattern)
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("IP address allowlist entries are not supported")
    suffix = get_tld(domain, strict=True)
    if wildcard and (suffix is None or suffix == domain):
        raise ValueError("wildcard must be below a recognized public suffix")
    return wildcard, domain


def host_allowed(host: str, patterns: Iterable[str]) -> bool:
    """Return whether *host* matches an explicit, validated profile allowlist."""
    if not isinstance(host, str):
        raise ValueError("host must be a non-empty hostname")
    try:
        ipaddress.ip_address(host.rstrip("."))
        return False
    except ValueError:
        target = _normal_host(host)
    validated = [_validate_pattern(pattern) for pattern in patterns]
    for wildcard, domain in validated:
        if (not wildcard and target == domain) or (
            wildcard and target.endswith("." + domain)
        ):
            return True
    return False


class ProfileCipher:
    """AES-GCM key ring with explicit versioned envelopes."""

    def __init__(self, primary_version: str, keys: dict[str, bytes]) -> None:
        self._primary_version = primary_version
        self._keys = keys

    @classmethod
    def from_values(
        cls, primary: str, previous: Sequence[str] | None = None
    ) -> "ProfileCipher":
        values = [primary, *(previous or ())]
        if not primary:
            raise ValueError("SESSION_ENCRYPTION_KEY is required")
        keys: dict[str, bytes] = {}
        materials: set[bytes] = set()
        for value in values:
            if not isinstance(value, str) or value.count(":") != 1:
                raise ValueError("profile keys must use version:urlsafe-base64-key")
            version, encoded = value.split(":", 1)
            if not version or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
                raise ValueError("profile key version is invalid")
            try:
                key = base64.b64decode(
                    encoded.encode("ascii"), altchars=b"-_", validate=True,
                )
            except (UnicodeEncodeError, ValueError) as exc:
                raise ValueError("profile key must be urlsafe base64") from exc
            if len(key) != 32:
                raise ValueError("profile keys must be 32-byte AES-256 keys")
            if version in keys or key in materials:
                raise ValueError("duplicate profile key version or material")
            keys[version] = key
            materials.add(key)
        return cls(primary.split(":", 1)[0], keys)

    @classmethod
    def from_environment(cls) -> "ProfileCipher":
        primary = os.environ.get("SESSION_ENCRYPTION_KEY", "")
        raw_previous = os.environ.get("SESSION_ENCRYPTION_PREVIOUS_KEYS", "")
        previous = raw_previous.split(",") if raw_previous else []
        return cls.from_values(primary, previous)

    def export_primary(self) -> str:
        return "%s:%s" % (
            self._primary_version,
            base64.urlsafe_b64encode(self._keys[self._primary_version]).decode("ascii"),
        )

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> ProfileEnvelope:
        if not isinstance(plaintext, bytes) or not isinstance(aad, bytes):
            raise TypeError("plaintext and AAD must be bytes")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._keys[self._primary_version]).encrypt(nonce, plaintext, aad)
        return ProfileEnvelope(ciphertext, nonce, self._primary_version)

    def decrypt(self, envelope: ProfileEnvelope, *, aad: bytes) -> bytes:
        key = self._keys.get(envelope.key_version)
        if key is None:
            raise ValueError("profile was encrypted with an unavailable key version")
        try:
            return AESGCM(key).decrypt(envelope.nonce, envelope.ciphertext, aad)
        except Exception as exc:
            raise ValueError("profile ciphertext or binding is invalid") from exc

    def needs_rotation(self, envelope: ProfileEnvelope) -> bool:
        return envelope.key_version != self._primary_version


class ProfileStore:
    """Core-only PostgreSQL repository for encrypted reusable profiles."""

    def __init__(self, pool: Any, cipher: ProfileCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    @staticmethod
    def _aad(profile_id: uuid.UUID, backend: str, pool_id: str) -> bytes:
        return json.dumps(
            [str(profile_id), backend, pool_id], separators=(",", ":")
        ).encode("utf-8")

    @staticmethod
    def _validate_fields(
        name: str, backend: str, pool_id: str, allowed_domains: Sequence[str]
    ) -> tuple[str, ...]:
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValueError("profile name must be 1 to 128 characters")
        if not isinstance(backend, str) or not backend or len(backend) > 128:
            raise ValueError("profile backend is required")
        if not isinstance(pool_id, str) or not pool_id or len(pool_id) > 128:
            raise ValueError("profile pool_id is required")
        if not allowed_domains:
            raise ValueError("profile must allow at least one domain")
        validated = tuple(pattern for pattern in allowed_domains)
        for pattern in validated:
            _validate_pattern(pattern)
        return validated

    async def create(
        self,
        *,
        name: str,
        backend: str,
        pool_id: str,
        allowed_domains: Sequence[str],
        payload: bytes,
        profile_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        domains = self._validate_fields(name, backend, pool_id, allowed_domains)
        if not isinstance(payload, bytes):
            raise TypeError("profile payload must be bytes")
        profile_id = profile_id or uuid.uuid4()
        envelope = self._cipher.encrypt(payload, aad=self._aad(profile_id, backend, pool_id))
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_profiles
                    (id, name, backend, pool_id, allowed_domains, ciphertext, nonce, key_version)
                VALUES ($1, $2, $3, $4, $5::text[], $6, $7, $8)
                """,
                profile_id, name, backend, pool_id, list(domains), envelope.ciphertext,
                envelope.nonce, envelope.key_version,
            )
        return profile_id

    async def load(self, profile_id: uuid.UUID, *, host: str) -> bytes:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM session_profiles WHERE id = $1 FOR UPDATE", profile_id
                )
                if row is None:
                    raise KeyError("session profile does not exist")
                if not host_allowed(host, row["allowed_domains"]):
                    raise PermissionError("target host is not allowed for this session profile")
                envelope = ProfileEnvelope(row["ciphertext"], row["nonce"], row["key_version"])
                payload = self._cipher.decrypt(
                    envelope, aad=self._aad(profile_id, row["backend"], row["pool_id"])
                )
                if self._cipher.needs_rotation(envelope):
                    rotated = self._cipher.encrypt(
                        payload, aad=self._aad(profile_id, row["backend"], row["pool_id"])
                    )
                    await conn.execute(
                        """
                        UPDATE session_profiles
                        SET ciphertext = $2, nonce = $3, key_version = $4, updated_at = now()
                        WHERE id = $1
                        """,
                        profile_id, rotated.ciphertext, rotated.nonce, rotated.key_version,
                    )
                return payload

    async def delete(self, profile_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM session_profiles WHERE id = $1", profile_id)
        return result == "DELETE 1"
