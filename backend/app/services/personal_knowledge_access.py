"""Authority predicates and read statements for the Personal Knowledge facade."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from typing import Literal, TypeAlias

from sqlalchemy import and_, case, exists, false, func, or_, select, true

from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment
from app.services.privacy_layer import SensitivityLevel, canonicalize_sensitivity, sensitivity_rank


_AGENT_GRANT_PURPOSES = frozenset(
    {
        "interactive_session",
        "autonomous_agent",
        "a2a_delegation",
        "subagent_delegation",
    }
)
_DELEGATED_PURPOSES = frozenset({"a2a_delegation", "subagent_delegation"})


@dataclass(frozen=True, slots=True)
class HumanBrowserPrincipal:
    """A human reading Personal KB content directly through a browser/API."""

    user_id: uuid.UUID
    principal_type: Literal["human_browser"] = "human_browser"
    # Canonical live-role facts for the PDEC-013 scoped business administrator
    # branch. Absent (default) preserves the legacy owner/grant-only contract.
    role: str | None = None
    home_tenant_id: uuid.UUID | None = None

    def evidence(self) -> dict[str, str | None]:
        return {
            "principal_type": self.principal_type,
            "user_id": str(self.user_id),
            "role": self.role,
            "home_tenant_id": str(self.home_tenant_id) if self.home_tenant_id else None,
        }

    def scoped_business_admin_for(self, tenant_id: uuid.UUID) -> bool:
        """PDEC-013 business scope: company administrators inside their own
        company, platform administrators inside the authenticated selected
        company (``home_tenant_id`` carries that selection for browser
        principals). Never granted to Agent runtime principals, and never for
        a company other than the authenticated one."""
        if self.role not in ("org_admin", "platform_admin"):
            return False
        return self.home_tenant_id is not None and str(self.home_tenant_id) == str(tenant_id)


@dataclass(frozen=True, slots=True)
class AgentRuntimePrincipal:
    """An Agent tool invocation acting for an authenticated requester."""

    agent_id: uuid.UUID
    requester_user_id: uuid.UUID | None
    session_id: str | None = None
    runtime_task_id: str | None = None
    delegation_id: str | None = None
    purpose: str = "interactive_session"
    autonomous: bool = False
    principal_type: Literal["agent_runtime"] = "agent_runtime"

    def evidence(self) -> dict[str, str | bool | None]:
        return {
            "principal_type": self.principal_type,
            "agent_id": str(self.agent_id),
            "requester_user_id": str(self.requester_user_id) if self.requester_user_id else None,
            "session_id": self.session_id,
            "runtime_task_id": self.runtime_task_id,
            "delegation_id": self.delegation_id,
            "purpose": self.purpose,
            "autonomous": self.autonomous,
        }


PersonalKnowledgePrincipal: TypeAlias = HumanBrowserPrincipal | AgentRuntimePrincipal
PersonalKnowledgeAction: TypeAlias = Literal["search", "read"]


@dataclass(frozen=True, slots=True)
class PersonalKnowledgePermissionDecision:
    """Typed mechanical result for one Personal Knowledge resource action."""

    allowed: bool
    action: PersonalKnowledgeAction
    owner_user_id: uuid.UUID
    authority_source: str
    sensitivity_ceiling: str | None
    deny_reason_code: str | None = None
    document_id: uuid.UUID | None = None
    document_sensitivity: str | None = None
    grant_id: uuid.UUID | None = None
    expires_at: datetime | None = None
    credential_reference_only: bool = False
    retryable: bool = False
    principal: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": "hive.personal_knowledge_permission_decision.v1",
            "allowed": self.allowed,
            "action": self.action,
            "owner_user_id": str(self.owner_user_id),
            "authority_source": self.authority_source,
            "sensitivity_ceiling": self.sensitivity_ceiling,
            "deny_reason_code": self.deny_reason_code,
            "document_id": str(self.document_id) if self.document_id else None,
            "document_sensitivity": self.document_sensitivity,
            "grant_id": str(self.grant_id) if self.grant_id else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "credential_reference_only": self.credential_reference_only,
            "retryable": self.retryable,
            "principal": dict(self.principal),
        }


def _permission_values(action: PersonalKnowledgeAction) -> tuple[str, ...]:
    if action == "search":
        return ("search", "read", "manage")
    if action == "read":
        return ("read", "manage")
    raise ValueError(f"unsupported Personal Knowledge action: {action}")


def _document_sensitivity_rank():
    return case(
        (KnowledgeDocument.sensitivity == "PL1_public", 1),
        (KnowledgeDocument.sensitivity == "PL2_pii", 2),
        (KnowledgeDocument.sensitivity == "PL3_sensitive", 3),
        (KnowledgeDocument.sensitivity == "PL4_credential", 4),
        else_=5,
    )


def _grant_sensitivity_rank():
    return case(
        (KnowledgeGrant.sensitivity_ceiling == "PL1_public", 1),
        (KnowledgeGrant.sensitivity_ceiling == "PL2_pii", 2),
        (KnowledgeGrant.sensitivity_ceiling == "PL3_sensitive", 3),
        (KnowledgeGrant.sensitivity_ceiling == "PL4_credential", 4),
        else_=0,
    )


def _resource_grant_predicate(*, owner_user_id: uuid.UUID):
    return or_(
        and_(KnowledgeGrant.resource_type == "scope", KnowledgeGrant.resource_id == owner_user_id),
        and_(KnowledgeGrant.resource_type == "document", KnowledgeGrant.resource_id == KnowledgeDocument.id),
        KnowledgeGrant.document_id == KnowledgeDocument.id,
    )


def _owner_agent_predicate(*, tenant_id: uuid.UUID, owner_user_id: uuid.UUID, agent_id: uuid.UUID):
    return exists(
        select(1).where(
            Agent.id == agent_id,
            Agent.tenant_id == tenant_id,
            Agent.deleted_at.is_(None),
            func.coalesce(Agent.owner_user_id, Agent.creator_id) == owner_user_id,
        )
    )


def _agent_grant_binding_predicate(principal: AgentRuntimePrincipal):
    if principal.purpose not in _AGENT_GRANT_PURPOSES:
        return false()
    if principal.purpose == "autonomous_agent":
        session_predicate = or_(
            KnowledgeGrant.session_id.is_(None),
            KnowledgeGrant.session_id == principal.session_id,
        )
        delegation_predicate = KnowledgeGrant.delegation_id.is_(None)
    else:
        if not principal.session_id:
            return false()
        session_predicate = KnowledgeGrant.session_id == principal.session_id
        if principal.purpose in _DELEGATED_PURPOSES:
            if not principal.delegation_id:
                return false()
            delegation_predicate = KnowledgeGrant.delegation_id == principal.delegation_id
        else:
            delegation_predicate = KnowledgeGrant.delegation_id.is_(None)
    return and_(
        KnowledgeGrant.grantee_type == "agent",
        KnowledgeGrant.grantee_id == principal.agent_id,
        KnowledgeGrant.requester_user_id == principal.requester_user_id,
        KnowledgeGrant.purpose == principal.purpose,
        session_predicate,
        delegation_predicate,
    )


def personal_knowledge_access_predicate(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
    action: PersonalKnowledgeAction = "search",
):
    """Return the requester/actor/grant predicate evaluated before content bytes leave PostgreSQL."""

    current_user_id = principal.user_id if isinstance(principal, HumanBrowserPrincipal) else principal.requester_user_id
    if isinstance(principal, HumanBrowserPrincipal) and current_user_id == owner_user_id:
        return true()

    if current_user_id is None:
        return false()

    if isinstance(principal, HumanBrowserPrincipal) and principal.scoped_business_admin_for(tenant_id):
        # PDEC-013: a scoped business administrator reads the managed
        # company's personal knowledge for business purposes. The service
        # layer still applies PL4 credential reference-only projection.
        return true()

    if isinstance(principal, HumanBrowserPrincipal):
        grantee_predicate = and_(
            KnowledgeGrant.grantee_type == "user",
            KnowledgeGrant.grantee_id == current_user_id,
        )
    else:
        if principal.requester_user_id is None:
            return false()
        grantee_predicate = _agent_grant_binding_predicate(principal)

    grant_predicate = exists(
        select(1).where(
            KnowledgeGrant.tenant_id == tenant_id,
            KnowledgeGrant.scope_type == "person",
            KnowledgeGrant.scope_id == owner_user_id,
            KnowledgeGrant.permission.in_(_permission_values(action)),
            grantee_predicate,
            _resource_grant_predicate(owner_user_id=owner_user_id),
            _grant_sensitivity_rank() >= _document_sensitivity_rank(),
            KnowledgeGrant.revoked_at.is_(None),
            or_(KnowledgeGrant.expires_at.is_(None), KnowledgeGrant.expires_at > func.now()),
        )
    )
    if (
        isinstance(principal, AgentRuntimePrincipal)
        and current_user_id == owner_user_id
        and not principal.autonomous
        and principal.purpose == "interactive_session"
        and principal.session_id is not None
        and principal.delegation_id is None
    ):
        return or_(
            _owner_agent_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                agent_id=principal.agent_id,
            ),
            grant_predicate,
        )
    return grant_predicate


def _row_value(row: Any) -> Any:
    try:
        return row[0]
    except (TypeError, KeyError, IndexError):
        return row


def _denied_decision(
    *,
    action: PersonalKnowledgeAction,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
    reason_code: str,
    document_id: uuid.UUID | None = None,
    document_sensitivity: str | None = None,
) -> PersonalKnowledgePermissionDecision:
    return PersonalKnowledgePermissionDecision(
        allowed=False,
        action=action,
        owner_user_id=owner_user_id,
        authority_source="none",
        sensitivity_ceiling=None,
        deny_reason_code=reason_code,
        document_id=document_id,
        document_sensitivity=document_sensitivity,
        principal=principal.evidence(),
    )


def _grant_matches_runtime(grant: Any, principal: PersonalKnowledgePrincipal) -> bool:
    if isinstance(principal, HumanBrowserPrincipal):
        return (
            str(getattr(grant, "grantee_type", "")) == "user"
            and getattr(grant, "grantee_id", None) == principal.user_id
        )
    if principal.requester_user_id is None or principal.purpose not in _AGENT_GRANT_PURPOSES:
        return False
    if str(getattr(grant, "grantee_type", "")) != "agent" or getattr(grant, "grantee_id", None) != principal.agent_id:
        return False
    if getattr(grant, "requester_user_id", None) != principal.requester_user_id:
        return False
    if str(getattr(grant, "purpose", "")) != principal.purpose:
        return False
    grant_session_id = str(getattr(grant, "session_id", "") or "") or None
    grant_delegation_id = str(getattr(grant, "delegation_id", "") or "") or None
    if principal.purpose == "autonomous_agent":
        return grant_delegation_id is None and (grant_session_id is None or grant_session_id == principal.session_id)
    if not principal.session_id or grant_session_id != principal.session_id:
        return False
    if principal.purpose in _DELEGATED_PURPOSES:
        return bool(principal.delegation_id) and grant_delegation_id == principal.delegation_id
    return grant_delegation_id is None


async def resolve_personal_knowledge_permission(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
    action: PersonalKnowledgeAction,
    document_id: uuid.UUID | None = None,
    document_sensitivity: str | None = None,
) -> PersonalKnowledgePermissionDecision:
    """Resolve one typed read decision without loading document title or segment bytes."""

    if isinstance(principal, HumanBrowserPrincipal) and principal.user_id == owner_user_id:
        credential_only = False
        if document_sensitivity is not None:
            try:
                credential_only = canonicalize_sensitivity(document_sensitivity) == SensitivityLevel.PL4_CREDENTIAL
            except ValueError:
                return _denied_decision(
                    action=action,
                    owner_user_id=owner_user_id,
                    principal=principal,
                    reason_code="document_sensitivity_invalid",
                    document_id=document_id,
                    document_sensitivity=document_sensitivity,
                )
        return PersonalKnowledgePermissionDecision(
            allowed=True,
            action=action,
            owner_user_id=owner_user_id,
            authority_source="human_owner_direct",
            sensitivity_ceiling=SensitivityLevel.PL4_CREDENTIAL.value,
            document_id=document_id,
            document_sensitivity=document_sensitivity,
            credential_reference_only=credential_only,
            principal=principal.evidence(),
        )

    if isinstance(principal, HumanBrowserPrincipal) and principal.scoped_business_admin_for(tenant_id):
        credential_only = False
        if document_sensitivity is not None:
            try:
                credential_only = canonicalize_sensitivity(document_sensitivity) == SensitivityLevel.PL4_CREDENTIAL
            except ValueError:
                return _denied_decision(
                    action=action,
                    owner_user_id=owner_user_id,
                    principal=principal,
                    reason_code="document_sensitivity_invalid",
                    document_id=document_id,
                    document_sensitivity=document_sensitivity,
                )
        return PersonalKnowledgePermissionDecision(
            allowed=True,
            action=action,
            owner_user_id=owner_user_id,
            authority_source="scoped_business_admin",
            sensitivity_ceiling=SensitivityLevel.PL4_CREDENTIAL.value,
            document_id=document_id,
            document_sensitivity=document_sensitivity,
            credential_reference_only=credential_only,
            principal=principal.evidence(),
        )

    if isinstance(principal, AgentRuntimePrincipal):
        if principal.requester_user_id is None:
            return _denied_decision(
                action=action,
                owner_user_id=owner_user_id,
                principal=principal,
                reason_code="requester_user_id_missing",
                document_id=document_id,
                document_sensitivity=document_sensitivity,
            )
        if principal.purpose not in _AGENT_GRANT_PURPOSES:
            return _denied_decision(
                action=action,
                owner_user_id=owner_user_id,
                principal=principal,
                reason_code="principal_purpose_invalid",
                document_id=document_id,
                document_sensitivity=document_sensitivity,
            )
        if principal.purpose != "autonomous_agent" and not principal.session_id:
            return _denied_decision(
                action=action,
                owner_user_id=owner_user_id,
                principal=principal,
                reason_code="session_binding_missing",
                document_id=document_id,
                document_sensitivity=document_sensitivity,
            )
        if principal.purpose in _DELEGATED_PURPOSES and not principal.delegation_id:
            return _denied_decision(
                action=action,
                owner_user_id=owner_user_id,
                principal=principal,
                reason_code="delegation_binding_missing",
                document_id=document_id,
                document_sensitivity=document_sensitivity,
            )
        if (
            principal.requester_user_id == owner_user_id
            and not principal.autonomous
            and principal.purpose == "interactive_session"
            and principal.session_id
            and principal.delegation_id is None
        ):
            owner_agent = (
                await session.execute(
                    select(Agent.id).where(
                        Agent.id == principal.agent_id,
                        Agent.tenant_id == tenant_id,
                        Agent.deleted_at.is_(None),
                        func.coalesce(Agent.owner_user_id, Agent.creator_id) == owner_user_id,
                    )
                )
            ).scalar_one_or_none()
            if owner_agent is not None:
                credential_only = False
                if document_sensitivity is not None:
                    try:
                        credential_only = (
                            canonicalize_sensitivity(document_sensitivity) == SensitivityLevel.PL4_CREDENTIAL
                        )
                    except ValueError:
                        return _denied_decision(
                            action=action,
                            owner_user_id=owner_user_id,
                            principal=principal,
                            reason_code="document_sensitivity_invalid",
                            document_id=document_id,
                            document_sensitivity=document_sensitivity,
                        )
                return PersonalKnowledgePermissionDecision(
                    allowed=True,
                    action=action,
                    owner_user_id=owner_user_id,
                    authority_source="interactive_owner_agent",
                    sensitivity_ceiling=SensitivityLevel.PL4_CREDENTIAL.value,
                    document_id=document_id,
                    document_sensitivity=document_sensitivity,
                    credential_reference_only=credential_only,
                    principal=principal.evidence(),
                )

    actor_predicate = (
        and_(KnowledgeGrant.grantee_type == "user", KnowledgeGrant.grantee_id == principal.user_id)
        if isinstance(principal, HumanBrowserPrincipal)
        else _agent_grant_binding_predicate(principal)
    )
    if document_id is None:
        resource_predicate = or_(
            and_(KnowledgeGrant.resource_type == "scope", KnowledgeGrant.resource_id == owner_user_id),
            KnowledgeGrant.resource_type == "document",
        )
    else:
        resource_predicate = or_(
            and_(KnowledgeGrant.resource_type == "scope", KnowledgeGrant.resource_id == owner_user_id),
            and_(KnowledgeGrant.resource_type == "document", KnowledgeGrant.resource_id == document_id),
            KnowledgeGrant.document_id == document_id,
        )
    rows = (
        await session.execute(
            select(KnowledgeGrant).where(
                KnowledgeGrant.tenant_id == tenant_id,
                KnowledgeGrant.scope_type == "person",
                KnowledgeGrant.scope_id == owner_user_id,
                KnowledgeGrant.permission.in_(_permission_values(action)),
                actor_predicate,
                resource_predicate,
                KnowledgeGrant.revoked_at.is_(None),
                or_(KnowledgeGrant.expires_at.is_(None), KnowledgeGrant.expires_at > func.now()),
            )
        )
    ).all()
    grants = [
        grant for row in rows if (grant := _row_value(row)) is not None and _grant_matches_runtime(grant, principal)
    ]
    if not grants:
        return _denied_decision(
            action=action,
            owner_user_id=owner_user_id,
            principal=principal,
            reason_code="explicit_grant_required",
            document_id=document_id,
            document_sensitivity=document_sensitivity,
        )

    valid_grants: list[tuple[int, Any]] = []
    for grant in grants:
        try:
            ceiling_rank = sensitivity_rank(getattr(grant, "sensitivity_ceiling", None))
        except ValueError:
            continue
        if document_sensitivity is not None:
            try:
                if ceiling_rank < sensitivity_rank(document_sensitivity):
                    continue
            except ValueError:
                return _denied_decision(
                    action=action,
                    owner_user_id=owner_user_id,
                    principal=principal,
                    reason_code="document_sensitivity_invalid",
                    document_id=document_id,
                    document_sensitivity=document_sensitivity,
                )
        valid_grants.append((ceiling_rank, grant))
    if not valid_grants:
        return _denied_decision(
            action=action,
            owner_user_id=owner_user_id,
            principal=principal,
            reason_code="sensitivity_ceiling_exceeded",
            document_id=document_id,
            document_sensitivity=document_sensitivity,
        )

    _rank, grant = max(valid_grants, key=lambda item: item[0])
    ceiling = canonicalize_sensitivity(getattr(grant, "sensitivity_ceiling", None)).value
    credential_only = False
    if document_sensitivity is not None:
        credential_only = canonicalize_sensitivity(document_sensitivity) == SensitivityLevel.PL4_CREDENTIAL
    return PersonalKnowledgePermissionDecision(
        allowed=True,
        action=action,
        owner_user_id=owner_user_id,
        authority_source=(
            "explicit_document_grant"
            if str(getattr(grant, "resource_type", "")) == "document"
            else f"explicit_{getattr(grant, 'grantee_type', 'principal')}_grant"
        ),
        sensitivity_ceiling=ceiling,
        document_id=document_id,
        document_sensitivity=document_sensitivity,
        grant_id=getattr(grant, "id", None),
        expires_at=getattr(grant, "expires_at", None),
        credential_reference_only=credential_only,
        principal=principal.evidence(),
    )


def personal_knowledge_agent_visibility_predicate(*, principal: PersonalKnowledgePrincipal):
    if isinstance(principal, HumanBrowserPrincipal):
        return true()
    return KnowledgeDocument.agent_searchable.is_(True)


def personal_knowledge_consumable_status_predicate(*, principal: PersonalKnowledgePrincipal):
    """Archive is a real consumption boundary: an Agent runtime only receives
    content for consumable (ready/degraded) documents, while the owner browser
    keeps full workbench visibility (including archived) for Restore."""
    if isinstance(principal, AgentRuntimePrincipal):
        return KnowledgeDocument.status.in_(["ready", "degraded"])
    return KnowledgeDocument.status != "deleted"


def build_personal_knowledge_document_list_statement(
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    principal: PersonalKnowledgePrincipal,
    action: PersonalKnowledgeAction = "read",
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
            personal_knowledge_consumable_status_predicate(principal=principal),
            personal_knowledge_agent_visibility_predicate(
                principal=principal,
            ),
            personal_knowledge_access_predicate(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                principal=principal,
                action=action,
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
