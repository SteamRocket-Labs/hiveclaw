"""D1 Undo uses G1 without granting members general lifecycle administration."""

import uuid

import pytest
from sqlalchemy import delete, select

from app.models.agent import Agent, AgentPermission
from app.models.hr_creation import HrCreationDraft
from tests.integration import test_g1_r3_lifecycle_postgres as lifecycle

lifecycle_stack = lifecycle.lifecycle_stack

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("scope", ["own_private", "shared", "reassigned", "other_requester", "legacy", "resource_grant"])
async def test_d1_member_undo_only_their_conversation_created_private_employee(
    lifecycle_stack, owner_sessionmaker, scope,
):
    from app.models.security_audit import ResourcePermission
    from app.models.trigger import AgentTrigger

    seed = lifecycle_stack
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent"])
        admin_id = agent.owner_user_id
        agent.creator_id = seed["member_id"]
        agent.owner_user_id = seed["member_id"]
        await db.execute(delete(AgentPermission).where(AgentPermission.agent_id == agent.id))
        db.add(HrCreationDraft(
            tenant_id=seed["tenant"], hr_agent_id=agent.id, session_id=seed["session"],
            requested_by_user_id=admin_id if scope == "other_requester" else seed["member_id"],
            created_agent_id=agent.id, blueprint_hash="undo-fixture", blueprint_json={"permission_scope": "self"},
            creation_idempotency_key=f"{'legacy' if scope == 'legacy' else 'direct-employee'}:{uuid.uuid4()}",
            status="completed",
        ))
        if scope == "shared":
            db.add(AgentPermission(tenant_id=seed["tenant"], agent_id=agent.id, scope_type="company"))
        elif scope == "reassigned":
            agent.owner_user_id = admin_id
            db.add(AgentPermission(tenant_id=seed["tenant"], agent_id=agent.id,
                                   scope_type="user", scope_id=seed["member_id"], access_level="use"))
        elif scope == "resource_grant":
            db.add(ResourcePermission(tenant_id=seed["tenant"], resource_type="agent", resource_id=agent.id,
                                      principal_type="user", principal_id=admin_id, actions=["read"]))
        await db.commit()

    stopped = await seed["member"].post(f"/agents/{seed['agent']}/deactivate")
    if scope != "own_private":
        assert stopped.status_code == 403, stopped.text
        async with owner_sessionmaker() as db:
            agent = await db.get(Agent, seed["agent"])
            assert agent.deactivated_at is None and agent.deleted_at is None
        return

    assert stopped.status_code == 204, stopped.text
    repeated = await seed["member"].post(f"/agents/{seed['agent']}/deactivate")
    assert repeated.status_code == 204, repeated.text
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent"])
        assert agent.deactivated_at is not None and agent.status == "stopped" and agent.deleted_at is None
        trigger = await db.scalar(select(AgentTrigger).where(AgentTrigger.agent_id == agent.id))
        assert trigger.is_enabled is False
    refused_restore = await seed["member"].post(f"/agents/{seed['agent']}/restore")
    assert refused_restore.status_code == 403, refused_restore.text
    restored = await seed["admin"].post(f"/agents/{seed['agent']}/restore")
    assert restored.status_code == 204, restored.text
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent"])
        assert agent.deactivated_at is None and agent.deleted_at is None and agent.status == "idle"
