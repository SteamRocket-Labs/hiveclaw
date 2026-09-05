"""PDEC-013 administrator business access to Company and Personal Knowledge.

The resolvers are the distinct canonical paths for the two knowledge planes;
these regressions pin the owner contract: a HUMAN scoped administrator gets
role-sourced business access without redundant ordinary grants (PL4 stays
credential-reference-only, evidence completeness stays required), while an
Agent runtime principal carrying an administrator's accountable_role gets no
widening, and ordinary members keep the exact grant-based contract.
"""

from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.services.company_knowledge_permissions import (
    CompanyKnowledgePrincipal,
    CompanyKnowledgeResource,
    resolve_company_knowledge_permission,
)
from app.services.personal_knowledge_access import (
    HumanBrowserPrincipal,
    personal_knowledge_access_predicate,
    resolve_personal_knowledge_permission,
)


class _Rows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[tuple[object]]:
        return [(value,) for value in self._values]

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _NoGrantSession:
    """A session that fails the test if the resolver consults grant rows."""

    async def execute(self, _statement):
        raise AssertionError("scoped administrator access must not consult ordinary grants")


class _GrantSession:
    def __init__(self, permissions: list[object]) -> None:
        self.permissions = permissions

    async def execute(self, _statement):
        return _Rows(self.permissions)


def _company_principal(**overrides) -> CompanyKnowledgePrincipal:
    tenant_id = uuid.uuid4()
    values = dict(
        tenant_id=tenant_id,
        accountable_user_id=uuid.uuid4(),
        accountable_role="org_admin",
        actor_type="user",
        purpose="interactive_session",
        session_id="session-1",
    )
    values["actor_id"] = values["accountable_user_id"]
    values.update(overrides)
    return CompanyKnowledgePrincipal(**values)


def _company_resource(**overrides) -> CompanyKnowledgeResource:
    tenant_id = uuid.uuid4()
    values = dict(
        tenant_id=tenant_id,
        resource_type="company_document",
        resource_id=uuid.uuid4(),
        resource_key=None,
        namespace="company.docs",
        sensitivity="PL2_pii",
        source_acl_snapshot_hash="hash-1",
        source_acl={"user_ids": [str(uuid.uuid4())], "role_names": []},
        evidence_access_complete=True,
        publication_status="active",
    )
    values.update(overrides)
    return CompanyKnowledgeResource(**values)


@pytest.mark.asyncio
async def test_human_org_admin_reads_company_content_without_ordinary_grant() -> None:
    principal = _company_principal()
    resource = _company_resource(tenant_id=principal.tenant_id)

    decision = await resolve_company_knowledge_permission(
        _NoGrantSession(), principal=principal, resource=resource, action="read"
    )

    assert decision.allowed is True
    assert "scoped_business_admin" in decision.authority_sources
    assert decision.redaction_policy == "none"


@pytest.mark.asyncio
async def test_human_platform_admin_reads_company_content_in_resolved_company() -> None:
    principal = _company_principal(accountable_role="platform_admin")
    resource = _company_resource(tenant_id=principal.tenant_id)

    decision = await resolve_company_knowledge_permission(
        _NoGrantSession(), principal=principal, resource=resource, action="search"
    )

    assert decision.allowed is True
    assert "scoped_business_admin" in decision.authority_sources


@pytest.mark.asyncio
async def test_admin_pl4_company_content_stays_credential_reference_only() -> None:
    principal = _company_principal()
    resource = _company_resource(tenant_id=principal.tenant_id, sensitivity="PL4_credential")

    decision = await resolve_company_knowledge_permission(
        _NoGrantSession(), principal=principal, resource=resource, action="read"
    )

    assert decision.allowed is True
    assert decision.redaction_policy == "credential_reference_only"


@pytest.mark.asyncio
async def test_admin_company_content_still_requires_complete_evidence_bundle() -> None:
    principal = _company_principal()
    resource = _company_resource(tenant_id=principal.tenant_id, evidence_access_complete=False)

    decision = await resolve_company_knowledge_permission(
        _NoGrantSession(), principal=principal, resource=resource, action="read"
    )

    assert decision.allowed is False
    assert decision.deny_reason_code == "complete_evidence_bundle_required"


@pytest.mark.asyncio
async def test_agent_actor_with_admin_role_gets_no_business_content_widening() -> None:
    """The distinct human check: an Agent runtime principal carrying an
    administrator's accountable_role never widens worker scope (PDEC-013)."""

    principal = _company_principal(actor_type="agent", actor_id=uuid.uuid4())
    resource = _company_resource(tenant_id=principal.tenant_id)

    decision = await resolve_company_knowledge_permission(
        _GrantSession([]), principal=principal, resource=resource, action="read"
    )

    assert decision.allowed is False
    assert decision.deny_reason_code == "explicit_resource_permission_required"


