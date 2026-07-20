"""Real PostgreSQL proof for atomic, replay-safe User offboarding."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.ai_asset import AIAssetRecord
from app.models.audit import ApprovalRequest, AuditLog
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.services.user_offboarding_service import find_user_offboarding_replay, offboard_loaded_user


async def test_user_offboarding_transfers_all_agents_and_replays_receipt(owner_sessionmaker) -> None:
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    agent_ids = [uuid.uuid4(), uuid.uuid4()]
    queued_runtime_task_id = uuid.uuid4()
    running_runtime_task_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    business_task_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Offboarding Tenant", slug=f"offboarding-{suffix}"))
        target = User(
            id=target_id,
            username=f"departing-{suffix}",
            email=f"departing-{suffix}@example.test",
            password_hash="x",
            display_name="Departing Member",
            tenant_id=tenant_id,
            role="member",
        )
        successor = User(
            id=successor_id,
            username=f"admin-{suffix}",
            email=f"admin-{suffix}@example.test",
            password_hash="x",
            display_name="Company Admin",
            tenant_id=tenant_id,
            role="org_admin",
        )
        db.add_all([target, successor])
        await db.flush()
        for index, agent_id in enumerate(agent_ids):
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    creator_id=target_id,
                    sponsor_user_id=target_id,
                    owner_user_id=target_id,
                    name=f"Departing Agent {index}",
                    role_description="Offboarding integration proof",
                )
            )
        await db.flush()
        group = AgentCollaborationGroup(
            tenant_id=tenant_id,
            name="Offboarding A2A group",
            purpose="Owner transfer proof",
            created_by_user_id=target_id,
            created_by_agent_id=agent_ids[0],
            status="active",
        )
        db.add(group)
        await db.flush()
        db.add(
            AgentCollaborationGroupMember(
                tenant_id=tenant_id,
                group_id=group.id,
                agent_id=agent_ids[0],
                agent_owner_user_id=target_id,
                role="owner",
                status="active",
                approved_by_user_id=target_id,
            )
        )
        db.add_all(
            [
                RuntimeTask(
                    id=queued_runtime_task_id,
                    task_type="subagent",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_ids[0],
                    root_user_id=target_id,
                    status="pending",
                ),
                RuntimeTask(
                    id=running_runtime_task_id,
                    task_type="business_task",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_ids[1],
                    root_user_id=target_id,
                    status="running",
                    claimed_by="departing-user-worker",
                    metadata_json={
                        "business_task_id": str(business_task_id),
                        "requester_user_id": str(target_id),
                        "phase": "invoking",
                    },
                ),
                ApprovalRequest(
                    id=approval_id,
                    agent_id=agent_ids[0],
                    tenant_id=tenant_id,
                    action_type="tool_execution",
                    details={"tool_name": "send_email"},
                    status="pending",
                    execution_status="pending",
                    requested_by=target_id,
                ),
            ]
        )
        await db.flush()
        db.add(
            Task(
                id=business_task_id,
                agent_id=agent_ids[1],
                tenant_id=tenant_id,
                title="Departing user business task",
                status="doing",
                created_by=target_id,
                request_id=f"business-{suffix}",
                request_hash="business-task-hash",
                active_runtime_task_id=running_runtime_task_id,
                last_execution_status="running",
            )
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        target = (
            await db.execute(select(User).where(User.id == target_id).with_for_update())
        ).scalar_one()
        successor = (
            await db.execute(select(User).where(User.id == successor_id).with_for_update())
        ).scalar_one()
        receipt = await offboard_loaded_user(
            db,
            target_user=target,
            successor=successor,
            actor=successor,
            expected_agent_ids=agent_ids,
            reason="Employment ended",
            request_id=f"offboard-{suffix}",
        )
        assert set(receipt.transferred_agent_ids) == set(agent_ids)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        target = (
            await db.execute(select(User).where(User.id == target_id).with_for_update())
        ).scalar_one()
        agents = list(
            (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)).order_by(Agent.id))).scalars().all()
        )
        assert target.is_active is False
        assert {agent.owner_user_id for agent in agents} == {successor_id}
        assert {agent.creator_id for agent in agents} == {target_id}
        assert {agent.sponsor_user_id for agent in agents} == {target_id}
        asset_owners = set(
            (
                await db.execute(
                    select(AIAssetRecord.owner_id).where(
                        AIAssetRecord.tenant_id == tenant_id,
                        AIAssetRecord.native_entity_id.in_(agent_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert asset_owners == {successor_id}
        membership = (
            await db.execute(
                select(AgentCollaborationGroupMember).where(
                    AgentCollaborationGroupMember.agent_id == agent_ids[0]
                )
            )
        ).scalar_one()
        assert membership.agent_owner_user_id == successor_id
        assert membership.status == "pending_owner_confirmation"
        assert membership.approved_by_user_id is None
        runtime_tasks = {
            task.id: task
            for task in (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.id.in_((queued_runtime_task_id, running_runtime_task_id))
                    )
                )
            )
            .scalars()
            .all()
        }
        assert runtime_tasks[queued_runtime_task_id].status == "killed"
        assert runtime_tasks[running_runtime_task_id].status == "needs_reconciliation"
        assert runtime_tasks[running_runtime_task_id].claimed_by is None
        assert runtime_tasks[running_runtime_task_id].claim_version == 1
        business_task = await db.get(Task, business_task_id)
        assert business_task is not None
        assert business_task.status == "needs_reconciliation"
        assert business_task.last_execution_status == "needs_reconciliation"
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        assert approval.status == "rejected"
        assert approval.execution_status == "rejected"
        assert approval.resolved_by == successor_id
        assert receipt.revocations.runtime_tasks == 2
        assert receipt.revocations.pending_approvals == 1

        replay = await find_user_offboarding_replay(
            db,
            target_user=target,
            successor_user_id=successor_id,
            expected_agent_ids=agent_ids,
            reason="Employment ended",
            request_id=f"offboard-{suffix}",
        )
        assert replay is not None
        assert set(replay.transferred_agent_ids) == set(agent_ids)
        audits = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action == "user:offboarded",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
