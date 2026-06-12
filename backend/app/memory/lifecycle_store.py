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
        version: int = 1,
        parent_id: str | None = None,
        supersedes: list[str] | None = None,
        superseded_by: str | None = None,
    ) -> MemoryLifecycleEntry:
        entry = self._create(
            content,
            LifecycleStatus.ACTIVE,
            entry_id=entry_id,
            version=version,
            parent_id=parent_id,
            supersedes=supersedes,
            metadata=metadata,
        )
        if superseded_by:
            entry.superseded_by = superseded_by
            entry.updated_at = datetime.now(UTC)
            self._flush()
        return entry

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

    def bump_access(self, entry_id: str, *, now: datetime | None = None) -> bool:
        """Increment access telemetry for one entry (D1: telemetry lives here).

        Returns True when the entry exists and was bumped; False when there is
        no record for `entry_id`. This is the only writer of `access_count` /
        `last_accessed` after D1 — the markdown prose is never restamped.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        when = (now or datetime.now(UTC)).astimezone(UTC)
        entry.access_count += 1
        entry.last_accessed = when
        entry.updated_at = when
        self._flush()
        return True

    def telemetry_map(self) -> dict[str, dict[str, str]]:
        """Project `{entry_id: {access_count, last_accessed}}` for read-side join."""
        out: dict[str, dict[str, str]] = {}
        for entry_id, entry in self._entries.items():
            out[entry_id] = {
                "access_count": str(entry.access_count),
                "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else "never",
            }
        return out

    def metadata_map(self) -> dict[str, dict[str, str]]:
        """Project `{entry_id: metadata}` for D2 read-side join.

        Excludes telemetry keys (`access_count`/`last_accessed`) — those are
        owned by the dedicated int fields via :meth:`telemetry_map`, so the join
        never resurrects a stale `access_count=0` from the metadata dict.
        """
        out: dict[str, dict[str, str]] = {}
        for entry_id, entry in self._entries.items():
            out[entry_id] = {
                key: value for key, value in entry.metadata.items() if key not in ("access_count", "last_accessed")
            }
        return out

    def upsert_active(self, entry_id: str, *, content: str, metadata: dict[str, str]) -> MemoryLifecycleEntry:
        """Ensure an ACTIVE record for `entry_id` carries the given metadata (D2).

        Backfill migrates inline `.md` metadata into the sidecar without loss —
        critically `sensitivity`, so access control never silently downgrades.
        Creates the record if missing; merges metadata into an existing one.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            return self.create_active(content, entry_id=entry_id, metadata=metadata)
        entry.metadata.update({str(key): str(value) for key, value in metadata.items() if value})
        if content and not entry.content:
            entry.content = content
        entry.updated_at = datetime.now(UTC)
        self._flush()
        return entry

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
    try:
        version = int(str(metadata.get("version") or "1").strip())
    except (TypeError, ValueError):
        version = 1
    supersedes = [item.strip() for item in str(metadata.get("supersedes") or "").split(",") if item.strip()]
    return store.create_active(
        content,
        entry_id=metadata.get("entry_id") or None,
        metadata={str(key): str(value) for key, value in metadata.items()},
        version=version,
        parent_id=metadata.get("parent_id") or None,
        supersedes=supersedes,
        superseded_by=metadata.get("superseded_by") or None,
    )


def bump_access_telemetry(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    entry_id: str,
    now: datetime | None = None,
) -> bool:
    """Bump access telemetry for one entry in the agent's lifecycle sidecar."""
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    return store.bump_access(entry_id, now=now)


def read_access_telemetry(data_root: Path, agent_id: uuid.UUID | str) -> dict[str, dict[str, str]]:
    """Read `{entry_id: {access_count, last_accessed}}` from the lifecycle sidecar.

    Empty dict when the sidecar does not exist yet — read-side callers then fall
    back to each entry's own zero defaults.
    """
    path = lifecycle_path(data_root, agent_id)
    if not path.exists():
        return {}
    return MemoryLifecycleStore(path).telemetry_map()


def read_sidecar_metadata(data_root: Path, agent_id: uuid.UUID | str) -> dict[str, dict[str, str]]:
    """Read `{entry_id: metadata}` from the lifecycle sidecar (D2 read-side join).

    Empty dict when the sidecar does not exist yet — callers then fall back to
    whatever inline metadata the prose still carries.
    """
    path = lifecycle_path(data_root, agent_id)
    if not path.exists():
        return {}
    return MemoryLifecycleStore(path).metadata_map()


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
