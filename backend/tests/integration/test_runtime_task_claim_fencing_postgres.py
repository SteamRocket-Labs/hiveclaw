from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_task_claim_service import RuntimeTaskClaimService


async def _seed_expired_web_chat_claim(sessionmaker):
    suffix = uuid.uuid4().hex[:10]
    async with sessionmaker() as db:
        tenant = Tenant(name="Runtime Claim Tenant", slug=f"runtime-claim-{suffix}")
        db.add(tenant)
        await db.flush()
        user = User(
            username=f"runtime-claim-{suffix}",
            email=f"runtime-claim-{suffix}@example.test",
            password_hash="x",
            display_name="Runtime Claim User",
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name="Runtime Claim Agent", creator_id=user.id, owner_user_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        session = ChatSession(agent_id=agent.id, tenant_id=tenant.id, user_id=user.id)
        db.add(session)
        await db.flush()
        task = RuntimeTask(
            task_type="web_chat_turn",
            status="running",
            tenant_id=tenant.id,
            parent_agent_id=agent.id,
            parent_session_id=str(session.id),
            root_user_id=user.id,
            prompt="resume exactly once",
            claimed_by="crashed-worker",
            claim_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            claim_version=7,
            attempt_count=1,
        )
        db.add(task)
        await db.commit()
        return tenant.id, task.id


async def test_two_workers_reclaim_one_expired_web_chat_run_exactly_once(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    tenant_id, task_id = await _seed_expired_web_chat_claim(owner_sessionmaker)

    async def claim(worker_id: str) -> list[RuntimeTask]:
        # The PostgreSQL table owner bypasses ENABLE-only RLS. Exercise the
        # tenant-scoped claim through the same non-owner authority that RLS
        # actually constrains so unrelated suite rows cannot enter this race.
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            return await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=("web_chat_turn",),
                lease_seconds=60,
            ).claim_available(batch_size=1)

    claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claimed = [task for batch in claims for task in batch]

    assert [task.id for task in claimed] == [task_id]
    assert claimed[0].claim_version == 8
    assert claimed[0].metadata_json["reclaimed_expired_claim"] is True

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        persisted = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id))).scalar_one()
        assert persisted.claimed_by in {"worker-a", "worker-b"}
        assert persisted.claim_version == 8
        assert persisted.attempt_count == 2
        assert persisted.claim_expires_at > datetime.now(UTC)
