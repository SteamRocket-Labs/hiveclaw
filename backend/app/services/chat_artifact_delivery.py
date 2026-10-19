"""Single-path chat transcript artifact delivery helpers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession


PLATFORM_ONLY_RUNTIME_SOURCES = {"rls_guard", "health_check", "ops_script", "migration"}
INTERNAL_TOP_LEVEL_DIRS = {"memory", "evolution", "runtime_artifacts", ".staging"}
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".jsonl", ".log", ".xml", ".yaml", ".yml"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
TEXT_PREVIEW_SNAPSHOT_MAX_BYTES = 64 * 1024
CHAT_ARTIFACT_SNAPSHOT_DIR = Path("runtime_artifacts") / "chat_artifact_snapshots"
CHAT_ARTIFACT_PROVENANCE_INDEX = Path("runtime_artifacts") / "chat_artifact_provenance_index.json"
CHAT_ARTIFACT_SNAPSHOT_GC_REPORT = Path("runtime_artifacts") / "chat_artifact_snapshot_gc.json"
CHAT_ARTIFACT_PROVENANCE_ENTRY_LIMIT = 20
CHAT_ARTIFACT_PROVENANCE_HINT_LIMIT = 5
CHAT_ARTIFACT_IDEMPOTENCY_CONSTRAINT = "uq_chat_artifacts_agent_session_run_path_snapshot"
CHAT_ARTIFACT_SNAPSHOT_GC_SCHEMA = "chat_artifact_snapshot_gc.v1"
DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS = 30.0


def tool_session_write_paths(
    tool_name: str,
    args: dict[str, Any],
    *,
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> list[str]:
    """Return user-facing workspace artifact paths written by a tool call."""
    if tool_name in ("write_file", "edit_file", "delete_file", "fs_write"):
        path = args.get("path")
        return [str(path)] if path else []
    if tool_name == "office_document_create":
        path = args.get("path")
        return [str(path)] if path else []
    if tool_name == "office_document_apply":
        path = args.get("output_path") or args.get("path")
        return [str(path)] if path else []
    paths: list[str] = []
    for artifact in artifacts or ():
        if not isinstance(artifact, dict):
            continue
        rel = _safe_workspace_relative_path(str(artifact.get("path") or ""))
        if rel is not None and rel.as_posix() not in paths:
            paths.append(rel.as_posix())
    return paths


def ensure_agent_session_source(runtime_source: str) -> None:
    """Fail closed if a pure platform job attempts to create an agent session."""
    if runtime_source in PLATFORM_ONLY_RUNTIME_SOURCES:
        raise ValueError(f"pure platform runtime source {runtime_source!r} must not create an agent session")


def _safe_workspace_relative_path(path: str) -> PurePosixPath | None:
    value = str(path or "").replace("\\", "/").strip()
    if not value:
        return None
    rel = PurePosixPath(value)
    if rel.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in rel.parts):
        return None
    if rel.parts[0] in INTERNAL_TOP_LEVEL_DIRS:
        return None
    if rel.parts[0] != "workspace":
        return None
    if len(rel.parts) > 1 and rel.parts[1] == "workspace":
        return None
    return rel


def _provenance_index_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / CHAT_ARTIFACT_PROVENANCE_INDEX


def _load_provenance_index(workspace_root: Path) -> dict[str, Any]:
    path = _provenance_index_path(workspace_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"version": 1, "paths": {}}
    if not isinstance(data, dict):
        return {"version": 1, "paths": {}}
    paths = data.get("paths")
    if not isinstance(paths, dict):
        data["paths"] = {}
    data["version"] = 1
    return data


def _write_provenance_index(workspace_root: Path, payload: dict[str, Any]) -> None:
    path = _provenance_index_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _provenance_entry_from_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    rel = _safe_workspace_relative_path(str(candidate.get("path") or ""))
    if rel is None:
        return None
    artifact_id = str(candidate.get("artifact_id") or candidate.get("id") or "").strip()
    return {
        "path": rel.as_posix(),
        "artifact_id": artifact_id or None,
        "agent_id": str(candidate.get("agent_id") or "").strip() or None,
        "session_id": str(candidate.get("session_id") or "").strip() or None,
        "runtime_task_id": str(candidate.get("runtime_task_id") or "").strip() or None,
        "source_agent_id": str(candidate.get("source_agent_id") or "").strip() or None,
        "owner_agent_id": str(candidate.get("owner_agent_id") or "").strip() or None,
        "delivery_agent_id": str(candidate.get("delivery_agent_id") or "").strip() or None,
        "source": str(candidate.get("source") or "").strip() or None,
        "action": str(candidate.get("action") or "").strip() or None,
        "snapshot_hash": str(candidate.get("snapshot_hash") or candidate.get("revision_id") or "").strip() or None,
        "content_hash": str(candidate.get("content_hash") or "").strip() or None,
        "created_at": str(candidate.get("created_at") or "").strip() or None,
    }


def record_workspace_artifact_provenance(workspace_root: Path, candidate: dict[str, Any]) -> None:
    """Persist a lightweight path -> latest delivered artifact index.

    The index is a prompt-time hint only. It does not hide files or authorize
    reads; it gives the model enough provenance to avoid treating another
    session's artifact as current-task evidence by accident.
    """
    entry = _provenance_entry_from_candidate(candidate)
    if not entry:
        return
    try:
        index = _load_provenance_index(workspace_root)
        paths = index.setdefault("paths", {})
        path_key = entry["path"]
        existing = paths.get(path_key)
        if not isinstance(existing, list):
            existing = []
        artifact_id = entry.get("artifact_id")
        retained = [
            item
            for item in existing
            if isinstance(item, dict) and (not artifact_id or item.get("artifact_id") != artifact_id)
        ]
        paths[path_key] = [entry, *retained][:CHAT_ARTIFACT_PROVENANCE_ENTRY_LIMIT]
        _write_provenance_index(workspace_root, index)
    except OSError:
        return


def _path_is_inside_directory(path: PurePosixPath, directory: PurePosixPath | None) -> bool:
    if directory is None:
        return True
    if path == directory:
        return True
    directory_parts = directory.parts
    return len(path.parts) > len(directory_parts) and path.parts[: len(directory_parts)] == directory_parts


def _latest_provenance_entries_for_path(
    workspace_root: Path,
    rel_path: str,
    *,
    directory: bool,
) -> list[dict[str, Any]]:
    rel = _safe_workspace_relative_path(rel_path)
    if rel is None:
        if directory and not str(rel_path or "").strip():
            rel = None
        else:
            return []
    index = _load_provenance_index(workspace_root)
    paths = index.get("paths") if isinstance(index, dict) else {}
    if not isinstance(paths, dict):
        return []
    entries: list[dict[str, Any]] = []
    for path_key, path_entries in paths.items():
        key_rel = _safe_workspace_relative_path(str(path_key))
        if key_rel is None:
            continue
        if directory:
            if not _path_is_inside_directory(key_rel, rel):
                continue
        elif key_rel != rel:
            continue
        if isinstance(path_entries, list):
            entries.extend(item for item in path_entries if isinstance(item, dict))
    entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return entries[:CHAT_ARTIFACT_PROVENANCE_HINT_LIMIT]


def workspace_artifact_provenance_hint(workspace_root: Path, rel_path: str, *, directory: bool = False) -> str:
    entries = _latest_provenance_entries_for_path(workspace_root, rel_path, directory=directory)
    if not entries:
        return ""
    lines = [
        "Provenance hint: workspace files may belong to another session or run. "
        "Use them as current task material only if the user referenced them or this task requires them."
    ]
    for entry in entries:
        details = [
            f"path={entry.get('path')}",
            f"session={entry.get('session_id') or 'unknown'}",
            f"runtime_task={entry.get('runtime_task_id') or 'none'}",
            f"artifact={entry.get('artifact_id') or 'unknown'}",
            f"source={entry.get('source') or 'unknown'}",
            f"action={entry.get('action') or 'unknown'}",
            f"created_at={entry.get('created_at') or 'unknown'}",
        ]
        lines.append(f"- {'; '.join(details)}")
    return "\n".join(lines)


def _preview_kind_for_path(path: PurePosixPath) -> str:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in OFFICE_EXTENSIONS:
        return "office"
    return "download"


def _snapshot_hash(*, rel_path: PurePosixPath, stat_data: Any | None, content_hash: str | None = None) -> str:
    payload = f"{rel_path.as_posix()}:sha256:{content_hash}" if content_hash else f"{rel_path.as_posix()}:{getattr(stat_data, 'st_size', '')}:{getattr(stat_data, 'st_mtime_ns', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_storage_rel_path(
    *,
    session_id: str | uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    snapshot_hash: str,
    rel_path: PurePosixPath,
) -> Path:
    suffix = rel_path.suffix.lower()
    filename = f"{snapshot_hash}{suffix}" if suffix else snapshot_hash
    run_key = str(runtime_task_id) if runtime_task_id else "no-runtime-task"
    return CHAT_ARTIFACT_SNAPSHOT_DIR / str(session_id) / run_key / filename


def _safe_snapshot_relative_path(value: Any) -> Path | None:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return None
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return None
    required_prefix = PurePosixPath(CHAT_ARTIFACT_SNAPSHOT_DIR.as_posix()).parts
    if rel.parts[: len(required_prefix)] != required_prefix:
        return None
    return Path(rel.as_posix())


def _write_snapshot_gc_report(workspace_root: Path, report: dict[str, Any]) -> None:
    report_path = workspace_root / CHAT_ARTIFACT_SNAPSHOT_GC_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(f"{report_path.suffix}.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(report_path)


def _prune_empty_snapshot_dirs(snapshot_root: Path) -> int:
    removed = 0
    if not snapshot_root.exists():
        return 0
    directories = [path for path in snapshot_root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def cleanup_chat_artifact_snapshots(
    *,
    workspace_root: Path,
    referenced_snapshot_paths: list[str] | tuple[str, ...] | set[str],
    retention_days: float = DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete expired unreferenced delivery snapshots and write an audit report."""
    root = workspace_root.resolve()
    snapshot_root = (root / CHAT_ARTIFACT_SNAPSHOT_DIR).resolve()
    current_time = now or datetime.now(timezone.utc)
    retention_days = max(float(retention_days), 0.0)
    cutoff = current_time - timedelta(days=retention_days)
    referenced_paths: set[Path] = set()
    for raw_path in referenced_snapshot_paths:
        rel = _safe_snapshot_relative_path(raw_path)
        if rel is None:
            continue
        referenced_paths.add((root / rel).resolve())

    removed_paths: list[str] = []
    removed_bytes = 0
    kept_referenced_count = 0
    kept_recent_count = 0
    scanned_count = 0
    if snapshot_root.exists():
        for path in snapshot_root.rglob("*"):
            if not path.is_file():
                continue
            scanned_count += 1
            resolved = path.resolve()
            try:
                rel_display = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            if resolved in referenced_paths:
                kept_referenced_count += 1
                continue
            try:
                stat_data = path.stat()
            except OSError:
                continue
            modified_at = datetime.fromtimestamp(stat_data.st_mtime, tz=timezone.utc)
            if modified_at > cutoff:
                kept_recent_count += 1
                continue
            try:
                size = stat_data.st_size
                path.unlink()
            except OSError:
                continue
            removed_paths.append(rel_display)
            removed_bytes += size

    pruned_empty_dirs = _prune_empty_snapshot_dirs(snapshot_root)
    report = {
        "schema": CHAT_ARTIFACT_SNAPSHOT_GC_SCHEMA,
        "generated_at": current_time.isoformat(),
        "workspace_root": str(root),
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "referenced_count": len(referenced_paths),
        "scanned_count": scanned_count,
        "removed_count": len(removed_paths),
        "removed_bytes": removed_bytes,
        "removed_paths": removed_paths[:200],
        "kept_referenced_count": kept_referenced_count,
        "kept_recent_count": kept_recent_count,
        "pruned_empty_dirs": pruned_empty_dirs,
    }
    _write_snapshot_gc_report(root, report)
    return report


