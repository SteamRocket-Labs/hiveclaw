"""Project organization references canonical files and works without granting source access."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import get_settings
from app.core.permissions import check_agent_access, is_scoped_business_admin
from app.core.policy import write_audit_event
from app.core.resource_authority import _has_explicit_resource_grant, load_explicit_resource_grant_ids, normalize_workspace_resource_path
from app.core.security import get_current_user
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.database import get_db
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.user import User
from app.models.workbench_project import WorkbenchProject, WorkbenchProjectEntry
from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.content_input_receipts import ContentInputReceiptError, latest_workspace_receipt, validate_browser_workspace_receipt
from app.services.workspace_resource_authority import WorkspaceAuthorityError, authorize_workspace_path

class ProjectRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def without_cached_denial(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "private, no-store"}
                raise

        return without_cached_denial


router = APIRouter(prefix="/projects", tags=["projects"], route_class=ProjectRoute)
KIND = "workbench_project"


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)


class ProjectEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    owner_email: str | None = Field(default=None, min_length=3, max_length=320)

    @model_validator(mode="after")
    def has_change(self):
        if self.title is None and self.archived is None and self.owner_email is None:
            raise ValueError("Select a project change")
        return self


class ProjectEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: uuid.UUID
    expected_revision: int = Field(ge=1)
    target_kind: Literal["workspace_file", "session"]
    agent_id: uuid.UUID
    path: str | None = None
    session_id: uuid.UUID | None = None
    section: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def exact_target(self):
        if self.target_kind == "workspace_file" and (not self.path or self.session_id is not None):
            raise ValueError("Select one canonical workspace path")
        if self.target_kind == "session" and (self.session_id is None or self.path is not None):
            raise ValueError("Select one existing work")
        return self


class ProjectEntryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    section: str | None = Field(default=None, max_length=100)
    removed: bool | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.section is None and self.removed is None:
            raise ValueError("Select a reference change")
        return self


class ProjectGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: uuid.UUID
    expected_revision: int = Field(ge=1)
    email: str = Field(min_length=3, max_length=320)
    role: Literal["reader", "editor"]


class ProjectRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


async def _scope(response: Response, expected_user_id: uuid.UUID, expected_tenant_id: uuid.UUID,
    user: User = Depends(get_current_user), db=Depends(get_db)):
    response.headers["Cache-Control"] = "private, no-store"
    if user.id != expected_user_id:
        raise HTTPException(409, detail={"code": "project_account_changed", "outcome": "not_applied"})
    tenant_id = await resolve_and_pin_tenant_scope(db, user, expected_tenant_id)
    return db, user, tenant_id


def _conflict(code="project_revision_conflict"):
    return HTTPException(409, detail={"code": code, "outcome": "not_applied",
        "message": "Read the current project before revising this request."})


async def _audit(db, user, project, action, **details):
    try:
        await write_audit_event(db, event_type=f"project.{action}", severity="info", actor_type="user", actor_id=user.id,
            tenant_id=project.tenant_id, action=action, resource_type=KIND, resource_id=project.id, details=details)
    except IntegrityError as exc:
        await db.rollback()
        raise _conflict("project_request_conflict") from exc


async def _authority(db, user, project, action):
    if project.owner_user_id == user.id:
        return "resource_owner", True
    if is_scoped_business_admin(user, resource_tenant_id=project.tenant_id):
        return "scoped_business_admin", True
    if await _has_explicit_resource_grant(db, user=user, resource_kind=KIND,
        resource_id=project.id, action=action):
        return "resource_grant", False
    raise HTTPException(403, detail={"code": "project_access_denied"})


async def _project(scope, project_id, action="read", *, lock=False, allow_archived=False):
    db, user, tenant = scope
    statement = select(WorkbenchProject).where(WorkbenchProject.id == project_id, WorkbenchProject.tenant_id == tenant)
    if lock:
        statement = statement.with_for_update()
    project = await db.scalar(statement.execution_options(populate_existing=True))
    if project is None:
        raise HTTPException(404, detail={"code": "project_not_found"})
    authority, manage = await _authority(db, user, project, action)
    if action == "write" and project.archived_at is not None and not allow_archived:
        raise HTTPException(409, detail={"code": "project_archived", "outcome": "not_applied"})
    if authority == "scoped_business_admin":
        await _audit(db, user, project, "administrator_access", operation=action, authority_source=authority)
    return project, manage


async def _summary(db, user, project, manage):
    writable = manage or await _has_explicit_resource_grant(db, user=user,
        resource_kind=KIND, resource_id=project.id, action="write")
    return {"id": str(project.id), "title": project.title, "revision": project.revision,
        "owner_user_id": str(project.owner_user_id) if project.owner_user_id else None,
        "archived": project.archived_at is not None, "can_manage": manage,
        "can_write": writable and project.archived_at is None}


def _revise(project, expected):
    if expected != project.revision:
        raise _conflict()
    project.revision += 1
    project.updated_at = datetime.now(UTC)


async def _commit(db):
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _conflict("project_request_conflict") from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(503, detail={"code": "project_outcome_unknown", "outcome": "unknown",
            "message": "Keep the request identifier and reload the project before retrying."}) from exc


async def _source(db, user, tenant, kind, target_id):
    if kind == "session":
        from app.api.chat_sessions import _get_run_session_and_agent

        session = await db.scalar(select(ChatSession).where(ChatSession.id == target_id, ChatSession.tenant_id == tenant))
        if session is None:
            raise HTTPException(404)
        await _get_run_session_and_agent(db=db, agent_id=session.agent_id, session_id=session.id,
            current_user=user, action="read_workbench")
        return {"agent_id": str(session.agent_id), "session_id": str(session.id), "title": session.title}
    manifest = await db.scalar(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.id == target_id,
        WorkspaceResourceManifest.tenant_id == tenant, WorkspaceResourceManifest.deleted_at.is_(None)))
    if manifest is None:
        raise HTTPException(404)
    access = await check_agent_access(db, user, manifest.agent_id)
    base = Path(get_settings().AGENT_DATA_DIR) / str(manifest.agent_id)
    if not (base / manifest.path).is_file():
        raise HTTPException(404)
    try:
        await authorize_workspace_path(db, user, agent_id=manifest.agent_id, path=manifest.path, action="read",
            path_exists=True, agent_access=access)
        receipt = await latest_workspace_receipt(db, tenant, manifest.agent_id, manifest.path)
        if receipt is not None:
            # Only metadata is projected here. File consumers still capture and verify
            # the actual bytes, hash, sources and current authority on every open.
            await validate_browser_workspace_receipt(db, tenant, user, receipt, receipt.content_sha256)
    except WorkspaceAuthorityError as exc:
        from app.api.files import _workspace_authority_http_error
        raise _workspace_authority_http_error(exc) from exc
    except ContentInputReceiptError as exc:
        from app.api.files import _content_receipt_http_error
        raise _content_receipt_http_error(exc) from exc
    return {"agent_id": str(manifest.agent_id), "path": manifest.path, "title": Path(manifest.path).name}


async def _entry_projection(scope, entry):
    db, user, tenant = scope
    result = {"id": str(entry.id), "target_kind": entry.target_kind, "section": entry.section,
        "removed": entry.removed_at is not None}
    try:
        result.update(await _source(db, user, tenant, entry.target_kind, entry.target_id), state="linked")
    except (HTTPException, WorkspaceAuthorityError, ContentInputReceiptError) as exc:
        status = exc.status_code if hasattr(exc, "status_code") else 403
        if status not in {403, 404, 409}:
            raise
        result["state"] = {403: "denied", 404: "not_found", 409: "needs_reconciliation"}[status]
    return result


@router.get("")
async def list_projects(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    include_archived: bool = False, scope=Depends(_scope)):
    db, user, tenant = scope
    statement = select(WorkbenchProject).where(WorkbenchProject.tenant_id == tenant)
    admin = is_scoped_business_admin(user, resource_tenant_id=tenant)
    if not admin:
        grants = await load_explicit_resource_grant_ids(db, user=user, resource_kind=KIND, action="read")
        statement = statement.where(or_(WorkbenchProject.owner_user_id == user.id, WorkbenchProject.id.in_(grants)))
    if not include_archived:
        statement = statement.where(WorkbenchProject.archived_at.is_(None))
    rows = list(await db.scalars(statement.order_by(WorkbenchProject.created_at, WorkbenchProject.id).offset(offset).limit(limit + 1)))
    result = []
    for row in rows[:limit]:
        try:
            _authority_source, manage = await _authority(db, user, row, "read")
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        if admin and row.owner_user_id != user.id:
            await _audit(db, user, row, "administrator_access", operation="list", authority_source="scoped_business_admin")
        result.append(await _summary(db, user, row, manage))
    await _commit(db)
    return {"projects": result, "next_offset": offset + limit if len(rows) > limit else None}


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, scope=Depends(_scope)):
    db, user, tenant = scope
    existing = await db.scalar(select(WorkbenchProject).where(WorkbenchProject.id == body.id, WorkbenchProject.tenant_id == tenant))
    if existing is not None:
        if existing.owner_user_id != user.id or existing.title != body.title:
            raise _conflict("project_request_conflict")
        return await _summary(db, user, existing, True)
    project = WorkbenchProject(id=body.id, tenant_id=tenant, owner_user_id=user.id, title=body.title, revision=1)
    db.add(project)
    await _audit(db, user, project, "created")
    await _commit(db)
    return await _summary(db, user, project, True)


@router.get("/{project_id}")
async def read_project(project_id: uuid.UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    include_removed: bool = False, scope=Depends(_scope)):
    db, user, tenant = scope
    project, manage = await _project(scope, project_id)
    revision = project.revision
    statement = select(WorkbenchProjectEntry).where(WorkbenchProjectEntry.project_id == project.id, WorkbenchProjectEntry.tenant_id == tenant)
    if not include_removed:
        statement = statement.where(WorkbenchProjectEntry.removed_at.is_(None))
    entries = list(await db.scalars(statement.order_by(WorkbenchProjectEntry.created_at, WorkbenchProjectEntry.id).offset(offset).limit(limit + 1)))
    projected = [await _entry_projection(scope, row) for row in entries[:limit]]
    project, manage = await _project(scope, project_id)
    if project.revision != revision:
        raise _conflict("project_changed_during_read")
    result = {**await _summary(db, user, project, manage), "entries": projected,
        "next_offset": offset + limit if len(entries) > limit else None}
    if manage:
        grants = list(await db.scalars(select(ResourcePermission).where(ResourcePermission.tenant_id == tenant,
            ResourcePermission.resource_type == KIND, ResourcePermission.resource_id == project.id,
            ResourcePermission.effect == "allow", ResourcePermission.revoked_at.is_(None))))
        recipients = {row.id: row for row in await db.scalars(select(User).where(User.tenant_id == tenant,
            User.id.in_([grant.principal_id for grant in grants if grant.principal_type == "user"])))}
        result["grants"] = [{"id": str(row.id), "user_id": str(row.principal_id), "actions": row.actions,
            "recipient_label": recipients[row.principal_id].display_name or recipients[row.principal_id].email
                if row.principal_id in recipients else None} for row in grants]
    await _commit(db)
    return result


async def _recipient(db, tenant, email):
    user = await db.scalar(select(User).where(User.tenant_id == tenant, User.is_active.is_(True), func.lower(User.email) == email.lower()))
    if user is None:
        raise HTTPException(422, detail={"code": "project_recipient_unavailable"})
    return user


@router.get("/{project_id}/entries/{entry_id}")
async def read_project_entry(project_id: uuid.UUID, entry_id: uuid.UUID, scope=Depends(_scope)):
    db, _user, tenant = scope
    project, _manage = await _project(scope, project_id)
    revision = project.revision
    entry = await db.scalar(select(WorkbenchProjectEntry).where(WorkbenchProjectEntry.id == entry_id,
        WorkbenchProjectEntry.project_id == project.id, WorkbenchProjectEntry.tenant_id == tenant))
    if entry is None:
        raise HTTPException(404, detail={"code": "project_reference_not_found"})
    result = await _entry_projection(scope, entry)
    current, _manage = await _project(scope, project_id)
    if current.revision != revision:
        raise _conflict("project_changed_during_read")
    await _commit(db)
    return result


@router.patch("/{project_id}")
async def edit_project(project_id: uuid.UUID, body: ProjectEdit, scope=Depends(_scope)):
    db, user, tenant = scope
    project, manage = await _project(scope, project_id, "write", lock=True, allow_archived=True)
    if (body.owner_email is not None or body.archived is not None) and not manage:
        raise HTTPException(403, detail={"code": "project_manager_required"})
    if project.archived_at is not None and body.archived is not False and body.owner_email is None:
        raise HTTPException(409, detail={"code": "project_archived", "outcome": "not_applied"})
    _revise(project, body.expected_revision)
    if body.title is not None:
        project.title = body.title
    if body.archived is not None:
        project.archived_at = datetime.now(UTC) if body.archived else None
    if body.owner_email is not None:
        project.owner_user_id = (await _recipient(db, tenant, body.owner_email)).id
    await _audit(db, user, project, "updated", fields=sorted(body.model_fields_set - {"expected_revision"}))
    await _commit(db)
    return {"id": str(project.id), "revision": project.revision}


@router.post("/{project_id}/entries", status_code=201)
async def add_entry(project_id: uuid.UUID, body: ProjectEntryCreate, scope=Depends(_scope)):
    db, user, tenant = scope
    project, _manage = await _project(scope, project_id, "write", lock=True)
    if body.target_kind == "workspace_file":
        try:
            path = normalize_workspace_resource_path(body.path)
        except ValueError as exc:
            raise HTTPException(422, detail="Select a canonical workspace path") from exc
        if not path.startswith("workspace/"):
            raise HTTPException(422, detail="Select a canonical workspace path")
        target = await db.scalar(select(WorkspaceResourceManifest.id).where(WorkspaceResourceManifest.tenant_id == tenant,
            WorkspaceResourceManifest.agent_id == body.agent_id, WorkspaceResourceManifest.path == path,
            WorkspaceResourceManifest.deleted_at.is_(None)))
    else:
        target = await db.scalar(select(ChatSession.id).where(ChatSession.tenant_id == tenant,
            ChatSession.agent_id == body.agent_id, ChatSession.id == body.session_id))
    if target is None:
        raise HTTPException(404, detail={"code": "project_source_not_found"})
    await _source(db, user, tenant, body.target_kind, target)
    existing = await db.get(WorkbenchProjectEntry, body.id)
    if existing is not None:
        if (existing.project_id != project.id or existing.target_kind != body.target_kind
            or existing.target_id != target or existing.section != body.section):
            raise _conflict("project_request_conflict")
        return {"id": str(existing.id), "revision": project.revision, "removed": existing.removed_at is not None}
    _revise(project, body.expected_revision)
    entry = WorkbenchProjectEntry(id=body.id, tenant_id=tenant, project_id=project.id,
        target_kind=body.target_kind, target_id=target, section=body.section)
    db.add(entry)
    await _audit(db, user, project, "reference_added", entry_id=str(entry.id), target_kind=entry.target_kind, target_id=str(target))
    await _commit(db)
    return {"id": str(entry.id), "revision": project.revision, "removed": False}


@router.patch("/{project_id}/entries/{entry_id}")
async def edit_entry(project_id: uuid.UUID, entry_id: uuid.UUID, body: ProjectEntryEdit, scope=Depends(_scope)):
    db, user, tenant = scope
    project, _manage = await _project(scope, project_id, "write", lock=True)
    entry = await db.scalar(select(WorkbenchProjectEntry).where(WorkbenchProjectEntry.id == entry_id,
        WorkbenchProjectEntry.project_id == project.id, WorkbenchProjectEntry.tenant_id == tenant))
    if entry is None:
        raise HTTPException(404)
    if body.removed is False:
        await _source(db, user, tenant, entry.target_kind, entry.target_id)
    _revise(project, body.expected_revision)
    if body.section is not None:
        entry.section = body.section
    if body.removed is not None:
        entry.removed_at = datetime.now(UTC) if body.removed else None
    await _audit(db, user, project, "reference_updated", entry_id=str(entry.id))
    await _commit(db)
    return {"id": str(entry.id), "revision": project.revision}


@router.post("/{project_id}/grants", status_code=201)
async def grant_project(project_id: uuid.UUID, body: ProjectGrantCreate, scope=Depends(_scope)):
    db, user, tenant = scope
    project, manage = await _project(scope, project_id, "write", lock=True)
    if not manage:
        raise HTTPException(403, detail={"code": "project_manager_required"})
    recipient = await _recipient(db, tenant, body.email)
    actions = ["read", "write"] if body.role == "editor" else ["read"]
    existing = await db.get(ResourcePermission, body.id)
    if existing is not None:
        if (existing.tenant_id != tenant or existing.resource_type != KIND or existing.resource_id != project.id
            or existing.principal_type != "user" or existing.principal_id != recipient.id or existing.actions != actions
            or existing.effect != "allow" or existing.conditions):
            raise _conflict("project_request_conflict")
        return {"id": str(existing.id), "revision": project.revision, "revoked": existing.revoked_at is not None}
    _revise(project, body.expected_revision)
    db.add(ResourcePermission(id=body.id, tenant_id=tenant, principal_type="user", principal_id=recipient.id,
        resource_type=KIND, resource_id=project.id, actions=actions, conditions={}, effect="allow", created_by_user_id=user.id))
    await _audit(db, user, project, "access_granted", grant_id=str(body.id), recipient_id=str(recipient.id), actions=actions)
    await _commit(db)
    return {"id": str(body.id), "revision": project.revision, "revoked": False}


@router.post("/{project_id}/grants/{grant_id}/revoke")
async def revoke_project_grant(project_id: uuid.UUID, grant_id: uuid.UUID, body: ProjectRevision, scope=Depends(_scope)):
    db, user, tenant = scope
    project, manage = await _project(scope, project_id, "write", lock=True, allow_archived=True)
    if not manage:
        raise HTTPException(403, detail={"code": "project_manager_required"})
    grant = await db.scalar(select(ResourcePermission).where(ResourcePermission.id == grant_id,
        ResourcePermission.tenant_id == tenant, ResourcePermission.resource_type == KIND,
        ResourcePermission.resource_id == project.id).with_for_update())
    if grant is None:
        raise HTTPException(404)
    if grant.revoked_at is None:
        _revise(project, body.expected_revision)
        grant.revoked_at, grant.revoked_by_user_id = datetime.now(UTC), user.id
        await _audit(db, user, project, "access_revoked", grant_id=str(grant.id))
    await _commit(db)
    return {"id": str(grant.id), "revision": project.revision, "revoked": True}
