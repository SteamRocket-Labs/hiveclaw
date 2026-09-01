from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access, check_agent_operator_reachability
from app.core.resource_authority import authorize_resource_action, normalize_workspace_resource_path
from app.core.security import get_current_user
from app.database import get_db
from app.models.chat_artifact import ChatArtifact
from app.models.user import User
from app.services.chat_artifact_delivery import resolve_chat_artifact_file
from app.services.invocation_trace import persist_invocation_span
from app.services.office_document_service import (
    OFFICE_PREVIEW_CSP,
    OfficeDocumentError,
    OfficeDocumentNotFoundError,
    OfficeDocumentPathError,
    OfficeDocumentService,
    OfficePreviewResult,
    OfficePreviewSourceChangedError,
    OfficePreviewTooLargeError,
    OfficePreviewUnsupportedTypeError,
)
from app.services.office_preview_metrics import record_office_preview
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


def _agent_workspace(agent_id: uuid.UUID) -> Path:
    return Path(settings.AGENT_DATA_DIR) / str(agent_id)


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
    allow_manager_override: bool = False,
    manager_override_reason: str | None = None,
):
    normalized = _require_office_workspace_path(path)
    target = OfficeDocumentService(_agent_workspace(agent_id)).resolve_document_path(normalized)
    resolved_agent_access = agent_access or await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if allow_manager_override and action == "read"
        else check_agent_access(db, current_user, agent_id)
    )
    try:
        decision = await authorize_workspace_path(
            db,
            current_user,
            agent_id=agent_id,
            path=normalized,
            action=action,
            path_exists=target.exists(),
            for_update=for_update,
            allow_manager_override=allow_manager_override,
            manager_override_reason=manager_override_reason,
            agent_access=resolved_agent_access,
        )
    except WorkspaceAuthorityError as exc:
        raise _workspace_authority_http_error(exc) from exc
    return resolved_agent_access, decision, target, normalized


def _content_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _preview_http_error(exc: OfficeDocumentError) -> HTTPException:
    if isinstance(exc, OfficeDocumentNotFoundError):
        status_code = 404
    elif isinstance(exc, (OfficeDocumentPathError, OfficePreviewUnsupportedTypeError)):
        status_code = 400
    elif isinstance(exc, OfficePreviewTooLargeError):
        status_code = 413
    elif isinstance(exc, OfficePreviewSourceChangedError):
        status_code = 409
    else:
        status_code = 503
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.error_code, "message": str(exc)},
    )


def _preview_response(
    result: OfficePreviewResult,
    *,
    trace_id: str,
    artifact_source: str | None = None,
) -> Response:
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "X-Office-Preview-Mode": result.preview_mode,
        "X-Office-Source-SHA256": result.source_sha256,
        "X-Office-Renderer-Version": result.renderer_version,
        "X-Office-Preview-Trace-ID": trace_id,
        "Content-Security-Policy": OFFICE_PREVIEW_CSP,
    }
    if artifact_source:
        headers["X-Office-Artifact-Source"] = artifact_source
    return Response(content=result.html, media_type="text/html", headers=headers)


async def _render_with_evidence(
    render,
    *,
    tenant_id: uuid.UUID | str | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    authority_source: str,
    source_kind: str,
    office_format: str,
) -> tuple[OfficePreviewResult, str]:
    started = time.perf_counter()
    request_id = uuid.uuid4()
    trace_id = f"office-preview-{request_id.hex}"
    span_id = f"render-{uuid.uuid4().hex[:16]}"
    try:
        result = await asyncio.to_thread(render)
    except OfficeDocumentError as exc:
        duration = time.perf_counter() - started
        record_office_preview(
            source_kind=source_kind,
            preview_mode="error",
            status="error",
            office_format=office_format,
            duration_seconds=duration,
            output_bytes=0,
            cache_hit=False,
            error_code=exc.error_code,
        )
        logger.warning(
            "Office preview failed: tenant=%s agent=%s source_kind=%s format=%s authority=%s error_code=%s",
            tenant_id,
            agent_id,
            source_kind,
            office_format,
            authority_source,
            exc.error_code,
        )
        await persist_invocation_span(
            db=None,
            tenant_id=tenant_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            parent_trace_id=None,
            span_type="office_preview",
            name=f"office_preview:{source_kind}",
            status="error",
            duration_ms=duration * 1000,
            agent_id=agent_id,
            user_id=user_id,
            runtime_task_id=None,
            session_id=None,
            request_id=request_id,
            execution_identity_type="user",
            execution_identity_id=user_id,
            execution_identity_label=None,
            metadata={
                "source_kind": source_kind,
                "office_format": office_format,
                "authority_source": authority_source,
                "error_code": exc.error_code,
            },
            error=exc.error_code,
        )
        raise _preview_http_error(exc) from exc

    duration = time.perf_counter() - started
    record_office_preview(
        source_kind=source_kind,
        preview_mode=result.preview_mode,
        status="ok",
        office_format=office_format,
        duration_seconds=duration,
        output_bytes=result.output_bytes,
        cache_hit=result.cache_hit,
        error_code=None,
    )
    logger.info(
        "Office preview rendered: tenant=%s agent=%s source_kind=%s format=%s authority=%s "
        "source_sha256=%s renderer=%s mode=%s cache_hit=%s duration_ms=%.2f output_bytes=%s",
        tenant_id,
        agent_id,
        source_kind,
        office_format,
        authority_source,
        result.source_sha256,
        result.renderer_version,
        result.preview_mode,
        result.cache_hit,
        duration * 1000,
        result.output_bytes,
    )
    await persist_invocation_span(
        db=None,
        tenant_id=tenant_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        parent_trace_id=None,
        span_type="office_preview",
        name=f"office_preview:{source_kind}",
        status="ok",
        duration_ms=duration * 1000,
        agent_id=agent_id,
        user_id=user_id,
        runtime_task_id=None,
        session_id=None,
        request_id=request_id,
        execution_identity_type="user",
        execution_identity_id=user_id,
        execution_identity_label=None,
        metadata={
            "source_kind": source_kind,
            "office_format": office_format,
            "authority_source": authority_source,
            "source_sha256": result.source_sha256,
            "renderer_version": result.renderer_version,
            "preview_mode": result.preview_mode,
            "cache_hit": result.cache_hit,
            "output_bytes": result.output_bytes,
            "fallback_reason": result.fallback_reason,
        },
    )
    return result, trace_id


