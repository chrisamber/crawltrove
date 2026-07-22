"""Fail-closed configuration for artifact storage backends."""

import os

from .filesystem import FilesystemArtifactStore
from .s3 import S3ArtifactStore


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for S3-compatible storage")
    return value


def _enabled(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes", "on"}


def artifact_store(worker_id: str | None = None):
    role = os.getenv("CRAWLTROVE_ROLE", "core")
    backend = os.getenv("ARTIFACT_STORE_BACKEND", "filesystem").lower()
    remote_workers = _enabled(os.getenv("CRAWLTROVE_REMOTE_WORKERS"))

    if backend == "filesystem":
        if role == "worker" or (role == "core" and remote_workers):
            raise RuntimeError("remote workers require S3-compatible storage")
        if role != "core":
            raise RuntimeError(f"unsupported artifact-store role: {role}")
        return FilesystemArtifactStore(os.getenv("ARTIFACT_ROOT", "data/artifacts"))

    if backend != "s3":
        raise RuntimeError(f"unsupported ARTIFACT_STORE_BACKEND: {backend}")

    bucket = _required("S3_BUCKET")
    endpoint_url = _required("S3_ENDPOINT_URL")
    region_name = _required("S3_REGION")
    access_key = _required("S3_ACCESS_KEY_ID")
    secret_key = _required("S3_SECRET_ACCESS_KEY")
    scope_worker_id = worker_id or os.getenv("CRAWLTROVE_WORKER_ID")
    if not scope_worker_id:
        raise RuntimeError("worker ID is required for S3-compatible storage")

    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    return S3ArtifactStore(client, bucket, scope_worker_id)
