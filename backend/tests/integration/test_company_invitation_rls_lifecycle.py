from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select


def _user(*, user_id: uuid.UUID, role: str, tenant_id: uuid.UUID | None):
    from app.models.user import User

    marker = user_id.hex[:12]
    return User(
        id=user_id,
        username=f"invite-{marker}",
        email=f"invite-{marker}@test.local",
        password_hash="test-only",
        display_name=f"Invite {marker}",
        role=role,
        tenant_id=tenant_id,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_nonowner_rls_company_admin_and_member_invitation_lifecycle(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    from app.api import admin as admin_api
    from app.api import enterprise as enterprise_api
    from app.api import tenants as tenants_api
    from app.database import tenant_scoped_session
    from app.models.invitation_code import InvitationCode
    from app.models.tenant import Tenant
    from app.models.user import User

    platform_tenant_id, platform_admin_id, admin_user_id, member_user_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(
            Tenant(
                id=platform_tenant_id,
                name="Invitation RLS Platform",
                slug=f"invitation-rls-platform-{platform_tenant_id.hex[:8]}",
                im_provider="web_only",
            )
        )
        db.add(_user(user_id=platform_admin_id, role="platform_admin", tenant_id=platform_tenant_id))
        db.add(_user(user_id=admin_user_id, role="member", tenant_id=None))
        db.add(_user(user_id=member_user_id, role="member", tenant_id=None))
        await db.commit()

    platform_principal = SimpleNamespace(
        id=platform_admin_id,
        role="platform_admin",
        tenant_id=platform_tenant_id,
    )
    async with tenant_scoped_session(platform_tenant_id, session_factory=app_user_sessionmaker) as db:
        creation = await admin_api.create_company(
            data=admin_api.CompanyCreateRequest(name="Invitation RLS Target"),
            current_user=platform_principal,
            db=db,
        )

    company_id = creation.company.id
    async with owner_sessionmaker() as db:
        company = await db.get(Tenant, company_id)
        admin_code = await db.scalar(
            select(InvitationCode).where(
                InvitationCode.tenant_id == company_id,
                InvitationCode.code == creation.admin_invitation_code,
            )
        )
        assert company is not None
        assert admin_code is not None and admin_code.granted_role == "org_admin"

    admin_principal = SimpleNamespace(id=admin_user_id, role="member", tenant_id=None)
    async with tenant_scoped_session(None, session_factory=app_user_sessionmaker) as db:
        admin_join = await tenants_api.join_company(
            data=tenants_api.JoinRequest(invitation_code=creation.admin_invitation_code),
            current_user=admin_principal,
            db=db,
        )
    assert admin_join.role == "org_admin"
    assert admin_join.tenant.id == company_id

    async with tenant_scoped_session(company_id, session_factory=app_user_sessionmaker) as db:
        member_batch = await enterprise_api.create_invitation_codes(
            data=enterprise_api.InvitationCodeCreate(count=1, max_uses=1),
            tenant_id=None,
            current_user=SimpleNamespace(id=admin_user_id, role="org_admin", tenant_id=company_id),
            db=db,
        )
    member_code = member_batch["codes"][0]

    member_principal = SimpleNamespace(id=member_user_id, role="member", tenant_id=None)
    async with tenant_scoped_session(None, session_factory=app_user_sessionmaker) as db:
        member_join = await tenants_api.join_company(
            data=tenants_api.JoinRequest(invitation_code=member_code),
            current_user=member_principal,
            db=db,
        )
    assert member_join.role == "member"
    assert member_join.tenant.id == company_id

    async with owner_sessionmaker() as db:
        stored_admin = await db.get(User, admin_user_id)
        stored_member = await db.get(User, member_user_id)
        codes = list(
            (
                await db.execute(
                    select(InvitationCode)
                    .where(InvitationCode.tenant_id == company_id)
                    .order_by(InvitationCode.granted_role)
                )
            ).scalars()
        )
        assert (stored_admin.tenant_id, stored_admin.role) == (company_id, "org_admin")
        assert (stored_member.tenant_id, stored_member.role) == (company_id, "member")
        assert [(row.granted_role, row.used_count) for row in codes] == [("member", 1), ("org_admin", 1)]
