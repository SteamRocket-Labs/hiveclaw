"""Single-path chat transcript artifact delivery helpers."""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
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
    snapshot = {
        "exists": True,
        "size": stat_data.st_size,
        "mtime_ns": stat_data.st_mtime_ns,
        "path": rel.as_posix(),
        "revision_id": snapshot_hash,
        "action": action,
        "tool_call_id": str(tool_call_id) if tool_call_id else None,
        "diff_summary": diff_summary,
    }
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
        "preview_kind": _preview_kind_for_path(rel),
        "source": source,
        "snapshot_hash": snapshot_hash,
        "snapshot": snapshot,
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
) -> list[dict[str, Any]]:
    """Build artifact-delivery parts for safe workspace paths WITHOUT DB rows.

    ``ChatArtifact.message_id`` is non-nullable, so producers that have no chat
    message (e.g. a Deep Research workflow run) cannot persist artifact rows.
    They still need the report to surface as a clickable timeline artifact, so
    this returns transcript-ready parts using the same mechanical path-safety
    and preview-kind decisions as :func:`build_artifact_candidate`.
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
        )
        if not candidate:
            continue
        dedupe_key = (candidate["path"], candidate["snapshot_hash"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
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
        "created_at": candidate.get("created_at"),
        "revision_id": candidate.get("revision_id"),
        "action": candidate.get("action"),
        "tool_call_id": candidate.get("tool_call_id"),
        "diff_summary": candidate.get("diff_summary"),
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
        "created_at": artifact.created_at.isoformat() if getattr(artifact, "created_at", None) else None,
        "revision_id": snapshot.get("revision_id") or artifact.snapshot_hash,
        "action": snapshot.get("action"),
        "tool_call_id": snapshot.get("tool_call_id"),
        "diff_summary": snapshot.get("diff_summary"),
    }


def create_chat_artifacts_for_message(
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
        )
        if not candidate:
            continue
        dedupe_key = (candidate["path"], candidate["snapshot_hash"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        artifact_id = uuid.UUID(str(candidate["artifact_id"]))
        db.add(
            ChatArtifact(
                id=artifact_id,
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
        )
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
