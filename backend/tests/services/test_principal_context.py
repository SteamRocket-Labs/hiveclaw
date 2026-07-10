from __future__ import annotations

from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


def test_principal_stack_orders_governance_company_owner_and_current_user() -> None:
    stack = PrincipalStack(
        platform=Principal(PrincipalRole.PLATFORM, "platform", "Hive"),
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "user-owner", "Alice"),
        creator=Principal(PrincipalRole.CREATOR, "user-creator", "Bob"),
        current_user=Principal(PrincipalRole.CURRENT_USER, "user-viewer", "Carol"),
    )

    assert [p.role for p in stack.ordered()] == [
        PrincipalRole.PLATFORM,
        PrincipalRole.COMPANY,
        PrincipalRole.OWNER,
        PrincipalRole.CREATOR,
        PrincipalRole.CURRENT_USER,
    ]
    assert stack.direct_owner_accountability_id == "user-owner"
    assert not stack.current_user_is_direct_owner


def test_company_admin_can_access_company_sensitive_memory() -> None:
    stack = PrincipalStack(
        platform=Principal(PrincipalRole.PLATFORM, "platform", "Hive"),
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "owner", "Owner"),
        current_user=Principal(PrincipalRole.COMPANY_ADMIN, "admin", "Admin"),
    )

    assert stack.can_access_sensitivity("PL3_sensitive")
    assert not stack.can_access_sensitivity("PL4_credential")
