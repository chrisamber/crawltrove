import base64
import os
import uuid

import pytest

from app.acquisition.profiles import ProfileCipher, ProfileStore, host_allowed
from tests.conftest import requires_db


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_profile_cipher_round_trip_and_rotation():
    old = ProfileCipher.from_values(f"v1:{_key()}")
    envelope = old.encrypt(b'{"cookies":[]}', aad=b"profile-a")

    rotated = ProfileCipher.from_values(f"v2:{_key()}", [old.export_primary()])

    assert rotated.decrypt(envelope, aad=b"profile-a") == b'{"cookies":[]}'
    assert rotated.needs_rotation(envelope) is True


def test_profile_cipher_rejects_bad_or_duplicate_keys():
    with pytest.raises(ValueError, match="32-byte"):
        ProfileCipher.from_values(
            "v1:" + base64.urlsafe_b64encode(os.urandom(31)).decode()
        )
    primary = f"v1:{_key()}"
    with pytest.raises(ValueError, match="duplicate"):
        ProfileCipher.from_values(primary, [primary])
    with pytest.raises(ValueError, match="base64"):
        ProfileCipher.from_values("v1:" + "!" * 44)


def test_domain_allowlist_is_explicit():
    assert host_allowed("example.com", ["example.com"])
    assert not host_allowed("www.example.com", ["example.com"])
    assert host_allowed("www.example.com", ["*.example.com"])
    assert not host_allowed("example.com", ["*.example.com"])
    with pytest.raises(ValueError):
        host_allowed("shop.co.uk", ["*.co.uk"])
    with pytest.raises(ValueError):
        host_allowed("example.com", ["example.com", "*.co.uk"])
    with pytest.raises(ValueError):
        host_allowed("example.com", ["*.localhost"])


def test_profile_cipher_aad_pins_profile_backend_and_pool():
    cipher = ProfileCipher.from_values(f"v1:{_key()}")
    envelope = cipher.encrypt(b"state", aad=b"profile-a:playwright:owned")

    with pytest.raises(ValueError):
        cipher.decrypt(envelope, aad=b"profile-a:playwright:other-pool")


@pytest.mark.asyncio
@requires_db
async def test_profile_store_rotates_pinned_payload_and_deletes(db):
    old = ProfileCipher.from_values(f"v1:{_key()}")
    profile_id = await ProfileStore(db, old).create(
        name=f"profile-{uuid.uuid4()}",
        backend="playwright",
        pool_id="owned-sg",
        allowed_domains=["example.com", "*.docs.example.com"],
        payload=b'{"cookies":[]}',
    )
    current = ProfileCipher.from_values(f"v2:{_key()}", [old.export_primary()])
    store = ProfileStore(db, current)

    assert await store.load(profile_id, host="v2.docs.example.com") == b'{"cookies":[]}'
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT key_version FROM session_profiles WHERE id = $1", profile_id
        ) == "v2"
    with pytest.raises(PermissionError):
        await store.load(profile_id, host="example.net")
    assert await store.delete(profile_id) is True
    assert await store.delete(profile_id) is False