def _artifact_preview_target(service: OfficeDocumentService, artifact: ChatArtifact) -> tuple[Path, str]:
    target, artifact_source = resolve_chat_artifact_file(artifact, service.workspace)
    if target is not None:
        return target, artifact_source
    if artifact_source == "missing_delivery_snapshot":
        raise OfficeDocumentNotFoundError("Artifact delivery snapshot is no longer available")
    raise OfficeDocumentNotFoundError("Legacy artifact workspace file is no longer available")


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
        service = OfficeDocumentService(_agent_workspace(agent_id))
        result = service.create_document(
            normalized,
            kind=body.kind,
            template_path=template_path,
        )
        created_path = service.resolve_document_path(normalized)
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
        # A successful create response is also the read-after-write boundary
        # for Current Workspace. FastAPI may finalize yielded dependencies
        # after the response starts, so commit the authority row before the
        # client can immediately request the preview.
        await db.commit()
    return {
        "status": "ok",
        **result,
        "authority_source": decision.authority_source,
        "operator_view": decision.operator_view,
    }


@router.get("/preview")
async def preview_office_document(
    agent_id: uuid.UUID,
    path: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )
    _access, decision, target, normalized = await _authorize_office_request_path(
        db,
        current_user,
        agent_id=agent_id,
        path=path,
        action="read",
        agent_access=agent_access,
        allow_manager_override=operator_view,
        manager_override_reason=operator_reason,
    )
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Office document not found"})

    service = OfficeDocumentService(_agent_workspace(agent_id))
    result, trace_id = await _render_with_evidence(
        lambda: service.render_preview(normalized),
        tenant_id=getattr(agent_access[0], "tenant_id", None),
        agent_id=agent_id,
        user_id=current_user.id,
        authority_source=decision.authority_source,
        source_kind="workspace",
        office_format=target.suffix.lower().lstrip("."),
    )
    return _preview_response(result, trace_id=trace_id)


@router.get("/artifacts/{artifact_id}/preview")
async def preview_office_artifact(
    agent_id: uuid.UUID,
    artifact_id: uuid.UUID,
    operator_view: bool = False,
    operator_reason: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent_access = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view
        else check_agent_access(db, current_user, agent_id)
    )
    artifact = (
        await db.execute(
            select(ChatArtifact).where(ChatArtifact.id == artifact_id, ChatArtifact.agent_id == agent_id).limit(1)
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found", "message": "Artifact not found"})

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

    service = OfficeDocumentService(_agent_workspace(agent_id))
    try:
        target, artifact_source = _artifact_preview_target(service, artifact)
    except OfficeDocumentError as exc:
        raise _preview_http_error(exc) from exc
    result, trace_id = await _render_with_evidence(
        lambda: service.render_preview_target(target, cache_key=f"artifact:{artifact.id}"),
        tenant_id=getattr(agent_access[0], "tenant_id", None),
        agent_id=agent_id,
        user_id=current_user.id,
        authority_source=decision.authority_source,
        source_kind="artifact",
        office_format=target.suffix.lower().lstrip("."),
    )
    return _preview_response(result, trace_id=trace_id, artifact_source=artifact_source)
