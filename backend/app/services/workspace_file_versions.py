"""File-level read model over durable session workspace checkpoints.

The checkpoint snapshot remains the only version authority. This module exposes
opaque, path-scoped version references without copying snapshot content into a
second history store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from app.services.session_workspace_snapshot import (
    _agent_root,
    _is_within,
    _load_manifest,
    _sha256,
    _workspace_relative_path,
)

WorkspaceFileVersionState = Literal["available", "deleted", "unavailable"]


class WorkspaceFileVersionError(RuntimeError):
    """Base error for file-version resolution."""


class WorkspaceFileVersionNotFound(WorkspaceFileVersionError):
    """The opaque version reference is not visible in the authorized scope."""


class WorkspaceFileVersionUnavailable(WorkspaceFileVersionError):
    """The snapshot exists but cannot be verified or safely consumed."""


@dataclass(frozen=True, slots=True)
class WorkspaceFileVersion:
    version_id: str
    created_at: str
    state: WorkspaceFileVersionState
    size: int
    content_hash: str | None
    restorable: bool
    unavailable_reason: str | None = None
    snapshot: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    content_path: Path | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class WorkspaceFileVersionContent:
    version_id: str
    state: WorkspaceFileVersionState
    content: bytes | None
    content_hash: str | None
    size: int


def _created_at(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc).isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _opaque_version_id(*, session_id: Any, snapshot: dict[str, Any], path: str) -> str:
    identity = {
        "session_id": str(session_id),
        "checkpoint_event_id": str(snapshot.get("checkpoint_event_id") or ""),
        "manifest_path": str(snapshot.get("manifest_path") or ""),
        "path": path,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:40]


def _unavailable_version(
    *,
    session_id: Any,
    snapshot: dict[str, Any],
    path: str,
    reason: str,
) -> WorkspaceFileVersion:
    return WorkspaceFileVersion(
        version_id=_opaque_version_id(session_id=session_id, snapshot=snapshot, path=path),
        created_at=_created_at(snapshot.get("created_at")),
        state="unavailable",
        size=0,
        content_hash=None,
        restorable=False,
        unavailable_reason=reason,
        snapshot=dict(snapshot),
    )


def _project_snapshot_version(
    *,
    agent_id: Any,
    session_id: Any,
    snapshot: dict[str, Any],
    path: str,
    data_root: Path | str | None,
) -> WorkspaceFileVersion:
    version_id = _opaque_version_id(session_id=session_id, snapshot=snapshot, path=path)
    try:
        manifest_path, manifest = _load_manifest(agent_id, snapshot, data_root=data_root)
        agent_root = _agent_root(agent_id, data_root=data_root)
        snapshots_root = (agent_root / "runtime_artifacts" / "session_workspace_snapshots" / str(session_id)).resolve()
        if (
            manifest_path.name != "manifest.json"
            or manifest_path.is_symlink()
            or not _is_within(manifest_path, snapshots_root)
            or str(manifest.get("agent_id") or "") != str(agent_id)
            or str(manifest.get("session_id") or "") != str(session_id)
            or str(manifest.get("checkpoint_event_id") or "") != str(snapshot.get("checkpoint_event_id") or "")
        ):
            raise ValueError("snapshot identity mismatch")

        matching: list[dict[str, Any]] = []
        for item in manifest.get("files") or []:
            if not isinstance(item, dict):
                continue
            try:
                item_path = _workspace_relative_path(item.get("path"))
            except ValueError:
                continue
            if item_path == path:
                matching.append(item)
        if len(matching) > 1:
            raise ValueError("duplicate snapshot path")

        complete = bool(manifest.get("complete", False))
        created_at = _created_at(manifest.get("created_at") or snapshot.get("created_at"))
        if not matching:
            skipped_paths = {
                _workspace_relative_path(item.get("path"))
                for item in manifest.get("skipped") or []
                if isinstance(item, dict) and item.get("path")
            }
            if not complete or path in skipped_paths:
                return WorkspaceFileVersion(
                    version_id=version_id,
                    created_at=created_at,
                    state="unavailable",
                    size=0,
                    content_hash=None,
                    restorable=False,
                    unavailable_reason="snapshot_incomplete",
                    snapshot=dict(snapshot),
                )
            return WorkspaceFileVersion(
                version_id=version_id,
                created_at=created_at,
                state="deleted",
                size=0,
                content_hash=None,
                restorable=True,
                snapshot=dict(snapshot),
            )

        item = matching[0]
        expected_hash = str(item.get("sha256") or "")
        expected_size = int(item.get("size") or 0)
        files_root = (manifest_path.parent / "files").resolve()
        content_path = (files_root / path).resolve()
        if (
            len(expected_hash) != 64
            or content_path.is_symlink()
            or not content_path.is_file()
            or not _is_within(content_path, files_root)
            or content_path.stat().st_size != expected_size
            or _sha256(content_path) != expected_hash
        ):
            raise ValueError("snapshot content verification failed")
        return WorkspaceFileVersion(
            version_id=version_id,
            created_at=created_at,
            state="available",
            size=expected_size,
            content_hash=expected_hash,
            restorable=complete,
            unavailable_reason=None if complete else "snapshot_incomplete",
            snapshot=dict(snapshot),
            content_path=content_path,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _unavailable_version(
            session_id=session_id,
            snapshot=snapshot,
            path=path,
            reason=f"snapshot_unavailable:{type(exc).__name__}",
        )


def collect_workspace_file_versions(
    *,
    agent_id: Any,
    path: Any,
    sessions: Iterable[Any],
    data_root: Path | str | None = None,
) -> list[WorkspaceFileVersion]:
    """Project all verified checkpoints for one workspace-relative file."""

    relative_path = _workspace_relative_path(path)
    projected: list[WorkspaceFileVersion] = []
    seen_ids: set[str] = set()
    for session in sessions:
        session_id = getattr(session, "id", None)
        metadata = getattr(session, "transcript_metadata_json", None)
        snapshots = metadata.get("workspace_snapshots") if isinstance(metadata, dict) else None
        if session_id is None or not isinstance(snapshots, dict):
            continue
        for snapshot in snapshots.values():
            if not isinstance(snapshot, dict):
                continue
            version = _project_snapshot_version(
                agent_id=agent_id,
                session_id=session_id,
                snapshot=snapshot,
                path=relative_path,
                data_root=data_root,
            )
            if version.version_id in seen_ids:
                continue
            seen_ids.add(version.version_id)
            projected.append(version)

    projected.sort(key=lambda item: (item.created_at, item.version_id))
    filtered: list[WorkspaceFileVersion] = []
    file_has_existed = False
    for version in projected:
        if version.state == "available":
            file_has_existed = True
        if version.state == "deleted" and not file_has_existed:
            continue
        filtered.append(version)
    filtered.reverse()
    return filtered


def resolve_workspace_file_version(
    *,
    agent_id: Any,
    path: Any,
    version_id: str,
    sessions: Iterable[Any],
    data_root: Path | str | None = None,
) -> WorkspaceFileVersion:
    for version in collect_workspace_file_versions(
        agent_id=agent_id,
        path=path,
        sessions=sessions,
        data_root=data_root,
    ):
        if version.version_id == version_id:
            return version
    raise WorkspaceFileVersionNotFound("workspace file version not found")


def read_workspace_file_version(
    *,
    agent_id: Any,
    path: Any,
    version_id: str,
    sessions: Iterable[Any],
    data_root: Path | str | None = None,
) -> WorkspaceFileVersionContent:
    version = resolve_workspace_file_version(
        agent_id=agent_id,
        path=path,
        version_id=version_id,
        sessions=sessions,
        data_root=data_root,
    )
    if version.state == "unavailable" or not version.restorable and version.content_path is None:
        raise WorkspaceFileVersionUnavailable(version.unavailable_reason or "workspace file version unavailable")
    if version.state == "deleted":
        return WorkspaceFileVersionContent(
            version_id=version.version_id,
            state=version.state,
            content=None,
            content_hash=None,
            size=0,
        )
    if version.content_path is None:
        raise WorkspaceFileVersionUnavailable("workspace file version content unavailable")
    try:
        content = version.content_path.read_bytes()
    except OSError as exc:
        raise WorkspaceFileVersionUnavailable("workspace file version content unavailable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != version.content_hash or len(content) != version.size:
        raise WorkspaceFileVersionUnavailable("workspace file version content changed during read")
    return WorkspaceFileVersionContent(
        version_id=version.version_id,
        state=version.state,
        content=content,
        content_hash=digest,
        size=len(content),
    )
