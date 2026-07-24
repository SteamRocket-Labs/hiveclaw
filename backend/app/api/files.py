"""File management API routes for agent workspaces."""

import asyncio
import uuid
from pathlib import Path
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, Depends, File as FastFile, HTTPException, Query, UploadFile as UploadFileType, status
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask

from app.config import get_settings
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.resource_authority import authorize_resource_action
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.audit import AuditLog
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_artifact_delivery import (
    read_chat_artifact_snapshot_content,
    resolve_chat_artifact_file,
)
from app.services.file_download_tokens import (
    ChannelFileContentChanged,
    InvalidChannelFileDownloadToken,
    NotChannelFileDownloadToken,
    snapshot_channel_file_content,
    verify_channel_file_download_token,
)
from app.services.external_capabilities.skill_source_adapter import stage_remote_external_skill_source_review
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

settings = get_settings()
router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""
    authority_source: str | None = None
    operator_view: bool = False


class FileContent(BaseModel):
    path: str
    content: str
    uses_snapshot: bool | None = None
    legacy_current_file_fallback: bool | None = None
    workspace_changed: bool | None = None
    snapshot_hash: str | None = None
    content_hash: str | None = None
    authority_source: str | None = None
    operator_view: bool = False


class FileWrite(BaseModel):
    content: str


class FileVersionCurrentState(BaseModel):
    exists: bool
    content_hash: str | None = None
    size: int = 0


class FileVersionSummary(BaseModel):
    version_id: str
    created_at: str
    state: str
    size: int
    content_hash: str | None = None
    restorable: bool


class FileVersionPage(BaseModel):
    path: str
    current: FileVersionCurrentState
    versions: list[FileVersionSummary]
    total: int
    offset: int
    limit: int
    has_more: bool
    coverage_complete: bool


class FileVersionContent(BaseModel):
    path: str
    version_id: str
    state: str
    content: str | None = None
    content_hash: str | None = None
    size: int
    is_binary: bool = False


class FileVersionRestoreRequest(BaseModel):
    expected_current_exists: bool
    expected_current_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_expected_state(self):
        if self.expected_current_exists and self.expected_current_hash is None:
            raise ValueError("expected_current_hash is required when the current file exists")
        if not self.expected_current_exists and self.expected_current_hash is not None:
            raise ValueError("expected_current_hash must be null when the current file does not exist")
        return self


class FileVersionRestoreResult(BaseModel):
    status: str
    path: str
    version_id: str
    current: FileVersionCurrentState


_MAX_VERSION_SESSION_SCAN = 500


def _agent_base_dir(agent_id: uuid.UUID) -> Path:
    return Path(settings.AGENT_DATA_DIR) / str(agent_id)


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _safe_path(agent_id: uuid.UUID, rel_path: str) -> Path:
    """Ensure the path is within the agent's directory (no path traversal)."""
    base = _agent_base_dir(agent_id)
    full = (base / rel_path).resolve()
    if not _is_within_path(full, base):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")
    return full


def _safe_agent_relative_path(agent_id: uuid.UUID, rel_path: str) -> Path:
    base = _agent_base_dir(agent_id)
    full = (base / rel_path).resolve()
    if not _is_within_path(full, base):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")
    return full


def _normalized_rel_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _is_governed_memory_path(path: str) -> bool:
    normalized = _normalized_rel_path(path)
    return normalized == "memory" or normalized.startswith("memory/")


def _is_user_workspace_path(path: str) -> bool:
    normalized = _normalized_rel_path(path)
    return normalized == "workspace" or normalized.startswith("workspace/")


def _require_workspace_file_path(path: str) -> str:
    normalized = _normalized_rel_path(path)
    if not normalized.startswith("workspace/") or normalized == "workspace":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "workspace_file_path_required", "message": "Choose one file below workspace/."},
        )
    return normalized


def _workspace_authority_http_error(exc: Exception) -> HTTPException:
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    if not isinstance(exc, WorkspaceAuthorityError):
        return HTTPException(status_code=403, detail=str(exc))
    status_code = 404 if exc.code == "workspace_resource_not_found" else 403
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _managed_system_path_message(path: str) -> str | None:
    normalized = _normalized_rel_path(path)
    if _is_governed_memory_path(normalized):
        return "memory/ is governed by the Memory Control Plane; use memory APIs instead of raw file writes."
    top_level = normalized.split("/", 1)[0]
    messages = {
        "logs": "logs/ is managed by platform services; raw file writes and deletes are not allowed.",
        "evolution": (
            "evolution/ is managed by platform services; use Skill Candidate Packages and governed APIs "
            "instead of raw file writes."
        ),
        "runtime_artifacts": (
            "runtime_artifacts/ is managed by platform services; raw file writes and deletes are not allowed."
        ),
    }
    return messages.get(top_level)


