from __future__ import annotations

import uuid

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model
from app.services.a2a_group_management import (
    build_a2a_group_management_read_model,
    search_a2a_invite_candidates,
)


async def test_pending_cross_owner_membership_is_management_only_until_owner_approval(
    owner_sessionmaker,
) -> None:
    tenant_id = uuid.uuid4()
    source_owner_id = uuid.uuid4()
    target_owner_id = uuid.uuid4()
    source_agent_id = uuid.uuid4()
    target_agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="A2A Management", slug=f"a2a-management-{suffix}"))
        await db.flush()
        source_owner = User(
            id=source_owner_id,
            username=f"a2a-source-{suffix}",
            email=f"a2a-source-{suffix}@example.test",
            password_hash="x",
            display_name="Source Owner",
            tenant_id=tenant_id,
            role="member",
        )
        target_owner = User(
            id=target_owner_id,
            username=f"a2a-target-{suffix}",
            email=f"a2a-target-{suffix}@example.test",
            password_hash="x",
            display_name="Target Owner",
            tenant_id=tenant_id,
            role="member",
        )
        db.add_all([source_owner, target_owner])
        await db.flush()
        source_agent = Agent(
            id=source_agent_id,
            name="Launch Coordinator",
            role_description="Coordinates launch",
            creator_id=source_owner_id,
            owner_user_id=source_owner_id,
            tenant_id=tenant_id,
            status="running",
        )
        target_agent = Agent(
            id=target_agent_id,
            name="Risk Reviewer",
            role_description="Reviews launch risk",
            creator_id=target_owner_id,
            owner_user_id=target_owner_id,
            tenant_id=tenant_id,
            status="running",
        )
        db.add_all([source_agent, target_agent])
        await db.flush()
        group = AgentCollaborationGroup(
            tenant_id=tenant_id,
            name="Launch room",
            purpose="Cross-owner launch review",
            created_by_user_id=source_owner_id,
            created_by_agent_id=source_agent_id,
            status="active",
        )
        db.add(group)
        await db.flush()
        source_member = AgentCollaborationGroupMember(
            tenant_id=tenant_id,
            group_id=group.id,
            agent_id=source_agent_id,
            agent_owner_user_id=source_owner_id,
            role="owner",
            status="active",
            approved_by_user_id=source_owner_id,
        )
        pending_member = AgentCollaborationGroupMember(
            tenant_id=tenant_id,
            group_id=group.id,
            agent_id=target_agent_id,
            agent_owner_user_id=target_owner_id,
            role="specialist",
            status="pending_owner_confirmation",
            invited_by_user_id=source_owner_id,
            invited_by_agent_id=source_agent_id,
            invitation_reason="Need risk sign-off",
        )
        db.add_all([source_member, pending_member])
        await db.flush()

        management = await build_a2a_group_management_read_model(
            db,
            source_agent=target_agent,
            current_user=target_owner,
        )
        runtime = await build_a2a_collaboration_read_model(db, target_agent_id)
        candidates = await search_a2a_invite_candidates(
            db,
            source_agent=source_agent,
            group_id=group.id,
            current_user=source_owner,
            query="Risk",
        )

        assert len(management["groups"]) == 1
        pending = next(
            member for member in management["groups"][0]["members"] if member["agent_id"] == str(target_agent_id)
        )
        assert pending["status"] == "pending_owner_confirmation"
        assert pending["can_approve"] is True
        assert pending["member_id"] == str(pending_member.id)
        assert runtime["collaboration_groups"] == []
        assert candidates[0]["agent_id"] == str(target_agent_id)
        assert candidates[0]["invite_action"] == "pending"
