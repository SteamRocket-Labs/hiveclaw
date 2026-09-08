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


def _patch_authorize_with_real_rows(monkeypatch, goals_api, seed):
    """Like _patch_authorize, but returns the real ORM agent/session rows so the
    un-stubbed continuation path (V2 ingress, authority resolution) can run."""

    agent_id, session_id, tenant_id, user_id = seed

    async def fake_authorize(db, _user, **kwargs):
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        agent = await db.get(Agent, agent_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and session is not None
        return SimpleNamespace(agent=agent, session=session)

    monkeypatch.setattr(goals_api, "authorize_session_action", fake_authorize)

    current_user = SimpleNamespace(id=user_id, role="org_admin", tenant_id=tenant_id)
    return current_user


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


async def test_resume_with_elapsed_time_budget_parks_before_dispatch(owner_sessionmaker, monkeypatch) -> None:
    """Production B4 shape: the goal declared a 600s wallclock budget that had
    already elapsed when the owner clicked Continue. The continuation must be
    rejected BEFORE dispatch — no RuntimeTask, no SessionTurnInput — with a
    visible, resumable budget_limited state (the numeric budget is not renewed
    by resume; the projection contract counts total wallclock since created_at).
    """
    from datetime import datetime, timedelta, timezone

    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput

    import app.api.session_goals as goals_api

    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, tenant_id, user_id = seed
    current_user = _patch_authorize_with_real_rows(monkeypatch, goals_api, seed)
    goal_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            AgentSessionGoal(
                id=goal_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                chat_session_id=session_id,
                created_by_user_id=user_id,
                objective="Wallclock-budget regression.",
                status="paused",
                token_budget=120000,
                max_continuation_turns=2,
                time_budget_seconds=600,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=3600),
            )
        )
        await db.commit()

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

    continuation = result["continuation"]
    assert continuation["ok"] is False
    assert continuation["decision"]["reason"] == "time budget exhausted"
    assert result["status"] == "budget_limited"
    assert result["controls"]["can_resume"] is True
    async with owner_sessionmaker() as db:
        stored = await db.get(AgentSessionGoal, goal_id)
        assert stored.status == "budget_limited"
        assert stored.continuation_count == 0
        assert stored.metadata_json["budget_limit_prompt"]
        task_count = (
            await db.execute(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id))
            )
        ).scalar_one()
        assert int(task_count) == 0
        input_count = (
            await db.execute(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == session_id)
            )
        ).scalar_one()
        assert int(input_count) == 0


async def test_resume_continuation_prompt_reaches_bound_model_round_input(owner_sessionmaker, monkeypatch) -> None:
    """Production B4 defect (run 20f23ce9): the goal continuation launched via
    start_web_chat_run(append_user_message=False), which stores the prompt only
    on the RuntimeTask — the canonical V2 round assembly (bind_round_inputs)
    builds model input exclusively from admitted SessionTurnInput rows, so the
    actual model_request_snapshot had bound_input_ids=[] and contained no goal
    content at all. This regression runs the real resume path and then the real
    production assembly function, asserting the continuation objective is
    present in the assembled round-1 messages with runtime (system), not user,
    provenance.
    """
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.services.session_model_round import bind_round_inputs

    import app.api.session_goals as goals_api

    marker = f"B4-GOAL-RESUME-MARKER-{uuid.uuid4().hex[:8]}"
    seed = await _seed(owner_sessionmaker)
    agent_id, session_id, tenant_id, user_id = seed
    current_user = _patch_authorize_with_real_rows(monkeypatch, goals_api, seed)
    # The runtime budget plane opens its own sessions through the app-global
    # factory; point it at the migrated container so the real root-run binding
    # (not its unavailable fallback) is exercised.
    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    goal_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            AgentSessionGoal(
                id=goal_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                chat_session_id=session_id,
                created_by_user_id=user_id,
                objective=f"Compute the {marker} arithmetic result and write it to the goal file.",
                status="paused",
                token_budget=120000,
                max_continuation_turns=2,
                time_budget_seconds=600,
            )
        )
        await db.commit()

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
    continuation = result["continuation"]
    assert continuation["ok"] is True

    async with owner_sessionmaker() as db:
        goal = await db.get(AgentSessionGoal, goal_id)
        assert goal is not None
        input_id = uuid.UUID(str(goal.metadata_json["last_continuation_input_id"]))
        row = await db.get(SessionTurnInput, input_id)
        assert row is not None, "the continuation prompt must be a durable admitted input"
        assert row.tenant_id == tenant_id
        assert row.intent == "start_turn"
        text = " ".join(str(part.get("text") or "") for part in (row.content_parts_json or []))
        assert marker in text
        assert any(str(part.get("role") or "") == "system" for part in (row.content_parts_json or []))

        task = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.parent_session_id == str(session_id),
                    RuntimeTask.task_type == "goal_continuation",
                )
            )
        ).scalar_one()
        assert task.metadata_json["session_v2_input_id"] == str(input_id)
        assert task.status in {"pending", "running"}

        messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=task.id,
            turn_id=task.metadata_json["turn_id"],
            round_index=1,
        )
        await db.commit()

    round_messages = [message for message in messages if message.get("role") in {"user", "system"}]
    assert round_messages, "the assembled round-1 model input must contain the continuation turn"
    assembled = " ".join(
        message["content"] if isinstance(message["content"], str) else str(message["content"])
        for message in round_messages
    )
    assert marker in assembled
    assert all(message["role"] == "system" for message in round_messages), (
        "runtime-authored continuation guidance must not claim user provenance"
    )

    async with owner_sessionmaker() as db:
        snapshot = (
            await db.execute(
                select(SessionModelResult).where(
                    SessionModelResult.tenant_id == tenant_id,
                    SessionModelResult.run_id == task.id,
                )
            )
        ).scalar_one()
        assert snapshot.bound_input_ids_json == [str(input_id)]
