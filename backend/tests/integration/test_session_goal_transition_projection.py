"""Goal transitions must not 500 on expired ``updated_at``.

WHY THIS FILE EXISTS
--------------------
``session_goals.transition_goal`` mutates the goal, appends the transition
event, then ``db.flush()``. ``AgentSessionGoal.updated_at`` carries
``onupdate=func.now()`` (a SQL-side onupdate), so that flush EXPIRES the
attribute; the synchronous ``build_session_goal_projection`` read of
``goal.updated_at`` then performs implicit IO — under asyncio that raises
``MissingGreenlet`` and the endpoint returns 500 AFTER the status change
was already flushed (production B4: Pause on an active /goal-created goal
returned HTTP 500 while the UI stayed Active). The same defect was already
fixed for ``start_session_goal`` with an explicit ``db.refresh``; the
transition path lacked it. Monkeypatched fake-DB parity tests cannot
observe this; these regressions run the real endpoint function against a
real AsyncSession/PostgreSQL.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.tenant import Tenant
from app.models.user import User


async def _seed(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Goal Transition Tenant", slug=f"goal-tr-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"goal-tr-{suffix}",
                email=f"goal-tr-{suffix}@example.test",
                password_hash="x",
                display_name="Goal Transition Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Goal Transition Agent {suffix}",
            role_description="Runs the durable goal transition regression.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(agent_id=agent.id, user_id=user_id, tenant_id=tenant_id, title=f"goal-tr-{suffix}")
        db.add(session)
        await db.commit()
        return agent.id, session.id, tenant_id, user_id


async def _seed_goal(owner_sessionmaker, seed, *, status="active", metadata=None) -> uuid.UUID:
    agent_id, session_id, tenant_id, user_id = seed
    goal_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            AgentSessionGoal(
                id=goal_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                chat_session_id=session_id,
                created_by_user_id=user_id,
                objective="Exercise durable goal pause without a started run.",
                status=status,
                token_budget=120000,
                max_continuation_turns=2,
                time_budget_seconds=600,
                metadata_json=metadata or {},
            )
        )
        await db.commit()
    return goal_id


def _patch_authorize(monkeypatch, goals_api, seed):
    agent_id, session_id, tenant_id, user_id = seed

    async def fake_authorize(_db, _user, **kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        return SimpleNamespace(
            agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            session=SimpleNamespace(id=session_id),
        )

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)
    return SimpleNamespace(id=user_id, role="org_admin")


async def _goal_event_count(owner_sessionmaker, session_id) -> int:
    async with owner_sessionmaker() as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type == "goal",
                    )
                )
            ).scalar_one()
        )


async def test_pause_active_goal_projects_without_lazy_io(owner_sessionmaker, monkeypatch) -> None:
    """Production B4 shape: active /goal-created goal, no run started, single
    Pause click. Before the fix this raised MissingGreenlet -> HTTP 500."""
    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, tenant_id, user_id = seed
    current_user = _patch_authorize(monkeypatch, goals_api, seed)
    goal_id = await _seed_goal(owner_sessionmaker, seed)

    async with owner_sessionmaker() as db:
        result = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=goal_id,
            body=goals_api.GoalTransitionIn(action="pause"),
            current_user=current_user,
            db=db,
        )
        await db.commit()

    assert result["status"] == "paused"
    assert result["controls"] == {"can_pause": False, "can_resume": True, "can_stop": True}
    assert result["updated_at"]
    async with owner_sessionmaker() as db:
        stored = await db.get(AgentSessionGoal, goal_id)
        assert stored.status == "paused"
    # exactly one durable transition event, no duplicate from the failed call
    assert await _goal_event_count(owner_sessionmaker, session_id) == 1


async def test_pause_is_idempotent_on_paused_goal(owner_sessionmaker, monkeypatch) -> None:
    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, _, _ = seed
    current_user = _patch_authorize(monkeypatch, goals_api, seed)
    goal_id = await _seed_goal(owner_sessionmaker, seed, status="paused")

    async with owner_sessionmaker() as db:
        result = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=goal_id,
            body=goals_api.GoalTransitionIn(action="pause"),
            current_user=current_user,
            db=db,
        )
        await db.commit()

    assert result["status"] == "paused"
    assert await _goal_event_count(owner_sessionmaker, session_id) == 1


async def test_stop_from_paused_and_active_cancel_identity(owner_sessionmaker, monkeypatch) -> None:
    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, _, _ = seed
    current_user = _patch_authorize(monkeypatch, goals_api, seed)
    goal_id = await _seed_goal(owner_sessionmaker, seed, status="paused")

    async with owner_sessionmaker() as db:
        result = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=goal_id,
            body=goals_api.GoalTransitionIn(action="stop"),
            current_user=current_user,
            db=db,
        )
        await db.commit()
    assert result["status"] == "cancelled"
    assert result["controls"] == {"can_pause": False, "can_resume": False, "can_stop": False}

    # stop from active with a live run: the cancel must be dispatched exactly
    # once, through the durable live-cancel ingress, with the stable
    # goal-bound idempotency key.
    cancels: list[dict] = []

    async def fake_cancel(**kwargs):
        cancels.append(kwargs)

    monkeypatch.setattr(goals_api, "submit_live_cancel_input", fake_cancel)
    run_id = uuid.uuid4()
    active_goal_id = await _seed_goal(owner_sessionmaker, seed, metadata={"last_continuation_run_id": str(run_id)})
    async with owner_sessionmaker() as db:
        stopped = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=active_goal_id,
            body=goals_api.GoalTransitionIn(action="stop"),
            current_user=current_user,
            db=db,
        )
        await db.commit()
    assert stopped["status"] == "cancelled"
    assert len(cancels) == 1
    assert cancels[0]["run_id"] == str(run_id)
    assert cancels[0]["idempotency_key"] == f"goal:{active_goal_id}:cancel-run:{run_id}"


async def test_resume_transitions_and_invokes_continuation(owner_sessionmaker, monkeypatch) -> None:
    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, _, _ = seed
    current_user = _patch_authorize(monkeypatch, goals_api, seed)
    goal_id = await _seed_goal(owner_sessionmaker, seed, status="paused")

    async def fake_continue(**kwargs):
        assert kwargs["goal"].id == goal_id
        return {"continuation": "stubbed"}

    monkeypatch.setattr(goals_api, "continue_session_goal", fake_continue)

    async with owner_sessionmaker() as db:
        result = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=goal_id,
            body=goals_api.GoalTransitionIn(action="resume"),
            current_user=current_user,
            db=db,
        )
        await db.commit()

    assert result["status"] == "active"
    assert result["continuation"] == {"continuation": "stubbed"}
    assert result["updated_at"]
    async with owner_sessionmaker() as db:
        stored = await db.get(AgentSessionGoal, goal_id)
        assert stored.blocked_count == 0
        assert stored.metadata_json["resumed_by_user_id"] == str(current_user.id)


async def test_resume_on_active_goal_is_a_no_op(owner_sessionmaker, monkeypatch) -> None:
    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, _, _ = seed
    current_user = _patch_authorize(monkeypatch, goals_api, seed)
    goal_id = await _seed_goal(owner_sessionmaker, seed, status="active")

    async with owner_sessionmaker() as db:
        result = await goals_api.transition_goal(
            agent_id=agent_id,
            session_id=session_id,
            goal_id=goal_id,
            body=goals_api.GoalTransitionIn(action="resume"),
            current_user=current_user,
            db=db,
        )
        await db.commit()

    assert result["status"] == "active"
    assert result["continuation"] is None
    assert await _goal_event_count(owner_sessionmaker, session_id) == 0
