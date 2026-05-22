from __future__ import annotations

from app.services.action_preflight import CharterZone
from app.services.agency_charter import (
    AgentAccountabilityContext,
    CompanyCharter,
    OwnerAgencyCharter,
    build_default_accountability_context,
)
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


def test_default_accountability_context_states_company_and_owner() -> None:
    context = build_default_accountability_context(
        company_id="tenant-1",
        company_name="Acme",
        owner_id="owner-1",
        owner_name="Alice",
        current_user_id="owner-1",
        current_user_name="Alice",
    )

    assert context.principal_stack.current_user_is_direct_owner
    assert context.company_charter.company_name == "Acme"
    assert context.owner_charter.owner_name == "Alice"
    assert "directly support Alice" in context.identity_statement()
    assert "within Acme's company charter" in context.identity_statement()


def test_owner_charter_classifies_full_authority_confirm_first_and_never_do() -> None:
    charter = OwnerAgencyCharter(
        owner_id="owner-1",
        owner_name="Alice",
        full_authority=("prepare local research brief",),
        confirm_first=("send external vendor reply",),
        never_do=("share credentials",),
    )

    assert charter.zone_for("prepare local research brief for Alice") == CharterZone.FULL_AUTHORITY
    assert charter.zone_for("send external vendor reply about pricing") == CharterZone.CONFIRM_FIRST
    assert charter.zone_for("share credentials with a vendor") == CharterZone.NEVER_DO
    assert charter.zone_for("do an uncategorized action") == CharterZone.CONFIRM_FIRST


def test_company_boundary_conflict_is_exposed_above_owner_authority() -> None:
    stack = PrincipalStack(
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "owner-1", "Alice"),
        current_user=Principal(PrincipalRole.CURRENT_USER, "owner-1", "Alice"),
    )
    context = AgentAccountabilityContext(
        principal_stack=stack,
        company_charter=CompanyCharter(
            company_id="tenant-1",
            company_name="Acme",
            goals=("Protect Acme reputation.",),
            boundaries=("external refund commitment requires finance approval",),
            escalation_targets=("company-admin",),
        ),
        owner_charter=OwnerAgencyCharter(
            owner_id="owner-1",
            owner_name="Alice",
            full_authority=("external refund commitment",),
            confirm_first=(),
            never_do=(),
        ),
    )

    posture = context.action_posture("send external refund commitment to customer")

    assert posture.charter_zone == CharterZone.FULL_AUTHORITY
    assert posture.company_boundary_conflict is True
    assert posture.escalation_target == "company-admin"
    assert "company_boundary_conflict" in posture.reasons
