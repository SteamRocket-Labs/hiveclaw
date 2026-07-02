"""File management API routes for agent workspaces."""

import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File as FastFile, HTTPException, UploadFile as UploadFileType, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from app.models.user import User
from app.services.chat_artifact_delivery import read_chat_artifact_snapshot_content
from app.services.file_download_tokens import (
    InvalidChannelFileDownloadToken,
    NotChannelFileDownloadToken,
    verify_channel_file_download_token,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

settings = get_settings()
router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""


class FileContent(BaseModel):
    path: str
    content: str
    uses_snapshot: bool | None = None
    legacy_current_file_fallback: bool | None = None
    workspace_changed: bool | None = None
    snapshot_hash: str | None = None
    content_hash: str | None = None


class FileWrite(BaseModel):
    content: str


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
        "enterprise_info/ contains governed company knowledge; use enterprise knowledge APIs instead of raw file writes."
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


@router.get("/", response_model=list[FileInfo])
async def list_files(
    agent_id: uuid.UUID,
    path: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List files and directories in an agent's file system."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)

    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path is not a directory")

    items = []
    base_abs = _agent_base_dir(agent_id).resolve()
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if entry.name == ".gitkeep" or _is_hidden_browser_entry(entry):
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
            )
        )
    return items


@router.get("/content", response_model=FileContent)
async def read_file(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the content of a file."""
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)

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
        return FileContent(path=path, content=f"[二进制文件: {target.name}, {target.stat().st_size} bytes]")

    try:
        async with aiofiles.open(target, "r", encoding="utf-8") as f:
            content = await f.read()
    except (UnicodeDecodeError, ValueError):
        return FileContent(path=path, content=f"[二进制文件: {target.name}, {target.stat().st_size} bytes]")
    return FileContent(path=path, content=content)


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the delivery-time snapshot for a chat artifact."""
    await check_agent_access(db, current_user, agent_id)
    artifact = await _load_chat_artifact_or_404(db=db, agent_id=agent_id, artifact_id=artifact_id)
    content = read_chat_artifact_snapshot_content(artifact, _agent_base_dir(agent_id))
    return FileContent(**content)


@router.get("/download")
async def download_file(
    agent_id: uuid.UUID,
    path: str,
    token: str = "",
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
):
    """Download / serve a file from the agent workspace (browser-friendly).

    Auth via Bearer header, access-token query parameter, or scoped channel file token.
    """
    if token:
        try:
            verify_channel_file_download_token(token=token, agent_id=agent_id, path=path)
        except NotChannelFileDownloadToken:
            pass
        except InvalidChannelFileDownloadToken as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        else:
            if _is_governed_memory_path(path):
                _raise_raw_memory_read_guard()
            target = _safe_path(agent_id, path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            return FileResponse(path=str(target), filename=target.name)

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
    if access_level == "use" and _is_governed_memory_path(path):
        _raise_raw_memory_read_guard()
    target = _safe_path(agent_id, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    agent_id: uuid.UUID,
    artifact_id: uuid.UUID,
    token: str = "",
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
):
    """Download the delivery-time artifact snapshot, falling back only for legacy rows."""
    jwt_token = credentials.credentials if credentials else token
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user = await _load_download_user_from_jwt(db=db, jwt_token=jwt_token)

    await check_agent_access(db, user, agent_id)
    artifact = await _load_chat_artifact_or_404(db=db, agent_id=agent_id, artifact_id=artifact_id)
    snapshot = artifact.snapshot_json or {}
    storage_rel = str(snapshot.get("snapshot_storage_path") or "").strip()
    target: Path | None = None
    if storage_rel:
        candidate = _safe_agent_relative_path(agent_id, storage_rel)
        if candidate.exists() and candidate.is_file():
            target = candidate
    if target is None:
        target = _safe_path(agent_id, artifact.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact file not found")
    return FileResponse(path=str(target), filename=artifact.name or target.name)


@router.put("/content")
async def write_file(
    agent_id: uuid.UUID,
    path: str,
    data: FileWrite,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write content to a file (create or overwrite)."""
    await check_agent_access(db, current_user, agent_id)
    _raise_managed_path_write_guard(path)
    target = _safe_path(agent_id, path)

    target.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(target, "w", encoding="utf-8") as f:
        await f.write(data.content)

    return {"status": "ok", "path": path}


@router.delete("/content")
async def delete_file(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a file."""
    await check_agent_access(db, current_user, agent_id)
    _raise_managed_path_write_guard(path)
    target = _safe_path(agent_id, path)

    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()

    return {"status": "ok", "path": path}


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
    await check_agent_access(db, current_user, agent_id)

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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a binary file to agent workspace."""
    await check_agent_access(db, current_user, agent_id)

    # Validate path prefix
    if not path.startswith("workspace/"):
        raise HTTPException(status_code=400, detail="只能上传到 workspace/ 目录；skills/ 需要走 Skill Gate")

    base = _agent_base_dir(agent_id)
    target_dir = (base / path).resolve()
    try:
        target_dir.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    target_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "unnamed"
    # Sanitize filename
    filename = filename.replace("/", "_").replace("\\", "_")
    _raise_upload_path_guard(path, filename)
    save_path = target_dir / filename

    content = await file.read()
    save_path.write_bytes(content)

    # Auto-extract text from non-text files
    extracted_path = None
    from app.services.text_extractor import needs_extraction, save_extracted_text

    if needs_extraction(filename):
        txt_file = save_extracted_text(save_path, content, filename)
        if txt_file:
            base_abs = base.resolve()
            extracted_path = str(txt_file.resolve().relative_to(base_abs))

    return {
        "status": "ok",
        "path": f"{path}/{filename}",
        "filename": filename,
        "size": len(content),
        "extracted_text_path": extracted_path,
    }


# ─── Enterprise Knowledge Base ─────────────────────────────────

enterprise_kb_router = APIRouter(prefix="/enterprise/knowledge-base", tags=["enterprise"])


@enterprise_kb_router.get("/openviking-status")
async def openviking_status(current_user: User = Depends(get_current_user)):
    """Check OpenViking connection status for the KB status indicator."""
    from app.services.viking_client import is_configured, _get_client

    if not is_configured():
        return {"connected": False, "reason": "not_configured"}
    client = _get_client()
    if not client:
        return {"connected": False, "reason": "client_error"}
    try:
        resp = await client.get("/health", timeout=5.0)
        if resp.status_code == 200:
            return {"connected": True, "version": resp.json().get("version", "unknown")}
        return {"connected": False, "reason": f"status_{resp.status_code}"}
    except Exception as e:
        return {"connected": False, "reason": str(e)[:100]}


def _enterprise_kb_dir(tenant_id: str) -> Path:
    return Path(settings.AGENT_DATA_DIR) / f"enterprise_info_{tenant_id}" / "knowledge_base"


def _enterprise_info_dir(tenant_id: str) -> Path:
    return Path(settings.AGENT_DATA_DIR) / f"enterprise_info_{tenant_id}"


@enterprise_kb_router.get("/files")
async def list_enterprise_kb_files(
    path: str = "",
    current_user: User = Depends(get_current_user),
):
    """List files in enterprise knowledge base (tenant-scoped)."""
    if not current_user.tenant_id:
        return []
    info_dir = _enterprise_info_dir(str(current_user.tenant_id)).resolve()
    info_dir.mkdir(parents=True, exist_ok=True)

    if path:
        target = (info_dir / path).resolve()
    else:
        target = info_dir
    if not _is_within_path(target, info_dir):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not target.exists() or not target.is_dir():
        return []

    items = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if entry.name == ".gitkeep":
            continue
        rel = str(entry.resolve().relative_to(info_dir.resolve()))
        stat = entry.stat()
        items.append(
            {
                "name": entry.name,
                "path": rel,
                "is_dir": entry.is_dir(),
                "size": stat.st_size if entry.is_file() else 0,
            }
        )
    return items


@enterprise_kb_router.post("/upload")
async def upload_enterprise_kb_file(
    file: UploadFileType = FastFile(...),
    sub_path: str = "",
    current_user: User = Depends(get_current_user),
):
    """Upload a file to enterprise knowledge base (tenant-scoped)."""
    # Only admin can upload to enterprise KB
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can upload to enterprise knowledge base")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")

    info_dir = _enterprise_info_dir(str(current_user.tenant_id))
    target_dir = (info_dir / sub_path).resolve()
    if not _is_within_path(target_dir, info_dir):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    target_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "unnamed"
    filename = filename.replace("/", "_").replace("\\", "_")
    save_path = target_dir / filename

    content = await file.read()
    save_path.write_bytes(content)

    # Auto-extract text from non-text files
    extracted_path = None
    from app.services.text_extractor import needs_extraction, save_extracted_text

    if needs_extraction(filename):
        txt_file = save_extracted_text(save_path, content, filename)
        if txt_file:
            extracted_path = str(txt_file.resolve().relative_to(info_dir.resolve()))

    # Auto-index in OpenViking (fire-and-forget, non-blocking)
    try:
        from app.services.viking_client import is_configured, add_resource

        if is_configured() and current_user.tenant_id:
            text_content = content.decode("utf-8", errors="ignore") if not needs_extraction(filename) else ""
            if extracted_path:
                txt_path = info_dir / extracted_path
                text_content = txt_path.read_text(errors="ignore") if txt_path.exists() else text_content
            if text_content.strip():
                import asyncio

                uri = (
                    f"viking://enterprise/knowledge_base/{sub_path}/{filename}"
                    if sub_path
                    else f"viking://enterprise/knowledge_base/{filename}"
                )
                asyncio.create_task(
                    add_resource(
                        content=text_content[:50000],
                        to=uri,
                        tenant_id=str(current_user.tenant_id),
                        user_id=str(current_user.id),
                        acl={"tenant_ids": [str(current_user.tenant_id)], "scope": "tenant"},
                        metadata={
                            "source_type": "enterprise_knowledge_base",
                            "path": f"{sub_path}/{filename}" if sub_path else filename,
                        },
                        reason=f"Enterprise KB upload: {filename}",
                    )
                )
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("OpenViking auto-index skipped: %s", e)

    return {
        "status": "ok",
        "path": f"{sub_path}/{filename}" if sub_path else filename,
        "filename": filename,
        "size": len(content),
        "extracted_text_path": extracted_path,
    }


@enterprise_kb_router.get("/content")
async def read_enterprise_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    """Read content of an enterprise knowledge base file (tenant-scoped)."""
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    info_dir = _enterprise_info_dir(str(current_user.tenant_id))
    target = (info_dir / path).resolve()
    if not _is_within_path(target, info_dir):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": content}
    except Exception:
        return {"path": path, "content": f"[二进制文件: {target.name}, {target.stat().st_size} bytes]"}


@enterprise_kb_router.put("/content")
async def write_enterprise_file(
    path: str,
    data: FileWrite,
    current_user: User = Depends(get_current_user),
):
    """Write content to an enterprise file (tenant-scoped)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can edit enterprise knowledge base")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")

    info_dir = _enterprise_info_dir(str(current_user.tenant_id))
    target = (info_dir / path).resolve()
    if not _is_within_path(target, info_dir):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    target.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(target, "w", encoding="utf-8") as f:
        await f.write(data.content)
    return {"status": "ok", "path": path}


@enterprise_kb_router.delete("/content")
async def delete_enterprise_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an enterprise knowledge base file (tenant-scoped)."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can delete enterprise knowledge base files")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")

    info_dir = _enterprise_info_dir(str(current_user.tenant_id))
    target = (info_dir / path).resolve()
    if not _is_within_path(target, info_dir):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "ok", "path": path}


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
        _fetch_github_directory,
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

    # 2. Fetch files from GitHub
    github_path = f"skills/{handle}/{slug}"
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    files = await _fetch_github_directory("openclaw", "skills", github_path, "main", token)
    from app.services.skill_installation import install_active_skill_package

    try:
        install_result = install_active_skill_package(
            workspace=base,
            folder_name=folder_name,
            files=files,
            source=f"agent_clawhub:{slug}",
            overwrite=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "skill_name": skill_info.get("displayName", slug),
        "folder_name": folder_name,
        "files_written": install_result["files_written"],
        "files": install_result["files"],
        "skill_guard": install_result["skill_guard"],
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

    from app.api.skills import _parse_github_url, _fetch_github_directory, _get_github_token

    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]
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
    files = await _fetch_github_directory(owner, repo, path, branch, token)
    if not files:
        raise HTTPException(404, "No files found")
    from app.services.skill_installation import install_active_skill_package

    try:
        install_result = install_active_skill_package(
            workspace=base,
            folder_name=folder_name,
            files=files,
            source=body.url,
            overwrite=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "folder_name": folder_name,
        "files_written": install_result["files_written"],
        "files": install_result["files"],
        "skill_guard": install_result["skill_guard"],
    }
