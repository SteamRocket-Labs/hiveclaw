from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[tuple[object]]:
        return [(value,) for value in self._values]


class _Session:
    def __init__(self, permissions: list[object]) -> None:
        self.permissions = permissions
        self.executed = 0

    async def execute(self, _statement):
        self.executed += 1
        return _Rows(self.permissions)


def _principal(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
    actor_type: str = "user",
    actor_id: uuid.UUID | None = None,
    purpose: str = "interactive_session",
    session_id: str | None = "session-1",
    delegation_id: str | None = None,
) -> CompanyKnowledgePrincipal:
    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role=role,
        actor_type=actor_type,
        actor_id=actor_id or user_id,
        department_id=None,
        team_ids=(),
        purpose=purpose,
        session_id=session_id,
        runtime_task_id=None,
        workflow_run_id=None,
        delegation_id=delegation_id,
    )


def _resource(
    *,
    tenant_id: uuid.UUID,
    sensitivity: str = "PL2_pii",
    source_acl: dict | None = None,
    evidence_access_complete: bool = True,
    publication_status: str = "active",
) -> CompanyKnowledgeResource:
    document_id = uuid.uuid4()
    return CompanyKnowledgeResource(
        tenant_id=tenant_id,
        resource_type="company_knowledge_document",
        resource_id=document_id,
        resource_key=f"document:{document_id}",
        namespace="company/policies",
        sensitivity=sensitivity,
        source_acl_snapshot_hash="a" * 64,
        source_acl=source_acl
        if source_acl is not None
        else {"user_ids": [], "role_names": ["member"], "department_ids": [], "agent_ids": []},
        evidence_access_complete=evidence_access_complete,
        publication_status=publication_status,
    )


def _permission(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    resource: CompanyKnowledgeResource,
    actions: list[str],
    effect: str = "allow",
    sensitivity_ceiling: str = "PL3_sensitive",
    purposes: list[str] | None = None,
    principal_type: str = "user",
    principal_key: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    conditions: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_id=user_id if principal_key is None else None,
        principal_key=principal_key,
        resource_type=resource.resource_type,
        resource_id=resource.resource_id,
        resource_key=resource.resource_key,
        actions=actions,
        effect=effect,
        sensitivity_ceiling=sensitivity_ceiling,
        purposes=purposes or [],
        source_acl_snapshot_hash=resource.source_acl_snapshot_hash,
        conditions=conditions or {},
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


@pytest.mark.asyncio
async def test_company_read_requires_explicit_grant_for_members_not_admins() -> None:
    """PDEC-013: the human company administrator reads by role; a member
    without a grant stays denied with the same typed reason as before."""

    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    admin = _principal(tenant_id=tenant_id, user_id=admin_id, role="org_admin")
    member = _principal(tenant_id=tenant_id, user_id=member_id, role="member")
    resource = _resource(tenant_id=tenant_id)

    admin_decision = await resolve_company_knowledge_permission(
        _Session([]),
        principal=admin,
        resource=resource,
        action="read",
    )
    assert admin_decision.allowed is True
    assert "scoped_business_admin" in admin_decision.authority_sources

    member_decision = await resolve_company_knowledge_permission(
        _Session([]),
        principal=member,
        resource=resource,
        action="read",
    )
    assert member_decision.allowed is False
    assert member_decision.deny_reason_code == "explicit_resource_permission_required"
    assert member_decision.authority_sources == ("tenant_membership",)
    assert member_decision.audit_payload["resource_id"] == str(resource.resource_id)


@pytest.mark.asyncio
async def test_company_admin_governs_metadata_and_reads_credentials_reference_only() -> None:
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    principal = _principal(tenant_id=tenant_id, user_id=admin_id, role="org_admin")
    resource = _resource(tenant_id=tenant_id, sensitivity="PL4_credential")
    session = _Session([])

    manage = await resolve_company_knowledge_permission(
        session,
        principal=principal,
        resource=resource,
        action="manage_permissions",
    )
    read = await resolve_company_knowledge_permission(
        session,
        principal=principal,
        resource=resource,
        action="read",
    )

    assert manage.allowed is True
    assert manage.authority_sources == ("tenant_membership", "scoped_business_admin")
    assert read.allowed is True
    assert read.redaction_policy == "credential_reference_only"


@pytest.mark.asyncio
async def test_company_review_needs_grant_for_members_and_role_for_admins() -> None:
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    principal = _principal(tenant_id=tenant_id, user_id=admin_id, role="org_admin")
    resource = _resource(
        tenant_id=tenant_id,
        sensitivity="PL3_sensitive",
        source_acl={"role_names": ["org_admin"]},
    )

    member_id = uuid.uuid4()
    member_principal = _principal(tenant_id=tenant_id, user_id=member_id, role="member")
    resource = _resource(
        tenant_id=tenant_id,
        sensitivity="PL3_sensitive",
        source_acl={"user_ids": [str(member_id)]},
    )
    member_denied = await resolve_company_knowledge_permission(
        _Session([]),
        principal=member_principal,
        resource=resource,
        action="review",
    )
    admin_review = await resolve_company_knowledge_permission(
        _Session([]),
        principal=principal,
        resource=resource,
        action="review",
    )
    member_allowed = await resolve_company_knowledge_permission(
        _Session(
            [
                _permission(
                    tenant_id=tenant_id,
                    user_id=member_principal.accountable_user_id,
                    resource=resource,
                    actions=["review"],
                    sensitivity_ceiling="PL3_sensitive",
                )
            ]
        ),
        principal=member_principal,
        resource=resource,
        action="review",
    )

    assert member_denied.allowed is False
    assert member_denied.deny_reason_code == "explicit_resource_permission_required"
    assert admin_review.allowed is True
    assert "scoped_business_admin" in admin_review.authority_sources
    assert member_allowed.allowed is True
    assert "source_acl_snapshot" in member_allowed.authority_sources


@pytest.mark.asyncio
async def test_matching_allow_requires_tenant_source_acl_sensitivity_and_complete_evidence() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    principal = _principal(tenant_id=tenant_id, user_id=user_id)
    resource = _resource(
        tenant_id=tenant_id,
        source_acl={"user_ids": [str(user_id)], "role_names": [], "department_ids": [], "agent_ids": []},
    )
    grant = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["search", "read", "cite"],
    )

    decision = await resolve_company_knowledge_permission(
        _Session([grant]),
        principal=principal,
        resource=resource,
        action="read",
    )

    assert decision.allowed is True
    assert decision.allowed_actions == ("cite", "read", "search")
    assert decision.authority_sources == (
        "tenant_membership",
        "resource_permission",
        "source_acl_snapshot",
        "published_evidence_bundle",
    )
    assert decision.source_acl_snapshot_hash == "a" * 64
    assert decision.redaction_policy == "none"
    assert decision.retryable is False

    incomplete = await resolve_company_knowledge_permission(
        _Session([grant]),
        principal=principal,
        resource=replace(resource, evidence_access_complete=False),
        action="read",
    )
    assert incomplete.allowed is False
    assert incomplete.deny_reason_code == "complete_evidence_bundle_required"


