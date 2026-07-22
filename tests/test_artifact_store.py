import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from app.artifacts import (
    ArtifactIntegrityError,
    ArtifactTooLarge,
    FilesystemArtifactStore,
    S3ArtifactStore,
    artifact_store,
)


async def chunks(*values: bytes):
    for value in values:
        yield value


async def test_filesystem_store_is_content_addressed_and_atomic(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    ref = await store.put(chunks(b"hello", b" world"), "text/markdown", 20)
    digest = hashlib.sha256(b"hello world").hexdigest()
    assert ref.sha256 == digest
    assert ref.uri == f"file://{tmp_path}/sha256/{digest[:2]}/{digest}"
    assert await store.get(ref) == b"hello world"
    assert await store.verify(ref) is True
    assert await store.healthcheck() is True


async def test_store_stops_before_limit_overshoot(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(ArtifactTooLarge):
        await store.put(chunks(b"1234", b"56"), "text/plain", 5)
    assert list(tmp_path.rglob("*.tmp")) == []


async def test_filesystem_rejects_reference_outside_configured_root(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    ref = await store.put(chunks(b"hello"), "text/plain", 5)
    foreign = ref.__class__(
        uri=f"file://{tmp_path.parent}/outside", size=ref.size,
        sha256=ref.sha256, media_type=ref.media_type,
    )
    with pytest.raises(ArtifactIntegrityError):
        await store.get(foreign)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, source, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = {
            "body": source.read(),
            "metadata": ExtraArgs["Metadata"],
            "encryption": ExtraArgs["ServerSideEncryption"],
        }

    def copy_object(self, *, Bucket, Key, CopySource, **_kwargs):
        self.objects[(Bucket, Key)] = self.objects[(
            CopySource["Bucket"], CopySource["Key"]
        )].copy()

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(item["body"])}

    def head_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"ContentLength": len(item["body"]), "Metadata": item["metadata"]}

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys):
        assert Bucket == "crawl"
        assert Prefix == "workers/worker-a/"
        assert MaxKeys == 1
        return {"Contents": []}


@pytest.fixture
def fake_s3():
    return FakeS3()


def test_s3_key_is_pinned_to_worker_prefix(fake_s3):
    store = S3ArtifactStore(fake_s3, "crawl", "worker-a")
    key = store.key_for_sha256("a" * 64)
    assert key == "workers/worker-a/sha256/aa/" + "a" * 64
    with pytest.raises(ValueError):
        S3ArtifactStore(fake_s3, "crawl", "../worker-b")


async def test_s3_store_publishes_and_verifies(fake_s3):
    store = S3ArtifactStore(fake_s3, "crawl", "worker-a")
    ref = await store.put(chunks(b"hello", b" world"), "text/plain", 20)
    assert await store.get(ref) == b"hello world"
    assert await store.verify(ref) is True
    assert await store.healthcheck() is True
    assert all(item["encryption"] == "AES256" for item in fake_s3.objects.values())
    assert not any("/tmp/" in key for _bucket, key in fake_s3.objects)


async def test_s3_store_rejects_foreign_reference(fake_s3):
    store = S3ArtifactStore(fake_s3, "crawl", "worker-a")
    ref = await store.put(chunks(b"hello"), "text/plain", 5)
    foreign = ref.__class__(
        uri=ref.uri.replace("worker-a", "worker-b"), size=ref.size,
        sha256=ref.sha256, media_type=ref.media_type,
    )
    with pytest.raises(ArtifactIntegrityError):
        await store.get(foreign)


def test_remote_mode_refuses_filesystem(monkeypatch):
    monkeypatch.setenv("CRAWLTROVE_ROLE", "worker")
    monkeypatch.setenv("ARTIFACT_STORE_BACKEND", "filesystem")
    with pytest.raises(RuntimeError, match="S3-compatible storage"):
        artifact_store(worker_id="worker-a")


def test_s3_factory_requires_complete_configuration(monkeypatch, fake_s3):
    monkeypatch.setenv("CRAWLTROVE_ROLE", "worker")
    monkeypatch.setenv("ARTIFACT_STORE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "crawl")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "worker-a")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda *args, **kwargs: calls.append((args, kwargs)) or fake_s3),
    )

    store = artifact_store(worker_id="worker-a")

    assert isinstance(store, S3ArtifactStore)
    assert calls[0][0] == ("s3",)
    assert calls[0][1]["endpoint_url"] == "http://minio:9000"


def test_s3_factory_does_not_silently_downgrade(monkeypatch):
    monkeypatch.setenv("CRAWLTROVE_ROLE", "worker")
    monkeypatch.setenv("ARTIFACT_STORE_BACKEND", "s3")
    for name in (
        "S3_BUCKET",
        "S3_ENDPOINT_URL",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        artifact_store(worker_id="worker-a")
