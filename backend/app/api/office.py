from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.resource_authority import normalize_workspace_resource_path
from app.core.security import get_current_user
from app.database import async_session, get_db, tenant_scoped_session
from app.models.user import User
from app.services.office_document_service import OfficeDocumentService
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityError,
    authorize_workspace_path,
    register_workspace_path,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/agents/{agent_id}/office", tags=["office"])


class OfficeDocumentCreateIn(BaseModel):
    path: str
    kind: str = Field(pattern="^(docx|xlsx|pptx)$")
    template_path: str | None = None


class OnlyOfficeCallback(BaseModel):
    status: int
    url: str | None = None
    key: str | None = None
    error: int | None = None


class OfficeForceSaveIn(BaseModel):
    path: str
    userdata: str | None = None


def _agent_workspace(agent_id: uuid.UUID) -> Path:
    return Path(settings.AGENT_DATA_DIR) / str(agent_id)


def _public_base_url() -> str:
    return (
        getattr(settings, "BASE_URL", "")
        or getattr(settings, "PUBLIC_BASE_URL", "")
        or os.environ.get("PUBLIC_BASE_URL", "")
        or os.environ.get("BASE_URL", "")
    ).rstrip("/")


def _onlyoffice_secret() -> str:
    return getattr(settings, "ONLYOFFICE_JWT_SECRET", "") or getattr(settings, "JWT_SECRET_KEY", "")


