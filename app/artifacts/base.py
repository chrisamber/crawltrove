"""Interfaces shared by immutable artifact stores."""

from dataclasses import dataclass
from typing import AsyncIterable, Protocol


class ArtifactTooLarge(ValueError):
    pass


class ArtifactIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    size: int
    sha256: str
    media_type: str


class ArtifactStore(Protocol):
    async def healthcheck(self) -> bool:
        raise NotImplementedError

    async def put(
        self,
        chunks: AsyncIterable[bytes],
        media_type: str,
        expected_max_bytes: int,
    ) -> ArtifactRef:
        raise NotImplementedError

    async def get(self, ref: ArtifactRef) -> bytes:
        raise NotImplementedError

    async def delete(self, ref: ArtifactRef) -> None:
        raise NotImplementedError

    async def exists(self, ref: ArtifactRef) -> bool:
        raise NotImplementedError

    async def verify(self, ref: ArtifactRef) -> bool:
        raise NotImplementedError