_ROOT_FILE_WRITE_ALLOWLIST: set[str] = set()
_ROOT_PREFIX_WRITE_ALLOWLIST = {"workspace", "skills"}
_ENTERPRISE_ASSET_PREFIX_MESSAGES = {
    "subagents": "subagents/ contains enterprise Sub-agent assets; use governed Sub-agent APIs instead of raw file writes.",
    "enterprise_info": (
        "enterprise_info/ is generated read-only company context; only company_profile.md and org_structure.md are exposed."
    ),
}
_ROOT_MANAGED_FILE_MESSAGES = {
    "soul.md": (
        "soul.md is governed by Dream/Soul promotion; direct file API writes are refused. "
        "Dream must produce soul.md.next and the promotion gate performs the audited commit."
    ),
    "HEARTBEAT.md": "HEARTBEAT.md is a platform template; heartbeat protocol updates must ship through system templates.",
    "DREAM.md": "DREAM.md is a platform template; dream protocol updates must ship through system templates.",
    "state.json": "state.json is a retired legacy runtime snapshot; runtime state belongs under runtime_artifacts/.",
    "tasks.json": "tasks.json is a read-only DB Task snapshot; use task APIs or Work Ledger tools instead.",
}


def _root_write_guard_message(path: str) -> str | None:
    normalized = _normalized_rel_path(path)
    if not normalized:
        return "Missing file path. Write deliverables under workspace/."
    if normalized in _ROOT_FILE_WRITE_ALLOWLIST:
        return None
    managed_file_message = _ROOT_MANAGED_FILE_MESSAGES.get(normalized)
    if managed_file_message:
        return managed_file_message
    top_level = normalized.split("/", 1)[0]
    enterprise_asset_message = _ENTERPRISE_ASSET_PREFIX_MESSAGES.get(top_level)
    if enterprise_asset_message:
        return enterprise_asset_message
    if "/" in normalized and top_level in _ROOT_PREFIX_WRITE_ALLOWLIST:
        return None
    if "/" not in normalized:
        return "Top-level work files are not allowed. Write deliverables under workspace/."
    if top_level not in _ROOT_PREFIX_WRITE_ALLOWLIST and not _managed_system_path_message(normalized):
        return f"{top_level}/ is not a writable agent file namespace."
    return None


def _skill_package_path_guard_message(path: str, *, operation: str) -> str | None:
    normalized = _normalized_rel_path(path)
    if not normalized.startswith("skills/"):
        return None
    del operation
    return (
        "Active skill packages are governed by Skill promotion. Direct file API writes, edits, uploads, "
        "and deletes under skills/ are refused; use save_skill to submit an activation candidate, or let "
        "Skill Distiller promote a verified SKILL.md.draft through Platform Skill Gate."
    )


def _raise_managed_path_write_guard(path: str) -> None:
    message = _managed_system_path_message(path)
    if message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
    root_message = _root_write_guard_message(path)
    if root_message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=root_message)
    skill_message = _skill_package_path_guard_message(path, operation="write")
    if skill_message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=skill_message)


def _raise_upload_path_guard(path: str, filename: str) -> None:
    normalized_dir = _normalized_rel_path(path)
    target = f"{normalized_dir.rstrip('/')}/{filename}" if normalized_dir else filename
    _raise_managed_path_write_guard(target)


def _is_hidden_browser_entry(entry: Path) -> bool:
    return entry.name.startswith(".")


def _raise_memory_write_guard() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="memory/ is governed by the Memory Control Plane; use memory APIs instead of raw file writes.",
    )


def _raise_raw_memory_read_guard() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Raw memory files require manage access; use the Knowledge read model for governed memory reads.",
    )


def _raise_raw_system_read_guard(path: str, *, access_level: str) -> None:
    """Agent use permits execution, not browsing internal Agent state."""

    normalized = _normalized_rel_path(path)
    from app.services.workspace_resource_authority import is_recovery_manifest_storage_path

    if is_recovery_manifest_storage_path(normalized):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Recovery Manifest storage is available only through its governed context resource.",
        )
    if not normalized:
        return
    top_level = normalized.split("/", 1)[0]
    if top_level in {"runtime_artifacts", "logs"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runtime artifacts and logs are available only through their governed read models.",
        )
    if access_level != "use" or _is_user_workspace_path(normalized):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "Raw Agent system files are not part of Agent use access. Use the scoped Knowledge, "
            "RuntimeTask, Skill, Activity, or Evolution read model instead."
        ),
    )


