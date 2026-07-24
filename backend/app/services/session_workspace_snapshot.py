"""Session workspace snapshot capture/restore for `/rewind mode=workspace`.

Snapshots cover the user work area `AGENT_DATA_DIR/<agent_id>/workspace`.
They deliberately do not include memory, soul, skills, logs, or other governed
agent state; those have their own lifecycle gates.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

MAX_WORKSPACE_SNAPSHOT_FILES = 1000
MAX_WORKSPACE_SNAPSHOT_FILE_BYTES = 5 * 1024 * 1024
MAX_WORKSPACE_SNAPSHOT_TOTAL_BYTES = 50 * 1024 * 1024
MAX_SESSION_WORKSPACE_SNAPSHOTS = 50


@dataclass(slots=True)
class WorkspaceRestoreResult:
    ok: bool
    checkpoint_event_id: str
    workspace_rel_path: str
    restored_files: list[str]
    deleted_files: list[str]
    unchanged_files: list[str]
    error: str | None = None
    transaction_id: str | None = None
    requires_finalize: bool = False

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
            "transaction_id": self.transaction_id,
            "requires_finalize": self.requires_finalize,
        }


@dataclass(slots=True)
class _WorkspaceLockLease:
    handle: Any

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


_ACTIVE_RESTORE_LEASES: dict[str, _WorkspaceLockLease] = {}


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


def _workspace_transactions_dir(agent_id: Any, *, data_root: Path | str | None = None) -> Path:
    return (_agent_root(agent_id, data_root=data_root) / "runtime_artifacts" / "workspace_transactions").resolve()


def _workspace_transaction_dir(
    agent_id: Any,
    transaction_id: str,
    *,
    data_root: Path | str | None = None,
) -> Path:
    return (_workspace_transactions_dir(agent_id, data_root=data_root) / transaction_id).resolve()


def _acquire_workspace_lock(
    agent_id: Any,
    *,
    data_root: Path | str | None = None,
) -> _WorkspaceLockLease:
    lock_root = _workspace_transactions_dir(agent_id, data_root=data_root)
    lock_root.mkdir(parents=True, exist_ok=True)
    handle = (lock_root / ".workspace.lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return _WorkspaceLockLease(handle=handle)


def _try_acquire_workspace_lock(
    agent_id: Any,
    *,
    data_root: Path | str | None = None,
) -> _WorkspaceLockLease | None:
    lock_root = _workspace_transactions_dir(agent_id, data_root=data_root)
    lock_root.mkdir(parents=True, exist_ok=True)
    handle = (lock_root / ".workspace.lock").open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return _WorkspaceLockLease(handle=handle)


@contextmanager
def agent_workspace_lock(agent_id: Any, *, data_root: Path | str | None = None):
    """Cross-process Agent workspace mutation lock."""

    lease = _acquire_workspace_lock(agent_id, data_root=data_root)
    try:
        yield
    finally:
        lease.release()


@asynccontextmanager
async def async_agent_workspace_lock(agent_id: Any, *, data_root: Path | str | None = None):
    """Cancellation-safe async acquisition of the cross-process workspace lock."""

    lease: _WorkspaceLockLease | None = None
    while lease is None:
        lease = _try_acquire_workspace_lock(agent_id, data_root=data_root)
        if lease is None:
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lease.release()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _workspace_relative_path(path: Any) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if raw.startswith("workspace/"):
        raw = raw[len("workspace/") :]
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"unsafe workspace path: {raw or '<empty>'}")
    return candidate.as_posix()


def workspace_path_state(*, workspace: Path | str, path: Any) -> dict[str, Any]:
    """Hash one workspace-relative path against an explicit workspace root."""
    workspace = Path(workspace).resolve()
    rel = _workspace_relative_path(path)
    target = (workspace / rel).resolve()
    if not _is_within(target, workspace):
        raise ValueError(f"workspace path escapes root: {rel}")
    if not target.is_file() or target.is_symlink():
        return {"path": f"workspace/{rel}", "exists": False, "sha256": None, "size": 0}
    stat = target.stat()
    if stat.st_size > MAX_WORKSPACE_SNAPSHOT_FILE_BYTES:
        raise ValueError(
            f"workspace file exceeds rewind evidence limit ({MAX_WORKSPACE_SNAPSHOT_FILE_BYTES} bytes): {rel}"
        )
    return {
        "path": f"workspace/{rel}",
        "exists": True,
        "sha256": _sha256(target),
        "size": stat.st_size,
    }


def workspace_file_state(
    *,
    agent_id: Any,
    path: Any,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    return workspace_path_state(
        workspace=_workspace_dir(agent_id, data_root=data_root),
        path=path,
    )


def workspace_file_states(
    *,
    agent_id: Any,
    paths: list[str],
    data_root: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for path in paths:
        state = workspace_file_state(agent_id=agent_id, path=path, data_root=data_root)
        states[state["path"]] = state
    return states


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
    with agent_workspace_lock(agent_id, data_root=data_root):
        agent_root = _agent_root(agent_id, data_root=data_root)
        workspace = _workspace_dir(agent_id, data_root=data_root)
        workspace.mkdir(parents=True, exist_ok=True)
        destination = _snapshot_dir(agent_id, session_id, checkpoint_event_id, data_root=data_root)
        if not _is_within(destination, agent_root):
            raise ValueError("workspace snapshot destination escapes agent root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.parent / f".{destination.name}.capture.{uuid.uuid4().hex}"
        backup = destination.parent / f".{destination.name}.previous.{uuid.uuid4().hex}"
        files_dir = stage / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        manifest_files: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total_bytes = 0
        try:
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
                "version": 2,
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
            _atomic_write_json(stage / "manifest.json", manifest)
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except Exception:
                if backup.exists():
                    os.replace(backup, destination)
                raise
            _remove_path(backup)
            _fsync_directory(destination.parent)
        finally:
            _remove_path(stage)
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            _remove_path(backup)

        manifest_path = destination / "manifest.json"
        return {
            "checkpoint_event_id": str(checkpoint_event_id),
            "workspace_rel_path": "workspace",
            "manifest_path": manifest_path.relative_to(agent_root).as_posix(),
            "file_count": len(manifest_files),
            "total_bytes": total_bytes,
            "complete": not skipped,
            "skipped_count": len(skipped),
            "created_at": manifest["created_at"],
            "manifest_version": manifest["version"],
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


def clone_workspace_snapshot_for_session(
    *,
    agent_id: Any,
    source_snapshot: dict[str, Any],
    target_session_id: Any,
    target_checkpoint_event_id: Any,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    """Atomically clone an immutable checkpoint so branch retention is independent."""
    with agent_workspace_lock(agent_id, data_root=data_root):
        source_manifest_path, source_manifest = _load_manifest(
            agent_id,
            source_snapshot,
            data_root=data_root,
        )
        if not source_manifest.get("complete", False):
            raise ValueError("incomplete workspace snapshot cannot be cloned")
        agent_root = _agent_root(agent_id, data_root=data_root)
        destination = _snapshot_dir(
            agent_id,
            target_session_id,
            target_checkpoint_event_id,
            data_root=data_root,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.parent / f".{destination.name}.clone.{uuid.uuid4().hex}"
        backup = destination.parent / f".{destination.name}.previous.{uuid.uuid4().hex}"
        try:
            shutil.copytree(source_manifest_path.parent, stage, symlinks=False)
            files_dir = (stage / "files").resolve()
            for item in source_manifest.get("files") or []:
                if not isinstance(item, dict):
                    raise ValueError("workspace snapshot manifest has an invalid file record")
                rel = _workspace_relative_path(item.get("path"))
                cloned_file = (files_dir / rel).resolve()
                if (
                    not _is_within(cloned_file, files_dir)
                    or not cloned_file.is_file()
                    or cloned_file.is_symlink()
                    or _sha256(cloned_file) != str(item.get("sha256") or "")
                ):
                    raise ValueError(f"workspace snapshot clone verification failed: {rel}")
            manifest = copy.deepcopy(source_manifest)
            manifest["session_id"] = str(target_session_id)
            manifest["checkpoint_event_id"] = str(target_checkpoint_event_id)
            manifest["created_at"] = datetime.now(timezone.utc).isoformat()
            manifest["cloned_from"] = {
                "session_id": str(source_manifest.get("session_id") or ""),
                "checkpoint_event_id": str(source_manifest.get("checkpoint_event_id") or ""),
                "manifest_path": source_manifest_path.relative_to(agent_root).as_posix(),
            }
            _atomic_write_json(stage / "manifest.json", manifest)
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except BaseException:
                if backup.exists():
                    os.replace(backup, destination)
                raise
            _remove_path(backup)
            _fsync_directory(destination.parent)
        finally:
            _remove_path(stage)
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            _remove_path(backup)

        manifest_path = destination / "manifest.json"
        return {
            "checkpoint_event_id": str(target_checkpoint_event_id),
            "workspace_rel_path": "workspace",
            "manifest_path": manifest_path.relative_to(agent_root).as_posix(),
            "file_count": int(manifest.get("file_count") or 0),
            "total_bytes": int(manifest.get("total_bytes") or 0),
            "complete": True,
            "skipped_count": len(manifest.get("skipped") or []),
            "created_at": manifest["created_at"],
            "manifest_version": int(manifest.get("version") or 2),
            "source_checkpoint_event_id": str(source_manifest.get("checkpoint_event_id") or ""),
        }


def delete_workspace_snapshot(
    *,
    agent_id: Any,
    snapshot: dict[str, Any],
    data_root: Path | str | None = None,
) -> bool:
    """Delete one platform snapshot path after strict Agent-root validation."""
    manifest_rel = snapshot.get("manifest_path") if isinstance(snapshot, dict) else None
    if not manifest_rel:
        return False
    agent_root = _agent_root(agent_id, data_root=data_root)
    snapshots_root = (agent_root / "runtime_artifacts" / "session_workspace_snapshots").resolve()
    manifest_path = (agent_root / str(manifest_rel)).resolve()
    if (
        manifest_path.name != "manifest.json"
        or not _is_within(manifest_path, snapshots_root)
        or manifest_path.parent == snapshots_root
    ):
        return False
    with agent_workspace_lock(agent_id, data_root=data_root):
        existed = manifest_path.parent.exists()
        _remove_path(manifest_path.parent)
        _fsync_directory(manifest_path.parent.parent)
        return existed


def _restore_failure(
    checkpoint_event_id: str,
    error: str,
    *,
    transaction_id: str | None = None,
) -> WorkspaceRestoreResult:
    return WorkspaceRestoreResult(
        ok=False,
        checkpoint_event_id=checkpoint_event_id,
        workspace_rel_path="workspace",
        restored_files=[],
        deleted_files=[],
        unchanged_files=[],
        error=error,
        transaction_id=transaction_id,
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _load_workspace_transaction_journal(transaction_dir: Path) -> dict[str, Any]:
    return json.loads((transaction_dir / "journal.json").read_text(encoding="utf-8"))


def _workspace_matches_expected_states(
    *,
    agent_id: Any,
    expected_states: Any,
    data_root: Path | str | None,
) -> bool:
    if not isinstance(expected_states, dict) or not expected_states:
        return False
    for raw_path, raw_expected in expected_states.items():
        if not isinstance(raw_expected, dict) or "exists" not in raw_expected:
            return False
        try:
            actual = workspace_file_state(
                agent_id=agent_id,
                path=raw_path,
                data_root=data_root,
            )
        except Exception:
            return False
        if bool(actual.get("exists")) != bool(raw_expected.get("exists")):
            return False
        if actual.get("exists") and actual.get("sha256") != raw_expected.get("sha256"):
            return False
    return True


def _state_signature(state: Any) -> tuple[bool, str | None] | None:
    if not isinstance(state, dict) or not isinstance(state.get("exists"), bool):
        return None
    if state["exists"] is False:
        return False, None
    sha256 = state.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64:
        return None
    return True, sha256


def _validate_workspace_restore_lineage(
    *,
    scoped_paths: set[str],
    manifest_by_path: dict[str, dict[str, Any]],
    expected_current_states: dict[str, dict[str, Any]],
    expected_lineage: dict[str, list[dict[str, Any]]] | None,
) -> str | None:
    lineage_by_path = expected_lineage if isinstance(expected_lineage, dict) else {}
    for rel in sorted(scoped_paths):
        canonical_path = f"workspace/{rel}"
        records = lineage_by_path.get(canonical_path)
        if not isinstance(records, list) or not records:
            return f"workspace path has no mutation lineage: {canonical_path}"
        snapshot_item = manifest_by_path.get(rel)
        cursor: tuple[bool, str | None] = (
            (True, str(snapshot_item.get("sha256"))) if snapshot_item is not None else (False, None)
        )
        for index, record in enumerate(records):
            if not isinstance(record, dict) or record.get("path") != canonical_path:
                return f"workspace path lineage is invalid: {canonical_path} mutation {index}"
            before = _state_signature(record.get("before_state"))
            after = _state_signature(record.get("after_state"))
            if before is None or after is None:
                return f"workspace path lineage is unverifiable: {canonical_path} mutation {index}"
            if before != cursor:
                return f"workspace path lineage diverged: {canonical_path} mutation {index}"
            cursor = after
        expected = _state_signature(expected_current_states.get(rel))
        if expected is None or cursor != expected:
            return f"workspace path lineage terminal state mismatch: {canonical_path}"
    return None


def _finalize_workspace_restore_locked(
    *,
    agent_id: Any,
    transaction_id: str,
    commit: bool,
    data_root: Path | str | None,
) -> bool:
    transaction_dir = _workspace_transaction_dir(agent_id, transaction_id, data_root=data_root)
    journal_path = transaction_dir / "journal.json"
    if not journal_path.is_file():
        return False
    journal = _load_workspace_transaction_journal(transaction_dir)
    state = str(journal.get("state") or "")
    desired_state = "committed" if commit else "rolled_back"
    if state == desired_state:
        return True
    if state not in {"prepared", "swapped"}:
        return False

    workspace = _workspace_dir(agent_id, data_root=data_root)
    backup = transaction_dir / "backup"
    stage = transaction_dir / "stage"
    discarded = transaction_dir / "discarded"
    if state == "prepared":
        # No DB control event can commit before the filesystem reaches
        # ``swapped``. A prepared journal therefore always rolls back. It may
        # represent a crash before either rename, between the two renames, or
        # immediately after stage installation but before journal promotion.
        if commit:
            return False
        workspace_existed = bool(journal.get("workspace_existed", True))
        _remove_path(discarded)
        if backup.exists():
            if workspace.exists() or workspace.is_symlink():
                os.replace(workspace, discarded)
            os.replace(backup, workspace)
            _remove_path(discarded)
        elif not workspace_existed and not stage.exists():
            _remove_path(workspace)
        _remove_path(stage)
        journal["state"] = desired_state
        journal["rollback_reason"] = "startup_recovered_prepared_transaction"
        journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(journal_path, journal)
        _fsync_directory(_agent_root(agent_id, data_root=data_root))
        return True

    if commit:
        _remove_path(backup)
    else:
        workspace_existed = bool(journal.get("workspace_existed", True))
        rollback_already_installed = workspace_existed and not backup.exists()
        if rollback_already_installed:
            if not _workspace_matches_expected_states(
                agent_id=agent_id,
                expected_states=journal.get("expected_current_states"),
                data_root=data_root,
            ):
                return False
            _remove_path(discarded)
        else:
            _remove_path(discarded)
            if workspace.exists() or workspace.is_symlink():
                os.replace(workspace, discarded)
            if workspace_existed:
                os.replace(backup, workspace)
            else:
                workspace.mkdir(parents=True, exist_ok=True)
            _remove_path(discarded)
    journal["state"] = desired_state
    journal[f"{desired_state}_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(journal_path, journal)
    _fsync_directory(_agent_root(agent_id, data_root=data_root))
    return True


def finalize_workspace_restore(
    *,
    agent_id: Any,
    transaction_id: str,
    commit: bool,
    data_root: Path | str | None = None,
) -> bool:
    lease = _ACTIVE_RESTORE_LEASES.pop(transaction_id, None)
    owns_lease = lease is not None
    if lease is None:
        lease = _acquire_workspace_lock(agent_id, data_root=data_root)
    try:
        return _finalize_workspace_restore_locked(
            agent_id=agent_id,
            transaction_id=transaction_id,
            commit=commit,
            data_root=data_root,
        )
    finally:
        if owns_lease or lease is not None:
            lease.release()


def finalize_workspace_restore_locked(
    *,
    agent_id: Any,
    transaction_id: str,
    commit: bool,
    data_root: Path | str | None = None,
) -> bool:
    """Finalize a restore transaction while the caller owns the workspace lock."""

    return _finalize_workspace_restore_locked(
        agent_id=agent_id,
        transaction_id=transaction_id,
        commit=commit,
        data_root=data_root,
    )


def recover_workspace_restore_transactions(
    *,
    committed_transaction_ids: set[str],
    data_root: Path | str | None = None,
) -> dict[str, int]:
    """Resolve crash-left ``swapped`` journals from durable DB evidence."""

    counts = {"scanned": 0, "committed": 0, "rolled_back": 0, "failed": 0, "orphans_removed": 0}
    root = _data_root(data_root)
    if not root.is_dir():
        return counts
    for agent_root in sorted(path for path in root.iterdir() if path.is_dir()):
        transactions_root = agent_root / "runtime_artifacts" / "workspace_transactions"
        if not transactions_root.is_dir():
            continue
        for transaction_dir in sorted(path for path in transactions_root.iterdir() if path.is_dir()):
            journal_path = transaction_dir / "journal.json"
            if not journal_path.is_file():
                if (transaction_dir / "backup").exists():
                    counts["failed"] += 1
                else:
                    _remove_path(transaction_dir)
                    counts["orphans_removed"] += 1
                continue
            try:
                journal = _load_workspace_transaction_journal(transaction_dir)
            except Exception:  # noqa: BLE001 - malformed journals remain for operator inspection.
                counts["failed"] += 1
                continue
            state = str(journal.get("state") or "")
            if state not in {"prepared", "swapped"}:
                continue
            counts["scanned"] += 1
            transaction_id = str(journal.get("transaction_id") or transaction_dir.name)
            should_commit = state == "swapped" and transaction_id in committed_transaction_ids
            try:
                finalized = finalize_workspace_restore(
                    agent_id=journal.get("agent_id") or agent_root.name,
                    transaction_id=transaction_id,
                    commit=should_commit,
                    data_root=root,
                )
            except Exception:  # noqa: BLE001 - retain journal/backup for reconciliation.
                finalized = False
            if not finalized:
                counts["failed"] += 1
            elif should_commit:
                counts["committed"] += 1
            else:
                counts["rolled_back"] += 1
    return counts


def _pending_workspace_restore_journals(
    *,
    data_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    root = _data_root(data_root)
    if not root.is_dir():
        return pending
    for journal_path in root.glob("*/runtime_artifacts/workspace_transactions/*/journal.json"):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - recovery reports malformed journals separately.
            continue
        if journal.get("state") in {"prepared", "swapped"}:
            pending.append(journal)
    return pending


async def recover_workspace_restores_from_transcript(
    *,
    data_root: Path | str | None = None,
) -> dict[str, int]:
    """Use committed transcript control events to resolve crash-left swaps."""

    pending = _pending_workspace_restore_journals(data_root=data_root)
    if not pending:
        return recover_workspace_restore_transactions(
            committed_transaction_ids=set(),
            data_root=data_root,
        )

    from sqlalchemy import select

    from app.database import async_session, enter_rls_bypass
    from app.models.audit import AuditLog
    from app.models.chat_transcript_event import ChatTranscriptEvent

    committed: set[str] = set()
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="workspace restore crash recovery") as bypass_db:
            for journal in pending:
                transaction_id = str(journal.get("transaction_id") or "")
                try:
                    agent_id = uuid.UUID(str(journal.get("agent_id")))
                    session_id = uuid.UUID(str(journal.get("session_id")))
                except (TypeError, ValueError):
                    continue
                event_id = (
                    await bypass_db.execute(
                        select(ChatTranscriptEvent.id)
                        .where(
                            ChatTranscriptEvent.agent_id == agent_id,
                            ChatTranscriptEvent.session_id == session_id,
                            ChatTranscriptEvent.event_type.in_(
                                ("session_workspace_rewind", "session_rewind_with_workspace")
                            ),
                            ChatTranscriptEvent.metadata_json.contains(
                                {"workspace_restore": {"transaction_id": transaction_id}}
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if event_id is None:
                    event_id = (
                        await bypass_db.execute(
                            select(AuditLog.id)
                            .where(
                                AuditLog.agent_id == agent_id,
                                AuditLog.action == "workspace_file_version_restored",
                                AuditLog.details.contains({"transaction_id": transaction_id}),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if event_id is not None:
                    committed.add(transaction_id)
        await db.rollback()
    return recover_workspace_restore_transactions(
        committed_transaction_ids=committed,
        data_root=data_root,
    )


def _restore_workspace_snapshot_impl(
    *,
    agent_id: Any,
    snapshot: dict[str, Any],
    restore_paths: list[str] | None = None,
    expected_current_states: dict[str, dict[str, Any]] | None = None,
    expected_lineage: dict[str, list[dict[str, Any]]] | None = None,
    require_lineage: bool = True,
    data_root: Path | str | None = None,
    defer_finalize: bool = False,
    lease: _WorkspaceLockLease | None,
) -> WorkspaceRestoreResult:
    """Install a workspace snapshot while the caller owns the workspace lock."""

    checkpoint_event_id = str(snapshot.get("checkpoint_event_id") or "")
    retain_lease = False
    transaction_id: str | None = None
    try:
        try:
            manifest_path, manifest = _load_manifest(agent_id, snapshot, data_root=data_root)
        except Exception as exc:  # noqa: BLE001 - command path returns structured failure.
            return _restore_failure(
                checkpoint_event_id,
                f"workspace snapshot manifest unavailable: {exc}",
            )
        if not manifest.get("complete", False):
            return _restore_failure(
                checkpoint_event_id,
                "workspace snapshot is incomplete and cannot be restored safely",
            )

        workspace = _workspace_dir(agent_id, data_root=data_root)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        files_dir = (manifest_path.parent / "files").resolve()
        manifest_items = [item for item in manifest.get("files") or [] if isinstance(item, dict)]
        manifest_by_path: dict[str, dict[str, Any]] = {}
        for item in manifest_items:
            try:
                rel = _workspace_relative_path(item.get("path"))
            except ValueError as exc:
                return _restore_failure(checkpoint_event_id, f"workspace snapshot file path is invalid: {exc}")
            source = (files_dir / rel).resolve()
            if not _is_within(source, files_dir):
                return _restore_failure(
                    checkpoint_event_id,
                    f"workspace snapshot file path is invalid: {rel}",
                )
            if not source.is_file() or source.is_symlink():
                return _restore_failure(
                    checkpoint_event_id,
                    f"workspace snapshot file unavailable: {rel}",
                )
            expected_hash = str(item.get("sha256") or "")
            if _sha256(source) != expected_hash:
                return _restore_failure(
                    checkpoint_event_id,
                    f"workspace snapshot file checksum mismatch: {rel}",
                )
            manifest_by_path[rel] = item

        if restore_paths is None:
            current_paths = {path.relative_to(workspace).as_posix() for path in _workspace_files(workspace)}
            scoped_paths = set(manifest_by_path) | current_paths
        else:
            try:
                scoped_paths = {_workspace_relative_path(path) for path in restore_paths}
            except ValueError as exc:
                return _restore_failure(checkpoint_event_id, str(exc))

        already_at_target = True
        for rel in scoped_paths:
            actual = workspace_file_state(
                agent_id=agent_id,
                path=f"workspace/{rel}",
                data_root=data_root,
            )
            snapshot_item = manifest_by_path.get(rel)
            target_signature = (True, str(snapshot_item.get("sha256"))) if snapshot_item is not None else (False, None)
            if _state_signature(actual) != target_signature:
                already_at_target = False
                break
        if already_at_target:
            return WorkspaceRestoreResult(
                ok=True,
                checkpoint_event_id=checkpoint_event_id,
                workspace_rel_path="workspace",
                restored_files=[],
                deleted_files=[],
                unchanged_files=sorted(scoped_paths),
            )

        normalized_expected: dict[str, dict[str, Any]] = {}
        for raw_path, state in (expected_current_states or {}).items():
            try:
                normalized_expected[_workspace_relative_path(raw_path)] = dict(state or {})
            except ValueError as exc:
                return _restore_failure(checkpoint_event_id, str(exc))
        if expected_current_states is not None and require_lineage:
            lineage_error = _validate_workspace_restore_lineage(
                scoped_paths=scoped_paths,
                manifest_by_path=manifest_by_path,
                expected_current_states=normalized_expected,
                expected_lineage=expected_lineage,
            )
            if lineage_error:
                return _restore_failure(checkpoint_event_id, lineage_error)
        if expected_current_states is not None:
            for rel in scoped_paths:
                expected = normalized_expected.get(rel)
                if expected is None:
                    return _restore_failure(
                        checkpoint_event_id,
                        f"workspace path has no session evidence: {rel}",
                    )
                actual = workspace_file_state(
                    agent_id=agent_id,
                    path=f"workspace/{rel}",
                    data_root=data_root,
                )
                if bool(actual.get("exists")) != bool(expected.get("exists")) or (
                    actual.get("exists") and actual.get("sha256") != expected.get("sha256")
                ):
                    return _restore_failure(
                        checkpoint_event_id,
                        f"workspace path changed after session evidence: {rel}",
                    )

        transaction_id = uuid.uuid4().hex
        transaction_dir = _workspace_transaction_dir(agent_id, transaction_id, data_root=data_root)
        transaction_dir.mkdir(parents=True, exist_ok=False)
        stage = transaction_dir / "stage"
        backup = transaction_dir / "backup"
        restored: list[str] = []
        deleted: list[str] = []
        unchanged: list[str] = []
        try:
            if workspace.exists():
                shutil.copytree(workspace, stage, symlinks=True)
            else:
                stage.mkdir(parents=True)

            for rel in sorted(scoped_paths):
                target = (stage / rel).resolve()
                if not _is_within(target, stage):
                    raise ValueError(f"workspace restore path escapes stage: {rel}")
                current = (workspace / rel).resolve()
                item = manifest_by_path.get(rel)
                if item is None:
                    if current.exists() or current.is_symlink():
                        deleted.append(rel)
                    _remove_path(target)
                    continue
                source = (files_dir / rel).resolve()
                expected_hash = str(item.get("sha256") or "")
                if current.is_file() and not current.is_symlink() and _sha256(current) == expected_hash:
                    unchanged.append(rel)
                    continue
                _remove_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if _sha256(target) != expected_hash:
                    raise ValueError(f"workspace staged file checksum mismatch: {rel}")
                restored.append(rel)
        except Exception as exc:  # noqa: BLE001 - pre-swap failure must be structured and leave no debris.
            _remove_path(transaction_dir)
            return _restore_failure(
                checkpoint_event_id,
                f"workspace restore staging failed: {exc}",
                transaction_id=transaction_id,
            )

        journal = {
            "schema": "hive.workspace_restore_transaction.v1",
            "transaction_id": transaction_id,
            "agent_id": str(agent_id),
            "session_id": str(manifest.get("session_id") or ""),
            "checkpoint_event_id": checkpoint_event_id,
            "state": "prepared",
            "workspace_existed": workspace.exists(),
            "restore_paths": [f"workspace/{path}" for path in sorted(scoped_paths)],
            "expected_current_states": expected_current_states,
            "expected_lineage": expected_lineage,
            "restored_files": sorted(restored),
            "deleted_files": sorted(deleted),
            "unchanged_files": sorted(unchanged),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        journal_path = transaction_dir / "journal.json"
        try:
            _atomic_write_json(journal_path, journal)
        except Exception as exc:  # noqa: BLE001 - no filesystem swap happened; remove the abandoned stage.
            _remove_path(transaction_dir)
            return _restore_failure(
                checkpoint_event_id,
                f"workspace restore journal prepare failed: {exc}",
                transaction_id=transaction_id,
            )

        try:
            if workspace.exists():
                os.replace(workspace, backup)
            os.replace(stage, workspace)
            _fsync_directory(workspace.parent)
        except Exception as exc:  # noqa: BLE001 - rollback must preserve the pre-restore workspace.
            if backup.exists():
                _remove_path(workspace)
                os.replace(backup, workspace)
                _fsync_directory(workspace.parent)
            journal["state"] = "rolled_back"
            journal["rollback_reason"] = f"atomic_swap_failed:{type(exc).__name__}:{exc}"
            journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(journal_path, journal)
            return _restore_failure(
                checkpoint_event_id,
                f"workspace atomic restore failed: {exc}",
                transaction_id=transaction_id,
            )

        journal["state"] = "swapped"
        journal["swapped_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _atomic_write_json(journal_path, journal)
        except Exception as exc:  # noqa: BLE001 - immediately roll back the prepared on-disk journal.
            rolled_back = _finalize_workspace_restore_locked(
                agent_id=agent_id,
                transaction_id=transaction_id,
                commit=False,
                data_root=data_root,
            )
            return _restore_failure(
                checkpoint_event_id,
                (
                    f"workspace restore swapped journal failed and was rolled back: {exc}"
                    if rolled_back
                    else f"workspace restore swapped journal failed; startup recovery required: {exc}"
                ),
                transaction_id=transaction_id,
            )
        result = WorkspaceRestoreResult(
            ok=True,
            checkpoint_event_id=checkpoint_event_id,
            workspace_rel_path="workspace",
            restored_files=sorted(restored),
            deleted_files=sorted(deleted),
            unchanged_files=sorted(unchanged),
            transaction_id=transaction_id,
            requires_finalize=defer_finalize,
        )
        if defer_finalize:
            if lease is not None:
                _ACTIVE_RESTORE_LEASES[transaction_id] = lease
                retain_lease = True
            return result
        if not _finalize_workspace_restore_locked(
            agent_id=agent_id,
            transaction_id=transaction_id,
            commit=True,
            data_root=data_root,
        ):
            return _restore_failure(
                checkpoint_event_id,
                "workspace restore could not finalize",
                transaction_id=transaction_id,
            )
        return result
    finally:
        if lease is not None and not retain_lease:
            lease.release()


def restore_workspace_snapshot_locked(
    *,
    agent_id: Any,
    snapshot: dict[str, Any],
    restore_paths: list[str] | None = None,
    expected_current_states: dict[str, dict[str, Any]] | None = None,
    expected_lineage: dict[str, list[dict[str, Any]]] | None = None,
    require_lineage: bool = True,
    data_root: Path | str | None = None,
    defer_finalize: bool = False,
) -> WorkspaceRestoreResult:
    """Restore while the caller already holds the Agent workspace lock."""

    return _restore_workspace_snapshot_impl(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=restore_paths,
        expected_current_states=expected_current_states,
        expected_lineage=expected_lineage,
        require_lineage=require_lineage,
        data_root=data_root,
        defer_finalize=defer_finalize,
        lease=None,
    )


def restore_workspace_snapshot(
    *,
    agent_id: Any,
    snapshot: dict[str, Any],
    restore_paths: list[str] | None = None,
    expected_current_states: dict[str, dict[str, Any]] | None = None,
    expected_lineage: dict[str, list[dict[str, Any]]] | None = None,
    require_lineage: bool = True,
    data_root: Path | str | None = None,
    defer_finalize: bool = False,
) -> WorkspaceRestoreResult:
    """Atomically install a full or session-scoped workspace snapshot."""

    lease = _acquire_workspace_lock(agent_id, data_root=data_root)
    return _restore_workspace_snapshot_impl(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=restore_paths,
        expected_current_states=expected_current_states,
        expected_lineage=expected_lineage,
        require_lineage=require_lineage,
        data_root=data_root,
        defer_finalize=defer_finalize,
        lease=lease,
    )


def index_session_workspace_snapshot(session: Any, *, checkpoint_event_id: Any, snapshot: dict[str, Any]) -> None:
    metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    snapshots = dict(metadata.get("workspace_snapshots") or {})
    checkpoint_key = str(checkpoint_event_id)
    snapshots.pop(checkpoint_key, None)
    snapshots[checkpoint_key] = snapshot
    metadata["workspace_snapshots"] = snapshots
    session.transcript_metadata_json = metadata


def prune_session_workspace_snapshots(
    *,
    agent_id: Any,
    session: Any,
    data_root: Path | str | None = None,
    keep: int | None = None,
) -> list[str]:
    """Prune oldest per-session checkpoints after branch snapshots are cloned."""
    keep_count = max(1, int(keep or MAX_SESSION_WORKSPACE_SNAPSHOTS))
    metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    snapshots = dict(metadata.get("workspace_snapshots") or {})
    if len(snapshots) <= keep_count:
        return []
    stale_keys = list(snapshots)[: len(snapshots) - keep_count]
    agent_root = _agent_root(agent_id, data_root=data_root)
    snapshots_root = (agent_root / "runtime_artifacts" / "session_workspace_snapshots").resolve()
    removed: list[str] = []
    with agent_workspace_lock(agent_id, data_root=data_root):
        for key in stale_keys:
            snapshot = snapshots.get(key)
            manifest_rel = snapshot.get("manifest_path") if isinstance(snapshot, dict) else None
            if manifest_rel:
                manifest_path = (agent_root / str(manifest_rel)).resolve()
                if (
                    manifest_path.name == "manifest.json"
                    and _is_within(manifest_path, snapshots_root)
                    and manifest_path.parent != snapshots_root
                ):
                    _remove_path(manifest_path.parent)
                    _fsync_directory(manifest_path.parent.parent)
            snapshots.pop(key, None)
            removed.append(key)
    metadata["workspace_snapshots"] = snapshots
    session.transcript_metadata_json = metadata
    return removed


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
    prune_session_workspace_snapshots(
        agent_id=agent_id,
        session=session,
        data_root=data_root,
    )
    return snapshot


def restore_session_workspace_snapshot(
    *,
    agent_id: Any,
    session: Any,
    checkpoint_event_id: Any,
    restore_paths: list[str] | None = None,
    expected_current_states: dict[str, dict[str, Any]] | None = None,
    expected_lineage: dict[str, list[dict[str, Any]]] | None = None,
    data_root: Path | str | None = None,
    defer_finalize: bool = False,
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
    return restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=restore_paths,
        expected_current_states=expected_current_states,
        expected_lineage=expected_lineage,
        data_root=data_root,
        defer_finalize=defer_finalize,
    )
