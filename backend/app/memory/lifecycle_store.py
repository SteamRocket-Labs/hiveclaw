"""In-memory lifecycle state machine for memory control-plane contracts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class LifecycleStatus(StrEnum):
    SKETCH = "sketch"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    DISCARDED = "discarded"


@dataclass(slots=True)
class MemoryLifecycleEntry:
    id: str
    content: str
    status: LifecycleStatus
    version: int = 1
    parent_id: str | None = None
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    expires_at: datetime | None = None
    access_count: int = 0
    last_accessed: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryLifecycleStore:
    def __init__(self) -> None:
        self._entries: dict[str, MemoryLifecycleEntry] = {}

    def create_sketch(self, content: str, *, expires_at: datetime) -> MemoryLifecycleEntry:
        return self._create(content, LifecycleStatus.SKETCH, expires_at=expires_at)

    def create_active(self, content: str) -> MemoryLifecycleEntry:
        return self._create(content, LifecycleStatus.ACTIVE)

    def promote(self, entry_id: str, *, approved_by: str) -> MemoryLifecycleEntry:
        entry = self.get(entry_id)
        entry.status = LifecycleStatus.ACTIVE
        entry.expires_at = None
        entry.metadata["approved_by"] = approved_by
        entry.updated_at = datetime.now(UTC)
        return entry

    def supersede(self, entry_id: str, new_content: str) -> MemoryLifecycleEntry:
        old = self.get(entry_id)
        replacement = self._create(
            new_content,
            LifecycleStatus.ACTIVE,
            version=old.version + 1,
            parent_id=old.id,
            supersedes=[old.id],
        )
        old.status = LifecycleStatus.SUPERSEDED
        old.superseded_by = replacement.id
        old.updated_at = datetime.now(UTC)
        return replacement

    def discard_expired(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        discarded: list[str] = []
        for entry in self._entries.values():
            if entry.status == LifecycleStatus.SKETCH and entry.expires_at and entry.expires_at <= current:
                entry.status = LifecycleStatus.DISCARDED
                entry.updated_at = current
                discarded.append(entry.id)
        return discarded

    def get(self, entry_id: str) -> MemoryLifecycleEntry:
        return self._entries[entry_id]

    def _create(
        self,
        content: str,
        status: LifecycleStatus,
        *,
        version: int = 1,
        parent_id: str | None = None,
        supersedes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryLifecycleEntry:
        entry = MemoryLifecycleEntry(
            id=str(uuid.uuid4()),
            content=content,
            status=status,
            version=version,
            parent_id=parent_id,
            supersedes=supersedes or [],
            expires_at=expires_at,
        )
        self._entries[entry.id] = entry
        return entry