@pytest.mark.asyncio
async def test_explicit_deny_precedes_allow_and_expired_or_revoked_rows_are_ignored() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    principal = _principal(tenant_id=tenant_id, user_id=user_id)
    resource = _resource(tenant_id=tenant_id)
    allow = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["read"],
    )
    deny = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["read"],
        effect="deny",
    )
    expired = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["read"],
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    revoked = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["read"],
        revoked_at=datetime.now(timezone.utc),
    )

    denied = await resolve_company_knowledge_permission(
        _Session([allow, deny]),
        principal=principal,
        resource=resource,
        action="read",
    )
    absent = await resolve_company_knowledge_permission(
        _Session([expired, revoked]),
        principal=principal,
        resource=resource,
        action="read",
    )

    assert denied.allowed is False
    assert denied.deny_reason_code == "explicit_deny"
    assert absent.allowed is False
    assert absent.deny_reason_code == "explicit_resource_permission_required"


@pytest.mark.asyncio
async def test_agent_grant_is_bound_to_accountable_user_session_purpose_and_delegation() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    principal = _principal(
        tenant_id=tenant_id,
        user_id=user_id,
        actor_type="agent",
        actor_id=agent_id,
        purpose="a2a_delegation",
        session_id="session-1",
        delegation_id="delegation-1",
    )
    resource = _resource(
        tenant_id=tenant_id,
        source_acl={"user_ids": [], "role_names": [], "department_ids": [], "agent_ids": [str(agent_id)]},
    )
    grant = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["search"],
        principal_type="agent",
        principal_key=f"agent:{agent_id}",
        purposes=["a2a_delegation"],
        conditions={
            "accountable_user_id": str(user_id),
            "session_id": "session-1",
            "delegation_id": "delegation-1",
        },
    )

    allowed = await resolve_company_knowledge_permission(
        _Session([grant]),
        principal=principal,
        resource=resource,
        action="search",
    )
    wrong_delegation = await resolve_company_knowledge_permission(
        _Session([grant]),
        principal=replace(principal, delegation_id="delegation-2"),
        resource=resource,
        action="search",
    )

    assert allowed.allowed is True
    assert wrong_delegation.allowed is False
    assert wrong_delegation.deny_reason_code == "explicit_resource_permission_required"


@pytest.mark.asyncio
async def test_fail_closed_for_cross_tenant_unpublished_missing_acl_or_acl_denial() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    principal = _principal(tenant_id=tenant_id, user_id=user_id)
    resource = _resource(tenant_id=tenant_id)
    grant = _permission(
        tenant_id=tenant_id,
        user_id=user_id,
        resource=resource,
        actions=["read"],
    )

    cases = (
        (replace(resource, tenant_id=uuid.uuid4()), "tenant_mismatch"),
        (replace(resource, publication_status="retired"), "publication_not_active"),
        (replace(resource, source_acl_snapshot_hash=None), "source_acl_unavailable"),
        (
            replace(
                resource,
                source_acl={"user_ids": [], "role_names": [], "department_ids": [], "agent_ids": []},
            ),
            "source_acl_denied",
        ),
    )
    for candidate, reason in cases:
        decision = await resolve_company_knowledge_permission(
            _Session([grant]),
            principal=principal,
            resource=candidate,
            action="read",
        )
        assert decision.allowed is False
        assert decision.deny_reason_code == reason
