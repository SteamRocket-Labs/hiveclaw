"""Authority predicates and read statements for the Personal Knowledge facade."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, TypeAlias

from sqlalchemy import and_, exists, false, func, or_, select, true

from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment


@dataclass(frozen=True, slots=True)
class HumanBrowserPrincipal:
    """A human reading Personal KB content directly through a browser/API."""

    user_id: uuid.UUID
    principal_type: Literal["human_browser"] = "human_browser"

    def evidence(self) -> dict[str, str]:
        return {"principal_type": self.principal_type, "user_id": str(self.user_id)}


@dataclass(frozen=True, slots=True)
class AgentRuntimePrincipal:
    """An Agent tool invocation acting for an authenticated requester."""

    agent_id: uuid.UUID
    requester_user_id: uuid.UUID | None
    session_id: str | None = None
    delegation_id: str | None = None
    principal_type: Literal["agent_runtime"] = "agent_runtime"

    def evidence(self) -> dict[str, str | None]:
        return {
            "principal_type": self.principal_type,
            "agent_id": str(self.agent_id),
            "requester_user_id": str(self.requester_user_id) if self.requester_user_id else None,
            "session_id": self.session_id,
            "delegation_id": self.delegation_id,
        }


PersonalKnowledgePrincipal: TypeAlias = HumanBrowserPrincipal | AgentRuntimePrincipal


def personal_knowledge_access_predicate(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
):
    """Return the owner/grant predicate without mixing human and Agent authority."""

    current_user_id = principal.user_id if isinstance(principal, HumanBrowserPrincipal) else principal.requester_user_id
    if current_user_id == owner_user_id:
        return true()

    owner_agent_predicate = None
    if isinstance(principal, AgentRuntimePrincipal):
        owner_agent_predicate = exists(
            select(1).where(
                Agent.id == principal.agent_id,
                Agent.tenant_id == tenant_id,
                Agent.deleted_at.is_(None),
                func.coalesce(Agent.owner_user_id, Agent.sponsor_user_id, Agent.creator_id) == owner_user_id,
            )
        )

    grantee_predicates = []
    if current_user_id is not None:
        grantee_predicates.append(
            and_(KnowledgeGrant.grantee_type == "user", KnowledgeGrant.grantee_id == current_user_id)
        )
    if isinstance(principal, AgentRuntimePrincipal):
        grantee_predicates.append(
            and_(KnowledgeGrant.grantee_type == "agent", KnowledgeGrant.grantee_id == principal.agent_id)
        )
    if not grantee_predicates:
        return owner_agent_predicate if owner_agent_predicate is not None else false()

    grant_predicate = exists(
        select(1).where(
            KnowledgeGrant.tenant_id == tenant_id,
            KnowledgeGrant.scope_type == "person",
            KnowledgeGrant.scope_id == owner_user_id,
            KnowledgeGrant.permission.in_(("read", "search", "manage")),
            or_(*grantee_predicates),
            or_(
                and_(KnowledgeGrant.resource_type == "scope", KnowledgeGrant.resource_id == owner_user_id),
                and_(KnowledgeGrant.resource_type == "document", KnowledgeGrant.resource_id == KnowledgeDocument.id),
                KnowledgeGrant.document_id == KnowledgeDocument.id,
            ),
            or_(KnowledgeGrant.expires_at.is_(None), KnowledgeGrant.expires_at > func.now()),
        )
    )
    if owner_agent_predicate is not None:
        return or_(owner_agent_predicate, grant_predicate)
    return grant_predicate


def personal_knowledge_agent_visibility_predicate(*, principal: PersonalKnowledgePrincipal):
    if isinstance(principal, HumanBrowserPrincipal):
        return true()
    return KnowledgeDocument.agent_searchable.is_(True)


def build_personal_knowledge_document_list_statement(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
    limit: int,
    document_id: uuid.UUID | None = None,
):
    segment_count = (
        select(func.count(KnowledgeSegment.id))
        .where(
            KnowledgeSegment.tenant_id == tenant_id,
            KnowledgeSegment.document_id == KnowledgeDocument.id,
            KnowledgeSegment.scope_type == "person",
            KnowledgeSegment.scope_id == owner_user_id,
        )
        .correlate(KnowledgeDocument)
        .scalar_subquery()
        .label("segment_count")
    )
    statement = (
        select(KnowledgeDocument, segment_count)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.scope_type == "person",
            KnowledgeDocument.scope_id == owner_user_id,
            KnowledgeDocument.status != "deleted",
            personal_knowledge_agent_visibility_predicate(
                principal=principal,
            ),
            personal_knowledge_access_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                principal=principal,
            ),
        )
        .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.created_at.desc())
        .limit(max(1, int(limit or 50)))
    )
    if document_id is not None:
        statement = statement.where(KnowledgeDocument.id == document_id)
    return statement


# Compatibility aliases remain private to the facade and existing internal callers.
_personal_knowledge_access_predicate = personal_knowledge_access_predicate
_personal_knowledge_agent_visibility_predicate = personal_knowledge_agent_visibility_predicate
