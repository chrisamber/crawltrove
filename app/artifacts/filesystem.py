"""Local, content-addressed artifact storage for single-host deployments."""

import hashlib
import os
import uuid
from pathlib import Path

from .base import ArtifactIntegrityError, ArtifactRef, ArtifactTooLarge


class FilesystemArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    async def healthcheck(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)

    def _path_for_ref(self, ref: ArtifactRef) -> Path:
        if not ref.uri.startswith("file://"):
            raise ArtifactIntegrityError("artifact URI is not a local file URI")
        path = Path(ref.uri[len("file://") :]).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError("artifact URI is outside the configured root") from exc
        expected = self.root / "sha256" / ref.sha256[:2] / ref.sha256
        if path != expected:
            raise ArtifactIntegrityError("artifact URI does not match its digest")
        return path

    async def put(
        self,
        chunks,
        media_type: str,
        expected_max_bytes: int,
    ) -> ArtifactRef:
        if expected_max_bytes < 0:
            raise ArtifactTooLarge("artifact size limit cannot be negative")

        tmp_dir = self.root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = tmp_dir / f"{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as output:
                async for chunk in chunks:
                    next_size = size + len(chunk)
                    if next_size > expected_max_bytes:
                        raise ArtifactTooLarge(
                            f"artifact exceeds {expected_max_bytes} byte limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                    size = next_size
                output.flush()
                os.fsync(output.fileno())

            sha256 = digest.hexdigest()
            destination = self.root / "sha256" / sha256[:2] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temp_path.unlink()
            else:
                os.replace(temp_path, destination)
            return ArtifactRef(
                uri=f"file://{destination}",
                size=size,
                sha256=sha256,
                media_type=media_type,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

    async def get(self, ref: ArtifactRef) -> bytes:
        path = self._path_for_ref(ref)
        data = path.read_bytes()
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact contents do not match its reference")
        return data

    async def delete(self, ref: ArtifactRef) -> None:
        self._path_for_ref(ref).unlink(missing_ok=True)

    async def exists(self, ref: ArtifactRef) -> bool:
        return self._path_for_ref(ref).is_file()

    async def verify(self, ref: ArtifactRef) -> bool:
        try:
            await self.get(ref)
        except FileNotFoundError:
            return False
        except ArtifactIntegrityError:
            return False
        return True