async def cleanup_chat_artifact_snapshots_for_agent(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    workspace_root: Path,
    retention_days: float = DEFAULT_CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = await db.execute(
        select(ChatArtifact.id, ChatArtifact.snapshot_json).where(ChatArtifact.agent_id == agent_id)
    )
    artifact_rows = result.all()
    live_artifact_ids = {str(artifact_id) for artifact_id, _snapshot in artifact_rows}
    referenced_paths: list[str] = []
    for _artifact_id, snapshot in artifact_rows:
        if isinstance(snapshot, dict):
            storage_path = str(snapshot.get("snapshot_storage_path") or "").strip()
            if storage_path:
                referenced_paths.append(storage_path)
    report = cleanup_chat_artifact_snapshots(
        workspace_root=workspace_root,
        referenced_snapshot_paths=referenced_paths,
        retention_days=retention_days,
        now=now,
    )
    report["office_preview_cache_removed_count"] = _cleanup_orphaned_office_preview_caches(
        workspace_root=workspace_root,
        live_artifact_ids=live_artifact_ids,
    )
    _write_snapshot_gc_report(Path(workspace_root).resolve(), report)
    return report


def _cleanup_orphaned_office_preview_caches(
    *,
    workspace_root: Path,
    live_artifact_ids: set[str],
) -> int:
    """Remove derived Office preview caches whose ChatArtifact no longer exists."""
    cache_root = Path(workspace_root).resolve() / ".office_meta" / "artifacts"
    if not cache_root.is_dir():
        return 0

    removed_count = 0
    for artifact_cache in cache_root.iterdir():
        if artifact_cache.is_symlink() or not artifact_cache.is_dir():
            continue
        try:
            uuid.UUID(artifact_cache.name)
        except ValueError:
            continue
        if artifact_cache.name in live_artifact_ids:
            continue
        shutil.rmtree(artifact_cache)
        removed_count += 1
    return removed_count


def _write_content_snapshot(
    *,
    source_path: Path,
    workspace_root: Path,
    session_id: str | uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    snapshot_hash: str,
    rel_path: PurePosixPath,
    content_hash: str,
) -> str:
    storage_rel = _snapshot_storage_rel_path(
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        snapshot_hash=snapshot_hash,
        rel_path=rel_path,
    )
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    try:
        with open_workspace_file(workspace_root, storage_rel.as_posix(), mode="xb") as target, source_path.open("rb") as source:
            shutil.copyfileobj(source, target)
    except WorkspaceAuthorityError as exc:
        if not isinstance(exc.__cause__, FileExistsError):
            raise
        with open_workspace_file(workspace_root, storage_rel.as_posix()) as existing:
            if hashlib.file_digest(existing, "sha256").hexdigest() != content_hash:
                raise WorkspaceAuthorityError("workspace_resource_changed", "The immutable artifact snapshot changed; regenerate the delivery before retrying.") from exc
    return storage_rel.as_posix()


def _read_preview_snapshot(path: Path, preview_kind: str) -> tuple[str | None, bool]:
    if preview_kind not in {"markdown", "text"}:
        return None, False
    try:
        data = path.read_bytes()
    except OSError:
        return None, False
    truncated = len(data) > TEXT_PREVIEW_SNAPSHOT_MAX_BYTES
    content = data[:TEXT_PREVIEW_SNAPSHOT_MAX_BYTES].decode("utf-8", errors="replace")
    return content, truncated


def legacy_artifact_preview(workspace_root: Path, path: str) -> dict[str, Any]:
    """Retain an unversioned preview without minting a delivery identity or source pin."""
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, snapshot_workspace_file

    relative = _safe_workspace_relative_path(path)
    if relative is None:
        return {}
    try:
        directory, captured = snapshot_workspace_file(workspace_root, relative.as_posix())
    except (OSError, ValueError, WorkspaceAuthorityError):
        return {}
    try:
        kind = _preview_kind_for_path(relative)
        content, truncated = _read_preview_snapshot(captured, kind)
        return {"name": relative.name, "preview_kind": kind, "size": captured.stat().st_size,
            "mime_type": mimetypes.guess_type(relative.name)[0], "preview_snapshot_content": content,
            "preview_snapshot_truncated": truncated, "legacy_current_file_fallback": True}
    finally:
        directory.cleanup()


def _read_text_or_binary_marker(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError, ValueError):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return f"[binary artifact snapshot: {path.name}, {size} bytes]"


def _workspace_changed(artifact: ChatArtifact, workspace_root: Path) -> bool | None:
    rel = _safe_workspace_relative_path(getattr(artifact, "path", ""))
    if rel is None:
        return None
    current = workspace_root / rel.as_posix()
    if not current.exists() or not current.is_file():
        return True
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    try:
        expected = (artifact.snapshot_json or {}).get("content_hash")
        if expected:
            with open_workspace_file(workspace_root, rel.as_posix()) as source:
                return hashlib.file_digest(source, "sha256").hexdigest() != expected
        current_hash = _snapshot_hash(rel_path=rel, stat_data=current.stat())
    except (OSError, WorkspaceAuthorityError):
        return True
    return current_hash != getattr(artifact, "snapshot_hash", None)


ArtifactFileSource = Literal[
    "delivery_snapshot",
    "legacy_current_file_fallback",
    "missing_delivery_snapshot",
    "missing_legacy_current_file",
]


def is_chat_artifact_snapshot_storage_path(path: str) -> bool:
    normalized = PurePosixPath(str(path).replace("\\", "/").strip()).as_posix().strip("/")
    return any(normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in (CHAT_ARTIFACT_SNAPSHOT_DIR.as_posix(), ".chat_artifact_snapshots"))


def resolve_chat_artifact_file(
    artifact: ChatArtifact,
    workspace_root: Path,
) -> tuple[Path | None, ArtifactFileSource]:
    """Resolve artifact bytes without substituting current data for a declared snapshot."""

    root = Path(workspace_root).resolve()
    from app.services.workspace_resource_authority import resolve_workspace_storage_path

    snapshot = artifact.snapshot_json if isinstance(artifact.snapshot_json, dict) else {}
    storage_rel = str(snapshot.get("snapshot_storage_path") or "").replace("\\", "/").strip()
    if storage_rel:
        rel = PurePosixPath(storage_rel)
        if not rel.is_absolute() and not any(part in {"", ".", ".."} for part in rel.parts):
            snapshot_path = resolve_workspace_storage_path(root, rel.as_posix())
            try:
                snapshot_path.relative_to(root)
            except ValueError:
                pass
            else:
                if snapshot_path.is_file():
                    return snapshot_path, "delivery_snapshot"
        return None, "missing_delivery_snapshot"

    current_rel = _safe_workspace_relative_path(str(getattr(artifact, "path", "") or ""))
    if current_rel is not None:
        current = resolve_workspace_storage_path(root, current_rel.as_posix())
        try:
            current.relative_to(root)
        except ValueError:
            pass
        else:
            if current.is_file():
                return current, "legacy_current_file_fallback"
    return None, "missing_legacy_current_file"


@contextmanager
def captured_chat_artifact_file(artifact: ChatArtifact, workspace_root: Path):
    """Lease a private, descriptor-checked copy through the complete consumer operation."""
    from app.services.workspace_resource_authority import snapshot_workspace_file

    target, source = resolve_chat_artifact_file(artifact, workspace_root)
    if target is None:
        yield None, source
        return
    snapshot = artifact.snapshot_json or {}
    path = snapshot["snapshot_storage_path"] if source == "delivery_snapshot" else artifact.path
    expected = snapshot.get("content_hash") if source == "delivery_snapshot" else None
    directory, captured = snapshot_workspace_file(Path(workspace_root), path, expected)
    try:
        yield captured, source
    finally:
        directory.cleanup()


def read_chat_artifact_snapshot_content(artifact: ChatArtifact, workspace_root: Path) -> dict[str, Any]:
    """Read the delivery-time artifact snapshot, falling back explicitly for legacy rows."""
    root = Path(workspace_root).resolve()
    snapshot = artifact.snapshot_json if isinstance(artifact.snapshot_json, dict) else {}
    with captured_chat_artifact_file(artifact, root) as (target, source):
        if target is not None:
            uses_snapshot = source == "delivery_snapshot"
            return {
                "path": artifact.path,
                "content": _read_text_or_binary_marker(target),
                "uses_snapshot": uses_snapshot,
                "legacy_current_file_fallback": source == "legacy_current_file_fallback",
                "workspace_changed": _workspace_changed(artifact, root) if uses_snapshot else None,
                "snapshot_hash": artifact.snapshot_hash,
                "content_hash": snapshot.get("content_hash"),
            }
    return {
        "path": artifact.path,
        "content": "",
        "uses_snapshot": False,
        "legacy_current_file_fallback": source == "missing_legacy_current_file",
        "workspace_changed": None,
        "snapshot_hash": artifact.snapshot_hash,
        "content_hash": snapshot.get("content_hash"),
        "missing": True,
    }


def build_artifact_candidate(
    *,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    path: str,
    workspace_root: Path,
    source: str = "workspace_write",
    action: str = "created",
    tool_call_id: str | uuid.UUID | None = None,
    diff_summary: str | None = None,
    owner_agent_id: str | uuid.UUID | None = None,
    source_agent_id: str | uuid.UUID | None = None,
    download_agent_id: str | uuid.UUID | None = None,
    delivery_agent_id: str | uuid.UUID | None = None,
    expected_content_hash: str | None = None,
    execution_evidence_ref: str | None = None,
    tool_invocation_id: uuid.UUID | None = None,
    selection_model_result_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Return a durable artifact payload for a safe workspace file path.

    This is deliberately mechanical. It decides path safety and preview type;
    semantic explanation remains the assistant's job.
    """
    rel = _safe_workspace_relative_path(path)
    if rel is None:
        return None

    from app.services.workspace_resource_authority import resolve_workspace_storage_path, snapshot_workspace_file

    root = workspace_root.resolve()
    absolute = resolve_workspace_storage_path(root, rel.as_posix())
    try:
        absolute.relative_to(root)
    except ValueError:
        return None
    if not absolute.exists() or not absolute.is_file():
        return None

    directory, captured = snapshot_workspace_file(root, rel.as_posix(), expected_content_hash)
    try:
        stat_data = captured.stat()
        mime_type, _encoding = mimetypes.guess_type(rel.name)
        modified_at = datetime.fromtimestamp(stat_data.st_mtime, tz=timezone.utc).isoformat()
        content_hash = _content_hash(captured)
        snapshot_hash = _snapshot_hash(rel_path=rel, stat_data=stat_data, content_hash=content_hash)
        if execution_evidence_ref is not None:
            snapshot_hash = hashlib.sha256(f"{snapshot_hash}\n{execution_evidence_ref}".encode()).hexdigest()
        if selection_model_result_id is not None:
            snapshot_hash = hashlib.sha256(f"{snapshot_hash}\nselection-model:{selection_model_result_id}".encode()).hexdigest()
        preview_kind = _preview_kind_for_path(rel)
        preview_snapshot_content, preview_snapshot_truncated = _read_preview_snapshot(captured, preview_kind)
        snapshot_storage_path = _write_content_snapshot(
            source_path=captured,
            workspace_root=root,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            snapshot_hash=snapshot_hash,
            rel_path=rel,
            content_hash=content_hash,
        )
    finally:
        directory.cleanup()
    owner_agent = str(owner_agent_id or agent_id)
    source_agent = str(source_agent_id or owner_agent)
    download_agent = str(download_agent_id or owner_agent)
    delivery_agent = str(delivery_agent_id) if delivery_agent_id else None
    snapshot = {
        "exists": True,
        "size": stat_data.st_size,
        "mtime_ns": stat_data.st_mtime_ns,
        "path": rel.as_posix(),
        "revision_id": snapshot_hash,
        "content_hash": content_hash,
        "snapshot_storage_path": snapshot_storage_path,
        "snapshot_retention": "chat_artifact_delivery_snapshot",
        "action": action,
        "tool_call_id": str(tool_call_id) if tool_call_id else None,
        "diff_summary": diff_summary,
        "owner_agent_id": owner_agent,
        "source_agent_id": source_agent,
        "download_agent_id": download_agent,
        "delivery_agent_id": delivery_agent,
    }
    if selection_model_result_id is not None:
        snapshot["selection_model_result_id"] = str(selection_model_result_id)
    if execution_evidence_ref is not None:
        snapshot["execution_evidence_ref"] = execution_evidence_ref
        snapshot["tool_invocation_id"] = str(tool_invocation_id)
    if preview_snapshot_content is not None:
        snapshot["preview_content"] = preview_snapshot_content
        snapshot["preview_content_truncated"] = preview_snapshot_truncated
    artifact_id = uuid.uuid4()
    return {
        "id": str(artifact_id),
        "artifact_id": str(artifact_id),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        "path": rel.as_posix(),
        "name": rel.name,
        "mime_type": mime_type,
        "size": stat_data.st_size,
        "modified_at": modified_at,
        "preview_kind": preview_kind,
        "source": source,
        "snapshot_hash": snapshot_hash,
        "content_hash": content_hash,
        "snapshot": snapshot,
        "preview_snapshot_content": preview_snapshot_content,
        "preview_snapshot_truncated": preview_snapshot_truncated if preview_snapshot_content is not None else None,
        "owner_agent_id": owner_agent,
        "source_agent_id": source_agent,
        "download_agent_id": download_agent,
        "delivery_agent_id": delivery_agent,
        "revision_id": snapshot_hash,
        "action": action,
        "tool_call_id": str(tool_call_id) if tool_call_id else None,
        "diff_summary": diff_summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def build_session_artifact_parts(
    *,
    agent_id: uuid.UUID,
    session_id: str | uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    paths: list[str] | tuple[str, ...],
    workspace_root: Path,
    source: str = "workspace_write",
    action: str = "created",
    owner_agent_id: str | uuid.UUID | None = None,
    source_agent_id: str | uuid.UUID | None = None,
    download_agent_id: str | uuid.UUID | None = None,
    delivery_agent_id: str | uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Build artifact-delivery parts for safe workspace paths WITHOUT DB rows.

    ``ChatArtifact.message_id`` is non-nullable, so producers that have no chat
    message cannot persist artifact rows. They still need outputs to surface as
    clickable timeline artifacts, so this returns transcript-ready parts using
    the same mechanical path-safety and preview-kind decisions as
    :func:`build_artifact_candidate`.
    """
    parts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        candidate = build_artifact_candidate(
            agent_id=agent_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            path=path,
            workspace_root=workspace_root,
            source=source,
            action=action,
            owner_agent_id=owner_agent_id,
            source_agent_id=source_agent_id,
            download_agent_id=download_agent_id,
            delivery_agent_id=delivery_agent_id,
        )
        if not candidate:
            continue
        dedupe_key = (candidate["path"], candidate["snapshot_hash"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        record_workspace_artifact_provenance(workspace_root, candidate)
        parts.append(_artifact_part_from_candidate(candidate))
    return parts


def _artifact_part_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "artifact",
        "artifact_id": str(candidate.get("artifact_id") or candidate.get("id")),
        "path": candidate["path"],
        "name": candidate["name"],
        "mime_type": candidate.get("mime_type"),
        "size": candidate.get("size"),
        "modified_at": candidate.get("modified_at"),
        "preview_kind": candidate.get("preview_kind", "download"),
        "source": candidate.get("source", "workspace_write"),
        "runtime_task_id": candidate.get("runtime_task_id"),
        "owner_agent_id": candidate.get("owner_agent_id"),
        "source_agent_id": candidate.get("source_agent_id"),
        "download_agent_id": candidate.get("download_agent_id"),
        "delivery_agent_id": candidate.get("delivery_agent_id"),
        "created_at": candidate.get("created_at"),
        "revision_id": candidate.get("revision_id"),
        "action": candidate.get("action"),
        "tool_call_id": candidate.get("tool_call_id"),
        "diff_summary": candidate.get("diff_summary"),
        "preview_snapshot_content": candidate.get("preview_snapshot_content"),
        "preview_snapshot_truncated": candidate.get("preview_snapshot_truncated"),
    }


def artifact_part_from_model(artifact: ChatArtifact) -> dict[str, Any]:
    snapshot = artifact.snapshot_json or {}
    return {
        "type": "artifact",
        "artifact_id": str(artifact.id),
        "path": artifact.path,
        "name": artifact.name,
        "mime_type": artifact.mime_type,
        "size": artifact.size,
        "modified_at": artifact.modified_at,
        "preview_kind": artifact.preview_kind,
        "source": artifact.source,
        "runtime_task_id": str(artifact.runtime_task_id) if artifact.runtime_task_id else None,
        "owner_agent_id": snapshot.get("owner_agent_id") or str(artifact.agent_id),
        "source_agent_id": snapshot.get("source_agent_id") or str(artifact.agent_id),
        "download_agent_id": snapshot.get("download_agent_id") or str(artifact.agent_id),
        "delivery_agent_id": snapshot.get("delivery_agent_id"),
        "created_at": artifact.created_at.isoformat() if getattr(artifact, "created_at", None) else None,
        "revision_id": snapshot.get("revision_id") or artifact.snapshot_hash,
        "content_hash": snapshot.get("content_hash"),
        "snapshot_storage_path": snapshot.get("snapshot_storage_path"),
        "action": snapshot.get("action"),
        "tool_call_id": snapshot.get("tool_call_id"),
        "diff_summary": snapshot.get("diff_summary"),
        "preview_snapshot_content": snapshot.get("preview_content"),
        "preview_snapshot_truncated": snapshot.get("preview_content_truncated"),
    }


def _chat_artifact_insert_statement(
    *,
    candidate: dict[str, Any],
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str | uuid.UUID,
    message_id: uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    source: str,
    owner_user_id: uuid.UUID | None = None,
    root_session_id: uuid.UUID | None = None,
    authority_state: str = "quarantined",
) -> Any:
    """Build the atomic artifact insert used by concurrent web-chat runs."""
    runtime_uuid = uuid.UUID(str(runtime_task_id)) if runtime_task_id else None
    return (
        pg_insert(ChatArtifact)
        .values(
            id=uuid.UUID(str(candidate["artifact_id"])),
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=uuid.UUID(str(session_id)),
            message_id=message_id,
            runtime_task_id=runtime_uuid,
            owner_user_id=owner_user_id,
            root_session_id=root_session_id,
            authority_state=authority_state,
            path=candidate["path"],
            name=candidate["name"],
            mime_type=candidate.get("mime_type"),
            size=candidate.get("size"),
            modified_at=candidate.get("modified_at"),
            preview_kind=candidate.get("preview_kind", "download"),
            source=candidate.get("source", source),
            snapshot_hash=candidate["snapshot_hash"],
            snapshot_json=candidate.get("snapshot"),
        )
        .on_conflict_do_nothing(constraint=CHAT_ARTIFACT_IDEMPOTENCY_CONSTRAINT)
        .returning(ChatArtifact.id)
    )


async def _load_existing_chat_artifact(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    runtime_task_id: uuid.UUID | None,
    path: str,
    snapshot_hash: str,
) -> ChatArtifact | None:
    result = await db.execute(
        select(ChatArtifact)
        .where(
            ChatArtifact.agent_id == agent_id,
            ChatArtifact.session_id == session_id,
            ChatArtifact.runtime_task_id == runtime_task_id,
            ChatArtifact.path == path,
            ChatArtifact.snapshot_hash == snapshot_hash,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _native_artifact_for_final_message(db, *, tenant_id, agent_id, session_id, runtime_task_id, path):
    from app.models.chat_transcript_event import ChatTranscriptEvent

    event = await db.scalar(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == tenant_id, ChatTranscriptEvent.agent_id == agent_id,
        ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.run_id == runtime_task_id,
        ChatTranscriptEvent.item_kind == "tool_result", ChatTranscriptEvent.lifecycle == "completed",
        or_(ChatTranscriptEvent.parts_json.contains([{"type": "artifact", "path": path}]),
            ChatTranscriptEvent.metadata_json["v2_payload"]["metadata"]["artifact_delivery"]["files"].contains([{"path": path}])),
    ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
    if event is None:
        return False, None
    parts = [part for part in event.parts_json or []
        if isinstance(part, dict) and part.get("type") == "artifact" and part.get("path") == path]
    if len(parts) != 1 or event.invocation_id is None:
        return True, None
    try:
        artifact_id = uuid.UUID(str(parts[0].get("artifact_id")))
    except ValueError:
        return True, None
    artifact = await db.get(ChatArtifact, artifact_id)
    if artifact is None or (artifact.tenant_id, artifact.agent_id, artifact.session_id, artifact.runtime_task_id, artifact.path) != (
        tenant_id, agent_id, session_id, runtime_task_id, path,
    ):
        return True, None
    snapshot = artifact.snapshot_json or {}
    if snapshot.get("tool_invocation_id") != str(event.invocation_id) or snapshot.get("execution_evidence_ref") != (
        f"workspace-output:{uuid.uuid5(event.invocation_id, 'workspace-output-evidence')}"
    ):
        return True, None
    return True, artifact


async def create_chat_artifacts_for_message(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    session_id: str | uuid.UUID,
    message_id: uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    paths: list[str] | tuple[str, ...],
    workspace_root: Path,
    source: str = "workspace_write",
    action: str = "created",
    tool_call_id: str | uuid.UUID | None = None,
    diff_summary: str | None = None,
    owner_agent_id: str | uuid.UUID | None = None,
    source_agent_id: str | uuid.UUID | None = None,
    download_agent_id: str | uuid.UUID | None = None,
    delivery_agent_id: str | uuid.UUID | None = None,
    rebind_existing_to_message: bool = False,
    owner_user_id: uuid.UUID | None = None,
    root_session_id: uuid.UUID | None = None,
    publication_errors: list[dict[str, Any]] | None = None,
    expected_content_hashes: dict[str, str] | None = None,
    source_receipt_ids: dict[str, uuid.UUID] | None = None,
    selection_model_result_id: uuid.UUID | None = None,
    execution_evidence_ref: str | None = None,
    tool_invocation_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Create artifact rows for safe candidate paths and return message parts."""
    from app.database import tenant_scoped_session
    from app.services.content_input_receipts import (
        ContentInputReceiptError, validate_artifact_content_receipt, validate_browser_workspace_receipt,
        validate_runtime_workspace_receipt,
    )
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, authorize_workspace_path

    parts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    session_uuid = uuid.UUID(str(session_id))
    runtime_uuid = uuid.UUID(str(runtime_task_id)) if runtime_task_id else None
    authority_user = runtime_principal = authority_error = machine_scope = None
    from app.services.machine_runtime_authority import is_machine_runtime_actor
    if isinstance(db, AsyncSession):
        from app.models.user import User

        authority_session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_uuid,
                    ChatSession.agent_id == agent_id,
                    ChatSession.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        owner_user_id = authority_session.user_id if authority_session is not None else None
        root_session_id = (
            authority_session.root_session_id or authority_session.id if authority_session is not None else None
        )
        if authority_session is not None and authority_session.user_id is None and runtime_uuid is not None:
            from app.services.machine_runtime_authority import MachineArtifactReader, load_machine_workspace_scope, machine_task_actor
            from app.services.personal_knowledge_access import AgentRuntimePrincipal

            try:
                actor = await machine_task_actor(db, tenant_id=tenant_id, agent_id=agent_id,
                    runtime_task_id=runtime_uuid, session_id=session_uuid, user_id=None,
                    scope='task.read' if source == 'workspace_read' and selection_model_result_id is not None else 'agent.execute')
                if actor is not None:
                    authority_user = actor
                    machine_scope = await load_machine_workspace_scope(db, actor=actor, session_id=session_uuid)
                    principal_type = MachineArtifactReader if source == 'workspace_read' and selection_model_result_id is not None else AgentRuntimePrincipal
                    runtime_principal = principal_type(agent_id=agent_id, requester_user_id=None,
                        session_id=str(session_uuid), runtime_task_id=str(runtime_uuid), purpose='machine_task')
            except HTTPException as exc:
                authority_error = exc
        if authority_session is not None and authority_session.audience_kind == "group":
            from app.models.runtime_task import RuntimeTask
            from app.services.personal_knowledge_access import AgentRuntimePrincipal
            from app.services.shared_group_authority import _load_shared_group_task_actor, load_shared_group_runtime_actor

            try:
                # A sealed read snapshot recovers its original reader, without starting another Agent effect.
                task = await db.get(RuntimeTask, runtime_uuid) if runtime_uuid else None
                if source == "workspace_read" and selection_model_result_id is not None:
                    actor = await _load_shared_group_task_actor(db, task=task, session=authority_session)
                else:
                    actor = await load_shared_group_runtime_actor(db, task=task, session=authority_session)
                owner_user_id = actor.id
                if owner_user_id is not None and source != "workspace_read":
                    runtime_principal = AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=owner_user_id,
                        session_id=str(session_uuid), runtime_task_id=str(runtime_uuid))
            except HTTPException as exc:
                authority_error = exc
            except (PermissionError, ValueError):
                owner_user_id = None
        if owner_user_id is not None:
            authority_user = await db.get(User, owner_user_id)
            if authority_user is not None and authority_user.tenant_id != tenant_id:
                authority_user = None
    authority_state = "owned" if owner_user_id is not None or is_machine_runtime_actor(authority_user) else "quarantined"

    def record_source_error(path, exc):
        if publication_errors is not None:
            publication_errors.append({"path": path, "status": "denied" if exc.status_code == 403 else "unavailable",
                "reason_code": exc.code, "retryable": False, "recovery_action": "reconcile_artifact_sources"})

    async def _register_path(candidate: dict[str, Any]) -> None:
        if not isinstance(db, AsyncSession) or tenant_id is None or source == "workspace_read":
            return
        from app.services.workspace_resource_authority import register_workspace_path

        await register_workspace_path(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            path=candidate["path"],
            owner_user_id=owner_user_id,
            root_session_id=root_session_id,
            source="chat_artifact_delivery",
            content_hash=candidate.get("content_hash"),
            allow_owner_rebind=False,
            authority_state=authority_state if is_machine_runtime_actor(authority_user) else None,
        )

    for path in paths:
        if _safe_workspace_relative_path(path) is None:
            continue
        if isinstance(db, AsyncSession):
            try:
                if authority_error is not None:
                    raise authority_error
                if authority_user is None:
                    raise WorkspaceAuthorityError(
                        "artifact_source_principal_required", "Artifact publication requires the session's source reader.",
                    )
                if machine_scope is not None:
                    if not machine_scope.can_read(_safe_workspace_relative_path(path).as_posix()):
                        raise WorkspaceAuthorityError('workspace_resource_forbidden', 'This output is outside the machine work.')
                else:
                    await authorize_workspace_path(
                        db, authority_user, agent_id=agent_id, path=path, action="read",
                        path_exists=True, session_id=session_uuid, runtime_principal=runtime_principal,
                    )
            except (WorkspaceAuthorityError, HTTPException) as exc:
                if publication_errors is not None:
                    denied = not isinstance(exc, HTTPException) or exc.status_code in {401, 403, 404}
                    publication_errors.append({
                        "path": path, "status": "denied" if denied else "unavailable",
                        "reason_code": exc.code if isinstance(exc, WorkspaceAuthorityError) else "artifact_source_access_unavailable",
                        "retryable": False, "recovery_action": "request_source_read_access" if denied else "retry_artifact_publication",
                    })
                continue
        if rebind_existing_to_message and runtime_uuid is not None and isinstance(db, AsyncSession):
            handled, native_artifact = await _native_artifact_for_final_message(
                db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_uuid,
                runtime_task_id=runtime_uuid, path=_safe_workspace_relative_path(path).as_posix(),
            )
            if handled:
                if native_artifact is not None:
                    try:
                        async with tenant_scoped_session(tenant_id) as source_db:
                            await validate_artifact_content_receipt(source_db, tenant_id, authority_user, native_artifact,
                                runtime_principal=runtime_principal)
                        with captured_chat_artifact_file(native_artifact, workspace_root) as (captured, _source):
                            if captured is None:
                                native_artifact = None
                    except (OSError, WorkspaceAuthorityError):
                        native_artifact = None
                    except ContentInputReceiptError as exc:
                        record_source_error(path, exc)
                        continue
                if native_artifact is not None:
                    native_artifact.message_id = message_id
                    parts.append(artifact_part_from_model(native_artifact))
                elif publication_errors is not None:
                    publication_errors.append({"path": path, "status": "unknown",
                        "reason_code": "workspace_execution_unverified", "retryable": False,
                        "recovery_action": "reconcile_artifact_publication"})
                continue
        try:
            candidate = build_artifact_candidate(
                agent_id=agent_id,
                session_id=session_uuid,
                runtime_task_id=runtime_uuid,
                path=path,
                workspace_root=workspace_root,
                source=source,
                action=action,
                tool_call_id=tool_call_id,
                diff_summary=diff_summary,
                owner_agent_id=owner_agent_id,
                source_agent_id=source_agent_id,
                download_agent_id=download_agent_id,
                delivery_agent_id=delivery_agent_id,
                expected_content_hash=(expected_content_hashes or {}).get(_safe_workspace_relative_path(path).as_posix()),
                execution_evidence_ref=execution_evidence_ref,
                tool_invocation_id=tool_invocation_id,
                selection_model_result_id=selection_model_result_id,
            )
        except (OSError, WorkspaceAuthorityError):
            if publication_errors is None:
                raise
            publication_errors.append({
                "path": path, "status": "unknown", "reason_code": "artifact_capture_failed",
                "retryable": False, "recovery_action": "reconcile_artifact_publication",
            })
            continue
        if not candidate:
            continue
        if isinstance(db, AsyncSession):
            from app.models.organization_identity import ContentInputReceipt
            from app.services.content_input_receipts import latest_workspace_receipt, workspace_artifact_ref

            # Native outputs bind only their executor's receipt, never a later same-byte write.
            receipt = None
            if (source_receipt_ids or {}).get(candidate["path"]):
                receipt = await db.get(ContentInputReceipt, source_receipt_ids[candidate["path"]], populate_existing=True)
            elif tool_invocation_id:
                receipt = await db.get(ContentInputReceipt,
                    uuid.uuid5(uuid.UUID(str(tool_invocation_id)), f"workspace-content:{candidate['path']}"), populate_existing=True)
            elif candidate["path"].startswith("workspace/"):
                receipt = await latest_workspace_receipt(db, tenant_id, agent_id, candidate["path"])
            candidate["snapshot"]["content_input_receipt_id"] = str(receipt.id) if (
                receipt is not None and receipt.tenant_id == tenant_id and receipt.carrier_kind == "workspace_artifact"
                and receipt.carrier_ref == workspace_artifact_ref(agent_id, candidate["path"])
                and receipt.content_sha256 == candidate["content_hash"]) else None
            try:
                async with tenant_scoped_session(tenant_id) as source_db:
                    source_receipt = receipt if candidate["snapshot"]["content_input_receipt_id"] is not None else None
                    if runtime_principal is not None:
                        await validate_runtime_workspace_receipt(source_db, tenant_id, runtime_principal,
                            source_receipt, candidate["content_hash"])
                    else:
                        await validate_browser_workspace_receipt(source_db, tenant_id, authority_user,
                            source_receipt, candidate["content_hash"])
            except ContentInputReceiptError as exc:
                record_source_error(path, exc)
                continue
        dedupe_key = (candidate["path"], candidate["snapshot_hash"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        insert_result = await db.execute(
            _chat_artifact_insert_statement(
                candidate=candidate,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_uuid,
                message_id=message_id,
                runtime_task_id=runtime_uuid,
                source=source,
                owner_user_id=owner_user_id,
                root_session_id=root_session_id,
                authority_state=authority_state,
            )
        )
        inserted_id = insert_result.scalar_one_or_none()
        if inserted_id is None:
            existing = await _load_existing_chat_artifact(
                db=db,
                agent_id=agent_id,
                session_id=session_uuid,
                runtime_task_id=runtime_uuid,
                path=candidate["path"],
                snapshot_hash=candidate["snapshot_hash"],
            )
            if existing is None:
                if isinstance(db, AsyncSession):
                    continue
                existing = ChatArtifact(
                    id=uuid.UUID(str(candidate["artifact_id"])),
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    session_id=session_uuid,
                    message_id=message_id,
                    runtime_task_id=runtime_uuid,
                    owner_user_id=owner_user_id,
                    root_session_id=root_session_id,
                    authority_state=authority_state,
                    path=candidate["path"],
                    name=candidate["name"],
                    mime_type=candidate.get("mime_type"),
                    size=candidate.get("size"),
                    modified_at=candidate.get("modified_at"),
                    preview_kind=candidate.get("preview_kind", "download"),
                    source=candidate.get("source", source),
                    snapshot_hash=candidate["snapshot_hash"],
                    snapshot_json=candidate.get("snapshot"),
                )
                db.add(existing)
                await db.flush()
            if isinstance(db, AsyncSession):
                try:
                    async with tenant_scoped_session(tenant_id) as source_db:
                        await validate_artifact_content_receipt(source_db, tenant_id, authority_user, existing,
                            runtime_principal=runtime_principal)
                except ContentInputReceiptError as exc:
                    record_source_error(path, exc)
                    continue
            if rebind_existing_to_message and existing.message_id != message_id:
                # Move only after the captured version's sources have passed revalidation.
                existing.message_id = message_id
            part = artifact_part_from_model(existing)
            await _register_path(candidate)
            record_workspace_artifact_provenance(workspace_root, part)
            parts.append(part)
            continue
        candidate["artifact_id"] = str(inserted_id)
        candidate["id"] = str(inserted_id)
        await _register_path(candidate)
        record_workspace_artifact_provenance(workspace_root, candidate)
        parts.append(_artifact_part_from_candidate(candidate))
    return parts


async def materialize_declared_read_artifacts(db, *, task, model_result_id, paths):
    """Snapshot model-selected files from their exact consumed read, without claiming a write."""
    if not paths:
        return []
    from app.config import get_settings
    from app.models.invocation_span import InvocationSpan
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_model_inputs import _model_dependencies, _origin_dependencies
    from app.services.session_terminal_outcome import TerminalReadArtifactSelectionNeedsReconciliation
    from app.services.web_chat_runtime import _ensure_tool_result_artifact_message

    model = await db.get(SessionModelResult, model_result_id)
    dependencies, complete = _model_dependencies(model) if model else ([], False)
    if model is None or (model.tenant_id, model.session_id, model.run_id) != (
        task.tenant_id, uuid.UUID(task.parent_session_id), task.id):
        return []
    read_ids = []
    for dep in dependencies:
        if dep["source_kind"] == "session_tool_invocation":
            try:
                read_ids.append(uuid.UUID(dep["source_id"]))
            except ValueError:
                continue
    invocations = list(await db.scalars(select(SessionToolInvocation).where(
        SessionToolInvocation.id.in_(read_ids), SessionToolInvocation.tenant_id == task.tenant_id,
        SessionToolInvocation.session_id == model.session_id, SessionToolInvocation.run_id == task.id,
        SessionToolInvocation.tool_name.in_(("read_file", "fs_read", "read_document")),
        SessionToolInvocation.effect_state == "effect_committed")))
    parts = []
    attempted_paths = set()
    for invocation in invocations:
        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        path = _safe_workspace_relative_path(str(arguments.get("path") or ""))
        if path is None or path.as_posix() not in paths:
            continue
        if not complete:
            raise TerminalReadArtifactSelectionNeedsReconciliation("the file's selecting model has incomplete input sources")
        path = path.as_posix()
        attempted_paths.add(path)
        sources, bound = await _origin_dependencies(db, tenant_id=task.tenant_id, agent_id=task.parent_agent_id,
            ref=f"session-tool-result:{invocation.id}")
        if not bound or not any(dep in dependencies for dep in sources if dep["source_kind"] == "session_tool_invocation"
            and dep["source_id"] == str(invocation.id)):
            continue
        evidence_id = uuid.uuid5(invocation.id, "workspace-input-evidence")
        span = await db.get(InvocationSpan, evidence_id)
        proof = (span.metadata_json or {}).get("workspace_input_evidence", {}) if span else {}
        try:
            receipt_id = uuid.UUID(proof["content_input_receipt_id"])
        except (KeyError, ValueError, TypeError):
            continue
        receipt = await db.get(ContentInputReceipt, receipt_id)
        if receipt is None or {"source_kind": "workspace_artifact", "source_id": str(receipt_id),
            "source_version": receipt.content_sha256} not in sources:
            continue
        message_id = uuid.uuid5(invocation.id, "read-result-artifact-message")
        anchor = await _ensure_tool_result_artifact_message(db, message_id=message_id, agent_id=task.parent_agent_id,
            tenant_id=task.tenant_id, session_id=model.session_id, user_id=task.root_user_id,
            external_principal_id=task.root_external_principal_id, content=json.dumps({"tool_name": invocation.tool_name,
                "invocation_id": str(invocation.id), "path": path, "source_receipt_id": str(receipt_id)}))
        errors = []
        created = await create_chat_artifacts_for_message(db=db, agent_id=task.parent_agent_id,
            tenant_id=task.tenant_id, session_id=model.session_id, message_id=message_id, runtime_task_id=task.id,
            paths=[path], workspace_root=Path(get_settings().AGENT_DATA_DIR) / str(task.parent_agent_id),
            source="workspace_read", action="referenced", tool_call_id=invocation.provider_tool_use_id,
            source_receipt_ids={path: receipt_id}, expected_content_hashes={path: receipt.content_sha256},
            selection_model_result_id=model.id,
            execution_evidence_ref=f"workspace-input:{evidence_id}", tool_invocation_id=invocation.id,
            publication_errors=errors)
        parts.extend(created)
        if errors:
            task.metadata_json = {**(task.metadata_json or {}), "artifact_read_publication_errors": errors}
        if not created and not await db.scalar(select(ChatArtifact.id).where(ChatArtifact.message_id == message_id).limit(1)):
            await db.delete(anchor)
    if attempted_paths - {part["path"] for part in parts}:
        raise TerminalReadArtifactSelectionNeedsReconciliation("declared file read cannot be recovered for its original reader")
    return parts


async def create_or_bind_chat_session(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    runtime_source: str,
    actor_type: str,
    runtime_task_id: uuid.UUID | None = None,
    parent_session_id: uuid.UUID | None = None,
    root_session_id: uuid.UUID | None = None,
    external_conversation_id: str | None = None,
    source_channel: str = "web",
    title_seed: str | None = None,
    session_kind: str | None = None,
    visibility_scope: str | None = None,
    listed_surface: str | None = None,
) -> ChatSession:
    """Create or bind the canonical transcript session for an agent run."""
    ensure_agent_session_source(runtime_source)
    if runtime_task_id:
        result = await db.execute(
            select(ChatSession).where(ChatSession.agent_id == agent_id, ChatSession.runtime_task_id == runtime_task_id)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
    if external_conversation_id:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == agent_id,
                ChatSession.external_conv_id == external_conversation_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

    now = datetime.now(timezone.utc)
    session = ChatSession(
        agent_id=agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title=(title_seed or runtime_source or "Agent Run")[:200],
        source_channel=source_channel,
        external_conv_id=external_conversation_id,
        session_kind=session_kind or ("human_chat" if runtime_source in {"web_chat", "channel_chat"} else "user_task"),
        actor_type=actor_type,
        runtime_source=runtime_source,
        visibility_scope=visibility_scope or "direct_user",
        listed_surface=listed_surface or "chat",
        parent_session_id=parent_session_id,
        root_session_id=root_session_id or parent_session_id,
        runtime_task_id=runtime_task_id,
        transcript_metadata_json={
            "runtime_source": runtime_source,
            "actor_type": actor_type,
            "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        },
        created_at=now,
        last_message_at=now,
    )
    db.add(session)
    await db.flush()
    return session