async def _authorized_workspace_version_sessions(
    *,
    agent_id: uuid.UUID,
    path: str,
    action: str,
    operator_view: bool,
    operator_reason: str | None,
    current_user: User,
    db: AsyncSession,
    for_update: bool = False,
):
    normalized = _require_workspace_file_path(path)
    agent_access = await check_agent_access(db, current_user, agent_id)
    target = _safe_path(agent_id, normalized)
    from app.services.workspace_resource_authority import authorize_workspace_path

    try:
        decision = await authorize_workspace_path(
            db,
            current_user,
            agent_id=agent_id,
            path=normalized,
            action=action,
            path_exists=target.is_file(),
            allow_manager_override=operator_view,
            manager_override_reason=operator_reason,
            for_update=for_update,
            agent_access=agent_access,
        )
    except Exception as exc:
        raise _workspace_authority_http_error(exc) from exc

    query = select(ChatSession).where(
        ChatSession.agent_id == agent_id,
        ChatSession.transcript_metadata_json.is_not(None),
    )
    if decision.root_session_id is not None:
        query = query.where(
            or_(
                ChatSession.id == decision.root_session_id,
                ChatSession.root_session_id == decision.root_session_id,
            )
        )
    elif not decision.operator_view:
        query = query.where(ChatSession.user_id == (decision.owner_user_id or current_user.id))
    query = query.order_by(ChatSession.created_at.desc()).limit(_MAX_VERSION_SESSION_SCAN + 1)
    sessions = (await db.execute(query)).scalars().all()
    coverage_complete = len(sessions) <= _MAX_VERSION_SESSION_SCAN
    return agent_access, decision, list(sessions[:_MAX_VERSION_SESSION_SCAN]), coverage_complete


def _version_summary(record) -> FileVersionSummary:
    return FileVersionSummary(
        version_id=record.version_id,
        created_at=record.created_at,
        state=record.state,
        size=record.size,
        content_hash=record.content_hash,
        restorable=record.restorable,
    )


def _current_file_version_state(agent_id: uuid.UUID, path: str) -> dict:
    from app.services.session_workspace_snapshot import workspace_file_state

    try:
        return workspace_file_state(agent_id=agent_id, path=path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workspace_file_version_unavailable", "message": str(exc)},
        ) from exc


@router.get("/", response_model=list[FileInfo])
async def list_files(
    agent_id: uuid.UUID,
    path: str = "",
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List files and directories in an agent's file system."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    _raise_raw_system_read_guard(path, access_level=access_level)
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)

    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path is not a directory")

    scope = None
    if _is_user_workspace_path(path):
        from app.services.workspace_resource_authority import load_workspace_authority_scope

        try:
            scope = await load_workspace_authority_scope(
                db,
                current_user,
                agent_id=agent_id,
                operator_view=operator_view,
                operator_reason=operator_reason,
                agent_access=(_agent, access_level),
            )
        except Exception as exc:
            raise _workspace_authority_http_error(exc) from exc

    items = []
    base_abs = _agent_base_dir(agent_id).resolve()
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if entry.name == ".gitkeep" or _is_hidden_browser_entry(entry):
            continue
        if access_level == "use" and not _normalized_rel_path(path) and entry.name != "workspace":
            continue
        if scope is not None and not scope.visible_child(path, entry.name, is_dir=entry.is_dir()):
            continue
        rel = str(entry.resolve().relative_to(base_abs))
        stat = entry.stat()
        items.append(
            FileInfo(
                name=entry.name,
                path=rel,
                is_dir=entry.is_dir(),
                size=stat.st_size if entry.is_file() else 0,
                modified_at=str(stat.st_mtime),
                authority_source=scope.authority_source if scope is not None else None,
                operator_view=bool(scope and scope.operator_view),
            )
        )
    return items


@router.get("/content", response_model=FileContent)
async def read_file(
    agent_id: uuid.UUID,
    path: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the content of a file."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    _raise_raw_system_read_guard(path, access_level=access_level)
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)
    resource_decision = None
    if _is_user_workspace_path(path):
        from app.services.workspace_resource_authority import authorize_workspace_path

        try:
            resource_decision = await authorize_workspace_path(
                db,
                current_user,
                agent_id=agent_id,
                path=path,
                action="read",
                path_exists=target.exists(),
                allow_manager_override=operator_view,
                manager_override_reason=operator_reason,
                agent_access=(_agent, access_level),
            )
        except Exception as exc:
            raise _workspace_authority_http_error(exc) from exc

    if not target.exists() or not target.is_file():
        # Known agent files return empty content instead of 404
        _known_files = {"soul.md", "HEARTBEAT.md"}
        if path in _known_files:
            return FileContent(path=path, content="")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    _BINARY_EXTS = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".mp4",
        ".wav",
        ".sqlite",
        ".db",
        ".bin",
        ".pyc",
        ".pyo",
    }
    if target.suffix.lower() in _BINARY_EXTS:
        return FileContent(
            path=path,
            content=f"[二进制文件: {target.name}, {target.stat().st_size} bytes]",
            authority_source=resource_decision.authority_source if resource_decision else None,
            operator_view=bool(resource_decision and resource_decision.operator_view),
        )

    try:
        async with aiofiles.open(target, "r", encoding="utf-8") as f:
            content = await f.read()
    except (UnicodeDecodeError, ValueError):
        return FileContent(
            path=path,
            content=f"[二进制文件: {target.name}, {target.stat().st_size} bytes]",
            authority_source=resource_decision.authority_source if resource_decision else None,
            operator_view=bool(resource_decision and resource_decision.operator_view),
        )
    return FileContent(
        path=path,
        content=content,
        authority_source=resource_decision.authority_source if resource_decision else None,
        operator_view=bool(resource_decision and resource_decision.operator_view),
    )


