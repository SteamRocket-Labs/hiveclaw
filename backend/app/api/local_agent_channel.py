"""Local Agent Channel API.

This router is the long-lived channel surface for bound local bridge runners.
It intentionally lives beside, not inside, the legacy gateway poll API.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.local_bridge import get_bridge_auth_context
from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import local_agent_channel_service as channel_service
from app.services.local_bridge_service import BridgeAuthContext

router = APIRouter(tags=["local-agent-channel"])
settings = get_settings()


class WsTicketOut(BaseModel):
    ticket: str
    expires_in: int
    single_use: bool = True


class LocalAgentChannelSessionIn(BaseModel):
    source: str = Field(default="web", min_length=1, max_length=32)
    title: str | None = Field(default=None, max_length=160)


class LocalAgentChannelSessionOut(BaseModel):
    id: uuid.UUID
    chat_session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    title: str | None = None
    source: str
    source_channel: str = "local_agent"
    session_kind: str = "local_agent_channel"
    status: str
    created_at: Any | None = None
    updated_at: Any | None = None
    last_message_at: Any | None = None


class LocalAgentChannelTimelineOut(BaseModel):
    session: LocalAgentChannelSessionOut
    events: list[dict[str, Any]]
    next_cursor: int = 0


class LocalAgentChannelMessageIn(BaseModel):
    content: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class LocalAgentChannelMessageOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    status: str
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    request_hash: str | None = None
    capability_snapshot_hash: str | None = None
    replay_key: str = ""
    receipt: dict[str, Any] | None = None
    created_at: Any | None = None


class LocalAgentChannelEventIn(BaseModel):
    session_id: uuid.UUID
    message_id: uuid.UUID | None = None
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class LocalAgentChannelResultIn(BaseModel):
    session_id: uuid.UUID
    message_id: uuid.UUID
    status: str = Field(default="completed")
    output: str = Field(default="")
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalAgentWorkspaceFileOut(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""


class LocalAgentWorkspaceContentOut(BaseModel):
    path: str
    content: str


class LocalAgentWorkspaceUploadOut(BaseModel):
    filename: str
    saved_filename: str
    size: int
    workspace_path: str
    extracted_text: str = ""
    preview_text: str = ""
    conversion: dict[str, Any] | None = None
    is_image: bool = False
    image_data_url: str = ""


_LOCAL_WORKSPACE_BINARY_EXTS = {
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


def _coerce_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _serialize_session(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "id": str(payload["id"]),
        "chat_session_id": str(payload["chat_session_id"]) if payload.get("chat_session_id") else None,
        "agent_id": str(payload["agent_id"]) if payload.get("agent_id") else None,
        "created_at": payload.get("created_at").isoformat() if payload.get("created_at") else None,
        "updated_at": payload.get("updated_at").isoformat() if payload.get("updated_at") else None,
        "last_message_at": payload.get("last_message_at").isoformat() if payload.get("last_message_at") else None,
    }


def _serialize_message(payload: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in payload.items() if key != "event"}
    return {
        **payload,
        "id": str(payload["id"]),
        "session_id": str(payload["session_id"]),
    }


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _local_agent_workspace_base_for(tenant_id: Any, user_id: Any) -> Path:
    tenant_segment = str(tenant_id or "no_tenant")
    user_segment = str(user_id or "anonymous")
    return Path(settings.AGENT_DATA_DIR) / "local_agents" / tenant_segment / "users" / user_segment


def _local_agent_workspace_base(current_user: User) -> Path:
    return _local_agent_workspace_base_for(
        getattr(current_user, "tenant_id", None),
        getattr(current_user, "id", None),
    )


def _safe_local_agent_workspace_path_for(tenant_id: Any, user_id: Any, rel_path: str | None) -> tuple[Path, Path]:
    base = _local_agent_workspace_base_for(tenant_id, user_id).resolve()
    workspace_root = (base / "workspace").resolve()
    normalized = str(rel_path or "workspace").replace("\\", "/").strip().lstrip("/")
    if not normalized:
        normalized = "workspace"
    target = (base / normalized).resolve()
    if not _is_within_path(target, workspace_root):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")
    return base, target


def _safe_local_agent_workspace_path(current_user: User, rel_path: str | None) -> tuple[Path, Path]:
    return _safe_local_agent_workspace_path_for(
        getattr(current_user, "tenant_id", None),
        getattr(current_user, "id", None),
        rel_path,
    )


def _workspace_rel_path(base: Path, target: Path) -> str:
    return str(target.resolve().relative_to(base.resolve())).replace("\\", "/")


def _safe_upload_filename(filename: str | None) -> str:
    normalized = str(filename or "").replace("\\", "/").strip()
    safe_name = Path(normalized).name
    if safe_name in {"", ".", ".."}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    return safe_name


def _non_overwriting_path(directory: Path, filename: str) -> Path:
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def _materialize_local_agent_attachments_for_host(
    *,
    tenant_id: Any,
    actor_user_id: uuid.UUID,
    host_owner_user_id: uuid.UUID,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Make browser-uploaded files readable by the host user's local runner."""

    if actor_user_id == host_owner_user_id:
        return list(attachments)

    materialized: list[dict[str, Any]] = []
    for raw_attachment in attachments:
        if not isinstance(raw_attachment, dict):
            materialized.append(raw_attachment)
            continue
        attachment = dict(raw_attachment)
        workspace_path = str(attachment.get("path") or attachment.get("workspace_path") or "").strip()
        if not workspace_path:
            materialized.append(attachment)
            continue

        _actor_base, actor_file = _safe_local_agent_workspace_path_for(tenant_id, actor_user_id, workspace_path)
        if not actor_file.exists() or not actor_file.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment file not found")

        filename = _safe_upload_filename(
            str(attachment.get("filename") or attachment.get("saved_filename") or actor_file.name)
        )
        host_base, host_dir = _safe_local_agent_workspace_path_for(
            tenant_id,
            host_owner_user_id,
            f"workspace/shared_uploads/{actor_user_id}",
        )
        host_dir.mkdir(parents=True, exist_ok=True)
        host_file = _non_overwriting_path(host_dir, filename)
        shutil.copy2(actor_file, host_file)
        host_workspace_path = _workspace_rel_path(host_base, host_file)

        attachment.update(
            {
                "path": host_workspace_path,
                "workspace_path": host_workspace_path,
                "source_workspace_path": workspace_path,
                "source_user_id": str(actor_user_id),
                "materialized_for_user_id": str(host_owner_user_id),
            }
        )
        materialized.append(attachment)
    return materialized


