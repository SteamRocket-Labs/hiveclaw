"""A retained employee grant must observe its current scope and access level."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.permissions import check_agent_access
from app.database import pin_rls_tenant_context
from app.models.agent import AgentPermission
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("mutation", ["scope", "level"])
async def test_retained_employee_grant_observes_current_scope_and_access(
    owner_sessionmaker, app_user_sessionmaker, mutation
):
    tenant, owner, agent, _session, _run = await _seed_session(owner_sessionmaker)
    grantee, grant_id = uuid4(), uuid4()
    async with owner_sessionmaker() as writer:
        writer.add(
            User(
                id=grantee,
                tenant_id=tenant,
                username=f"employee-grantee-{grantee.hex}",
                email=f"{grantee.hex}@grant.test",
                password_hash="fixture",
                display_name="Shared employee grantee",
                role="member",
            )
        )
        writer.add(
            AgentPermission(
                id=grant_id,
                tenant_id=tenant,
                agent_id=agent,
                scope_type="user",
                scope_id=grantee,
                access_level="manage",
            )
        )
        await writer.commit()

    async def decision(db):
        try:
            _agent, access = await check_agent_access(db, await db.get(User, grantee), agent)
            return 200, access
        except HTTPException as error:
            return error.status_code, None

    async with app_user_sessionmaker() as reader:
        await pin_rls_tenant_context(reader, tenant)
        role = (
            await reader.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert role == (False, False)
        retained = await reader.get(AgentPermission, grant_id)
        assert retained.scope_id == grantee and retained.access_level == "manage"
        assert await decision(reader) == (200, "manage")
        await reader.commit()

        async with owner_sessionmaker() as writer:
            current = await writer.get(AgentPermission, grant_id)
            if mutation == "scope":
                current.scope_id = owner
            else:
                current.access_level = "use"
            await writer.commit()
        try:
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, tenant)
                current_decision = await decision(fresh)
                assert current_decision == ((403, None) if mutation == "scope" else (200, "use"))
            await pin_rls_tenant_context(reader, tenant)
            assert await decision(reader) == current_decision
        finally:
            async with owner_sessionmaker() as writer:
                current = await writer.get(AgentPermission, grant_id)
                current.scope_id, current.access_level = grantee, "manage"
                await writer.commit()

        await pin_rls_tenant_context(reader, tenant)
        assert await decision(reader) == (200, "manage")
        assert retained.scope_id == grantee and retained.access_level == "manage"