@router.get("/versions", response_model=FileVersionPage)
async def list_file_versions(
    agent_id: uuid.UUID,
    path: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List authorized, checkpoint-backed versions for one workspace file."""
    normalized = _require_workspace_file_path(path)
    _agent_access, _decision, sessions, coverage_complete = await _authorized_workspace_version_sessions(
        agent_id=agent_id,
        path=normalized,
        action="read",
        operator_view=operator_view,
        operator_reason=operator_reason,
        current_user=current_user,
        db=db,
    )
    from app.services.workspace_file_versions import collect_workspace_file_versions

    versions = await asyncio.to_thread(
        collect_workspace_file_versions,
        agent_id=agent_id,
        path=normalized,
        sessions=sessions,
    )
    page = versions[offset : offset + limit]
    current = _current_file_version_state(agent_id, normalized)
    return FileVersionPage(
        path=normalized,
        current=FileVersionCurrentState(
            exists=bool(current.get("exists")),
            content_hash=current.get("sha256"),
            size=int(current.get("size") or 0),
        ),
        versions=[_version_summary(record) for record in page],
        total=len(versions),
        offset=offset,
        limit=limit,
        has_more=offset + len(page) < len(versions),
        coverage_complete=coverage_complete,
    )


@router.get("/versions/{version_id}/content", response_model=FileVersionContent)
async def read_file_version(
    agent_id: uuid.UUID,
    version_id: str,
    path: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the exact bytes of one authorized checkpoint version when textual."""
    normalized = _require_workspace_file_path(path)
    _agent_access, _decision, sessions, _coverage_complete = await _authorized_workspace_version_sessions(
        agent_id=agent_id,
        path=normalized,
        action="read",
        operator_view=operator_view,
        operator_reason=operator_reason,
        current_user=current_user,
        db=db,
    )
    from app.services.workspace_file_versions import (
        WorkspaceFileVersionNotFound,
        WorkspaceFileVersionUnavailable,
        read_workspace_file_version,
    )

    try:
        version = await asyncio.to_thread(
            read_workspace_file_version,
            agent_id=agent_id,
            path=normalized,
            version_id=version_id,
            sessions=sessions,
        )
    except WorkspaceFileVersionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File version not found") from exc
    except WorkspaceFileVersionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workspace_file_version_unavailable", "message": str(exc)},
        ) from exc

    content: str | None = None
    is_binary = False
    if version.content is not None:
        try:
            content = version.content.decode("utf-8")
            is_binary = "\x00" in content
            if is_binary:
                content = None
        except UnicodeDecodeError:
            is_binary = True
    return FileVersionContent(
        path=normalized,
        version_id=version.version_id,
        state=version.state,
        content=content,
        content_hash=version.content_hash,
        size=version.size,
        is_binary=is_binary,
    )


