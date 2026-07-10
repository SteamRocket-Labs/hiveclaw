"""Session workspace snapshot capture/restore for `/rewind mode=workspace`.

Snapshots cover the user work area `AGENT_DATA_DIR/<agent_id>/workspace`.
They deliberately do not include memory, soul, skills, logs, or other governed
agent state; those have their own lifecycle gates.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

MAX_WORKSPACE_SNAPSHOT_FILES = 1000
MAX_WORKSPACE_SNAPSHOT_FILE_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES = 50 * 1024 * 1024


@dataclass(slots=True)
class WorkspaceRestoreResult:
    ok: bool
    checkpoint_event_id: str
    workspace_rel_path: str
    restored_files: list[str]
    deleted_files: list[str]
    unchanged_files: list[str]
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checkpoint_event_id": self.checkpoint_event_id,
            "workspace_rel_path": self.workspace_rel_path,
            "restored_files": self.restored_files,
            "deleted_files": self.deleted_files,
            "unchanged_files": self.unchanged_files,
            "restored_count": len(self.restored_files),
            "deleted_count": len(self.deleted_files),
            "unchanged_count": len(self.unchanged_files),
            "error": self.error,
        }


def _data_root(data_root: Path | str | None = None) -> Path:
    return Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)


def _agent_root(agent_id: Any, *, data_root: Path | str | None = None) -> Path:
    return (_data_root(data_root) / str(agent_id)).resolve()


def _workspace_dir(agent_id: Any, *, data_root: Path | str | None = None) -> Path:
    return (_agent_root(agent_id, data_root=data_root) / "workspace").resolve()


def _safe_checkpoint_slug(checkpoint_event_id: Any) -> str:
    raw = str(checkpoint_event_id or "").strip() or "checkpoint"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw)[:160]


def _snapshot_dir(
    agent_id: Any, session_id: Any, checkpoint_event_id: Any, *, data_root: Path | str | None = None
) -> Path:
    return (
        _agent_root(agent_id, data_root=data_root)
        / "runtime_artifacts"
        / "session_workspace_snapshots"
        / str(session_id)
        / _safe_checkpoint_slug(checkpoint_event_id)
    ).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_files(workspace: Path) -> list[Path]:
    if not workspace.exists():
        return []
    return sorted(path for path in workspace.rglob("*") if path.is_file() and not path.is_symlink())


def capture_workspace_snapshot(
    *,
    agent_id: Any,
    session_id: Any,
    checkpoint_event_id: Any,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    agent_root = _agent_root(agent_id, data_root=data_root)
    workspace = _workspace_dir(agent_id, data_root=data_root)
    workspace.mkdir(parents=True, exist_ok=True)
    destination = _snapshot_dir(agent_id, session_id, checkpoint_event_id, data_root=data_root)
    if not _is_within(destination, agent_root):
        raise ValueError("workspace snapshot destination escapes agent root")
    if destination.exists():
        shutil.rmtree(destination)
    files_dir = destination / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    manifest_files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total_bytes = 0
    for path in _workspace_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        stat = path.stat()
        if len(manifest_files) >= MAX_WORKSPACE_SNAPSHOT_FILES:
            skipped.append({"path": rel, "reason": "file_count_limit"})
            continue
        if stat.st_size > MAX_WORKSPACE_SNAPSHOT_FILE_BYTES:
            skipped.append({"path": rel, "reason": "file_size_limit", "size": stat.st_size})
            continue
        if total_bytes + stat.st_size > MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES:
            skipped.append({"path": rel, "reason": "total_size_limit", "size": stat.st_size})
            continue
        target = (files_dir / rel).resolve()
        if not _is_within(target, files_dir):
            skipped.append({"path": rel, "reason": "unsafe_relative_path"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest_files.append({"path": rel, "size": stat.st_size, "sha256": _sha256(path)})
        total_bytes += stat.st_size

    manifest = {
        "version": 1,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "checkpoint_event_id": str(checkpoint_event_id),
        "workspace_rel_path": "workspace",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "complete": not skipped,
        "file_count": len(manifest_files),
        "total_bytes": total_bytes,
        "files": manifest_files,
        "skipped": skipped,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "checkpoint_event_id": str(checkpoint_event_id),
        "workspace_rel_path": "workspace",
        "manifest_path": manifest_path.relative_to(agent_root).as_posix(),
        "file_count": len(manifest_files),
        "total_bytes": total_bytes,
        "complete": not skipped,
        "skipped_count": len(skipped),
        "created_at": manifest["created_at"],
    }


def _load_manifest(
    agent_id: Any, snapshot: dict[str, Any], *, data_root: Path | str | None = None
) -> tuple[Path, dict[str, Any]]:
    agent_root = _agent_root(agent_id, data_root=data_root)
    rel_manifest = Path(str(snapshot.get("manifest_path") or ""))
    manifest_path = (agent_root / rel_manifest).resolve()
    if not _is_within(manifest_path, agent_root):
        raise ValueError("workspace snapshot manifest escapes agent root")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path, manifest


def restore_workspace_snapshot(
    *,
    agent_id: Any,
    snapshot: dict[str, Any],
    data_root: Path | str | None = None,
) -> WorkspaceRestoreResult:
    checkpoint_event_id = str(snapshot.get("checkpoint_event_id") or "")
    try:
        manifest_path, manifest = _load_manifest(agent_id, snapshot, data_root=data_root)
    except Exception as exc:  # noqa: BLE001 - command path returns structured failure.
        return WorkspaceRestoreResult(
            ok=False,
            checkpoint_event_id=checkpoint_event_id,
            workspace_rel_path="workspace",
            restored_files=[],
            deleted_files=[],
            unchanged_files=[],
            error=f"workspace snapshot manifest unavailable: {exc}",
        )
    if not manifest.get("complete", False):
        return WorkspaceRestoreResult(
            ok=False,
            checkpoint_event_id=checkpoint_event_id,
            workspace_rel_path="workspace",
            restored_files=[],
            deleted_files=[],
            unchanged_files=[],
            error="workspace snapshot is incomplete and cannot be restored safely",
        )

    workspace = _workspace_dir(agent_id, data_root=data_root)
    workspace.mkdir(parents=True, exist_ok=True)
    files_dir = (manifest_path.parent / "files").resolve()
    manifest_files = [item for item in manifest.get("files") or [] if isinstance(item, dict)]
    target_paths: set[str] = set()
    for item in manifest_files:
        rel = str(item.get("path") or "")
        source = (files_dir / rel).resolve()
        if not rel or not _is_within(source, files_dir):
            return WorkspaceRestoreResult(
                ok=False,
                checkpoint_event_id=checkpoint_event_id,
                workspace_rel_path="workspace",
                restored_files=[],
                deleted_files=[],
                unchanged_files=[],
                error=f"workspace snapshot file path is invalid: {rel or '<empty>'}",
            )
        if not source.is_file() or source.is_symlink():
            return WorkspaceRestoreResult(
                ok=False,
                checkpoint_event_id=checkpoint_event_id,
                workspace_rel_path="workspace",
                restored_files=[],
                deleted_files=[],
                unchanged_files=[],
                error=f"workspace snapshot file unavailable: {rel}",
            )
        expected_hash = str(item.get("sha256") or "")
        if _sha256(source) != expected_hash:
            return WorkspaceRestoreResult(
                ok=False,
                checkpoint_event_id=checkpoint_event_id,
                workspace_rel_path="workspace",
                restored_files=[],
                deleted_files=[],
                unchanged_files=[],
                error=f"workspace snapshot file checksum mismatch: {rel}",
            )
        target_paths.add(rel)

    restored: list[str] = []
    unchanged: list[str] = []
    deleted: list[str] = []

    for current in _workspace_files(workspace):
        rel = current.relative_to(workspace).as_posix()
        if rel in target_paths:
            continue
        current.unlink()
        deleted.append(rel)

    for item in manifest_files:
        rel = str(item.get("path") or "")
        source = (files_dir / rel).resolve()
        target = (workspace / rel).resolve()
        if not rel or not _is_within(source, files_dir) or not _is_within(target, workspace):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = str(item.get("sha256") or "")
        current_hash = _sha256(target) if target.exists() else ""
        if target.exists() and current_hash == expected_hash:
            unchanged.append(rel)
            continue
        shutil.copy2(source, target)
        restored.append(rel)

    for directory in sorted(
        (path for path in workspace.rglob("*") if path.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    return WorkspaceRestoreResult(
        ok=True,
        checkpoint_event_id=checkpoint_event_id,
        workspace_rel_path="workspace",
        restored_files=sorted(restored),
        deleted_files=sorted(deleted),
        unchanged_files=sorted(unchanged),
    )


def index_session_workspace_snapshot(session: Any, *, checkpoint_event_id: Any, snapshot: dict[str, Any]) -> None:
    metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    snapshots = dict(metadata.get("workspace_snapshots") or {})
    snapshots[str(checkpoint_event_id)] = snapshot
    metadata["workspace_snapshots"] = snapshots
    session.transcript_metadata_json = metadata


def capture_session_workspace_snapshot(
    *,
    agent_id: Any,
    session: Any,
    checkpoint_event_id: Any,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=getattr(session, "id", ""),
        checkpoint_event_id=checkpoint_event_id,
        data_root=data_root,
    )
    index_session_workspace_snapshot(session, checkpoint_event_id=checkpoint_event_id, snapshot=snapshot)
    return snapshot


def restore_session_workspace_snapshot(
    *,
    agent_id: Any,
    session: Any,
    checkpoint_event_id: Any,
    data_root: Path | str | None = None,
) -> WorkspaceRestoreResult:
    metadata = getattr(session, "transcript_metadata_json", None)
    snapshots = metadata.get("workspace_snapshots") if isinstance(metadata, dict) else None
    snapshot = snapshots.get(str(checkpoint_event_id)) if isinstance(snapshots, dict) else None
    if not isinstance(snapshot, dict):
        return WorkspaceRestoreResult(
            ok=False,
            checkpoint_event_id=str(checkpoint_event_id),
            workspace_rel_path="workspace",
            restored_files=[],
            deleted_files=[],
            unchanged_files=[],
            error="workspace_snapshot_missing",
        )
    return restore_workspace_snapshot(agent_id=agent_id, snapshot=snapshot, data_root=data_root)