def _local_agent_host_user_id(agent: Any) -> uuid.UUID:
    if getattr(agent, "agent_type", None) != "local_agent":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent is not a local agent")
    host_user_id = getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)
    if host_user_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Local agent has no host owner")
    return _coerce_uuid(host_user_id)


class LocalAgentChannelConnectionManager:
    """Best-effort in-process fanout for online local runners."""

    def __init__(self) -> None:
        self._by_user: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, *, context: BridgeAuthContext, websocket: WebSocket) -> None:
        async with self._lock:
            self._by_user.setdefault(str(context.user_id), []).append(websocket)

    async def disconnect(self, *, context: BridgeAuthContext, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._by_user.get(str(context.user_id), [])
            self._by_user[str(context.user_id)] = [item for item in sockets if item is not websocket]

    async def send_to_user(self, user_id: uuid.UUID, payload: dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._by_user.get(str(user_id), []))
        dead: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(jsonable_encoder(payload))
            except Exception:
                dead.append(socket)
        if dead:
            async with self._lock:
                current = self._by_user.get(str(user_id), [])
                self._by_user[str(user_id)] = [item for item in current if item not in dead]

    async def send_to_agent(self, agent_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """Compatibility no-op surface for old callers.

        Local Agent Channel fanout is user-scoped now. New code must call
        `send_to_user`.
        """
        logger.debug("[LocalAgentChannel] ignored legacy agent fanout for {}", agent_id)


channel_ws_manager = LocalAgentChannelConnectionManager()


class LocalAgentBrowserChannelManager:
    """Best-effort in-process fanout for browser Local Agent Channel pages."""

    def __init__(self) -> None:
        self._by_session: dict[tuple[str, str], list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, *, owner_user_id: uuid.UUID, session_id: uuid.UUID, websocket: WebSocket) -> None:
        async with self._lock:
            self._by_session.setdefault((str(owner_user_id), str(session_id)), []).append(websocket)

    async def disconnect(self, *, owner_user_id: uuid.UUID, session_id: uuid.UUID, websocket: WebSocket) -> None:
        key = (str(owner_user_id), str(session_id))
        async with self._lock:
            sockets = self._by_session.get(key, [])
            self._by_session[key] = [item for item in sockets if item is not websocket]

    async def send_to_session(self, owner_user_id: uuid.UUID, session_id: uuid.UUID, payload: dict[str, Any]) -> None:
        key = (str(owner_user_id), str(session_id))
        async with self._lock:
            sockets = list(self._by_session.get(key, []))
        dead: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(jsonable_encoder(payload))
            except Exception:
                dead.append(socket)
        if dead:
            async with self._lock:
                current = self._by_session.get(key, [])
                self._by_session[key] = [item for item in current if item not in dead]


browser_channel_ws_manager = LocalAgentBrowserChannelManager()


def _runner_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "event"}


async def _broadcast_browser_channel_event(
    *, owner_user_id: uuid.UUID, session_id: uuid.UUID, event: dict[str, Any] | None
) -> None:
    if not event:
        return
    await browser_channel_ws_manager.send_to_session(
        owner_user_id,
        session_id,
        {"type": "event", "event": event},
    )


@router.post("/local-bridge/channel/ws-ticket", response_model=WsTicketOut, status_code=201)
async def create_local_agent_channel_ws_ticket(
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.create_ws_ticket(
        db, context=context, ttl_seconds=channel_service.DEFAULT_WS_TICKET_SECONDS
    )


@router.post(
    "/local-agents/sessions/default",
    response_model=LocalAgentChannelSessionOut,
)
async def get_or_create_current_user_default_local_agent_channel_session(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    payload = await channel_service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=current_user.id,
    )
    return _serialize_session(payload)


@router.post(
    "/local-agents/sessions",
    response_model=LocalAgentChannelSessionOut,
    status_code=201,
)
async def create_current_user_local_agent_channel_session(
    body: LocalAgentChannelSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current user has no tenant")
    payload = await channel_service.create_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=current_user.id,
        source_agent_id=None,
        source=body.source,
        title=body.title,
    )
    return _serialize_session(payload)


@router.post(
    "/local-agents/sessions/{session_id}/messages",
    response_model=LocalAgentChannelMessageOut,
    status_code=201,
)
async def create_current_user_local_agent_channel_message(
    session_id: uuid.UUID,
    body: LocalAgentChannelMessageIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = await channel_service.enqueue_channel_message(
        db,
        session_id=session_id,
        owner_user_id=current_user.id,
        sender_user_id=current_user.id,
        sender_agent_id=None,
        content=body.content,
        attachments=body.attachments,
        metadata={**body.metadata, "source": "local_agents_page"},
        idempotency_key=body.idempotency_key,
    )
    await channel_ws_manager.send_to_user(
        current_user.id, {"type": "message", "message": _runner_message_payload(payload)}
    )
    await _broadcast_browser_channel_event(
        owner_user_id=current_user.id,
        session_id=session_id,
        event=payload.get("event"),
    )
    return _serialize_message(payload)


@router.get("/local-agents/sessions/{session_id}/timeline", response_model=LocalAgentChannelTimelineOut)
async def get_current_user_local_agent_channel_timeline(
    session_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session, owner_user_id = await channel_service.get_channel_session_for_actor(
        db, session_id=session_id, actor_user_id=current_user.id
    )
    events = await channel_service.list_channel_events(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return {
        "session": _serialize_session(session),
        "events": events,
        "next_cursor": events[-1]["sequence"] if events else after_sequence,
    }


@router.post("/local-agents/sessions/{session_id}/ws-ticket", response_model=WsTicketOut, status_code=201)
async def create_current_user_local_agent_channel_browser_ws_ticket(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await channel_service.create_browser_session_ws_ticket(
        db,
        tenant_id=getattr(current_user, "tenant_id", None),
        actor_user_id=current_user.id,
        session_id=session_id,
        ttl_seconds=channel_service.DEFAULT_BROWSER_WS_TICKET_SECONDS,
    )


@router.get("/local-agents/sessions/{session_id}/events")
async def list_current_user_local_agent_channel_events(
    session_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _session, owner_user_id = await channel_service.get_channel_session_for_actor(
        db, session_id=session_id, actor_user_id=current_user.id
    )
    events = await channel_service.list_channel_events(
        db,
        session_id=session_id,
        owner_user_id=owner_user_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return {
        "events": events,
        "next_cursor": events[-1]["sequence"] if events else after_sequence,
    }


@router.get("/local-agents/workspace/files", response_model=list[LocalAgentWorkspaceFileOut])
async def list_current_user_local_agent_workspace_files(
    path: str = Query(default="workspace"),
    current_user: User = Depends(get_current_user),
):
    base, target = _safe_local_agent_workspace_path(current_user, path)
    workspace_root = (base / "workspace").resolve()
    if not target.exists() and target == workspace_root:
        target.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path is not a directory")

    items: list[LocalAgentWorkspaceFileOut] = []
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
        if entry.name == ".gitkeep":
            continue
        stat = entry.stat()
        items.append(
            LocalAgentWorkspaceFileOut(
                name=entry.name,
                path=_workspace_rel_path(base, entry),
                is_dir=entry.is_dir(),
                size=stat.st_size if entry.is_file() else 0,
                modified_at=str(stat.st_mtime),
            )
        )
    return items


@router.get("/local-agents/workspace/content", response_model=LocalAgentWorkspaceContentOut)
async def read_current_user_local_agent_workspace_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    base, target = _safe_local_agent_workspace_path(current_user, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    rel_path = _workspace_rel_path(base, target)
    if target.suffix.lower() in _LOCAL_WORKSPACE_BINARY_EXTS:
        return LocalAgentWorkspaceContentOut(
            path=rel_path,
            content=f"[二进制文件: {target.name}, {target.stat().st_size} bytes]",
        )
    try:
        async with aiofiles.open(target, "r", encoding="utf-8") as file:
            content = await file.read()
    except (UnicodeDecodeError, ValueError):
        content = f"[二进制文件: {target.name}, {target.stat().st_size} bytes]"
    return LocalAgentWorkspaceContentOut(path=rel_path, content=content)


@router.get("/local-agents/workspace/download")
async def download_current_user_local_agent_workspace_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    _base, target = _safe_local_agent_workspace_path(current_user, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@router.post("/local-agents/workspace/upload", response_model=LocalAgentWorkspaceUploadOut)
async def upload_current_user_local_agent_workspace_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    filename = _safe_upload_filename(file.filename)
    base, uploads_dir = _safe_local_agent_workspace_path(current_user, "workspace/uploads")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    save_path = _non_overwriting_path(uploads_dir, filename)
    content = await file.read()
    save_path.write_bytes(content)

    preview_text = ""
    if save_path.suffix.lower() not in _LOCAL_WORKSPACE_BINARY_EXTS:
        try:
            preview_text = content.decode("utf-8")
        except UnicodeDecodeError:
            preview_text = ""
    if len(preview_text) > 6000:
        preview_text = f"{preview_text[:6000]}\n\n...[内容已截断，共 {len(preview_text)} 字]"

    return LocalAgentWorkspaceUploadOut(
        filename=filename,
        saved_filename=save_path.name,
        size=len(content),
        workspace_path=_workspace_rel_path(base, save_path),
        extracted_text=preview_text,
        preview_text=preview_text,
        conversion=None,
        is_image=save_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"},
        image_data_url="",
    )


@router.post(
    "/agents/{agent_id}/local-agent/sessions/default",
    response_model=LocalAgentChannelSessionOut,
)
async def get_or_create_agent_default_local_agent_channel_session(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent has no tenant")
    payload = await channel_service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        title=getattr(agent, "name", None) or "Local Agent",
    )
    return _serialize_session(payload)


@router.get(
    "/agents/{agent_id}/local-agent/sessions",
    response_model=list[LocalAgentChannelSessionOut],
)
async def list_agent_local_agent_channel_sessions(
    agent_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    rows = await channel_service.list_agent_channel_sessions(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        limit=limit,
    )
    return [_serialize_session(row) for row in rows]


@router.post(
    "/agents/{agent_id}/local-agent/sessions",
    response_model=LocalAgentChannelSessionOut,
    status_code=201,
)
async def create_local_agent_channel_session(
    agent_id: uuid.UUID,
    body: LocalAgentChannelSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    payload = await channel_service.create_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        source=body.source,
        title=body.title,
    )
    return _serialize_session(payload)


@router.get(
    "/agents/{agent_id}/local-agent/sessions/{session_id}",
    response_model=LocalAgentChannelSessionOut,
)
async def resolve_local_agent_channel_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    payload = await channel_service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        session_id=session_id,
    )
    return _serialize_session(payload)


@router.delete(
    "/agents/{agent_id}/local-agent/sessions/{session_id}",
    status_code=204,
)
async def delete_local_agent_channel_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    await channel_service.archive_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        session_id=session_id,
    )
    return None


@router.post(
    "/agents/{agent_id}/local-agent/sessions/{session_id}/messages",
    response_model=LocalAgentChannelMessageOut,
    status_code=201,
)
async def create_local_agent_channel_message(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: LocalAgentChannelMessageIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    await channel_service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        session_id=session_id,
    )
    attachments = _materialize_local_agent_attachments_for_host(
        tenant_id=tenant_id,
        actor_user_id=current_user.id,
        host_owner_user_id=host_owner_user_id,
        attachments=body.attachments,
    )
    payload = await channel_service.enqueue_channel_message(
        db,
        session_id=session_id,
        owner_user_id=host_owner_user_id,
        sender_user_id=current_user.id,
        sender_agent_id=agent.id,
        content=body.content,
        attachments=attachments,
        metadata={**body.metadata, "source_agent_id": str(agent.id)},
        idempotency_key=body.idempotency_key,
    )
    await channel_ws_manager.send_to_user(
        host_owner_user_id, {"type": "message", "message": _runner_message_payload(payload)}
    )
    await _broadcast_browser_channel_event(
        owner_user_id=host_owner_user_id,
        session_id=session_id,
        event=payload.get("event"),
    )
    return _serialize_message(payload)


@router.get("/agents/{agent_id}/local-agent/sessions/{session_id}/events")
async def list_local_agent_channel_events(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    await channel_service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        session_id=session_id,
    )
    events = await channel_service.list_channel_events(
        db,
        session_id=session_id,
        owner_user_id=host_owner_user_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return {
        "events": events,
        "next_cursor": events[-1]["sequence"] if events else after_sequence,
    }


@router.get("/agents/{agent_id}/local-agent/sessions/{session_id}/workspace/download")
async def download_agent_local_agent_channel_workspace_file(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    host_owner_user_id = _local_agent_host_user_id(agent)
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    await channel_service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_user_id,
        actor_user_id=current_user.id,
        source_agent_id=agent.id,
        session_id=session_id,
    )
    _base, target = _safe_local_agent_workspace_path_for(tenant_id, host_owner_user_id, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@router.get("/local-bridge/channel/workspace/download")
async def download_local_bridge_channel_workspace_file(
    path: str,
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
):
    _base, target = _safe_local_agent_workspace_path_for(context.tenant_id, context.user_id, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(path=str(target), filename=target.name)


@router.get("/local-bridge/channel/poll")
async def poll_local_agent_channel(
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
    db: AsyncSession = Depends(get_db),
):
    return {"messages": await channel_service.poll_pending_channel_messages(db, context=context)}


@router.post("/local-bridge/channel/events", status_code=201)
async def record_local_agent_channel_event(
    body: LocalAgentChannelEventIn,
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
    db: AsyncSession = Depends(get_db),
):
    event = await channel_service.record_channel_event(
        db,
        context=context,
        session_id=body.session_id,
        message_id=body.message_id,
        event_type=body.type,
        payload=body.payload,
    )
    await _broadcast_browser_channel_event(owner_user_id=context.user_id, session_id=body.session_id, event=event)
    return event


@router.post("/local-bridge/channel/report")
async def report_local_agent_channel_result(
    body: LocalAgentChannelResultIn,
    context: BridgeAuthContext = Depends(get_bridge_auth_context),
    db: AsyncSession = Depends(get_db),
):
    result = await channel_service.record_channel_result(
        db,
        context=context,
        session_id=body.session_id,
        message_id=body.message_id,
        result_status=body.status,
        output=body.output,
        artifacts=body.artifacts,
        metadata=body.metadata,
    )
    await _broadcast_browser_channel_event(
        owner_user_id=context.user_id,
        session_id=body.session_id,
        event=result.get("event") if isinstance(result, dict) else None,
    )
    return result


@router.websocket("/local-agents/sessions/{session_id}/ws")
async def local_agent_browser_channel_ws(
    websocket: WebSocket,
    session_id: uuid.UUID,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        context = await channel_service.resolve_browser_session_ws_ticket(db, ticket=ticket, session_id=session_id)
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == status.HTTP_401_UNAUTHORIZED else 4403)
        return

    owner_user_id = _coerce_uuid(context["owner_user_id"])
    await websocket.accept()
    await browser_channel_ws_manager.connect(owner_user_id=owner_user_id, session_id=session_id, websocket=websocket)
    await websocket.send_json(
        jsonable_encoder(
            {
                "type": "hello",
                "session_id": str(session_id),
                "owner_user_id": str(owner_user_id),
            }
        )
    )
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            if message_type == "ping":
                await channel_service.mark_channel_seen(db, context=context)
                await websocket.send_json({"type": "pong"})
                continue
            await websocket.send_json(
                jsonable_encoder({"type": "error", "error": f"Unsupported browser message type: {message_type}"})
            )
    except WebSocketDisconnect:
        pass
    finally:
        await browser_channel_ws_manager.disconnect(
            owner_user_id=owner_user_id,
            session_id=session_id,
            websocket=websocket,
        )


@router.websocket("/local-bridge/channel/ws")
async def local_agent_channel_ws(
    websocket: WebSocket,
    ticket: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    client_host = websocket.client.host if websocket.client else None
    user_agent = websocket.headers.get("user-agent")
    try:
        context = await channel_service.resolve_ws_ticket(
            db,
            ticket=ticket,
            last_seen_ip=client_host,
            user_agent=user_agent,
        )
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == status.HTTP_401_UNAUTHORIZED else 4403)
        return

    await websocket.accept()
    await channel_ws_manager.connect(context=context, websocket=websocket)
    await websocket.send_json(
        jsonable_encoder(
            {
                "type": "hello",
                "connection_id": str(context.connection_id),
                "owner_user_id": str(context.user_id),
            }
        )
    )
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            if message_type == "ping":
                await channel_service.mark_channel_seen(db, context=context)
                await websocket.send_json({"type": "pong"})
                continue
            if message_type == "ready":
                ready = await channel_service.mark_channel_ready(
                    db,
                    context=context,
                    runtime_kind=str(data.get("runtime_kind") or context.client_kind),
                    capabilities=dict(data.get("capabilities") or {}),
                )
                await websocket.send_json(
                    jsonable_encoder(
                        {
                            "type": "ready_ack",
                            "status": ready["status"],
                            "snapshot_hash": ready["snapshot_hash"],
                            "effective_capabilities": ready["effective_capabilities"],
                            "expires_at": ready["expires_at"],
                        }
                    )
                )
                for message in await channel_service.poll_pending_channel_messages(db, context=context):
                    await websocket.send_json(jsonable_encoder({"type": "message", "message": message}))
                continue
            if message_type == "ack":
                message_id = _coerce_uuid(data.get("message_id"))
                await channel_service.ack_channel_message(db, context=context, message_id=message_id)
                await websocket.send_json(jsonable_encoder({"type": "ack_ack", "message_id": str(message_id)}))
                continue
            if message_type == "event":
                event = await channel_service.record_channel_event(
                    db,
                    context=context,
                    session_id=_coerce_uuid(data.get("session_id")),
                    message_id=_coerce_uuid(data["message_id"]) if data.get("message_id") else None,
                    event_type=str(data.get("event_type") or data.get("event") or "text"),
                    payload=dict(data.get("payload") or {}),
                )
                await _broadcast_browser_channel_event(
                    owner_user_id=context.user_id,
                    session_id=_coerce_uuid(data.get("session_id")),
                    event=event,
                )
                await websocket.send_json(jsonable_encoder({"type": "event_ack", "event_id": event["id"]}))
                continue
            if message_type == "result":
                message_id = _coerce_uuid(data.get("message_id"))
                result = await channel_service.record_channel_result(
                    db,
                    context=context,
                    session_id=_coerce_uuid(data.get("session_id")),
                    message_id=message_id,
                    result_status=str(data.get("status") or "completed"),
                    output=str(data.get("output") or ""),
                    artifacts=list(data.get("artifacts") or []),
                    metadata=dict(data.get("metadata") or {}),
                )
                await _broadcast_browser_channel_event(
                    owner_user_id=context.user_id,
                    session_id=_coerce_uuid(data.get("session_id")),
                    event=result.get("event") if isinstance(result, dict) else None,
                )
                await websocket.send_json(
                    jsonable_encoder(
                        {
                            "type": "result_ack",
                            "message_id": str(message_id),
                            "status": result["status"],
                            "receipt": result.get("receipt"),
                        }
                    )
                )
                continue
            await websocket.send_json(
                jsonable_encoder({"type": "error", "error": f"Unsupported message type: {message_type}"})
            )
    except WebSocketDisconnect:
        try:
            await channel_service.mark_channel_offline(db, context=context)
        except Exception as exc:
            logger.debug("[LocalAgentChannel] offline mark failed during WS disconnect: {}", exc)
    finally:
        await channel_ws_manager.disconnect(context=context, websocket=websocket)