@router.get("/versions/{version_id}/download")
async def download_file_version(
    agent_id: uuid.UUID,
    version_id: str,
    path: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download an authorized checkpoint file without exposing snapshot paths."""
    normalized = _require_workspace_file_path(path)
    _agent_access, _decision, sessions, _coverage_complete = await _authorized_workspace_version_sessions(
        agent_id=agent_id,
        path=normalized,
        action="read",
        operator_view=operator_view,
        operator_reason=operator_reason,
        current_user=current_user,
        db=db,
    )
    from app.services.workspace_file_versions import (
        WorkspaceFileVersionNotFound,
        WorkspaceFileVersionUnavailable,
        read_workspace_file_version,
    )

    try:
        version = await asyncio.to_thread(
            read_workspace_file_version,
            agent_id=agent_id,
            path=normalized,
            version_id=version_id,
            sessions=sessions,
        )
    except WorkspaceFileVersionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File version not found") from exc
    except WorkspaceFileVersionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workspace_file_version_unavailable", "message": str(exc)},
        ) from exc
    if version.state != "available" or version.content is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workspace_file_version_unavailable", "message": "This version has no downloadable file."},
        )
    filename = quote(Path(normalized).name, safe="")
    return Response(
        content=version.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/versions/{version_id}/restore", response_model=FileVersionRestoreResult)
async def restore_file_version(
    agent_id: uuid.UUID,
    version_id: str,
    path: str,
    data: FileVersionRestoreRequest,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restore one file from a checkpoint with exact-current-state protection."""
    normalized = _require_workspace_file_path(path)
    from app.services.session_workspace_snapshot import (
        async_agent_workspace_lock,
        finalize_workspace_restore_locked,
        restore_workspace_snapshot_locked,
    )
    from app.services.workspace_file_versions import (
        WorkspaceFileVersionNotFound,
        resolve_workspace_file_version,
    )
    from app.services.workspace_resource_authority import (
        authorize_workspace_path,
        mark_workspace_path_deleted,
        register_workspace_path,
    )

    async with async_agent_workspace_lock(agent_id):
        agent_access, _read_decision, sessions, _coverage_complete = await _authorized_workspace_version_sessions(
            agent_id=agent_id,
            path=normalized,
            action="read",
            operator_view=operator_view,
            operator_reason=operator_reason,
            current_user=current_user,
            db=db,
            for_update=True,
        )
        agent = agent_access[0]
        try:
            version = await asyncio.to_thread(
                resolve_workspace_file_version,
                agent_id=agent_id,
                path=normalized,
                version_id=version_id,
                sessions=sessions,
            )
        except WorkspaceFileVersionNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File version not found") from exc
        if not version.restorable or version.state == "unavailable":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "workspace_file_version_unavailable",
                    "message": "This checkpoint is incomplete or no longer verifiable.",
                },
            )

        current = _current_file_version_state(agent_id, normalized)
        expected_signature = (data.expected_current_exists, data.expected_current_hash)
        actual_signature = (bool(current.get("exists")), current.get("sha256"))
        if actual_signature != expected_signature:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "workspace_file_version_conflict",
                    "message": "The file changed after version history was opened. Refresh before restoring.",
                },
            )

        try:
            mutation_decision = await authorize_workspace_path(
                db,
                current_user,
                agent_id=agent_id,
                path=normalized,
                action="delete" if version.state == "deleted" else "write",
                path_exists=bool(current.get("exists")),
                allow_manager_override=operator_view,
                manager_override_reason=operator_reason,
                for_update=True,
                agent_access=agent_access,
            )
        except Exception as exc:
            raise _workspace_authority_http_error(exc) from exc

        restore = await asyncio.to_thread(
            restore_workspace_snapshot_locked,
            agent_id=agent_id,
            snapshot=version.snapshot,
            restore_paths=[normalized],
            expected_current_states={normalized: current},
            require_lineage=False,
            defer_finalize=True,
        )
        if not restore.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "workspace_file_version_restore_failed",
                    "message": restore.error or "The file version could not be restored.",
                },
            )

        transaction_id = str(restore.transaction_id or "")
        target_state = _current_file_version_state(agent_id, normalized)
        try:
            if target_state.get("exists"):
                await register_workspace_path(
                    db,
                    tenant_id=agent.tenant_id,
                    agent_id=agent_id,
                    path=normalized,
                    owner_user_id=mutation_decision.owner_user_id or current_user.id,
                    root_session_id=mutation_decision.root_session_id,
                    source="workspace_file_version_restore",
                    content_hash=target_state.get("sha256"),
                    allow_owner_rebind=False,
                )
            else:
                await mark_workspace_path_deleted(db, agent_id=agent_id, path=normalized)
            db.add(
                AuditLog(
                    tenant_id=agent.tenant_id,
                    user_id=current_user.id,
                    agent_id=agent_id,
                    action="workspace_file_version_restored",
                    details={
                        "path": normalized,
                        "version_id": version.version_id,
                        "transaction_id": transaction_id or None,
                        "previous_exists": bool(current.get("exists")),
                        "previous_content_hash": current.get("sha256"),
                        "target_exists": bool(target_state.get("exists")),
                        "target_content_hash": target_state.get("sha256"),
                        "authority_source": mutation_decision.authority_source,
                        "operator_view": bool(mutation_decision.operator_view),
                    },
                )
            )
            await db.flush()
            await db.commit()
        except BaseException:
            await db.rollback()
            if transaction_id:
                await asyncio.to_thread(
                    finalize_workspace_restore_locked,
                    agent_id=agent_id,
                    transaction_id=transaction_id,
                    commit=False,
                )
            raise

        if transaction_id:
            finalized = await asyncio.to_thread(
                finalize_workspace_restore_locked,
                agent_id=agent_id,
                transaction_id=transaction_id,
                commit=True,
            )
            if not finalized:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "code": "workspace_file_version_restore_needs_reconciliation",
                        "message": "The restore was committed and requires startup reconciliation.",
                    },
                )
        return FileVersionRestoreResult(
            status="unchanged" if not transaction_id else "restored",
            path=normalized,
            version_id=version.version_id,
            current=FileVersionCurrentState(
                exists=bool(target_state.get("exists")),
                content_hash=target_state.get("sha256"),
                size=int(target_state.get("size") or 0),
            ),
        )


