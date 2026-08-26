"""start_immediately goal start must not 500 on expired ``updated_at``.

WHY THIS FILE EXISTS
--------------------
``session_goals.start_session_goal`` updates ``goal.metadata_json`` and
flushes AFTER the goal run starts. ``AgentSessionGoal.updated_at`` carries
``onupdate=func.now()`` (a SQL-side onupdate), so that flush EXPIRES the
attribute; the synchronous ``build_session_goal_projection`` read of
``goal.updated_at`` then performs implicit IO — under asyncio that raises
``MissingGreenlet`` and the endpoint returns 500 AFTER the goal row and the
runtime task were already created (fresh_1855: goal row active, task running,
empty transcript, API 500). Monkeypatched fake-DB parity tests cannot observe
any of this; this regression runs the real endpoint function against a real
AsyncSession/PostgreSQL.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import User


async def _seed_principals(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Goal Tenant", slug=f"goal-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"goal-{suffix}",
                email=f"goal-{suffix}@example.test",
                password_hash="x",
                display_name="Goal Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Goal Agent {suffix}",
            role_description="Runs the durable goal regression.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(agent_id=agent.id, user_id=user_id, tenant_id=tenant_id, title=f"goal-{suffix}")
        db.add(session)
        await db.commit()
        return agent.id, session.id, tenant_id, user_id


async def test_start_goal_start_immediately_projects_without_lazy_io(owner_sessionmaker, monkeypatch) -> None:
    import app.api.session_goals as goals_api

    agent_id, session_id, tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    current_user = SimpleNamespace(id=user_id, role="org_admin")

    async def fake_authorize(_db, _user, **kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        return SimpleNamespace(
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=session_id),
        )

    async def fake_start_web_chat_run(**kwargs):
        # Mirror the real payload shape; the run id is the request id hex.
        return {"run_id": str(kwargs["run_id"]), "status": "pending"}

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(goals_api, "start_web_chat_run", fake_start_web_chat_run)

    request_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        result = await goals_api.start_session_goal(
            agent_id=agent_id,
            session_id=session_id,
            body=goals_api.StartGoalIn(
                objective="Finish the durable goal regression.",
                content="J-04 exercise the production journey contract with durable goal.",
                request_id=request_id,
                start_immediately=True,
            ),
            current_user=current_user,
            db=db,
        )
        await db.rollback()

    assert result["run"]["run_id"] == str(request_id)
    assert result["status"] == "active"
    assert result["updated_at"]


async def test_goal_replay_returns_canonical_terminal_task_status(owner_sessionmaker, monkeypatch) -> None:
    """Replay resolves the tenant-scoped canonical RuntimeTask, not the stale
    goal snapshot (fresh2_1829: terminal task replayed as "pending")."""
    import app.api.session_goals as goals_api
    from sqlalchemy import func, select as sel

    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    agent_id, session_id, tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    current_user = SimpleNamespace(id=user_id, role="org_admin")

    async def fake_authorize(_db, _user, **kwargs):
        return SimpleNamespace(
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=kwargs["session_id"]),
        )

    async def fake_start_web_chat_run(**kwargs):
        # Write-time snapshot is "pending" even though the canonical task
        # later completes — exactly the stale-metadata defect.
        return {"run_id": str(kwargs["run_id"]), "status": "pending"}

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    monkeypatch.setattr(goals_api, "start_web_chat_run", fake_start_web_chat_run)

    request_id = uuid.uuid4()
    body = goals_api.StartGoalIn(
        objective="Replay canonical status regression.",
        content="J-04 exercise the production journey contract with durable goal replay.",
        request_id=request_id,
        start_immediately=True,
    )
    async with owner_sessionmaker() as db:
        first = await goals_api.start_session_goal(
            agent_id=agent_id, session_id=session_id, body=body, current_user=current_user, db=db
        )
        await db.commit()
    assert first["run"]["status"] == "pending"

    # The canonical ledger row completes (bound to this agent/session/goal —
    # the goal id IS the request id on the insert-keyed path).
    async with owner_sessionmaker() as db:
        db.add(
            RuntimeTask(
                id=request_id,
                task_type="web_chat_turn",
                status="completed",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_session_id=str(session_id),
                tenant_id=tenant_id,
                metadata_json={"goal_id": str(request_id), "source": "session_goal"},
            )
        )
        await db.commit()
        tasks_before = (
            await db.execute(
                sel(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id))
            )
        ).scalar_one()
        events_before = (
            await db.execute(
                sel(func.count()).select_from(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
            )
        ).scalar_one()

    async with owner_sessionmaker() as db:
        replay = await goals_api.start_session_goal(
            agent_id=agent_id, session_id=session_id, body=body, current_user=current_user, db=db
        )
        await db.rollback()

    assert replay["id"] == str(request_id)
    assert replay["run"]["run_id"] == str(request_id)
    assert replay["run"]["replayed"] is True
    assert replay["run"]["status"] == "completed"

    async with owner_sessionmaker() as db:
        tasks_after = (
            await db.execute(
                sel(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id))
            )
        ).scalar_one()
        events_after = (
            await db.execute(
                sel(func.count()).select_from(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
            )
        ).scalar_one()
    assert int(tasks_after) == int(tasks_before)
    assert int(events_after) == int(events_before)

    # Refusal: a DIFFERENT goal whose metadata points at this task must NOT
    # derive the task's status (goal binding fails -> typed snapshot
    # fallback, never a cross-goal derivation).
    other_session_id = uuid.uuid4()
    other_request_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChatSession(agent_id=agent_id, user_id=user_id, tenant_id=tenant_id, id=other_session_id, title="goal-b")
        )
        await db.flush()
        other = goals_api.AgentSessionGoal(
            id=other_request_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            chat_session_id=other_session_id,
            created_by_user_id=user_id,
            objective="Unrelated goal must not inherit status.",
            status="active",
            metadata_json={"last_goal_run_id": str(request_id), "last_goal_run_status": "pending"},
        )
        db.add(other)
        await db.commit()
        other_body = goals_api.StartGoalIn(
            objective="Unrelated goal must not inherit status.",
            content="J-04 unrelated goal replay.",
            request_id=other_request_id,
            start_immediately=True,
        )
        other_replay = await goals_api.start_session_goal(
            agent_id=agent_id, session_id=other_session_id, body=other_body, current_user=current_user, db=db
        )
        await db.rollback()
    assert other_replay["run"]["run_id"] == str(request_id)
    assert other_replay["run"]["replayed"] is True
    assert other_replay["run"]["status"] == "pending"
