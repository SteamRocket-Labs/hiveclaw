"""In-memory lifecycle state machine for memory control-plane contracts."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

LIFECYCLE_RECOVERY_SCHEMA = "hive.memory.lifecycle-recovery.v1"


class LifecycleStatus(StrEnum):
    SKETCH = "sketch"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    DISCARDED = "discarded"


# BaseLevel ring size (dynamic-memory-activation design §4.3): the K most
# recent access timestamps feed the power-law frequency term.
RECENT_ACCESS_RING_SIZE = 8


class LifecycleSnapshotCorruptionError(ValueError):
    """The lifecycle sidecar is syntactically or structurally incomplete."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably replace one file without exposing a partial snapshot."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _last_good_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.last-good")


def _refresh_last_good(path: Path, content: bytes) -> None:
    backup = _last_good_path(path)
    if backup.exists() and backup.read_bytes() == content:
        return
    _atomic_write_bytes(backup, content)


def _recovery_root(path: Path) -> Path:
    return path.parent / f"{path.stem}-recovery"


@contextlib.contextmanager
def _exclusive_lifecycle_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.with_name(f".{path.name}.lock").open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _serialized_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Reload and mutate one lifecycle snapshot under its cross-process lock."""

    @wraps(method)
    def wrapped(self: MemoryLifecycleStore, *args: Any, **kwargs: Any) -> Any:
        with self._serialized_write():
            return method(self, *args, **kwargs)

    return wrapped


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
    recent_accesses: list[datetime] = field(default_factory=list)
    credit: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryLifecycleStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: dict[str, MemoryLifecycleEntry] = {}
        self._mutation_depth = 0
        self._load()

    @_serialized_mutation
    def create_sketch(
        self,
        content: str,
        *,
        expires_at: datetime,
        entry_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MemoryLifecycleEntry:
        entry = self._create(
            content,
            LifecycleStatus.SKETCH,
            entry_id=entry_id,
            expires_at=expires_at,
            metadata=metadata,
        )
        self._flush()
        return entry

    @_serialized_mutation
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

    @_serialized_mutation
    def promote(self, entry_id: str, *, approved_by: str) -> MemoryLifecycleEntry:
        entry = self.get(entry_id)
        entry.status = LifecycleStatus.ACTIVE
        entry.expires_at = None
        entry.metadata["approved_by"] = approved_by
        entry.updated_at = datetime.now(UTC)
        self._flush()
        return entry

    @_serialized_mutation
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

    @_serialized_mutation
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

    @_serialized_mutation
    def record_conflict(
        self,
        entry_id: str,
        *,
        conflicts_with: list[str] | tuple[str, ...],
        reason: str,
        source_refs: list[str] | tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> MemoryLifecycleEntry:
        """Mark an active memory as conflicted until an owner/company review resolves it."""

        entry = self.get(entry_id)
        when = (now or datetime.now(UTC)).astimezone(UTC)
        entry.metadata.update(
            {
                "conflict_status": "needs_review",
                "conflicts_with": ",".join(str(item).strip() for item in conflicts_with if str(item).strip()),
                "conflict_reason": str(reason or "").strip(),
                "conflict_source_refs": ",".join(
                    str(item).strip() for item in (source_refs or []) if str(item).strip()
                ),
                "conflict_recorded_at": when.isoformat(),
            }
        )
        entry.updated_at = when
        self._flush()
        return entry

    @_serialized_mutation
    def mark_reference_revalidation_required(
        self,
        entry_id: str,
        *,
        reason: str,
        source_refs: list[str] | tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> MemoryLifecycleEntry:
        """Hold a memory out of prompt activation until its evidence refs are revalidated."""

        entry = self.get(entry_id)
        when = (now or datetime.now(UTC)).astimezone(UTC)
        refs = [str(item).strip() for item in (source_refs or []) if str(item).strip()]
        entry.metadata.update(
            {
                "reference_status": "revalidation_required",
                "revalidation_reason": str(reason or "").strip(),
                "revalidation_source_refs": ",".join(refs),
                "revalidation_required_at": when.isoformat(),
            }
        )
        if refs:
            entry.metadata["invalid_evidence_refs"] = ",".join(refs)
        entry.updated_at = when
        self._flush()
        return entry

    @_serialized_mutation
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

    @_serialized_mutation
    def bump_access(self, entry_id: str, *, now: datetime | None = None, create_if_missing: bool = False) -> bool:
        """Increment access telemetry for one entry (D1: telemetry lives here).

        Returns True when the entry exists (or was created) and was bumped;
        False when there is no record for `entry_id`. This is the only writer
        of `access_count` / `last_accessed` / `recent_accesses` after D1 —
        the markdown prose is never restamped. ``create_if_missing`` covers
        entries with no authored lifecycle record (knowledge/milestone pages):
        frequency reinforcement still needs a telemetry row keyed by their id.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            if not create_if_missing:
                return False
            entry = MemoryLifecycleEntry(
                id=entry_id,
                content="",
                status=LifecycleStatus.ACTIVE,
                metadata={"record_kind": "access_telemetry"},
            )
            self._entries[entry.id] = entry
        when = (now or datetime.now(UTC)).astimezone(UTC)
        entry.access_count += 1
        entry.last_accessed = when
        entry.recent_accesses = [*entry.recent_accesses, when][-RECENT_ACCESS_RING_SIZE:]
        entry.updated_at = when
        self._flush()
        return True

    @_serialized_mutation
    def apply_feedback_credit(self, entry_id: str, *, delta: float, now: datetime | None = None) -> bool:
        """Accumulate owner-feedback credit on one entry (M3 FeedbackCredit).

        Mechanical bookkeeping only — sidecar state, never markdown prose, so
        the Memory Gate write surfaces are untouched.
        """
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.credit = round(entry.credit + float(delta), 6)
        entry.updated_at = (now or datetime.now(UTC)).astimezone(UTC)
        self._flush()
        return True

    def telemetry_map(self) -> dict[str, dict[str, object]]:
        """Project `{entry_id: {access_count, last_accessed, recent_accesses, credit}}`."""
        out: dict[str, dict[str, object]] = {}
        for entry_id, entry in self._entries.items():
            out[entry_id] = {
                "access_count": str(entry.access_count),
                "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else "never",
                "recent_accesses": [moment.isoformat() for moment in entry.recent_accesses],
                "credit": str(entry.credit),
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

    @_serialized_mutation
    def upsert_active(self, entry_id: str, *, content: str, metadata: dict[str, str]) -> MemoryLifecycleEntry:
        """Ensure an ACTIVE record for `entry_id` carries the given metadata (D2).

        Backfill migrates inline `.md` metadata into the sidecar without loss —
        critically `sensitivity`, so access control never silently downgrades.
        Creates the record if missing; merges metadata into an existing one.
        """
        clean_metadata, access_count, last_accessed = _split_inline_telemetry(metadata)
        entry = self._entries.get(entry_id)
        if entry is None:
            return self.create_active(content, entry_id=entry_id, metadata=metadata)
        entry.metadata.update({str(key): str(value) for key, value in clean_metadata.items() if value})
        if access_count is not None:
            entry.access_count = access_count
        if last_accessed is not None:
            entry.last_accessed = last_accessed
        if content and not entry.content:
            entry.content = content
        entry.updated_at = datetime.now(UTC)
        self._flush()
        return entry

    def get(self, entry_id: str) -> MemoryLifecycleEntry:
        return self._entries[entry_id]

    def entries(self) -> list[MemoryLifecycleEntry]:
        return list(self._entries.values())

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
        clean_metadata, access_count, last_accessed = _split_inline_telemetry(metadata or {})
        entry = MemoryLifecycleEntry(
            id=entry_id or str(uuid.uuid4()),
            content=content,
            status=status,
            version=version,
            parent_id=parent_id,
            supersedes=supersedes or [],
            expires_at=expires_at,
            metadata=clean_metadata,
        )
        if access_count is not None:
            entry.access_count = access_count
        if last_accessed is not None:
            entry.last_accessed = last_accessed
        self._entries[entry.id] = entry
        return entry

    @contextlib.contextmanager
    def _serialized_write(self) -> Iterator[None]:
        if self._mutation_depth:
            yield
            return
        self._mutation_depth = 1
        try:
            if self._path is None:
                yield
                return
            with _exclusive_lifecycle_lock(self._path):
                self._load_unlocked()
                yield
        finally:
            self._mutation_depth = 0

    def _flush(self) -> None:
        if self._path is None:
            return
        if self._mutation_depth == 0:
            raise RuntimeError("lifecycle writes must run inside the serialized mutation boundary")
        records = [_serialize_entry(entry) for entry in self._entries.values()]
        payload = (json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write_bytes(self._path, payload)
        try:
            _refresh_last_good(self._path, payload)
        except OSError:
            # Canonical commit already succeeded. Keep serving it and emit a
            # visible operational error; the next load refreshes last-good.
            logger.exception("failed to refresh lifecycle last-good snapshot: %s", self._path)

    def _load(self) -> None:
        if self._path is None:
            return
        with _exclusive_lifecycle_lock(self._path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._entries.clear()
        if self._path is None or not self._path.exists():
            return
        raw = self._path.read_bytes()
        try:
            entries = _parse_lifecycle_snapshot(raw)
        except LifecycleSnapshotCorruptionError as exc:
            self._recover_corrupt_snapshot(raw=raw, error=exc)
            return
        self._entries.update(entries)
        try:
            _refresh_last_good(self._path, raw)
        except OSError:
            logger.exception("failed to protect lifecycle last-good snapshot: %s", self._path)

    def _recover_corrupt_snapshot(self, *, raw: bytes, error: LifecycleSnapshotCorruptionError) -> None:
        if self._path is None:  # pragma: no cover - guarded by _load_unlocked
            return
        event_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + f"-{uuid.uuid4().hex}"
        recovery_root = _recovery_root(self._path)
        quarantine_root = recovery_root / "quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        primary_quarantine = quarantine_root / f"{event_id}.primary.corrupt"
        os.replace(self._path, primary_quarantine)
        _fsync_directory(self._path.parent)
        _fsync_directory(quarantine_root)

        backup = _last_good_path(self._path)
        backup_quarantine: Path | None = None
        recovered_from_backup = False
        restore_failure: OSError | None = None
        if backup.exists():
            backup_raw = backup.read_bytes()
            try:
                backup_entries = _parse_lifecycle_snapshot(backup_raw)
            except LifecycleSnapshotCorruptionError:
                backup_quarantine = quarantine_root / f"{event_id}.backup.corrupt"
                os.replace(backup, backup_quarantine)
                _fsync_directory(backup.parent)
                _fsync_directory(quarantine_root)
            else:
                try:
                    _atomic_write_bytes(self._path, backup_raw)
                except OSError as exc:
                    restore_failure = exc
                else:
                    self._entries.update(backup_entries)
                    recovered_from_backup = True

        receipt = {
            "schema_version": LIFECYCLE_RECOVERY_SCHEMA,
            "event_id": event_id,
            "detected_at": datetime.now(UTC).isoformat(),
            "source_file": self._path.name,
            "corrupt_sha256": hashlib.sha256(raw).hexdigest(),
            "corruption": str(error),
            "quarantine_path": primary_quarantine.relative_to(self._path.parent).as_posix(),
            "backup_quarantine_path": (
                backup_quarantine.relative_to(self._path.parent).as_posix() if backup_quarantine else None
            ),
            "recovered_from_backup": recovered_from_backup,
            "restore_error": str(restore_failure) if restore_failure else None,
        }
        receipt_path = recovery_root / f"{event_id}.receipt.json"
        try:
            _atomic_write_bytes(
                receipt_path,
                (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        except OSError:
            logger.exception("failed to persist lifecycle corruption receipt: %s", receipt_path)
        logger.error(
            "quarantined corrupt lifecycle snapshot path=%s quarantine=%s recovered_from_backup=%s",
            self._path,
            primary_quarantine,
            recovered_from_backup,
        )
        if restore_failure is not None:
            raise restore_failure


def lifecycle_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "control" / "lifecycle.json"


def legacy_lifecycle_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return Path(data_root) / str(agent_id) / "memory" / "lifecycle.json"


def _read_lifecycle_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    canonical = lifecycle_path(data_root, agent_id)
    if canonical.exists():
        return canonical
    legacy = legacy_lifecycle_path(data_root, agent_id)
    if legacy.exists():
        return legacy
    return canonical


def lifecycle_read_path(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    return _read_lifecycle_path(data_root, agent_id)


def _migrate_legacy_lifecycle_if_needed(data_root: Path, agent_id: uuid.UUID | str) -> Path:
    canonical = lifecycle_path(data_root, agent_id)
    if canonical.exists():
        return canonical
    legacy = legacy_lifecycle_path(data_root, agent_id)
    if legacy.exists():
        with _exclusive_lifecycle_lock(canonical):
            if not canonical.exists():
                _atomic_write_bytes(canonical, legacy.read_bytes())
    return canonical


def record_active_memory_lifecycle(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    content: str,
    metadata: dict[str, str],
) -> MemoryLifecycleEntry:
    store = MemoryLifecycleStore(_migrate_legacy_lifecycle_if_needed(data_root, agent_id))
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
    create_if_missing: bool = False,
) -> bool:
    """Bump access telemetry for one entry in the agent's lifecycle sidecar."""
    store = MemoryLifecycleStore(_migrate_legacy_lifecycle_if_needed(data_root, agent_id))
    return store.bump_access(entry_id, now=now, create_if_missing=create_if_missing)


def apply_feedback_credit(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    entry_id: str,
    delta: float,
    now: datetime | None = None,
) -> bool:
    """Apply an owner-feedback credit delta to one sidecar entry (M3)."""
    store = MemoryLifecycleStore(_migrate_legacy_lifecycle_if_needed(data_root, agent_id))
    return store.apply_feedback_credit(entry_id, delta=delta, now=now)


def apply_feedback_credit_to_recent(
    data_root: Path,
    agent_id: uuid.UUID | str,
    *,
    delta: float,
    since: datetime,
    now: datetime | None = None,
) -> list[str]:
    """Credit every entry activated (bumped) at or after ``since`` (M3).

    "Recently activated" is approximated by the sidecar's own access telemetry
    — every prompt-included memory bumps ``last_accessed``, so entries touched
    within the session window are exactly the recall set the owner is reacting
    to. The session working set (M4, design §4.2) narrows this to precise W_t
    membership once it lands. Returns credited entry ids, sorted.
    """
    store = MemoryLifecycleStore(_migrate_legacy_lifecycle_if_needed(data_root, agent_id))
    boundary = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
    credited: list[str] = []
    for entry in store.entries():
        if entry.last_accessed is None or entry.last_accessed < boundary:
            continue
        if store.apply_feedback_credit(entry.id, delta=delta, now=now):
            credited.append(entry.id)
    return sorted(credited)


def read_access_telemetry(data_root: Path, agent_id: uuid.UUID | str) -> dict[str, dict[str, object]]:
    """Read `{entry_id: {access_count, last_accessed, recent_accesses, credit}}`.

    Empty dict when the sidecar does not exist yet — read-side callers then fall
    back to each entry's own zero defaults.
    """
    path = _read_lifecycle_path(data_root, agent_id)
    if not path.exists():
        return {}
    return MemoryLifecycleStore(path).telemetry_map()


def read_sidecar_metadata(data_root: Path, agent_id: uuid.UUID | str) -> dict[str, dict[str, str]]:
    """Read `{entry_id: metadata}` from the lifecycle sidecar (D2 read-side join).

    Empty dict when the sidecar does not exist yet — callers then fall back to
    whatever inline metadata the prose still carries.
    """
    path = _read_lifecycle_path(data_root, agent_id)
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
        "recent_accesses": [_dt(moment) for moment in entry.recent_accesses],
        "credit": entry.credit,
        "metadata": dict(entry.metadata),
        "created_at": _dt(entry.created_at),
        "updated_at": _dt(entry.updated_at),
    }


def _deserialize_entry(record: dict[str, Any]) -> MemoryLifecycleEntry:
    recent_accesses = [
        moment
        for moment in (_parse_dt(value) for value in record.get("recent_accesses", []) or [])
        if moment is not None
    ]
    try:
        credit = float(record.get("credit", 0.0) or 0.0)
    except (TypeError, ValueError):
        credit = 0.0
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
        recent_accesses=recent_accesses[-RECENT_ACCESS_RING_SIZE:],
        credit=credit,
        metadata={str(key): str(value) for key, value in (record.get("metadata") or {}).items()},
        created_at=_parse_dt(record.get("created_at")) or datetime.now(UTC),
        updated_at=_parse_dt(record.get("updated_at")) or datetime.now(UTC),
    )


def _parse_lifecycle_snapshot(raw: bytes) -> dict[str, MemoryLifecycleEntry]:
    try:
        records = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleSnapshotCorruptionError("lifecycle snapshot is not valid UTF-8 JSON") from exc
    if not isinstance(records, list):
        raise LifecycleSnapshotCorruptionError("lifecycle snapshot root must be a list")

    entries: dict[str, MemoryLifecycleEntry] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} must be an object")
        raw_id = record.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} has no valid id")
        if "status" not in record:
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} has no status")
        if not isinstance(record.get("metadata", {}), dict):
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} metadata must be an object")
        if not isinstance(record.get("supersedes", []), list):
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} supersedes must be a list")
        if not isinstance(record.get("recent_accesses", []), list):
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} recent_accesses must be a list")
        try:
            entry = _deserialize_entry(record)
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise LifecycleSnapshotCorruptionError(f"lifecycle record {index} is invalid") from exc
        if entry.id in entries:
            raise LifecycleSnapshotCorruptionError(f"duplicate lifecycle record id: {entry.id}")
        entries[entry.id] = entry
    return entries


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


def _split_inline_telemetry(metadata: dict[str, str]) -> tuple[dict[str, str], int | None, datetime | None]:
    """Move D1 legacy telemetry metadata into dedicated sidecar fields."""
    clean = {str(key): str(value) for key, value in metadata.items() if key not in {"access_count", "last_accessed"}}

    access_count: int | None = None
    raw_count = metadata.get("access_count")
    if raw_count not in (None, ""):
        try:
            access_count = max(0, int(str(raw_count).strip()))
        except (TypeError, ValueError):
            access_count = None

    last_accessed: datetime | None = None
    raw_last = str(metadata.get("last_accessed") or "").strip()
    if raw_last and raw_last.lower() != "never":
        try:
            last_accessed = _parse_dt(raw_last)
        except (TypeError, ValueError):
            last_accessed = None

    return clean, access_count, last_accessed
