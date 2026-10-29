"""Employee access observes current owner and creator facts through real RLS."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.permissions import check_agent_access
from app.database import pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("role", ["member", "platform_admin"])
async def test_employee_access_refreshes_both_related_identities(owner_sessionmaker, app_user_sessionmaker, role):
    tenant, creator_id, agent_id, _session, _run = await _seed_session(owner_sessionmaker)
    owner_id, reader_id = uuid4(), uuid4()
    async with owner_sessionmaker() as writer:
        for user_id, label, user_role in (
            (owner_id, "Current employee owner", "member"),
            (reader_id, "Employee reader", role),
        ):
            writer.add(
                User(
                    id=user_id,
                    tenant_id=tenant,
                    username=f"related-identity-{user_id.hex}",
                    email=f"{user_id.hex}@related.test",
                    password_hash="fixture",
                    display_name=label,
                    role=user_role,
                )
            )
        (await writer.get(User, creator_id)).display_name = "Original employee creator"
        (await writer.get(Agent, agent_id)).owner_user_id = owner_id
        writer.add(
            AgentPermission(
                id=uuid4(),
                tenant_id=tenant,
                agent_id=agent_id,
                scope_type="user",
                scope_id=reader_id,
                access_level="use",
            )
        )
        await writer.commit()

    async with app_user_sessionmaker() as reader:
        await pin_rls_tenant_context(reader, tenant)
        flags = (
            await reader.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert flags == (False, False)
        user = await reader.get(User, reader_id)
        agent, access = await check_agent_access(reader, user, agent_id)
        assert access == ("manage" if role == "platform_admin" else "use")
        assert agent.owner.id == owner_id and agent.creator.id == creator_id
        assert agent.owner.display_name == "Current employee owner"
        assert agent.creator.display_name == "Original employee creator"
        await reader.commit()

        async with owner_sessionmaker() as writer:
            (await writer.get(User, owner_id)).display_name = "Updated employee owner"
            (await writer.get(User, creator_id)).display_name = "Updated employee creator"
            await writer.commit()

        await pin_rls_tenant_context(reader, tenant)
        current, _access = await check_agent_access(reader, user, agent_id)
        assert current is agent
        assert current.owner.display_name == "Updated employee owner"
        assert current.creator.display_name == "Updated employee creator"
        assert await reader.scalar(text("SELECT current_setting('app.current_tenant_id')")) == str(tenant)
        await reader.commit()

        async with owner_sessionmaker() as writer:
            (await writer.get(User, owner_id)).is_active = False
            await writer.commit()
        try:
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, tenant)
                with pytest.raises(HTTPException) as expected:
                    await check_agent_access(fresh, await fresh.get(User, reader_id), agent_id)
                assert expected.value.status_code == 410
            await pin_rls_tenant_context(reader, tenant)
            with pytest.raises(HTTPException) as retained:
                await check_agent_access(reader, user, agent_id)
            assert retained.value.status_code == expected.value.status_code
        finally:
            async with owner_sessionmaker() as writer:
                (await writer.get(User, owner_id)).is_active = True
                await writer.commit()

        await pin_rls_tenant_context(reader, tenant)
        restored, _access = await check_agent_access(reader, user, agent_id)
        assert restored.owner.is_active and restored.owner.display_name == "Updated employee owner"
