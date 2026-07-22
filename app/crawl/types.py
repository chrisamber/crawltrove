from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID


@dataclass(frozen=True)
class ClaimedTask:
    id: UUID
    job_id: UUID
    url: str
    normalized_url: str
    origin_key: str
    depth: int
    attempt: int
    lease_token: UUID
    deadline_at: datetime
    config: Mapping[str, Any]
    byte_allowance: int
    artifact_allowance: int
    required_capabilities: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class TaskResult:
    final_url: str
    status_code: Optional[int]
    title: str
    markdown: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    discovered_urls: tuple[str, ...] = ()
