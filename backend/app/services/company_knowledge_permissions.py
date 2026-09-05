"""Single typed authority resolver for Company Knowledge and Ontology.

The resolver evaluates only authenticated mechanical facts: tenant binding,
explicit ResourcePermission rows, purpose/delegation bindings, source ACL
snapshots, sensitivity, publication state, and complete evidence support. It
never inspects natural-language content and never loads protected content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypeAlias

from sqlalchemy import or_, select

from app.models.security_audit import ResourcePermission
from app.services.privacy_layer import SensitivityLevel, canonicalize_sensitivity, sensitivity_rank


CompanyKnowledgeAction: TypeAlias = Literal[
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
]

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
_RUNTIME_PUBLICATION_ACTIONS = frozenset(
    {"discover", "search", "read", "cite", "export", "execute_action", "query", "simulate"}
)
_CONTENT_ACTIONS = frozenset(
    {
        "search",
        "read",
        "cite",
        "review",
        "approve",
        "publish",
        "export",
        "execute_action",
        "query",
    }
)
_TENANT_ADMIN_METADATA_ACTIONS = frozenset(
    {
        "discover",
        "propose",
        "manage_permissions",
        "install_package",
        "activate_package",
        "curate",
    }
)
_ADMIN_ROLES = frozenset({"org_admin"})
_SCOPED_BUSINESS_ADMIN_ROLES = frozenset({"org_admin", "platform_admin"})


@dataclass(frozen=True, slots=True)
class CompanyKnowledgePrincipal:
    """Accountable and acting principals for one Company Knowledge decision."""

    tenant_id: uuid.UUID
    accountable_user_id: uuid.UUID
    accountable_role: str
    actor_type: str
    actor_id: uuid.UUID
    department_id: uuid.UUID | None = None
    team_ids: tuple[uuid.UUID, ...] = ()
    purpose: str | None = None
    session_id: str | None = None
    runtime_task_id: str | None = None
    workflow_run_id: str | None = None
    delegation_id: str | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "accountable_user_id": str(self.accountable_user_id),
            "accountable_role": self.accountable_role,
            "actor_type": self.actor_type,
            "actor_id": str(self.actor_id),
            "department_id": str(self.department_id) if self.department_id else None,
            "team_ids": [str(item) for item in self.team_ids],
            "purpose": self.purpose,
            "session_id": self.session_id,
            "runtime_task_id": self.runtime_task_id,
            "workflow_run_id": self.workflow_run_id,
            "delegation_id": self.delegation_id,
        }


@dataclass(frozen=True, slots=True)
class CompanyKnowledgeResource:
    """Permission-visible metadata for a Company asset; no protected body bytes."""

    tenant_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID | None
    resource_key: str | None
    namespace: str
    sensitivity: str
    source_acl_snapshot_hash: str | None
    source_acl: dict[str, Any] | None
    evidence_access_complete: bool
    publication_status: str | None
    field_ref: str | None = None
    validity_active: bool = True


@dataclass(frozen=True, slots=True)
class CompanyKnowledgePermissionDecision:
    """Typed, audit-ready result shared by API, tools, UI, and workers.

    ``redaction_policy`` and ``audit_payload`` are evidence fields only: no
    consumer enforces them. The executable authority for PL4 credentials is
    the gateway's hard stop (``company_knowledge_gateway`` reference-only
    projection), and persisted scoped-administrator audit rows are written by
    the API boundary — not by this decision object.
    """

    allowed: bool
    requested_action: CompanyKnowledgeAction
    allowed_actions: tuple[str, ...]
    authority_sources: tuple[str, ...]
    sensitivity_ceiling: str | None
    source_acl_snapshot_hash: str | None
    redaction_policy: str
    deny_reason_code: str | None = None
    approval_requirement: dict[str, Any] | None = None
    retryable: bool = False
    permission_ids: tuple[uuid.UUID, ...] = ()
    audit_payload: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": "hive.company_knowledge_permission_decision.v1",
            "allowed": self.allowed,
            "requested_action": self.requested_action,
            "allowed_actions": list(self.allowed_actions),
            "authority_sources": list(self.authority_sources),
            "deny_reason_code": self.deny_reason_code,
            "sensitivity_ceiling": self.sensitivity_ceiling,
            "source_acl_snapshot_hash": self.source_acl_snapshot_hash,
            "redaction_policy": self.redaction_policy,
            "approval_requirement": self.approval_requirement,
            "retryable": self.retryable,
            "permission_ids": [str(item) for item in self.permission_ids],
            "audit_payload": dict(self.audit_payload),
        }


def _row_value(row: Any) -> Any:
    try:
        return row[0]
    except (TypeError, KeyError, IndexError):
        return row


def _principal_refs(principal: CompanyKnowledgePrincipal) -> set[tuple[str, uuid.UUID | None, str | None]]:
    refs: set[tuple[str, uuid.UUID | None, str | None]] = {
        ("user", principal.accountable_user_id, f"user:{principal.accountable_user_id}"),
        ("role", None, principal.accountable_role),
        ("role", None, f"role:{principal.accountable_role}"),
    }
    if principal.department_id is not None:
        refs.add(("department", principal.department_id, f"department:{principal.department_id}"))
    for team_id in principal.team_ids:
        refs.add(("team", team_id, f"team:{team_id}"))
    refs.add((principal.actor_type, principal.actor_id, f"{principal.actor_type}:{principal.actor_id}"))
    return refs


def _matches_principal(permission: Any, principal: CompanyKnowledgePrincipal) -> bool:
    permission_type = str(getattr(permission, "principal_type", "") or "")
    permission_id = getattr(permission, "principal_id", None)
    permission_key = str(getattr(permission, "principal_key", "") or "") or None
    return any(
        permission_type == principal_type
        and (
            (permission_id is not None and permission_id == principal_id)
            or (permission_key is not None and permission_key == principal_key)
        )
        for principal_type, principal_id, principal_key in _principal_refs(principal)
    )


def _matches_resource(permission: Any, resource: CompanyKnowledgeResource) -> bool:
    permission_type = str(getattr(permission, "resource_type", "") or "")
    permission_id = getattr(permission, "resource_id", None)
    permission_key = str(getattr(permission, "resource_key", "") or "") or None
    exact = permission_type == resource.resource_type and (
        (permission_id is not None and permission_id == resource.resource_id)
        or (permission_key is not None and permission_key == resource.resource_key)
    )
    company_scope = permission_type == "company_knowledge_scope" and (
        permission_id == resource.tenant_id or permission_key == f"tenant:{resource.tenant_id}"
    )
    namespace = permission_type == "company_knowledge_namespace" and permission_key in {
        resource.namespace,
        f"namespace:{resource.namespace}",
    }
    if not (exact or company_scope or namespace):
        return False
    conditions = dict(getattr(permission, "conditions", {}) or {})
    fields = conditions.get("field_refs")
    if fields:
        return resource.field_ref is not None and resource.field_ref in {str(value) for value in fields}
    return True


def _matches_runtime_binding(permission: Any, principal: CompanyKnowledgePrincipal) -> bool:
    purposes = {str(value) for value in (getattr(permission, "purposes", None) or [])}
    if purposes and principal.purpose not in purposes:
        return False
    conditions = dict(getattr(permission, "conditions", {}) or {})
    expected = {
        "accountable_user_id": str(principal.accountable_user_id),
        "session_id": principal.session_id,
        "runtime_task_id": principal.runtime_task_id,
        "workflow_run_id": principal.workflow_run_id,
        "delegation_id": principal.delegation_id,
    }
    for key, actual in expected.items():
        if key in conditions and str(conditions[key]) != str(actual):
            return False
    return True


def _permission_is_live(permission: Any, *, now: datetime) -> bool:
    if getattr(permission, "revoked_at", None) is not None:
        return False
    expires_at = getattr(permission, "expires_at", None)
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


def _matches_acl(principal: CompanyKnowledgePrincipal, source_acl: dict[str, Any]) -> bool:
    if source_acl.get("all_tenant_members") is True or source_acl.get("public") is True:
        return True
    allowed_user_ids = {str(value) for value in source_acl.get("user_ids", [])}
    allowed_roles = {str(value) for value in source_acl.get("role_names", [])}
    allowed_departments = {str(value) for value in source_acl.get("department_ids", [])}
    allowed_agents = {str(value) for value in source_acl.get("agent_ids", [])}
    allowed_teams = {str(value) for value in source_acl.get("team_ids", [])}
    return any(
        (
            str(principal.accountable_user_id) in allowed_user_ids,
            principal.accountable_role in allowed_roles,
            principal.department_id is not None and str(principal.department_id) in allowed_departments,
            principal.actor_type == "agent" and str(principal.actor_id) in allowed_agents,
            any(str(team_id) in allowed_teams for team_id in principal.team_ids),
        )
    )


def _audit_payload(
    *,
    principal: CompanyKnowledgePrincipal,
    resource: CompanyKnowledgeResource,
    action: CompanyKnowledgeAction,
) -> dict[str, Any]:
    return {
        "tenant_id": str(principal.tenant_id),
        "requested_action": action,
        "resource_type": resource.resource_type,
        "resource_id": str(resource.resource_id) if resource.resource_id else None,
        "resource_key": resource.resource_key,
        "namespace": resource.namespace,
        "field_ref": resource.field_ref,
        "sensitivity": resource.sensitivity,
        "source_acl_snapshot_hash": resource.source_acl_snapshot_hash,
        "principal": principal.evidence(),
    }


def _denied(
    *,
    principal: CompanyKnowledgePrincipal,
    resource: CompanyKnowledgeResource,
    action: CompanyKnowledgeAction,
    reason: str,
    authority_sources: tuple[str, ...] = ("tenant_membership",),
    retryable: bool = False,
) -> CompanyKnowledgePermissionDecision:
    return CompanyKnowledgePermissionDecision(
        allowed=False,
        requested_action=action,
        allowed_actions=(),
        authority_sources=authority_sources,
        sensitivity_ceiling=None,
        source_acl_snapshot_hash=resource.source_acl_snapshot_hash,
        redaction_policy="withhold_resource_existence",
        deny_reason_code=reason,
        retryable=retryable,
        audit_payload=_audit_payload(principal=principal, resource=resource, action=action),
    )


async def resolve_company_knowledge_permission(
    session: Any,
    *,
    principal: CompanyKnowledgePrincipal,
    resource: CompanyKnowledgeResource,
    action: CompanyKnowledgeAction,
    now: datetime | None = None,
) -> CompanyKnowledgePermissionDecision:
    """Resolve one Company permission decision without reading protected content."""

    if action not in _ACTIONS:
        raise ValueError(f"unsupported Company Knowledge action: {action}")
    if principal.tenant_id != resource.tenant_id:
        return _denied(principal=principal, resource=resource, action=action, reason="tenant_mismatch")
    if action in _RUNTIME_PUBLICATION_ACTIONS and (
        resource.publication_status != "active" or not resource.validity_active
    ):
        return _denied(principal=principal, resource=resource, action=action, reason="publication_not_active")
    if action in _CONTENT_ACTIONS and (not resource.source_acl_snapshot_hash or resource.source_acl is None):
        return _denied(
            principal=principal,
            resource=resource,
            action=action,
            reason="source_acl_unavailable",
            retryable=True,
        )

    # PDEC-013 human administrator business access. This fast path is keyed on
    # the HUMAN actor (browser/API), never on ``accountable_role`` alone: an
    # Agent runtime principal carrying an administrator's role must not widen
    # worker scope. Organization administrators hold this authority inside
    # their own company (the tenant match above already failed closed
    # otherwise); platform administrators hold it inside the company the
    # request resolved to. No redundant ordinary ResourcePermission is
    # required; provenance stays intact — the source ACL snapshot must exist
    # (checked above), the evidence bundle must be complete, and PL4
    # credentials remain reference-only.
    if (
        principal.actor_type != "agent"
        and principal.accountable_role in _SCOPED_BUSINESS_ADMIN_ROLES
        and action in _CONTENT_ACTIONS | _TENANT_ADMIN_METADATA_ACTIONS
    ):
        if action in _CONTENT_ACTIONS and not resource.evidence_access_complete:
            return _denied(
                principal=principal,
                resource=resource,
                action=action,
                reason="complete_evidence_bundle_required",
                authority_sources=("tenant_membership", "scoped_business_admin"),
            )
        return CompanyKnowledgePermissionDecision(
            allowed=True,
            requested_action=action,
            allowed_actions=tuple(sorted(_CONTENT_ACTIONS | _TENANT_ADMIN_METADATA_ACTIONS)),
            authority_sources=("tenant_membership", "scoped_business_admin"),
            sensitivity_ceiling=SensitivityLevel.PL4_CREDENTIAL.value if action in _CONTENT_ACTIONS else "PL1_public",
            source_acl_snapshot_hash=resource.source_acl_snapshot_hash,
            redaction_policy="credential_reference_only"
            if resource.sensitivity == "PL4_credential"
            else ("metadata_only" if action not in _CONTENT_ACTIONS else "none"),
            audit_payload=_audit_payload(principal=principal, resource=resource, action=action),
        )

    if principal.accountable_role in _ADMIN_ROLES and action in _TENANT_ADMIN_METADATA_ACTIONS:
        return CompanyKnowledgePermissionDecision(
            allowed=True,
            requested_action=action,
            allowed_actions=tuple(sorted(_TENANT_ADMIN_METADATA_ACTIONS)),
            authority_sources=("tenant_membership", "tenant_admin_metadata_governance"),
            sensitivity_ceiling="PL1_public",
            source_acl_snapshot_hash=resource.source_acl_snapshot_hash,
            redaction_policy="metadata_only",
            audit_payload=_audit_payload(principal=principal, resource=resource, action=action),
        )

    current_time = now or datetime.now(timezone.utc)
    result = await session.execute(
        select(ResourcePermission).where(
            ResourcePermission.tenant_id == principal.tenant_id,
            or_(
                ResourcePermission.resource_type == resource.resource_type,
                ResourcePermission.resource_type == "company_knowledge_scope",
                ResourcePermission.resource_type == "company_knowledge_namespace",
            ),
        )
    )
    candidates = [
        permission
        for row in result.all()
        if (permission := _row_value(row)) is not None
        and getattr(permission, "tenant_id", None) == principal.tenant_id
        and _permission_is_live(permission, now=current_time)
        and _matches_principal(permission, principal)
        and _matches_resource(permission, resource)
        and _matches_runtime_binding(permission, principal)
        and (
            not getattr(permission, "source_acl_snapshot_hash", None)
            or getattr(permission, "source_acl_snapshot_hash", None) == resource.source_acl_snapshot_hash
        )
    ]
    denied_rows = [
        permission
        for permission in candidates
        if str(getattr(permission, "effect", "allow") or "allow") == "deny"
        and action in {str(value) for value in (getattr(permission, "actions", None) or [])}
    ]
    if denied_rows:
        return _denied(
            principal=principal,
            resource=resource,
            action=action,
            reason="explicit_deny",
            authority_sources=("tenant_membership", "resource_permission_deny"),
        )

    allow_rows = [
        permission
        for permission in candidates
        if str(getattr(permission, "effect", "allow") or "allow") == "allow"
        and action in {str(value) for value in (getattr(permission, "actions", None) or [])}
    ]
    if not allow_rows:
        return _denied(
            principal=principal,
            resource=resource,
            action=action,
            reason="explicit_resource_permission_required",
        )

    valid_sensitivity_rows: list[tuple[int, Any]] = []
    try:
        resource_sensitivity_rank = sensitivity_rank(resource.sensitivity)
    except ValueError:
        return _denied(
            principal=principal,
            resource=resource,
            action=action,
            reason="resource_sensitivity_invalid",
        )
    for permission in allow_rows:
        try:
            ceiling = canonicalize_sensitivity(getattr(permission, "sensitivity_ceiling", None)).value
            ceiling_rank = sensitivity_rank(ceiling)
        except ValueError:
            continue
        if ceiling_rank >= resource_sensitivity_rank:
            valid_sensitivity_rows.append((ceiling_rank, permission))
    if not valid_sensitivity_rows:
        return _denied(
            principal=principal,
            resource=resource,
            action=action,
            reason="sensitivity_ceiling_exceeded",
            authority_sources=("tenant_membership", "resource_permission"),
        )

    if action in _CONTENT_ACTIONS:
        if not resource.source_acl_snapshot_hash or resource.source_acl is None:
            return _denied(
                principal=principal,
                resource=resource,
                action=action,
                reason="source_acl_unavailable",
                authority_sources=("tenant_membership", "resource_permission"),
                retryable=True,
            )
        if not _matches_acl(principal, resource.source_acl):
            return _denied(
                principal=principal,
                resource=resource,
                action=action,
                reason="source_acl_denied",
                authority_sources=("tenant_membership", "resource_permission"),
            )
        if not resource.evidence_access_complete:
            return _denied(
                principal=principal,
                resource=resource,
                action=action,
                reason="complete_evidence_bundle_required",
                authority_sources=("tenant_membership", "resource_permission", "source_acl_snapshot"),
            )

    allowed_actions = sorted(
        {
            str(candidate_action)
            for _rank, permission in valid_sensitivity_rows
            for candidate_action in (getattr(permission, "actions", None) or [])
            if str(candidate_action) in _ACTIONS
        }
    )
    ceiling_rank, ceiling_permission = max(valid_sensitivity_rows, key=lambda item: item[0])
    del ceiling_rank
    sensitivity_ceiling = canonicalize_sensitivity(getattr(ceiling_permission, "sensitivity_ceiling", None)).value
    authority_sources = ["tenant_membership", "resource_permission"]
    if action in _CONTENT_ACTIONS:
        authority_sources.extend(["source_acl_snapshot", "published_evidence_bundle"])
    redaction_policy = (
        "credential_reference_only"
        if resource.sensitivity == "PL4_credential"
        else ("field_acl" if resource.field_ref else "none")
    )
    return CompanyKnowledgePermissionDecision(
        allowed=True,
        requested_action=action,
        allowed_actions=tuple(allowed_actions),
        authority_sources=tuple(authority_sources),
        sensitivity_ceiling=sensitivity_ceiling,
        source_acl_snapshot_hash=resource.source_acl_snapshot_hash,
        redaction_policy=redaction_policy,
        permission_ids=tuple(sorted((permission.id for _rank, permission in valid_sensitivity_rows), key=str)),
        audit_payload=_audit_payload(principal=principal, resource=resource, action=action),
    )


__all__ = [
    "CompanyKnowledgeAction",
    "CompanyKnowledgePermissionDecision",
    "CompanyKnowledgePrincipal",
    "CompanyKnowledgeResource",
    "resolve_company_knowledge_permission",
]
