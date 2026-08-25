from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.execution_environment import ExecutionEnvironment
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User


async def _seed_actor(db, *, label: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Environment {label}", slug=f"environment-{label}-{suffix}")
    db.add(tenant)
    await db.flush()
    user = User(
        username=f"environment-{label}-{suffix}",
        email=f"environment-{label}-{suffix}@example.test",
        password_hash="x",
        display_name=f"Environment {label} Owner",
        tenant_id=tenant.id,
        role="org_admin",
    )
    db.add(user)
    await db.flush()
    agent = Agent(
        name=f"Environment {label} Agent",
        creator_id=user.id,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        status="idle",
    )
    db.add(agent)
    await db.flush()
    return tenant.id, user.id, agent.id


async def test_environment_rls_and_binding_reject_cross_tenant_authority(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    async with owner_sessionmaker() as db:
        tenant_a, user_a, agent_a = await _seed_actor(db, label="a")
        tenant_b, _user_b, agent_b = await _seed_actor(db, label="b")
        environment_a = ExecutionEnvironment(
            tenant_id=tenant_a,
            agent_id=agent_a,
            provider_key="local_os_sandbox",
            desired_state="running",
            observed_state="pending",
            policy_snapshot_hash="a" * 64,
        )
        environment_b = ExecutionEnvironment(
            tenant_id=tenant_b,
            agent_id=agent_b,
            provider_key="local_os_sandbox",
            desired_state="running",
            observed_state="pending",
            policy_snapshot_hash="b" * 64,
        )
        db.add_all((environment_a, environment_b))
        await db.commit()
        environment_a_id = environment_a.id

    async with tenant_scoped_session(tenant_a, session_factory=app_user_sessionmaker) as db:
        visible = (await db.execute(select(ExecutionEnvironment))).scalars().all()
        assert [item.id for item in visible] == [environment_a_id]

    async with owner_sessionmaker() as db:
        db.add(
            ExecutionEnvironment(
                tenant_id=tenant_b,
                agent_id=agent_a,
                provider_key="local_os_sandbox",
                desired_state="running",
                observed_state="pending",
                policy_snapshot_hash="c" * 64,
            )
        )
        with pytest.raises(IntegrityError, match="Agent tenant mismatch"):
            await db.flush()
        await db.rollback()

    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                task_type="web_chat_turn",
                status="pending",
                tenant_id=tenant_b,
                parent_agent_id=agent_b,
                root_user_id=user_a,
                environment_id=environment_a_id,
            )
        )
        with pytest.raises(IntegrityError, match="environment tenant mismatch"):
            await db.flush()
        await db.rollback()
