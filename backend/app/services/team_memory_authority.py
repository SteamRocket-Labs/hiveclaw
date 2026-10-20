"""Resolve shared-memory workspaces through existing team and session authority."""

import uuid

from sqlalchemy import select

from app.core.permissions import is_scoped_business_admin
from app.core.source_read_purpose import business_inspection_allowed
from app.core.policy import write_audit_event
from app.models.agent_team import AgentTeam
from app.models.chat_session import ChatSession
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.tenant import Tenant
from app.services.business_teams import BusinessTeamError, _live_user
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source, _runtime_input_actor


def parse_workspace(workspace_key):
    try:
        kind, raw_id = workspace_key.split(":", 1)
        workspace_id = uuid.UUID(raw_id)
        if kind not in {"business_team", "agent_team"} or workspace_key != f"{kind}:{workspace_id}":
            raise ValueError("noncanonical_workspace")
        return kind, workspace_id
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContentInputReceiptError("team_memory_workspace_unbound",
            "Select an authorized team. Unbound legacy memory remains preserved for source confirmation.", 409) from exc


async def _browser_actor(db, user):
    if user is None:
        raise ContentInputReceiptError("team_memory_workspace_denied", "Sign in with a current company account.", 401)
    try:
        return await _live_user(db, user, fresh=True)
    except BusinessTeamError as exc:
        raise ContentInputReceiptError("team_memory_workspace_denied", exc.message, exc.status_code) from exc


async def _live_tenant(db, tenant_id):
    tenant = await db.get(Tenant, tenant_id, populate_existing=True)
    if tenant is None or not tenant.is_active:
        raise ContentInputReceiptError("team_memory_workspace_denied", "This company is no longer active.", 404)


async def authorize_workspace(db, tenant_id, workspace_key, *, user=None, runtime_request=None, write=False):
    kind, identity = parse_workspace(workspace_key)
    await _live_tenant(db, tenant_id)
    if runtime_request is not None:
        _principal, actor = await _runtime_input_actor(db, tenant_id, runtime_request)
    else:
        actor = await _browser_actor(db, user)
    admin = runtime_request is None and business_inspection_allowed() and is_scoped_business_admin(actor, resource_tenant_id=tenant_id)
    if actor is None or not actor.is_active or (actor.tenant_id != tenant_id and not admin):
        raise ContentInputReceiptError("team_memory_workspace_denied", "This team workspace is not available.", 404)
    model = BusinessTeam if kind == "business_team" else AgentTeam
    team = await db.get(model, identity, populate_existing=True)
    if team is None or team.tenant_id != tenant_id:
        raise ContentInputReceiptError("team_memory_workspace_denied", "This team workspace is not available.", 404)
    if kind == "business_team":
        membership = await db.scalar(select(BusinessTeamMembership).where(
            BusinessTeamMembership.tenant_id == tenant_id, BusinessTeamMembership.business_team_id == identity,
            BusinessTeamMembership.user_id == actor.id, BusinessTeamMembership.status == "active"
        ).execution_options(populate_existing=True))
        if (membership is None or team.status != "active") and not admin:
            raise ContentInputReceiptError("team_memory_workspace_denied", "This team workspace is not available.", 404)
        if admin and (membership is None or team.status != "active"):
            await write_audit_event(db, event_type="team_memory.business_access", actor_type="user",
                actor_id=actor.id, tenant_id=tenant_id, action="write" if write else "read",
                resource_type="business_team", resource_id=identity, details={"workspace_kind": kind})
    else:
        session = await db.get(ChatSession, team.parent_session_id, populate_existing=True)
        if session is None or session.tenant_id != tenant_id or session.agent_id != team.lead_agent_id:
            raise ContentInputReceiptError("team_memory_workspace_unavailable", "The parent work needs reconciliation.", 503)
        await _authorize_session_input_source(db, tenant_id, session, user=actor, runtime_request=runtime_request)
    writable = team.status == "active"
    if write and not writable:
        raise ContentInputReceiptError("team_memory_workspace_inactive", "This team is inactive. Restore it before changing its memory.", 403)
    return {"workspace_key": workspace_key, "kind": kind, "name": team.name,
        "status": team.status, "can_write": writable, "can_delete": bool(admin and writable)}


async def available_workspaces(db, tenant_id, *, user, agent_id=None):
    """List only workspaces the current reader can actually open."""
    await _live_tenant(db, tenant_id)
    user = await _browser_actor(db, user)
    if user is None or not user.is_active or (user.tenant_id != tenant_id
        and not is_scoped_business_admin(user, resource_tenant_id=tenant_id)):
        raise ContentInputReceiptError("team_memory_workspace_denied", "This company scope is no longer available.", 404)
    business_ids = select(BusinessTeam.id).where(BusinessTeam.tenant_id == tenant_id)
    if not is_scoped_business_admin(user, resource_tenant_id=tenant_id):
        business_ids = business_ids.join(BusinessTeamMembership,
            BusinessTeamMembership.business_team_id == BusinessTeam.id).where(
                BusinessTeamMembership.user_id == user.id, BusinessTeamMembership.status == "active",
                BusinessTeamMembership.tenant_id == tenant_id, BusinessTeam.status == "active")
    agent_ids = select(AgentTeam.id).where(AgentTeam.tenant_id == tenant_id)
    if agent_id is not None:
        agent_ids = agent_ids.where(AgentTeam.lead_agent_id == agent_id)
    candidates = [("business_team", identity) for identity in (await db.scalars(business_ids)).all()]
    candidates.extend(("agent_team", identity) for identity in (await db.scalars(agent_ids)).all())
    result, unavailable = [], False
    for kind, identity in candidates:
        try:
            result.append(await authorize_workspace(db, tenant_id, f"{kind}:{identity}", user=user))
        except ContentInputReceiptError as exc:
            if exc.status_code >= 500:
                unavailable = True
    return {"tenant_id": str(tenant_id), "user_id": str(user.id), "workspaces": result, "unavailable": unavailable}