async def _load_chat_artifact_or_404(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> ChatArtifact:
    result = await db.execute(
        select(ChatArtifact).where(ChatArtifact.id == artifact_id, ChatArtifact.agent_id == agent_id).limit(1)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return artifact


async def _load_download_user_from_jwt(*, db: AsyncSession, jwt_token: str) -> User:
    """Authenticate browser-friendly download URLs before tenant-scoped file reads.

    TenantMiddleware only sees Authorization headers. Direct browser downloads
    carry JWTs in the query string, so the endpoint must pin RLS from the token
    before loading the user row.
    """
    from app.core.security import decode_access_token

    payload = decode_access_token(jwt_token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    token_tenant_id = payload.get("tid")
    if token_tenant_id:
        try:
            await pin_rls_tenant_context(db, token_tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    try:
        user_uuid = uuid.UUID(str(user_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


@router.get("/artifacts/{artifact_id}/content", response_model=FileContent)
async def read_artifact_content(
    agent_id: uuid.UUID,
    artifact_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the delivery-time snapshot for a chat artifact."""
    agent_access = await check_agent_access(db, current_user, agent_id)
    artifact = await _load_chat_artifact_or_404(db=db, agent_id=agent_id, artifact_id=artifact_id)
    decision = await authorize_resource_action(
        db,
        current_user,
        agent_id=agent_id,
        resource_kind="chat_artifact",
        resource_id=artifact.id,
        action="read",
        owner_user_id=getattr(artifact, "owner_user_id", None),
        root_session_id=getattr(artifact, "root_session_id", None) or getattr(artifact, "session_id", None),
        authority_state=getattr(artifact, "authority_state", None) or "quarantined",
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
        agent_access=agent_access,
    )
    content = read_chat_artifact_snapshot_content(artifact, _agent_base_dir(agent_id))
    content["authority_source"] = decision.authority_source
    content["operator_view"] = decision.operator_view
    return FileContent(**content)


@router.get("/download")
async def download_file(
    agent_id: uuid.UUID,
    path: str,
    token: str = "",
    operator_view: bool = False,
    operator_reason: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
):
    """Download / serve a file from the agent workspace (browser-friendly).

    Auth via Bearer header, access-token query parameter, or scoped channel file token.
    """
    if token:
        try:
            channel_token = verify_channel_file_download_token(
                token=token,
                agent_id=agent_id,
                path=path,
            )
        except NotChannelFileDownloadToken:
            pass
        except InvalidChannelFileDownloadToken as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        else:
            if not _normalized_rel_path(path).startswith("workspace/"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Scoped channel file tokens may download only workspace/ deliverables.",
                )
            if _is_governed_memory_path(path):
                _raise_raw_memory_read_guard()
            target = _safe_path(agent_id, path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            try:
                snapshot_dir, delivery_path = snapshot_channel_file_content(
                    path=target,
                    payload=channel_token,
                )
            except (ChannelFileContentChanged, OSError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="File changed after channel delivery authorization",
                ) from exc
            return FileResponse(
                path=str(delivery_path),
                filename=target.name,
                background=(BackgroundTask(snapshot_dir.cleanup) if snapshot_dir is not None else None),
            )

    # Resolve JWT token from either Bearer header or query param
    jwt_token = None
    if credentials:
        jwt_token = credentials.credentials
    elif token:
        jwt_token = token

    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    user = await _load_download_user_from_jwt(db=db, jwt_token=jwt_token)

    _agent, access_level = await check_agent_access(db, user, agent_id)
    _raise_raw_system_read_guard(path, access_level=access_level)
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)
    if _is_user_workspace_path(path):
        from app.services.workspace_resource_authority import authorize_workspace_path

        try:
            await authorize_workspace_path(
                db,
                user,
                agent_id=agent_id,
                path=path,
                action="read",
                path_exists=target.exists(),
                allow_manager_override=operator_view,
                manager_override_reason=operator_reason,
                agent_access=(_agent, access_level),
            )
        except Exception as exc:
            raise _workspace_authority_http_error(exc) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    agent_id: uuid.UUID,
    artifact_id: uuid.UUID,
    token: str = "",
    operator_view: bool = False,
    operator_reason: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
):
    """Download the delivery-time artifact snapshot, falling back only for legacy rows."""
    jwt_token = credentials.credentials if credentials else token
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user = await _load_download_user_from_jwt(db=db, jwt_token=jwt_token)

    agent_access = await check_agent_access(db, user, agent_id)
    artifact = await _load_chat_artifact_or_404(db=db, agent_id=agent_id, artifact_id=artifact_id)
    await authorize_resource_action(
        db,
        user,
        agent_id=agent_id,
        resource_kind="chat_artifact",
        resource_id=artifact.id,
        action="read",
        owner_user_id=getattr(artifact, "owner_user_id", None),
        root_session_id=getattr(artifact, "root_session_id", None) or getattr(artifact, "session_id", None),
        authority_state=getattr(artifact, "authority_state", None) or "quarantined",
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
        agent_access=agent_access,
    )
    target, artifact_source = resolve_chat_artifact_file(artifact, _agent_base_dir(agent_id))
    if target is None:
        detail = (
            "Artifact delivery snapshot is no longer available"
            if artifact_source == "missing_delivery_snapshot"
            else "Artifact file not found"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return FileResponse(path=str(target), filename=artifact.name or target.name)


@router.put("/content")
async def write_file(
    agent_id: uuid.UUID,
    path: str,
    data: FileWrite,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write content to a file (create or overwrite)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    _raise_managed_path_write_guard(path)
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    async with async_agent_workspace_lock(agent_id):
        target = _safe_path(agent_id, path)
        resource_decision = None
        if _is_user_workspace_path(path):
            from app.services.workspace_resource_authority import authorize_workspace_path

            try:
                resource_decision = await authorize_workspace_path(
                    db,
                    current_user,
                    agent_id=agent_id,
                    path=path,
                    action="write",
                    path_exists=target.exists(),
                    allow_manager_override=operator_view,
                    manager_override_reason=operator_reason,
                    for_update=True,
                    agent_access=(agent, _access),
                )
            except Exception as exc:
                raise _workspace_authority_http_error(exc) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "w", encoding="utf-8") as f:
            await f.write(data.content)
        if resource_decision is not None:
            import hashlib

            from app.services.workspace_resource_authority import register_workspace_path

            await register_workspace_path(
                db,
                tenant_id=agent.tenant_id,
                agent_id=agent_id,
                path=path,
                owner_user_id=resource_decision.owner_user_id or current_user.id,
                root_session_id=resource_decision.root_session_id,
                source="workspace_api_write",
                content_hash=hashlib.sha256(data.content.encode("utf-8")).hexdigest(),
                allow_owner_rebind=resource_decision.operator_view and resource_decision.manifest is None,
            )

    return {
        "status": "ok",
        "path": path,
        "authority_source": resource_decision.authority_source if resource_decision else None,
        "operator_view": bool(resource_decision and resource_decision.operator_view),
    }


@router.delete("/content")
async def delete_file(
    agent_id: uuid.UUID,
    path: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a file."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    _raise_managed_path_write_guard(path)
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    async with async_agent_workspace_lock(agent_id):
        target = _safe_path(agent_id, path)
        if not target.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

        resource_decisions = []
        if _is_user_workspace_path(path):
            from app.services.workspace_resource_authority import authorize_workspace_path

            candidate_paths = (
                [
                    str(item.resolve().relative_to(_agent_base_dir(agent_id).resolve()))
                    for item in target.rglob("*")
                    if item.is_file()
                ]
                if target.is_dir()
                else [path]
            )
            if not candidate_paths and target.is_dir():
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "workspace_empty_directory_unowned",
                        "message": "Empty legacy directories have no owner and require Operator View.",
                    },
                )
            for candidate_path in candidate_paths:
                try:
                    resource_decisions.append(
                        (
                            candidate_path,
                            await authorize_workspace_path(
                                db,
                                current_user,
                                agent_id=agent_id,
                                path=candidate_path,
                                action="delete",
                                path_exists=True,
                                allow_manager_override=operator_view,
                                manager_override_reason=operator_reason,
                                for_update=True,
                                agent_access=(agent, _access_level),
                            ),
                        )
                    )
                except Exception as exc:
                    raise _workspace_authority_http_error(exc) from exc

        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()

        from app.services.workspace_resource_authority import mark_workspace_path_deleted

        for candidate_path, _decision in resource_decisions:
            await mark_workspace_path_deleted(db, agent_id=agent_id, path=candidate_path)

    return {
        "status": "ok",
        "path": path,
        "authority_source": resource_decisions[0][1].authority_source if resource_decisions else None,
        "operator_view": any(decision.operator_view for _path, decision in resource_decisions),
    }


class ImportSkillBody(BaseModel):
    skill_id: str


@router.post("/import-skill")
async def import_skill_to_agent(
    agent_id: uuid.UUID,
    body: ImportSkillBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a global skill into this agent's skills/ workspace folder.

    Copies all files from the global skill registry into
    <agent_workspace>/skills/<folder_name>/.
    """
    await require_agent_manage_access(db, current_user, agent_id)

    from sqlalchemy.orm import selectinload
    from app.models.skill import Skill

    # Load the global skill with its files
    result = await db.execute(select(Skill).where(Skill.id == body.skill_id).options(selectinload(Skill.files)))
    skill = result.scalar_one_or_none()
    from app.api.skills import _skill_visible_to_user

    if not skill or not _skill_visible_to_user(skill, current_user):
        raise HTTPException(status_code=404, detail="Skill not found")

    if not skill.files:
        raise HTTPException(status_code=400, detail="Skill has no files")

    base = _agent_base_dir(agent_id)
    from app.services.skill_installation import install_active_skill_package

    try:
        install_result = install_active_skill_package(
            workspace=base,
            folder_name=skill.folder_name,
            files=[{"path": f.path, "content": f.content} for f in skill.files],
            source=f"registry_skill:{skill.id}",
            overwrite=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "skill_name": skill.name,
        "folder_name": skill.folder_name,
        "files_written": install_result["files_written"],
        "files": install_result["files"],
        "skill_guard": install_result["skill_guard"],
    }


upload_router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])


@upload_router.post("/upload")
async def upload_file_to_workspace(
    agent_id: uuid.UUID,
    file: UploadFileType = FastFile(...),
    path: str = "workspace/knowledge_base",
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a binary file to agent workspace."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    # Validate path prefix
    if not path.startswith("workspace/"):
        raise HTTPException(status_code=400, detail="只能上传到 workspace/ 目录；skills/ 需要走 Skill Gate")

    base = _agent_base_dir(agent_id)
    target_dir = (base / path).resolve()
    try:
        target_dir.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    filename = file.filename or "unnamed"
    # Sanitize filename
    filename = filename.replace("/", "_").replace("\\", "_")
    _raise_upload_path_guard(path, filename)
    save_path = target_dir / filename

    content = await file.read()
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    async with async_agent_workspace_lock(agent_id):
        from app.services.workspace_resource_authority import authorize_workspace_path, register_workspace_path

        relative_save_path = f"{path.rstrip('/')}/{filename}"
        try:
            resource_decision = await authorize_workspace_path(
                db,
                current_user,
                agent_id=agent_id,
                path=relative_save_path,
                action="upload",
                path_exists=save_path.exists(),
                allow_manager_override=operator_view,
                manager_override_reason=operator_reason,
                for_update=True,
                agent_access=(agent, _access),
            )
        except Exception as exc:
            raise _workspace_authority_http_error(exc) from exc
        target_dir.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(content)

        import hashlib

        await register_workspace_path(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            path=relative_save_path,
            owner_user_id=resource_decision.owner_user_id or current_user.id,
            root_session_id=resource_decision.root_session_id,
            source="workspace_api_upload",
            content_hash=hashlib.sha256(content).hexdigest(),
            allow_owner_rebind=resource_decision.operator_view and resource_decision.manifest is None,
        )

        # Auto-extract text from non-text files
        extracted_path = None
        from app.services.text_extractor import needs_extraction, save_extracted_text

        if needs_extraction(filename):
            txt_file = save_extracted_text(save_path, content, filename)
            if txt_file:
                base_abs = base.resolve()
                extracted_path = str(txt_file.resolve().relative_to(base_abs))
                extracted_content = txt_file.read_bytes()
                await register_workspace_path(
                    db,
                    tenant_id=agent.tenant_id,
                    agent_id=agent_id,
                    path=extracted_path,
                    owner_user_id=resource_decision.owner_user_id or current_user.id,
                    root_session_id=resource_decision.root_session_id,
                    source="workspace_api_extraction",
                    content_hash=hashlib.sha256(extracted_content).hexdigest(),
                    allow_owner_rebind=False,
                )

    return {
        "status": "ok",
        "path": f"{path}/{filename}",
        "filename": filename,
        "size": len(content),
        "extracted_text_path": extracted_path,
        "authority_source": resource_decision.authority_source,
        "operator_view": resource_decision.operator_view,
    }


# ─── Agent-level ClawHub / URL Skill Import ─────────────────


class ClawhubImportBody(BaseModel):
    slug: str


class UrlImportBody(BaseModel):
    url: str


@router.post("/import-from-clawhub")
async def agent_import_from_clawhub(
    agent_id: uuid.UUID,
    body: ClawhubImportBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a skill from ClawHub directly into this agent's skills/ workspace."""
    await check_agent_access(db, current_user, agent_id)

    from app.api.skills import (
        CLAWHUB_BASE,
        _get_github_token,
    )
    import httpx

    slug = body.slug
    base = _agent_base_dir(agent_id)
    folder_name = slug
    existing_skill_md = base / "skills" / folder_name / "SKILL.md"
    if existing_skill_md.exists():
        return {
            "status": "already_installed",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
        }

    # 1. Fetch metadata from ClawHub
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{CLAWHUB_BASE}/v1/skills/{slug}")
            if resp.status_code == 429:
                raise HTTPException(429, "ClawHub rate limit exceeded. Please wait and try again.")
            if resp.status_code != 200:
                raise HTTPException(502, f"ClawHub API error: {resp.status_code}")
            meta = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to connect to ClawHub: {e}")

    skill_info = meta.get("skill", {})
    owner_info = meta.get("owner", {})
    handle = owner_info.get("handle", "").lower()
    if not handle:
        raise HTTPException(400, "Could not determine skill owner from ClawHub metadata")

    # 2. Stage GitHub archive through the materializer boundary
    github_path = f"skills/{handle}/{slug}"
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    review_result = await stage_remote_external_skill_source_review(
        db,
        tenant_id=current_user.tenant_id,
        created_by_user_id=current_user.id,
        source_uri=f"clawhub:{slug}",
        fetch_uri=f"https://github.com/openclaw/skills/tree/main/{github_path}",
        folder_name=folder_name,
        source_format="clawhub_skill",
        token=token,
    )

    return {
        **review_result,
        "skill_name": skill_info.get("displayName", slug),
    }


@router.post("/import-from-url")
async def agent_import_from_url(
    agent_id: uuid.UUID,
    body: UrlImportBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a skill from a GitHub URL directly into this agent's skills/ workspace."""
    await check_agent_access(db, current_user, agent_id)

    from app.api.skills import _parse_github_url, _get_github_token

    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL")

    repo, path = parsed["repo"], parsed["path"]
    # Derive folder name
    folder_name = path.rstrip("/").split("/")[-1] if path else repo
    base = _agent_base_dir(agent_id)
    existing_skill_md = base / "skills" / folder_name / "SKILL.md"
    if existing_skill_md.exists():
        return {
            "status": "already_installed",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
        }

    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    return await stage_remote_external_skill_source_review(
        db,
        tenant_id=current_user.tenant_id,
        created_by_user_id=current_user.id,
        source_uri=body.url,
        folder_name=folder_name,
        source_format="external_skill_url",
        token=token,
    )
