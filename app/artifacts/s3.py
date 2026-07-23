"""Prefix-scoped S3-compatible artifact storage."""

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from .base import ArtifactIntegrityError, ArtifactRef, ArtifactStorageError, ArtifactTooLarge


logger = logging.getLogger(__name__)

_WORKER_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_SPOOL_LIMIT = 8 * 1024 * 1024
_NOT_FOUND_CODES = frozenset({
    "404",
    "NoSuchKey",
    "NotFound",
    "NoSuchBucket",
    "404 Not Found",
})


def _is_not_found(exc: BaseException) -> bool:
    if not isinstance(exc, ClientError):
        return False
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = str(error.get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or status == 404


def _storage_error(operation: str, exc: BaseException) -> ArtifactStorageError:
    code = None
    status = None
    if isinstance(exc, ClientError):
        response = getattr(exc, "response", None) or {}
        error = response.get("Error") or {}
        code = error.get("Code")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    detail = f"{type(exc).__name__}"
    if code:
        detail = f"{detail} code={code}"
    if status is not None:
        detail = f"{detail} status={status}"
    return ArtifactStorageError(f"S3 {operation} failed: {detail}")


class S3ArtifactStore:
    def __init__(self, client, bucket: str, worker_id: str) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        if not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker ID must contain only letters, digits, _ or -")
        self.client = client
        self.bucket = bucket
        self.worker_id = worker_id
        self.prefix = f"workers/{worker_id}/"

    async def healthcheck(self) -> bool:
        try:
            await asyncio.to_thread(
                self.client.list_objects_v2,
                Bucket=self.bucket,
                Prefix=self.prefix,
                MaxKeys=1,
            )
        except (ClientError, BotoCoreError, OSError):
            return False
        return True

    def key_for_sha256(self, sha256: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("SHA-256 digest must be 64 lowercase hexadecimal characters")
        return f"{self.prefix}sha256/{sha256[:2]}/{sha256}"

    def _key_for_ref(self, ref: ArtifactRef) -> str:
        key = self.key_for_sha256(ref.sha256)
        if ref.uri != f"s3://{self.bucket}/{key}":
            raise ArtifactIntegrityError("artifact URI is outside this worker S3 prefix")
        return key

    @staticmethod
    def _metadata(ref: ArtifactRef) -> dict[str, str]:
        return {
            "sha256": ref.sha256,
            "media_type": ref.media_type,
            "size": str(ref.size),
        }

    async def put(self, chunks, media_type: str, expected_max_bytes: int) -> ArtifactRef:
        if expected_max_bytes < 0:
            raise ArtifactTooLarge("artifact size limit cannot be negative")

        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_LIMIT, mode="w+b")
        temporary_key = f"{self.prefix}tmp/{uuid.uuid4().hex}"
        temporary_may_exist = False
        try:
            digest = hashlib.sha256()
            size = 0
            async for chunk in chunks:
                next_size = size + len(chunk)
                if next_size > expected_max_bytes:
                    raise ArtifactTooLarge(f"artifact exceeds {expected_max_bytes} byte limit")
                digest.update(chunk)
                spool.write(chunk)
                size = next_size

            ref = ArtifactRef(
                uri="",
                size=size,
                sha256=digest.hexdigest(),
                media_type=media_type,
            )
            key = self.key_for_sha256(ref.sha256)
            ref = ArtifactRef(
                uri=f"s3://{self.bucket}/{key}",
                size=ref.size,
                sha256=ref.sha256,
                media_type=ref.media_type,
            )
            spool.seek(0)
            temporary_may_exist = True
            try:
                await asyncio.to_thread(
                    self.client.upload_fileobj,
                    spool,
                    self.bucket,
                    temporary_key,
                    ExtraArgs={
                        "ContentType": media_type,
                        "Metadata": self._metadata(ref),
                        "ServerSideEncryption": os.getenv(
                            "S3_SERVER_SIDE_ENCRYPTION", "AES256"
                        ),
                    },
                )
                await asyncio.to_thread(
                    self.client.copy_object,
                    Bucket=self.bucket,
                    Key=key,
                    CopySource={"Bucket": self.bucket, "Key": temporary_key},
                    MetadataDirective="COPY",
                    ServerSideEncryption=os.getenv("S3_SERVER_SIDE_ENCRYPTION", "AES256"),
                )
                await asyncio.to_thread(
                    self.client.delete_object, Bucket=self.bucket, Key=temporary_key
                )
            except (ClientError, BotoCoreError, OSError) as exc:
                raise _storage_error("put", exc) from exc
            temporary_may_exist = False
            return ref
        finally:
            spool.close()
            if temporary_may_exist:
                try:
                    await asyncio.to_thread(
                        self.client.delete_object, Bucket=self.bucket, Key=temporary_key
                    )
                except Exception:
                    # Never mask the original upload/copy failure with cleanup noise.
                    logger.warning(
                        "failed to delete temporary S3 object bucket=%s key=%s",
                        self.bucket,
                        temporary_key,
                        exc_info=True,
                    )

    async def get(self, ref: ArtifactRef) -> bytes:
        key = self._key_for_ref(ref)
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=key
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ArtifactIntegrityError("artifact object is missing") from exc
            raise _storage_error("get", exc) from exc
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("get", exc) from exc
        body = response["Body"]
        try:
            data = await asyncio.to_thread(body.read)
        finally:
            await asyncio.to_thread(body.close)
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact contents do not match its reference")
        return data

    async def delete(self, ref: ArtifactRef) -> None:
        key = self._key_for_ref(ref)
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError, OSError) as exc:
            raise _storage_error("delete", exc) from exc

    async def exists(self, ref: ArtifactRef) -> bool:
        key = self._key_for_ref(ref)
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise _storage_error("exists", exc) from exc
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("exists", exc) from exc
        return True

    async def verify(self, ref: ArtifactRef) -> bool:
        key = self._key_for_ref(ref)
        try:
            response = await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=key
            )
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise _storage_error("verify", exc) from exc
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("verify", exc) from exc
        metadata = {name.lower(): value for name, value in response.get("Metadata", {}).items()}
        return (
            response.get("ContentLength") == ref.size
            and metadata == self._metadata(ref)
        )
