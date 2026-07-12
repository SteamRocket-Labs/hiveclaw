"""Agent Knowledge read model API (spec §11 / §12 P7).

Six read-only endpoints over the memory engine. The frontend Knowledge plane
consumes these instead of parsing raw file layout; raw Markdown remains
available through the existing workspace file APIs as the advanced view.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db, tenant_scoped_session
from app.models.agent import Agent
from app.models.user import User
from app.services.personal_knowledge_access import HumanBrowserPrincipal
from app.services.personal_knowledge_service import PersonalKnowledgeService
from app.services.personal_knowledge_proposals import PersonalKnowledgeProposalService
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/knowledge", tags=["agent-knowledge"])
personal_router = APIRouter(prefix="/knowledge/personal", tags=["personal-knowledge"])


class PersonalKnowledgeIngestRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    markdown: str = Field(..., min_length=1)
    source_kind: str = Field("paste", min_length=1, max_length=40)
    source_uri: str | None = None
    agent_searchable: bool = True
    sensitivity: str = Field("internal", min_length=1, max_length=30)


class PersonalKnowledgeUrlImportRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    title: str | None = Field(None, max_length=300)
    agent_searchable: bool = True
    sensitivity: str = Field("internal", min_length=1, max_length=30)


class PersonalKnowledgeDocumentPatchRequest(BaseModel):
    agent_searchable: bool | None = None
    sensitivity: str | None = Field(None, min_length=1, max_length=30)
    status: str | None = Field(None, min_length=1, max_length=40)


class PersonalKnowledgeGrantRequest(BaseModel):
    resource_type: str = Field("scope", pattern="^(scope|document)$")
    resource_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    grantee_type: str = Field(..., pattern="^(user|agent|session)$")
    grantee_id: uuid.UUID
    permission: str = Field("search", pattern="^(read|search|manage)$")
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonalKnowledgeProposalDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str | None = Field(None, max_length=2000)


class PersonalKnowledgeRollbackRequest(BaseModel):
    target_version: int = Field(..., ge=1)


def _data_root() -> Path:
    return Path(get_settings().AGENT_DATA_DIR)


def _owner_user_id_for_personal_kb(agent: Agent) -> uuid.UUID:
    owner_id = (
        getattr(agent, "owner_user_id", None)
        or getattr(agent, "sponsor_user_id", None)
        or getattr(agent, "creator_id", None)
    )
    if owner_id is None:
        raise HTTPException(status_code=409, detail="Agent owner is not configured")
    return uuid.UUID(str(owner_id))


def _tenant_id_for_agent(agent: Agent, current_user: User) -> uuid.UUID:
    tenant_id = getattr(agent, "tenant_id", None) or getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=409, detail="Tenant is not configured")
    return uuid.UUID(str(tenant_id))


def _tenant_id_for_user(current_user: User) -> uuid.UUID:
    tenant_id = getattr(current_user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=409, detail="Tenant is not configured")
    return uuid.UUID(str(tenant_id))


def _as_jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _as_jsonable(item) for key, item in value.items()}
    return value


def _dataclass_payload(value: Any) -> dict[str, Any]:
    raw = asdict(value) if is_dataclass(value) else vars(value)
    return {key: _as_jsonable(item) for key, item in raw.items()}


def _source_preview_response(preview: Any) -> Response:
    filename = str(getattr(preview, "filename", "source")).replace('"', "")
    return Response(
        content=getattr(preview, "content", b""),
        media_type=str(getattr(preview, "mime_type", "application/octet-stream")),
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


async def _process_current_user_personal_import_jobs(*, tenant_id: uuid.UUID, owner_user_id: uuid.UUID) -> None:
    async with tenant_scoped_session(tenant_id) as db:
        await PersonalKnowledgeService().process_import_jobs(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            current_user_id=owner_user_id,
            limit=5,
            statuses=("queued",),
        )


def _schedule_personal_import_worker(
    background_tasks: BackgroundTasks,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> None:
    background_tasks.add_task(
        _process_current_user_personal_import_jobs,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )


def _principal_stack_for_read(agent: Agent, current_user: User) -> PrincipalStack:
    owner_id = getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)
    current_role = PrincipalRole.COMPANY_ADMIN if current_user.role == "org_admin" else PrincipalRole.CURRENT_USER
    current = Principal(
        role=current_role,
        id=str(current_user.id),
        label=getattr(current_user, "display_name", None) or getattr(current_user, "email", "") or "",
    )
    owner = Principal(role=PrincipalRole.OWNER, id=str(owner_id), label="") if owner_id else None
    creator_id = getattr(agent, "creator_id", None)
    creator = (
        Principal(role=PrincipalRole.CREATOR, id=str(creator_id), label="")
        if creator_id and creator_id != owner_id
        else None
    )
    company = (
        Principal(role=PrincipalRole.COMPANY, id=str(agent.tenant_id), label="")
        if getattr(agent, "tenant_id", None)
        else None
    )
    platform = (
        Principal(role=PrincipalRole.PLATFORM, id=str(current_user.id), label=current.label)
        if current_user.role == "platform_admin"
        else None
    )
    return PrincipalStack(
        platform=platform,
        company=company,
        direct_owner=owner,
        creator=creator,
        current_user=current,
    )


@personal_router.get("/documents")
async def list_current_user_personal_documents(
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    documents = await service.list_personal_documents(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
        limit=limit,
    )
    return {"documents": [_dataclass_payload(document) for document in documents]}


@personal_router.post("/documents")
async def ingest_current_user_personal_document(
    background_tasks: BackgroundTasks,
    body: PersonalKnowledgeIngestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    tenant_id = _tenant_id_for_user(current_user)
    owner_user_id = uuid.UUID(str(current_user.id))
    result = await service.queue_markdown_import(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        title=body.title,
        markdown=body.markdown,
        source_kind=body.source_kind,
        source_uri=body.source_uri,
        created_by_user_id=owner_user_id,
        agent_searchable=body.agent_searchable,
        sensitivity=body.sensitivity,
    )
    await db.commit()
    _schedule_personal_import_worker(background_tasks, tenant_id=tenant_id, owner_user_id=owner_user_id)
    return _dataclass_payload(result)


@personal_router.post("/imports")
async def import_current_user_personal_file(
    background_tasks: BackgroundTasks,
    title: str | None = Form(None),
    agent_searchable: bool = Form(True),
    sensitivity: str = Form("internal"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")
    data = await file.read()
    service = PersonalKnowledgeService()
    tenant_id = _tenant_id_for_user(current_user)
    owner_user_id = uuid.UUID(str(current_user.id))
    result = await service.queue_source_bytes_import(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        filename=file.filename,
        data=data,
        title=title,
        source_kind="upload",
        source_uri=f"upload://{file.filename}",
        source_mime_type=file.content_type,
        created_by_user_id=owner_user_id,
        agent_searchable=agent_searchable,
        sensitivity=sensitivity,
    )
    await db.commit()
    _schedule_personal_import_worker(background_tasks, tenant_id=tenant_id, owner_user_id=owner_user_id)
    return _dataclass_payload(result)


@personal_router.post("/import-url")
async def import_current_user_personal_url(
    background_tasks: BackgroundTasks,
    body: PersonalKnowledgeUrlImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    tenant_id = _tenant_id_for_user(current_user)
    owner_user_id = uuid.UUID(str(current_user.id))
    try:
        result = await service.queue_url_import(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            url=body.url,
            title=body.title,
            created_by_user_id=owner_user_id,
            agent_searchable=body.agent_searchable,
            sensitivity=body.sensitivity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    _schedule_personal_import_worker(background_tasks, tenant_id=tenant_id, owner_user_id=owner_user_id)
    return _dataclass_payload(result)


@personal_router.get("/import-jobs")
async def list_current_user_personal_import_jobs(
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    jobs = await service.list_import_jobs(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        limit=limit,
    )
    return {"jobs": [_dataclass_payload(job) for job in jobs]}


@personal_router.post("/import-jobs/{job_id}/retry")
async def retry_current_user_personal_import_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    result = await service.retry_import_job(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        job_id=job_id,
        current_user_id=uuid.UUID(str(current_user.id)),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Personal knowledge import job not found")
    await db.commit()
    return _dataclass_payload(result)


@personal_router.get("/grants")
async def list_current_user_personal_grants(
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    grants = await service.list_personal_grants(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        current_user_id=uuid.UUID(str(current_user.id)),
        limit=limit,
    )
    return {"grants": [_dataclass_payload(grant) for grant in grants]}


@personal_router.post("/grants")
async def create_current_user_personal_grant(
    body: PersonalKnowledgeGrantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resource_id = body.resource_id
    if body.resource_type == "scope" and resource_id is None:
        resource_id = uuid.UUID(str(current_user.id))
    if body.resource_type == "document" and resource_id is None:
        resource_id = body.document_id
    service = PersonalKnowledgeService()
    try:
        grant = await service.create_personal_grant(
            db,
            tenant_id=_tenant_id_for_user(current_user),
            owner_user_id=uuid.UUID(str(current_user.id)),
            current_user_id=uuid.UUID(str(current_user.id)),
            resource_type=body.resource_type,
            resource_id=resource_id,
            document_id=body.document_id,
            grantee_type=body.grantee_type,
            grantee_id=body.grantee_id,
            permission=body.permission,
            expires_at=body.expires_at,
            grant_metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if grant is None:
        raise HTTPException(status_code=403, detail="Only the personal knowledge owner can manage grants")
    await db.commit()
    return _dataclass_payload(grant)


@personal_router.delete("/grants/{grant_id}")
async def delete_current_user_personal_grant(
    grant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    deleted = await service.delete_personal_grant(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        current_user_id=uuid.UUID(str(current_user.id)),
        grant_id=grant_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Personal knowledge grant not found")
    await db.commit()
    return {"deleted": True}


@personal_router.get("/search")
async def search_current_user_personal_documents(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    results = await service.search_personal(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        query=q,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
        limit=limit,
    )
    return {"results": [_dataclass_payload(result) for result in results]}


@personal_router.get("/graph")
async def get_current_user_personal_graph(
    limit: int = Query(100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    graph = await service.list_personal_graph(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        current_user_id=uuid.UUID(str(current_user.id)),
        limit=limit,
    )
    return _dataclass_payload(graph)


@personal_router.get("/proposals")
async def list_current_user_personal_knowledge_proposals(
    status: str | None = Query(None, pattern="^(pending|approved|committed|rejected)$"),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner_user_id = uuid.UUID(str(current_user.id))
    proposals = await PersonalKnowledgeProposalService().list_proposals(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=owner_user_id,
        statuses=(status,) if status else None,
        limit=limit,
    )
    return {"proposals": [_dataclass_payload(proposal) for proposal in proposals]}


@personal_router.post("/proposals/{proposal_id}/decision")
async def decide_current_user_personal_knowledge_proposal(
    proposal_id: uuid.UUID,
    body: PersonalKnowledgeProposalDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner_user_id = uuid.UUID(str(current_user.id))
    try:
        proposal = await PersonalKnowledgeProposalService().review(
            db,
            tenant_id=_tenant_id_for_user(current_user),
            owner_user_id=owner_user_id,
            proposal_id=proposal_id,
            reviewer_user_id=owner_user_id,
            decision=body.decision,
            reason=body.reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    await db.commit()
    return _dataclass_payload(proposal)


@personal_router.get("/documents/{document_id}/revisions")
async def list_current_user_personal_knowledge_revisions(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner_user_id = uuid.UUID(str(current_user.id))
    try:
        revisions = await PersonalKnowledgeProposalService().revision_history(
            db,
            tenant_id=_tenant_id_for_user(current_user),
            owner_user_id=owner_user_id,
            document_id=document_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"revisions": _as_jsonable(revisions)}


@personal_router.post("/documents/{document_id}/rollback")
async def rollback_current_user_personal_knowledge_document(
    document_id: uuid.UUID,
    body: PersonalKnowledgeRollbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner_user_id = uuid.UUID(str(current_user.id))
    try:
        result = await PersonalKnowledgeProposalService().rollback_document(
            db,
            tenant_id=_tenant_id_for_user(current_user),
            owner_user_id=owner_user_id,
            document_id=document_id,
            target_version=body.target_version,
            reviewer_user_id=owner_user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return _dataclass_payload(result)


@personal_router.get("/documents/{document_id}")
async def get_current_user_personal_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    document = await service.get_personal_document(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        document_id=document_id,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Personal knowledge document not found")
    return _dataclass_payload(document)


@personal_router.get("/documents/{document_id}/source-preview")
async def get_current_user_personal_document_source_preview(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    preview = await service.get_personal_document_source_preview(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        document_id=document_id,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
    )
    if preview is None:
        raise HTTPException(status_code=404, detail="Personal knowledge source preview not found")
    return _source_preview_response(preview)


@personal_router.patch("/documents/{document_id}")
async def patch_current_user_personal_document(
    document_id: uuid.UUID,
    body: PersonalKnowledgeDocumentPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    document = await service.patch_personal_document(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        document_id=document_id,
        current_user_id=uuid.UUID(str(current_user.id)),
        agent_id=None,
        agent_searchable=body.agent_searchable,
        sensitivity=body.sensitivity,
        status=body.status,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Personal knowledge document not found")
    await db.commit()
    return _dataclass_payload(document)


@personal_router.post("/documents/{document_id}/rebuild-index")
async def rebuild_current_user_personal_document_index(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = PersonalKnowledgeService()
    result = await service.rebuild_personal_document_index(
        db,
        tenant_id=_tenant_id_for_user(current_user),
        owner_user_id=uuid.UUID(str(current_user.id)),
        document_id=document_id,
        current_user_id=uuid.UUID(str(current_user.id)),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Personal knowledge document not found")
    await db.commit()
    return _dataclass_payload(result)


@router.get("/personal/documents")
async def list_personal_documents(
    agent_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    service = PersonalKnowledgeService()
    documents = await service.list_personal_documents(
        db,
        tenant_id=_tenant_id_for_agent(agent, current_user),
        owner_user_id=_owner_user_id_for_personal_kb(agent),
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
        limit=limit,
    )
    return {"documents": [_dataclass_payload(document) for document in documents]}


@router.post("/personal/documents")
async def ingest_personal_document(
    agent_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    body: PersonalKnowledgeIngestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    owner_user_id = _owner_user_id_for_personal_kb(agent)
    if current_user.id != owner_user_id:
        raise HTTPException(status_code=403, detail="Only the personal knowledge owner can write this scope")

    service = PersonalKnowledgeService()
    tenant_id = _tenant_id_for_agent(agent, current_user)
    result = await service.queue_markdown_import(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        title=body.title,
        markdown=body.markdown,
        source_kind=body.source_kind,
        source_uri=body.source_uri,
        created_by_user_id=current_user.id,
        agent_searchable=body.agent_searchable,
        sensitivity=body.sensitivity,
    )
    await db.commit()
    _schedule_personal_import_worker(background_tasks, tenant_id=tenant_id, owner_user_id=owner_user_id)
    return _dataclass_payload(result)


@router.get("/personal/search")
async def search_personal_documents(
    agent_id: uuid.UUID,
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    service = PersonalKnowledgeService()
    results = await service.search_personal(
        db,
        tenant_id=_tenant_id_for_agent(agent, current_user),
        owner_user_id=_owner_user_id_for_personal_kb(agent),
        query=q,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
        limit=limit,
    )
    return {"results": [_dataclass_payload(result) for result in results]}


@router.get("/personal/documents/{document_id}")
async def get_personal_document(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    service = PersonalKnowledgeService()
    document = await service.get_personal_document(
        db,
        tenant_id=_tenant_id_for_agent(agent, current_user),
        owner_user_id=_owner_user_id_for_personal_kb(agent),
        document_id=document_id,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Personal knowledge document not found")
    return _dataclass_payload(document)


@router.get("/personal/documents/{document_id}/source-preview")
async def get_personal_document_source_preview(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    service = PersonalKnowledgeService()
    preview = await service.get_personal_document_source_preview(
        db,
        tenant_id=_tenant_id_for_agent(agent, current_user),
        owner_user_id=_owner_user_id_for_personal_kb(agent),
        document_id=document_id,
        principal=HumanBrowserPrincipal(user_id=uuid.UUID(str(current_user.id))),
    )
    if preview is None:
        raise HTTPException(status_code=404, detail="Personal knowledge source preview not found")
    return _source_preview_response(preview)


@router.get("/overview")
async def get_overview(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.models.runtime_task import RuntimeTask
    from app.services.knowledge_read_model import attach_dream_runtime_status, build_knowledge_overview

    latest_dream = (
        await db.execute(
            select(RuntimeTask)
            .where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type == "dream")
            .order_by(RuntimeTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return attach_dream_runtime_status(build_knowledge_overview(_data_root(), agent_id), latest_dream)


@router.get("/observability")
async def get_observability(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """C8 memory-pipeline observability: consolidation-debt state + trajectory
    and per-axis T2 label aggregates from the derived index tables."""
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import build_memory_observability

    return build_memory_observability(_data_root(), agent_id)


@router.get("/pages")
async def get_pages(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_pages

    return {
        "pages": list_knowledge_pages(
            _data_root(), agent_id, principal_stack=_principal_stack_for_read(agent, current_user)
        )
    }


@router.get("/pages/{page_id:path}")
async def get_page(
    agent_id: uuid.UUID,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import get_knowledge_page

    page = get_knowledge_page(
        _data_root(), agent_id, page_id, principal_stack=_principal_stack_for_read(agent, current_user)
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Knowledge page not found")
    return page


@router.get("/entries")
async def get_entries(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_entries

    return {
        "entries": list_knowledge_entries(
            _data_root(), agent_id, principal_stack=_principal_stack_for_read(agent, current_user)
        )
    }


@router.get("/events")
async def get_events(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_events

    return {"events": list_knowledge_events(_data_root(), agent_id)}


@router.get("/candidates")
async def get_candidates(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await check_agent_access(db, current_user, agent_id)
    from app.services.knowledge_read_model import list_knowledge_candidates

    return list_knowledge_candidates(_data_root(), agent_id)