@pytest.mark.asyncio
async def test_admin_cross_tenant_company_content_still_denied() -> None:
    principal = _company_principal()
    resource = _company_resource()  # different tenant

    decision = await resolve_company_knowledge_permission(
        _NoGrantSession(), principal=principal, resource=resource, action="read"
    )

    assert decision.allowed is False
    assert decision.deny_reason_code == "tenant_mismatch"


# ─── Personal Knowledge ────────────────────────────────────


@pytest.mark.asyncio
async def test_org_admin_reads_employee_personal_document_in_company() -> None:
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    principal = HumanBrowserPrincipal(
        user_id=uuid.uuid4(),
        role="org_admin",
        home_tenant_id=tenant_id,
    )

    decision = await resolve_personal_knowledge_permission(
        _NoGrantSession(),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        principal=principal,
        action="read",
        document_id=uuid.uuid4(),
        document_sensitivity="PL3_sensitive",
    )

    assert decision.allowed is True
    assert decision.authority_source == "scoped_business_admin"
    assert decision.credential_reference_only is False


@pytest.mark.asyncio
async def test_platform_admin_personal_read_requires_the_selected_company() -> None:
    tenant_id = uuid.uuid4()
    # A platform administrator whose authenticated selected company
    # (``home_tenant_id`` on the browser principal) differs from the target
    # company must not read that company's personal knowledge by role alone
    # (PDEC-013 selected-company boundary): deny without consulting grants.
    foreign_principal = HumanBrowserPrincipal(
        user_id=uuid.uuid4(),
        role="platform_admin",
        home_tenant_id=uuid.uuid4(),  # home/selected company differs from the target company
    )

    foreign_decision = await resolve_personal_knowledge_permission(
        _GrantSession([]),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=foreign_principal,
        action="search",
    )

    assert foreign_decision.allowed is False
    assert foreign_decision.authority_source == "none"

    # Inside the authenticated selected company the role authority holds.
    selected_principal = HumanBrowserPrincipal(
        user_id=uuid.uuid4(),
        role="platform_admin",
        home_tenant_id=tenant_id,
    )
    selected_decision = await resolve_personal_knowledge_permission(
        _NoGrantSession(),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=selected_principal,
        action="search",
    )
    assert selected_decision.allowed is True
    assert selected_decision.authority_source == "scoped_business_admin"


@pytest.mark.asyncio
async def test_org_admin_personal_read_never_exposes_pl4_body() -> None:
    tenant_id = uuid.uuid4()
    principal = HumanBrowserPrincipal(user_id=uuid.uuid4(), role="org_admin", home_tenant_id=tenant_id)

    decision = await resolve_personal_knowledge_permission(
        _NoGrantSession(),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=principal,
        action="read",
        document_id=uuid.uuid4(),
        document_sensitivity="PL4_credential",
    )

    assert decision.allowed is True
    assert decision.credential_reference_only is True


@pytest.mark.asyncio
async def test_org_admin_personal_scope_stays_inside_own_company() -> None:
    tenant_id = uuid.uuid4()
    admin = HumanBrowserPrincipal(user_id=uuid.uuid4(), role="org_admin", home_tenant_id=tenant_id)
    foreign_admin = HumanBrowserPrincipal(user_id=uuid.uuid4(), role="org_admin", home_tenant_id=uuid.uuid4())
    member = HumanBrowserPrincipal(user_id=uuid.uuid4())

    decision_admin = await resolve_personal_knowledge_permission(
        _NoGrantSession(),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=admin,
        action="read",
    )
    decision_foreign = await resolve_personal_knowledge_permission(
        _GrantSession([]),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=foreign_admin,
        action="read",
    )
    decision_member = await resolve_personal_knowledge_permission(
        _GrantSession([]),
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=member,
        action="read",
    )

    assert decision_admin.allowed is True
    assert decision_foreign.allowed is False
    assert decision_member.allowed is False


def test_admin_predicate_grants_tenant_wide_personal_visibility() -> None:
    from sqlalchemy.sql import true

    tenant_id = uuid.uuid4()
    admin = HumanBrowserPrincipal(user_id=uuid.uuid4(), role="org_admin", home_tenant_id=tenant_id)

    predicate = personal_knowledge_access_predicate(
        tenant_id=tenant_id,
        owner_user_id=uuid.uuid4(),
        principal=admin,
        action="search",
    )

    # The admin predicate is the unconditional-true expression.
    assert predicate.compare(true())
