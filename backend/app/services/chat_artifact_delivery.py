"""Single-path chat transcript artifact delivery helpers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
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


def tool_session_write_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Return user-facing workspace artifact paths written by a tool call."""
    if tool_name in ("write_file", "edit_file", "fs_write"):
        path = args.get("path")
        return [str(path)] if path else []
    if tool_name == "office_document_create":
        path = args.get("path")
        return [str(path)] if path else []
    if tool_name == "office_document_apply":
        path = args.get("output_path") or args.get("path")
        return [str(path)] if path else []
    return []


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


def _snapshot_hash(*, rel_path: PurePosixPath, stat_data: Any | None) -> str:
    payload = f"{rel_path.as_posix()}:{getattr(stat_data, 'st_size', '')}:{getattr(stat_data, 'st_mtime_ns', '')}"
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
    result = await db.execute(select(ChatArtifact.snapshot_json).where(ChatArtifact.agent_id == agent_id))
    snapshot_payloads = result.scalars().all()
    referenced_paths: list[str] = []
    for snapshot in snapshot_payloads:
        if isinstance(snapshot, dict):
            storage_path = str(snapshot.get("snapshot_storage_path") or "").strip()
            if storage_path:
                referenced_paths.append(storage_path)
    return cleanup_chat_artifact_snapshots(
        workspace_root=workspace_root,
        referenced_snapshot_paths=referenced_paths,
        retention_days=retention_days,
        now=now,
    )


def _write_content_snapshot(
    *,
    source_path: Path,
    workspace_root: Path,
    session_id: str | uuid.UUID,
    runtime_task_id: str | uuid.UUID | None,
    snapshot_hash: str,
    rel_path: PurePosixPath,
) -> str:
    storage_rel = _snapshot_storage_rel_path(
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        snapshot_hash=snapshot_hash,
        rel_path=rel_path,
    )
    target = workspace_root / storage_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(source_path.read_bytes())
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
    try:
        current_hash = _snapshot_hash(rel_path=rel, stat_data=current.stat())
    except OSError:
        return True
    return current_hash != getattr(artifact, "snapshot_hash", None)


def read_chat_artifact_snapshot_content(artifact: ChatArtifact, workspace_root: Path) -> dict[str, Any]:
    """Read the delivery-time artifact snapshot, falling back explicitly for legacy rows."""
    snapshot = artifact.snapshot_json or {}
    storage_rel = str(snapshot.get("snapshot_storage_path") or "").strip()
    workspace_root = workspace_root.resolve()
    if storage_rel:
        snapshot_path = (workspace_root / storage_rel).resolve()
        try:
            snapshot_path.relative_to(workspace_root)
        except ValueError:
            snapshot_path = workspace_root / "__invalid_snapshot_path__"
        if snapshot_path.exists() and snapshot_path.is_file():
            return {
                "path": artifact.path,
                "content": _read_text_or_binary_marker(snapshot_path),
                "uses_snapshot": True,
                "legacy_current_file_fallback": False,
                "workspace_changed": _workspace_changed(artifact, workspace_root),
                "snapshot_hash": artifact.snapshot_hash,
                "content_hash": snapshot.get("content_hash"),
            }

    current = (workspace_root / str(artifact.path or "")).resolve()
    try:
        current.relative_to(workspace_root)
    except ValueError:
        current = workspace_root / "__invalid_current_path__"
    if current.exists() and current.is_file():
        return {
            "path": artifact.path,
            "content": _read_text_or_binary_marker(current),
            "uses_snapshot": False,
            "legacy_current_file_fallback": True,
            "workspace_changed": None,
            "snapshot_hash": artifact.snapshot_hash,
            "content_hash": snapshot.get("content_hash"),
        }
    return {
        "path": artifact.path,
        "content": "",
        "uses_snapshot": False,
        "legacy_current_file_fallback": True,
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
) -> dict[str, Any] | None:
    """Return a durable artifact payload for a safe workspace file path.

    This is deliberately mechanical. It decides path safety and preview type;
    semantic explanation remains the assistant's job.
    """
    rel = _safe_workspace_relative_path(path)
    if rel is None:
        return None

    root = workspace_root.resolve()
    absolute = (root / rel.as_posix()).resolve()
    try:
        absolute.relative_to(root)
    except ValueError:
        return None
    if not absolute.exists() or not absolute.is_file():
        return None

    stat_data = absolute.stat()
    mime_type, _encoding = mimetypes.guess_type(rel.name)
    modified_at = datetime.fromtimestamp(stat_data.st_mtime, tz=timezone.utc).isoformat()
    snapshot_hash = _snapshot_hash(rel_path=rel, stat_data=stat_data)
    preview_kind = _preview_kind_for_path(rel)
    preview_snapshot_content, preview_snapshot_truncated = _read_preview_snapshot(absolute, preview_kind)
    content_hash = _content_hash(absolute)
    snapshot_storage_path = _write_content_snapshot(
        source_path=absolute,
        workspace_root=root,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        snapshot_hash=snapshot_hash,
        rel_path=rel,
    )
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
) -> list[dict[str, Any]]:
    """Create artifact rows for safe candidate paths and return message parts."""
    parts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    session_uuid = uuid.UUID(str(session_id))
    runtime_uuid = uuid.UUID(str(runtime_task_id)) if runtime_task_id else None
    for path in paths:
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
        )
        if not candidate:
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
            part = artifact_part_from_model(existing)
            record_workspace_artifact_provenance(workspace_root, part)
            parts.append(part)
            continue
        candidate["artifact_id"] = str(inserted_id)
        candidate["id"] = str(inserted_id)
        record_workspace_artifact_provenance(workspace_root, candidate)
        parts.append(_artifact_part_from_candidate(candidate))
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
