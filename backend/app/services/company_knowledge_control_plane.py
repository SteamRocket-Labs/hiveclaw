"""Governed Company Knowledge permission management and business projections."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.agent_team import AgentTeam
from app.models.company_knowledge import CompanyKnowledgeProposal
from app.models.external_principal import ExternalPrincipal
from app.models.knowledge import KnowledgeDocument
from app.models.security_audit import ResourcePermission
from app.models.user import Department, User
from app.services.company_knowledge_contracts import (
    company_knowledge_proposal_requires_materialization,
)
from app.services.company_knowledge_evidence import (
    CompanyKnowledgeEventInput,
    append_company_knowledge_event,
)
from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.privacy_layer import canonicalize_sensitivity


_PRINCIPAL_TYPES = frozenset({"user", "agent", "role", "department", "team", "integration"})
_ROLE_KEYS = frozenset({"role:member", "role:org_admin", "role:platform_admin"})
_RESOURCE_TYPES = frozenset(
    {
        "company_knowledge_scope",
        "company_knowledge_namespace",
        "company_knowledge_document",
        "company_ontology_namespace",
        "company_ontology_object",
        "company_ontology_assertion",
        "company_ontology_link",
        "company_ontology_event",
    }
)
_ACTIONS = frozenset(
    {
        "discover",
        "search",
        "read",
        "cite",
        "propose",
        "review",
        "approve",
        "publish",
        "retire",
        "restore",
        "manage_permissions",
        "export",
        "execute_action",
        "install_package",
        "activate_package",
        "curate",
        "query",
        "simulate",
    }
)
_PURPOSES = frozenset(
    {
        "interactive_session",
        "autonomous_agent",
        "a2a_delegation",
        "subagent_delegation",
        "workflow",
        "trigger",
    }
)
_REVIEW_QUEUE_STATUSES = frozenset(
    {
        "submitted",
        "in_review",
        "changes_requested",
        "approved",
        "publish_failed",
    }
)
_BUSINESS_CAPABILITIES = (
    ("find_and_read", frozenset({"discover", "search", "read", "cite", "export"})),
    ("propose_updates", frozenset({"propose"})),
    ("review_and_publish", frozenset({"review", "approve", "publish"})),
    (
        "manage_lifecycle",
        frozenset(
            {
                "retire",
                "restore",
                "manage_permissions",
                "install_package",
                "activate_package",
                "curate",
            }
        ),
    ),
    ("use_company_model", frozenset({"query", "simulate", "execute_action"})),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _hash(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompanyKnowledgePermissionGrantInput:
    principal_type: str
    principal_id: uuid.UUID | None
    principal_key: str | None
    resource_type: str
    resource_id: uuid.UUID | None
    resource_key: str | None
    actions: tuple[str, ...]
    effect: str
    sensitivity_ceiling: str
    purposes: tuple[str, ...]
    expires_at: datetime | None
    idempotency_key: str


class CompanyKnowledgeProposalAuthority(Protocol):
    async def authorize_proposal_action(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        proposal: CompanyKnowledgeProposal,
        action: str,
    ) -> dict[str, Any]: ...


def normalize_company_knowledge_permission_grant(
    request: CompanyKnowledgePermissionGrantInput,
    *,
    now: datetime | None = None,
) -> CompanyKnowledgePermissionGrantInput:
    principal_type = str(request.principal_type or "").strip()
    if principal_type not in _PRINCIPAL_TYPES:
        raise ValueError("unsupported_company_knowledge_principal_type")
    if (request.principal_id is None) == (not str(request.principal_key or "").strip()):
        raise ValueError("exactly_one_principal_reference_required")
    principal_key = str(request.principal_key or "").strip() or None
    if principal_type == "role":
        if request.principal_id is not None or principal_key not in _ROLE_KEYS:
            raise ValueError("invalid_company_knowledge_role_reference")
    elif principal_key is not None:
        raise ValueError("company_knowledge_principal_id_required")

    resource_type = str(request.resource_type or "").strip()
    if resource_type not in _RESOURCE_TYPES:
        raise ValueError("unsupported_company_knowledge_resource_type")
    if (request.resource_id is None) == (not str(request.resource_key or "").strip()):
        raise ValueError("exactly_one_resource_reference_required")
    resource_key = str(request.resource_key or "").strip() or None
    if resource_type in {"company_knowledge_scope", "company_knowledge_document"} and request.resource_id is None:
        raise ValueError("company_knowledge_resource_id_required")
    if resource_type in {"company_knowledge_namespace", "company_ontology_namespace"} and (
        resource_key is None or not resource_key.startswith("namespace:")
    ):
        raise ValueError("company_knowledge_namespace_key_required")

    actions = tuple(sorted({str(value).strip() for value in request.actions if str(value).strip()}))
    if not actions or any(action not in _ACTIONS for action in actions):
        raise ValueError("unsupported_company_knowledge_permission_action")
    effect = str(request.effect or "").strip()
    if effect not in {"allow", "deny"}:
        raise ValueError("unsupported_company_knowledge_permission_effect")
    sensitivity_ceiling = canonicalize_sensitivity(request.sensitivity_ceiling).value
    purposes = tuple(sorted({str(value).strip() for value in request.purposes if str(value).strip()}))
    if any(purpose not in _PURPOSES for purpose in purposes):
        raise ValueError("unsupported_company_knowledge_permission_purpose")
    expires_at = request.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= (now or _utcnow()):
            raise ValueError("company_knowledge_permission_expiry_must_be_future")
    idempotency_key = str(request.idempotency_key or "").strip()
    if not idempotency_key:
        raise ValueError("company_knowledge_permission_idempotency_required")
    return CompanyKnowledgePermissionGrantInput(
        principal_type=principal_type,
        principal_id=request.principal_id,
        principal_key=principal_key,
        resource_type=resource_type,
        resource_id=request.resource_id,
        resource_key=resource_key,
        actions=actions,
        effect=effect,
        sensitivity_ceiling=sensitivity_ceiling,
        purposes=purposes,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
    )


def business_capabilities_for_actions(actions: tuple[str, ...]) -> tuple[str, ...]:
    action_set = set(actions)
    return tuple(
        capability for capability, machine_actions in _BUSINESS_CAPABILITIES if action_set.intersection(machine_actions)
    )


class CompanyKnowledgePermissionService:
    """Single mutation boundary for Company Knowledge ``ResourcePermission`` rows."""

    def __init__(self, *, proposal_authority: CompanyKnowledgeProposalAuthority) -> None:
        self._proposal_authority = proposal_authority

    @staticmethod
    async def _require_manage(
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
    ) -> dict[str, Any]:
        decision = await resolve_company_knowledge_permission(
            session,
            principal=principal,
            resource=CompanyKnowledgeResource(
                tenant_id=principal.tenant_id,
                resource_type="company_knowledge_scope",
                resource_id=principal.tenant_id,
                resource_key=f"tenant:{principal.tenant_id}",
                namespace="company",
                sensitivity="PL1_public",
                source_acl_snapshot_hash=None,
                source_acl=None,
                evidence_access_complete=True,
                publication_status=None,
            ),
            action="manage_permissions",
        )
        if not decision.allowed:
            raise PermissionError(decision.deny_reason_code or "company_knowledge_permission_denied")
        return decision.evidence()

    @staticmethod
    async def _validate_principal(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        request: CompanyKnowledgePermissionGrantInput,
    ) -> None:
        if request.principal_type == "role":
            return
        model = {
            "user": User,
            "agent": Agent,
            "department": Department,
            "team": AgentTeam,
            "integration": ExternalPrincipal,
        }[request.principal_type]
        row = await session.get(model, request.principal_id)
        if row is None or uuid.UUID(str(row.tenant_id)) != tenant_id:
            raise LookupError("company_knowledge_permission_principal_not_found")
        if request.principal_type == "user" and getattr(row, "is_active", True) is not True:
            raise ValueError("company_knowledge_permission_principal_inactive")
        if request.principal_type == "agent" and (
            getattr(row, "deleted_at", None) is not None or getattr(row, "deactivated_at", None) is not None
        ):
            raise ValueError("company_knowledge_permission_principal_inactive")
        if request.principal_type == "team" and (
            str(getattr(row, "status", "")) != "active" or getattr(row, "closed_at", None) is not None
        ):
            raise ValueError("company_knowledge_permission_principal_inactive")
        if request.principal_type == "integration" and str(getattr(row, "status", "")) != "active":
            raise ValueError("company_knowledge_permission_principal_inactive")

    @staticmethod
    async def _validate_resource(
        session: Any,
        *,
        tenant_id: uuid.UUID,
        request: CompanyKnowledgePermissionGrantInput,
    ) -> None:
        if request.resource_type == "company_knowledge_scope":
            if request.resource_id != tenant_id:
                raise ValueError("company_knowledge_scope_must_match_tenant")
            return
        if request.resource_type == "company_knowledge_document":
            document = await session.get(KnowledgeDocument, request.resource_id)
            if (
                document is None
                or document.tenant_id != tenant_id
                or document.scope_type != "company"
                or document.scope_id != tenant_id
            ):
                raise LookupError("company_knowledge_permission_resource_not_found")

    async def _principal_label(self, session: Any, row: ResourcePermission) -> str:
        if row.principal_type == "role":
            return {
                "role:member": "All employees",
                "role:org_admin": "Company administrators",
                "role:platform_admin": "Platform administrators",
            }.get(str(row.principal_key), "Company role")
        model_and_field = {
            "user": (User, "display_name"),
            "agent": (Agent, "name"),
            "department": (Department, "name"),
            "team": (AgentTeam, "name"),
            "integration": (ExternalPrincipal, "display_name"),
        }.get(row.principal_type)
        if model_and_field is None or row.principal_id is None:
            return "Unavailable principal"
        model, field = model_and_field
        target = await session.get(model, row.principal_id)
        return str(getattr(target, field, "") or "Unavailable principal")

    @staticmethod
    async def _resource_label(session: Any, row: ResourcePermission) -> tuple[str, str]:
        if row.resource_type == "company_knowledge_scope":
            return "company", "All Company Knowledge"
        if row.resource_type in {"company_knowledge_namespace", "company_ontology_namespace"}:
            return "namespace", str(row.resource_key or "").removeprefix("namespace:")
        if row.resource_type == "company_knowledge_document" and row.resource_id is not None:
            document = await session.get(KnowledgeDocument, row.resource_id)
            return "document", str(getattr(document, "title", "") or "Unavailable document")
        kind = row.resource_type.removeprefix("company_ontology_").replace("_", " ")
        label = str(row.resource_key or kind)
        return kind, label.split(":", 1)[-1]

    async def _summary(self, session: Any, row: ResourcePermission) -> dict[str, Any]:
        now = _utcnow()
        active = row.revoked_at is None and (row.expires_at is None or row.expires_at > now)
        resource_kind, resource_label = await self._resource_label(session, row)
        return {
            "permission_id": str(row.id),
            "principal": {
                "kind": row.principal_type,
                "label": await self._principal_label(session, row),
            },
            "resource": {
                "kind": resource_kind,
                "label": resource_label,
            },
            "capabilities": list(business_capabilities_for_actions(tuple(row.actions or []))),
            "effect": row.effect,
            "sensitivity_ceiling": row.sensitivity_ceiling,
            "purposes": list(row.purposes or []),
            "expires_at": row.expires_at,
            "active": active,
        }

    async def list_permissions(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
    ) -> list[dict[str, Any]]:
        await self._require_manage(session, principal=principal)
        rows = (
            (
                await session.execute(
                    select(ResourcePermission)
                    .where(
                        ResourcePermission.tenant_id == principal.tenant_id,
                        ResourcePermission.resource_type.in_(sorted(_RESOURCE_TYPES)),
                    )
                    .order_by(ResourcePermission.created_at.desc(), ResourcePermission.id)
                )
            )
            .scalars()
            .all()
        )
        return [await self._summary(session, row) for row in rows]

    async def list_review_queue(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        statement = select(CompanyKnowledgeProposal).where(CompanyKnowledgeProposal.tenant_id == principal.tenant_id)
        if status:
            if status not in _REVIEW_QUEUE_STATUSES:
                raise ValueError("unsupported_company_knowledge_review_queue_status")
            statement = statement.where(CompanyKnowledgeProposal.status == status)
        else:
            statement = statement.where(CompanyKnowledgeProposal.status.in_(sorted(_REVIEW_QUEUE_STATUSES)))
        rows = (
            (
                await session.execute(
                    statement.order_by(
                        CompanyKnowledgeProposal.submitted_at.desc().nullslast(),
                        CompanyKnowledgeProposal.created_at.desc(),
                        CompanyKnowledgeProposal.id,
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        result: list[dict[str, Any]] = []
        for proposal in rows:
            try:
                await self._proposal_authority.authorize_proposal_action(
                    session,
                    principal=principal,
                    proposal=proposal,
                    action="review",
                )
            except (LookupError, PermissionError):
                continue
            document_id = proposal.materialized_document_id or proposal.source_document_id
            document = await session.get(KnowledgeDocument, document_id) if document_id else None
            patch = dict(proposal.proposed_patch_json or {})
            result.append(
                {
                    "proposal_id": str(proposal.id),
                    "title": str(getattr(document, "title", "") or "Untitled proposal"),
                    "status": proposal.status,
                    "kind": proposal.proposal_kind,
                    "namespace": proposal.proposed_namespace,
                    "sensitivity": proposal.proposed_sensitivity,
                    "risk_level": proposal.risk_level,
                    "reason": str(patch.get("reason") or ""),
                    "created_by": ("digital_employee" if proposal.created_by_type == "agent" else "company_member"),
                    "state_version": proposal.state_version,
                    "materialization_required": company_knowledge_proposal_requires_materialization(
                        proposal_kind=proposal.proposal_kind,
                        proposed_patch=patch,
                    ),
                    "materialized": proposal.materialized_document_id is not None,
                    "submitted_at": proposal.submitted_at,
                    "updated_at": proposal.updated_at,
                }
            )
        return result

    async def grant_permission(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        request: CompanyKnowledgePermissionGrantInput,
        trace_id: str,
    ) -> dict[str, Any]:
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_manage_company_knowledge_permissions")
        policy = await self._require_manage(session, principal=principal)
        normalized = normalize_company_knowledge_permission_grant(request)
        await self._validate_principal(
            session,
            tenant_id=principal.tenant_id,
            request=normalized,
        )
        await self._validate_resource(
            session,
            tenant_id=principal.tenant_id,
            request=normalized,
        )
        request_hash = _hash(asdict(normalized))
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": (f"company-knowledge-permission:{principal.tenant_id}:{normalized.idempotency_key}")},
        )
        existing = (
            await session.execute(
                select(ResourcePermission)
                .where(
                    ResourcePermission.tenant_id == principal.tenant_id,
                    ResourcePermission.conditions["company_knowledge_management"]["idempotency_key"].astext
                    == normalized.idempotency_key,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            management = dict(existing.conditions or {}).get("company_knowledge_management", {})
            if management.get("request_hash") != request_hash:
                raise ValueError("company_knowledge_permission_idempotency_conflict")
            return await self._summary(session, existing)

        row = ResourcePermission(
            tenant_id=principal.tenant_id,
            principal_type=normalized.principal_type,
            principal_id=normalized.principal_id,
            principal_key=normalized.principal_key,
            resource_type=normalized.resource_type,
            resource_id=normalized.resource_id,
            resource_key=normalized.resource_key,
            actions=list(normalized.actions),
            conditions={
                "company_knowledge_management": {
                    "schema": "hive.company_knowledge_permission_management.v1",
                    "idempotency_key": normalized.idempotency_key,
                    "request_hash": request_hash,
                }
            },
            effect=normalized.effect,
            sensitivity_ceiling=normalized.sensitivity_ceiling,
            purposes=list(normalized.purposes),
            source_acl_snapshot_hash=None,
            expires_at=normalized.expires_at,
            created_by_user_id=principal.accountable_user_id,
        )
        session.add(row)
        await session.flush()
        await append_company_knowledge_event(
            session,
            event_input=CompanyKnowledgeEventInput(
                tenant_id=principal.tenant_id,
                event_type="company_knowledge.permission_granted",
                actor_type=principal.actor_type,
                actor_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
                resource_type="resource_permission",
                resource_id=row.id,
                resource_version=1,
                source_refs=(),
                source_hash=request_hash,
                policy_snapshot=policy,
                trace_id=trace_id,
                request_id=None,
                idempotency_key=f"{normalized.idempotency_key}:granted",
                outcome="active",
                payload={
                    "principal_type": row.principal_type,
                    "resource_type": row.resource_type,
                    "effect": row.effect,
                    "capabilities": list(business_capabilities_for_actions(tuple(normalized.actions))),
                },
                occurred_at=_utcnow(),
            ),
        )
        return await self._summary(session, row)

    async def revoke_permission(
        self,
        session: Any,
        *,
        principal: CompanyKnowledgePrincipal,
        permission_id: uuid.UUID,
        reason: str,
        trace_id: str,
    ) -> dict[str, Any]:
        if principal.actor_type != "user":
            raise PermissionError("agents_cannot_manage_company_knowledge_permissions")
        if not str(reason or "").strip():
            raise ValueError("company_knowledge_permission_revoke_reason_required")
        policy = await self._require_manage(session, principal=principal)
        row = (
            await session.execute(
                select(ResourcePermission)
                .where(
                    ResourcePermission.id == permission_id,
                    ResourcePermission.tenant_id == principal.tenant_id,
                    ResourcePermission.resource_type.in_(sorted(_RESOURCE_TYPES)),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("company_knowledge_permission_not_found")
        if row.revoked_at is not None:
            return {"permission_id": str(row.id), "status": "revoked"}
        now = _utcnow()
        row.revoked_at = now
        row.revoked_by_user_id = principal.accountable_user_id
        await append_company_knowledge_event(
            session,
            event_input=CompanyKnowledgeEventInput(
                tenant_id=principal.tenant_id,
                event_type="company_knowledge.permission_revoked",
                actor_type=principal.actor_type,
                actor_id=principal.actor_id,
                accountable_user_id=principal.accountable_user_id,
                resource_type="resource_permission",
                resource_id=row.id,
                resource_version=1,
                source_refs=(),
                source_hash=_hash(
                    {
                        "permission_id": row.id,
                        "reason": reason,
                        "revoked_at": now,
                    }
                ),
                policy_snapshot=policy,
                trace_id=trace_id,
                request_id=None,
                idempotency_key=f"company-permission:{row.id}:revoked",
                outcome="revoked",
                payload={
                    "reason": str(reason).strip(),
                    "capabilities": list(business_capabilities_for_actions(tuple(row.actions or []))),
                },
                occurred_at=now,
            ),
        )
        return {"permission_id": str(row.id), "status": "revoked"}


__all__ = [
    "CompanyKnowledgePermissionGrantInput",
    "CompanyKnowledgePermissionService",
    "business_capabilities_for_actions",
    "normalize_company_knowledge_permission_grant",
]
