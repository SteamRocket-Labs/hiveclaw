"""In-memory lifecycle state machine for memory control-plane contracts."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


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
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: dict[str, MemoryLifecycleEntry] = {}
        self._load()

    def create_sketch(
        self,
        content: str,
        *,
        expires_at: datetime,
        entry_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryLifecycleEntry:
        return self._create(
            content,
            LifecycleStatus.SKETCH,
            entry_id=entry_id,
            expires_at=expires_at,
            metadata=metadata,
        )

    def create_active(
        self,
        content: str,
        *,
        entry_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryLifecycleEntry:
        return self._create(content, LifecycleStatus.ACTIVE, entry_id=entry_id, metadata=metadata)

    def promote(self, entry_id: str, *, approved_by: str) -> MemoryLifecycleEntry:
        entry = self.get(entry_id)
        entry.status = LifecycleStatus.ACTIVE
        entry.expires_at = None
        entry.metadata["approved_by"] = approved_by
        entry.updated_at = datetime.now(UTC)
        self._flush()
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
        self._flush()
        return replacement

    def mark_retired(
        self,
        entry_id: str,
        *,
        status: LifecycleStatus,
        content: str = "",
        superseded_by: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryLifecycleEntry:
        """Record a retirement edge (SUPERSEDED / ARCHIVED) for an entry.

        Upserts: legacy MD lines that never got a lifecycle record on write
        still get an auditable terminal record on retirement. Existing
        entries keep their history and gain the new status + edge metadata.
        """
        if status not in (LifecycleStatus.SUPERSEDED, LifecycleStatus.ARCHIVED):
            raise ValueError(f"mark_retired only accepts superseded/archived, got {status}")
        entry = self._entries.get(entry_id)
        if entry is None:
            entry = MemoryLifecycleEntry(id=entry_id, content=content, status=status)
            self._entries[entry.id] = entry
        else:
            entry.status = status
        if superseded_by:
            entry.superseded_by = superseded_by
        if metadata:
            entry.metadata.update({str(key): str(value) for key, value in metadata.items()})
        entry.updated_at = datetime.now(UTC)
        self._flush()
        return entry

    def discard_expired(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        discarded: list[str] = []
        for entry in self._entries.values():
            if entry.status == LifecycleStatus.SKETCH and entry.expires_at and entry.expires_at <= current:
                entry.status = LifecycleStatus.DISCARDED
                entry.updated_at = current
                discarded.append(entry.id)
        if discarded:
            self._flush()
        return discarded

    def get(self, entry_id: str) -> MemoryLifecycleEntry:
        return self._entries[entry_id]

    def _create(
        self,
        content: str,
        status: LifecycleStatus,
        *,
        entry_id: str | None = None,
        version: int = 1,
        parent_id: str | None = None,
        supersedes: list[str] | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryLifecycleEntry:
        entry = MemoryLifecycleEntry(
            id=entry_id or str(uuid.uuid4()),
            content=content,
            status=status,
            version=version,
            parent_id=parent_id,
            supersedes=supersedes or [],
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )
        self._entries[entry.id] = entry
        self._flush()
        return entry

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        records = [_serialize_entry(entry) for entry in self._entries.values()]
        self._path.write_text(json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            records = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(records, list):
            return
        for record in records:
            try:
                entry = _deserialize_entry(record)
            except (KeyError, TypeError, ValueError):
                continue
            self._entries[entry.id] = entry


def lifecycle_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "lifecycle.json"


def record_active_memory_lifecycle(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    content: str,
    metadata: dict[str, str],
) -> MemoryLifecycleEntry:
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    return store.create_active(
        content,
        entry_id=metadata.get("entry_id") or None,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


def _serialize_entry(entry: MemoryLifecycleEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "content": entry.content,
        "status": entry.status.value,
        "version": entry.version,
        "parent_id": entry.parent_id,
        "supersedes": list(entry.supersedes),
        "superseded_by": entry.superseded_by,
        "expires_at": _dt(entry.expires_at),
        "access_count": entry.access_count,
        "last_accessed": _dt(entry.last_accessed),
        "metadata": dict(entry.metadata),
        "created_at": _dt(entry.created_at),
        "updated_at": _dt(entry.updated_at),
    }


def _deserialize_entry(record: dict[str, Any]) -> MemoryLifecycleEntry:
    return MemoryLifecycleEntry(
        id=str(record["id"]),
        content=str(record.get("content", "")),
        status=LifecycleStatus(str(record["status"])),
        version=int(record.get("version", 1)),
        parent_id=record.get("parent_id"),
        supersedes=[str(value) for value in record.get("supersedes", []) or []],
        superseded_by=record.get("superseded_by"),
        expires_at=_parse_dt(record.get("expires_at")),
        access_count=int(record.get("access_count", 0)),
        last_accessed=_parse_dt(record.get("last_accessed")),
        metadata={str(key): str(value) for key, value in (record.get("metadata") or {}).items()},
        created_at=_parse_dt(record.get("created_at")) or datetime.now(UTC),
        updated_at=_parse_dt(record.get("updated_at")) or datetime.now(UTC),
    )


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
