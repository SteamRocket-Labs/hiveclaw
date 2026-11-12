"""One-statement live reader facts for an existing source check point."""

import uuid
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import joinedload, load_only

from app.models.agent import Agent, AgentPermission
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User


def _json_rows(model, fields, *where):
    return select(func.jsonb_agg(func.jsonb_build_object(
        *[value for field in fields for value in (field, getattr(model, field))]
    ))).where(*where).correlate(Agent).scalar_subquery()


def source_reader_statement(user, agent_id, *, source=None):
    """Select live ACL inputs together; the canonical ACL still decides access."""
    scope = _preflight_inputs_for_statement() if isinstance(agent_id, uuid.UUID) and source is None else None
    key = ("source_reader_statement", user.id, agent_id)
    if scope is not None and key in scope:
        statement = scope[key]
        return statement
    permissions = _json_rows(AgentPermission, ("scope_type", "scope_id", "access_level"),
        AgentPermission.agent_id == Agent.id)
    teams = select(func.jsonb_agg(BusinessTeamMembership.business_team_id)).join(
        BusinessTeam, BusinessTeam.id == BusinessTeamMembership.business_team_id).join(
        User, User.id == BusinessTeamMembership.user_id).where(
            BusinessTeamMembership.user_id == user.id, BusinessTeamMembership.status == "active",
            BusinessTeamMembership.tenant_id == Agent.tenant_id, BusinessTeam.status == "active",
            BusinessTeam.tenant_id == Agent.tenant_id, User.is_active.is_(True),
            User.tenant_id == Agent.tenant_id).correlate(Agent).scalar_subquery()
    resources = resource_permission_rows("agent", Agent.id, Agent.tenant_id)
    statement = select(Agent, Tenant.is_active, permissions, teams, resources)
    statement = (statement.select_from(Agent).where(Agent.id == agent_id) if source is None
        else statement.select_from(source).outerjoin(Agent, Agent.id == agent_id))
    statement = (statement.outerjoin(Tenant, Tenant.id == Agent.tenant_id)
        .options(load_only(Agent.id, Agent.tenant_id, Agent.owner_user_id, Agent.creator_id,
            Agent.deleted_at, Agent.deactivated_at, Agent.deactivation_reason, Agent.name,
            Agent.role_description, Agent.status, Agent.agent_class, Agent.agent_type, Agent.created_at),
            joinedload(Agent.owner).load_only(User.id, User.is_active),
            joinedload(Agent.creator).load_only(User.id, User.is_active))
        .execution_options(populate_existing=True))
    if scope is not None:
        scope[key] = statement
    return statement


def _preflight_inputs_for_statement():
    from app.services.content_input_receipts import _DISCLOSURE_SOURCE_VALIDATIONS

    scope = _DISCLOSURE_SOURCE_VALIDATIONS.get()
    return scope.statements if scope is not None and scope.active and scope.source_catalog else None


def source_uuid(value):
    # Match UUID's string normalization without casting malformed stored text.
    normalized = func.lower(func.replace(func.btrim(func.replace(func.replace(value, "urn:", ""), "uuid:", ""), "{}"), "-", ""))
    return case((normalized.op("~")("^[a-f0-9]{32}$"), cast(normalized, UUID(as_uuid=True))))


def resource_permission_rows(kind, resource_id, tenant_id):
    return _json_rows(ResourcePermission, ("principal_type", "principal_id", "actions", "effect",
        "conditions", "revoked_at", "expires_at"), ResourcePermission.resource_type == kind,
        ResourcePermission.resource_id == resource_id, ResourcePermission.tenant_id == tenant_id)


def permission_rows(values):
    rows = []
    for value in values or []:
        fields = dict(value)
        for key in ("scope_id", "principal_id"):
            if fields.get(key) is not None:
                fields[key] = uuid.UUID(fields[key])
        for key in ("revoked_at", "expires_at"):
            if fields.get(key) is not None:
                fields[key] = datetime.fromisoformat(fields[key])
        rows.append(SimpleNamespace(**fields))
    return rows


def model_source_state_columns():
    from app.models.session_v2 import SessionModelResult

    # Reconciliation bookkeeping is live; the immutable request payload is
    # reused separately. Only this one key of the response seal is consulted.
    seal = case((func.jsonb_typeof(SessionModelResult.seal_json) == "object",
        case((SessionModelResult.seal_json == {}, SessionModelResult.seal_json),
            else_=func.jsonb_build_object("provider_response_ref", SessionModelResult.seal_json["provider_response_ref"]))
    ))
    return (SessionModelResult.state, SessionModelResult.reconciliation_owner,
        SessionModelResult.round_committed_event_id, seal)


def reader_columns(*models):
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    columns = {
        User: (User.id, User.tenant_id, User.role, User.department_id, User.is_active, User.username, User.display_name),
        ChatSession: (ChatSession.id, ChatSession.tenant_id, ChatSession.agent_id, ChatSession.user_id,
            ChatSession.audience_kind, ChatSession.external_principal_id, ChatSession.source_channel,
            ChatSession.group_installation_ref, ChatSession.group_subject_id, ChatSession.session_kind,
            ChatSession.runtime_source, ChatSession.runtime_task_id),
        RuntimeTask: (RuntimeTask.id, RuntimeTask.tenant_id, RuntimeTask.root_user_id,
            RuntimeTask.parent_agent_id, RuntimeTask.child_agent_id, RuntimeTask.parent_session_id, RuntimeTask.child_session_id),
    }
    return tuple(load_only(*columns[model]) for model in models)