def _onlyoffice_command_secret() -> str:
    secret = getattr(settings, "ONLYOFFICE_JWT_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="ONLYOFFICE JWT secret is not configured")
    return secret


def _document_type_for_suffix(suffix: str) -> str:
    match suffix.lower():
        case ".docx":
            return "word"
        case ".xlsx":
            return "cell"
        case ".pptx":
            return "slide"
        case _:
            raise HTTPException(status_code=400, detail="Unsupported office document type")


def _token_expiry(expires_delta: timedelta | None = None) -> datetime:
    if expires_delta is None:
        seconds = int(getattr(settings, "ONLYOFFICE_DOWNLOAD_TOKEN_EXPIRE_SECONDS", 300))
        expires_delta = timedelta(seconds=seconds)
    return datetime.now(UTC) + expires_delta


def make_document_token(
    *,
    agent_id: uuid.UUID,
    path: str,
    purpose: str,
    user_id: uuid.UUID,
    expires_delta: timedelta | None = None,
) -> str:
    secret = _onlyoffice_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Office signing secret is not configured")
    payload = {
        "aid": str(agent_id),
        "path": path,
        "purpose": purpose,
        "uid": str(user_id),
        "authority_action": "write" if purpose == "callback" else "read",
        "exp": _token_expiry(expires_delta),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _verify_document_token(*, agent_id: uuid.UUID, path: str, token: str, purpose: str) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing office token")
    try:
        payload = jwt.decode(token, _onlyoffice_secret(), algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired office token") from exc
    if payload.get("aid") != str(agent_id) or payload.get("path") != path or payload.get("purpose") != purpose:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Office token scope mismatch")
    return payload


def _document_key(path: Path, rel_path: str) -> str:
    stat = path.stat()
    raw = f"{rel_path}:{stat.st_size}:{stat.st_mtime_ns}"
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clean_identity_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _editor_user_identity(current_user: User) -> dict[str, str]:
    user_id = str(current_user.id)
    tenant_id = _clean_identity_value(getattr(current_user, "tenant_id", None))
    editor_id = f"{tenant_id}:{user_id}" if tenant_id else user_id
    editor_name = (
        _clean_identity_value(getattr(current_user, "display_name", None))
        or _clean_identity_value(getattr(current_user, "username", None))
        or _clean_identity_value(getattr(current_user, "name", None))
        or user_id
    )
    return {"id": editor_id, "name": editor_name}


def _download_url(agent_id: uuid.UUID, path: str, *, user_id: uuid.UUID) -> str:
    token = make_document_token(
        agent_id=agent_id,
        path=path,
        purpose="download",
        user_id=user_id,
    )
    query = urlencode({"path": path, "token": token})
    return f"{_public_base_url()}/api/agents/{agent_id}/office/download?{query}"


def _callback_url(agent_id: uuid.UUID, path: str, *, user_id: uuid.UUID) -> str:
    token = make_document_token(
        agent_id=agent_id,
        path=path,
        purpose="callback",
        user_id=user_id,
        expires_delta=timedelta(hours=12),
    )
    query = urlencode({"path": path, "token": token})
    return f"{_public_base_url()}/api/agents/{agent_id}/office/callback?{query}"


def _document_command_url(document_key: str) -> str:
    docs_url = (
        getattr(settings, "ONLYOFFICE_INTERNAL_DOCS_URL", "") or getattr(settings, "ONLYOFFICE_DOCS_URL", "")
    ).rstrip("/")
    if not docs_url:
        raise HTTPException(status_code=503, detail="ONLYOFFICE document server is not configured")
    return f"{docs_url}/command?{urlencode({'shardkey': document_key})}"


async def record_office_callback_event(*, agent_id: uuid.UUID, path: str, status: int, error: int | None) -> None:
    logger.warning(
        "ONLYOFFICE callback reported error: agent=%s path=%s status=%s error=%s",
        agent_id,
        path,
        status,
        error,
    )


def _require_office_workspace_path(path: str) -> str:
    try:
        normalized = normalize_workspace_resource_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Office document path escapes the Agent workspace") from exc
    if not normalized.startswith("workspace/"):
        raise HTTPException(status_code=400, detail="Office documents must be stored below workspace/")
    return normalized


def _workspace_authority_http_error(exc: WorkspaceAuthorityError) -> HTTPException:
    status_code = 404 if exc.code in {"workspace_resource_not_found"} else 403
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
    )


async def _authorize_office_request_path(
    db: AsyncSession,
    current_user: User,
    *,
    agent_id: uuid.UUID,
    path: str,
    action: str,
    agent_access=None,
    for_update: bool = False,
):
    normalized = _require_office_workspace_path(path)
    target = OfficeDocumentService(_agent_workspace(agent_id)).resolve_document_path(normalized)
    resolved_agent_access = agent_access or await check_agent_access(db, current_user, agent_id)
    try:
        decision = await authorize_workspace_path(
            db,
            current_user,
            agent_id=agent_id,
            path=normalized,
            action=action,
            path_exists=target.exists(),
            for_update=for_update,
            agent_access=resolved_agent_access,
        )
    except WorkspaceAuthorityError as exc:
        raise _workspace_authority_http_error(exc) from exc
    return resolved_agent_access, decision, target, normalized


@asynccontextmanager
async def _authorize_office_token_payload(
    *,
    agent_id: uuid.UUID,
    path: str,
    payload: dict,
    expected_action: str,
):
    if payload.get("authority_action") != expected_action:
        raise HTTPException(status_code=403, detail="Office token authority action mismatch")
    try:
        user_id = uuid.UUID(str(payload.get("uid") or ""))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Office token is missing requester authority") from exc
    tenant_id = await resolve_tenant_for_agent(agent_id, session_factory=async_session)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="Office token Agent has no tenant authority")
    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="office_capability_token",
    ) as db:
        user = (
            await db.execute(
                select(User).where(
                    User.id == user_id,
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=403, detail="Office token requester is no longer active")
        agent_access, decision, target, normalized = await _authorize_office_request_path(
            db,
            user,
            agent_id=agent_id,
            path=path,
            action=expected_action,
        )
        yield {
            "db": db,
            "user": user,
            "agent": agent_access[0],
            "decision": decision,
            "target": target,
            "path": normalized,
        }


def _content_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@router.post("/documents")
async def create_office_document(
    agent_id: uuid.UUID,
    body: OfficeDocumentCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_access = await check_agent_access(db, current_user, agent_id)
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    async with async_agent_workspace_lock(agent_id):
        _access, decision, _target, normalized = await _authorize_office_request_path(
            db,
            current_user,
            agent_id=agent_id,
            path=body.path,
            action="create",
            agent_access=agent_access,
            for_update=True,
        )
        template_path = None
        if body.template_path:
            (
                _template_access,
                _template_decision,
                _template_target,
                template_path,
            ) = await _authorize_office_request_path(
                db,
                current_user,
                agent_id=agent_id,
                path=body.template_path,
                action="read",
                agent_access=agent_access,
            )
        result = OfficeDocumentService(_agent_workspace(agent_id)).create_document(
            normalized,
            kind=body.kind,
            template_path=template_path,
        )
        created_path = OfficeDocumentService(_agent_workspace(agent_id)).resolve_document_path(normalized)
        await register_workspace_path(
            db,
            tenant_id=agent_access[0].tenant_id,
            agent_id=agent_id,
            path=normalized,
            owner_user_id=decision.owner_user_id or current_user.id,
            root_session_id=decision.root_session_id,
            source="office_api_create",
            content_hash=_content_hash(created_path),
            allow_owner_rebind=False,
        )
    return {
        "status": "ok",
        **result,
        "authority_source": decision.authority_source,
        "operator_view": decision.operator_view,
    }


@router.get("/editor-config")
async def get_editor_config(
    agent_id: uuid.UUID,
    path: str,
    mode: str = Query(default="edit", pattern="^(edit|view)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_access = await check_agent_access(db, current_user, agent_id)
    _access, decision, target, normalized = await _authorize_office_request_path(
        db,
        current_user,
        agent_id=agent_id,
        path=path,
        action="write" if mode == "edit" else "read",
        agent_access=agent_access,
    )
    docs_url = getattr(settings, "ONLYOFFICE_DOCS_URL", "").rstrip("/")
    secret = getattr(settings, "ONLYOFFICE_JWT_SECRET", "")
    if not docs_url or not secret:
        return {
            "enabled": False,
            "reason": "onlyoffice_not_configured",
            "required_env": ["ONLYOFFICE_DOCS_URL", "ONLYOFFICE_JWT_SECRET"],
        }

    service = OfficeDocumentService(_agent_workspace(agent_id))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Office document not found")

    file_type = target.suffix.lower().lstrip(".")
    document_key = _document_key(target, normalized)
    editor_user = _editor_user_identity(current_user)
    if mode == "edit":
        service.set_active_editor_session(normalized, session_id=document_key, user_id=editor_user["id"])

    config = {
        "document": {
            "fileType": file_type,
            "key": document_key,
            "title": target.name,
            "url": _download_url(agent_id, normalized, user_id=current_user.id),
        },
        "documentType": _document_type_for_suffix(target.suffix),
        "editorConfig": {
            "mode": mode,
            "callbackUrl": _callback_url(agent_id, normalized, user_id=current_user.id),
            "user": editor_user,
            "customization": {
                "forcesave": True,
            },
        },
    }
    config["token"] = jwt.encode(config, secret, algorithm="HS256")
    return {
        "enabled": True,
        "documentServerUrl": docs_url,
        "config": config,
        "authority_source": decision.authority_source,
        "operator_view": decision.operator_view,
    }


@router.get("/download")
async def download_document(agent_id: uuid.UUID, path: str, token: str):
    payload = _verify_document_token(agent_id=agent_id, path=path, token=token, purpose="download")
    async with _authorize_office_token_payload(
        agent_id=agent_id,
        path=path,
        payload=payload,
        expected_action="read",
    ) as authority:
        target = authority["target"]
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Office document not found")
        return FileResponse(str(target), filename=target.name)


@router.post("/force-save")
async def force_save_document(
    agent_id: uuid.UUID,
    body: OfficeForceSaveIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_access = await check_agent_access(db, current_user, agent_id)
    _access, _decision, target, normalized = await _authorize_office_request_path(
        db,
        current_user,
        agent_id=agent_id,
        path=body.path,
        action="write",
        agent_access=agent_access,
    )
    service = OfficeDocumentService(_agent_workspace(agent_id))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Office document not found")

    active_session = service.get_active_editor_session(normalized)
    active_session_id = active_session.get("session_id") if active_session else None
    document_key = (
        active_session_id
        if active_session_id and active_session_id != "onlyoffice"
        else _document_key(target, body.path)
    )
    command: dict[str, str] = {
        "c": "forcesave",
        "key": document_key,
    }
    if body.userdata:
        command["userdata"] = body.userdata

    token = jwt.encode(command, _onlyoffice_command_secret(), algorithm="HS256")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(_document_command_url(document_key), json={"token": token})
        response.raise_for_status()
    return {"status": "ok", "result": response.json()}


def _rewrite_to_internal_docs_url(url: str) -> str:
    external = getattr(settings, "ONLYOFFICE_DOCS_URL", "").rstrip("/")
    internal = getattr(settings, "ONLYOFFICE_INTERNAL_DOCS_URL", "").rstrip("/")
    if external and internal and url.startswith(external):
        return internal + url[len(external) :]
    return url


@router.post("/callback")
async def onlyoffice_callback(
    agent_id: uuid.UUID,
    path: str,
    token: str,
    payload: OnlyOfficeCallback,
):
    token_payload = _verify_document_token(agent_id=agent_id, path=path, token=token, purpose="callback")
    service = OfficeDocumentService(_agent_workspace(agent_id))
    normalized = _require_office_workspace_path(path)
    # Closing an editor or recording its failure is a signed, path-scoped
    # cleanup operation. It must remain possible after the requester's Agent
    # access is revoked, otherwise a stale active-editor marker blocks every
    # future governed writer indefinitely.
    if payload.status == 4:
        service.clear_active_editor_session(normalized, session_id=payload.key)
        return {"error": 0}
    if payload.status in (3, 7):
        await record_office_callback_event(
            agent_id=agent_id,
            path=normalized,
            status=payload.status,
            error=payload.error,
        )
        return {"error": 0}

    async with _authorize_office_token_payload(
        agent_id=agent_id,
        path=path,
        payload=token_payload,
        expected_action="write",
    ) as authority:
        normalized = authority["path"]

        if payload.status in (2, 6):
            if not payload.url:
                logger.warning("ONLYOFFICE save callback missing url: agent=%s path=%s", agent_id, normalized)
                return {"error": 1}
            download_url = _rewrite_to_internal_docs_url(payload.url)
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(download_url)
                response.raise_for_status()
            from app.services.session_workspace_snapshot import async_agent_workspace_lock

            async with async_agent_workspace_lock(agent_id):
                service.atomic_save_bytes(
                    normalized,
                    response.content,
                    reason=f"onlyoffice-status-{payload.status}",
                    require_no_active_editor=False,
                )
                target = service.resolve_document_path(normalized)
                decision = authority["decision"]
                await register_workspace_path(
                    authority["db"],
                    tenant_id=authority["agent"].tenant_id,
                    agent_id=agent_id,
                    path=normalized,
                    owner_user_id=decision.owner_user_id or authority["user"].id,
                    root_session_id=decision.root_session_id,
                    source="onlyoffice_callback",
                    content_hash=_content_hash(target),
                    allow_owner_rebind=False,
                )
        elif payload.status == 1:
            service.set_active_editor_session(
                normalized,
                session_id=payload.key or "onlyoffice",
                user_id=str(authority["user"].id),
            )

    return {"error": 0}
