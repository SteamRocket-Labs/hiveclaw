"""Requester-owned opaque secrets are removed before every reviewed Web write."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.custom_api_account import CustomApiAccount
from app.models.user import User
from tests.integration.test_d9s_stored_secret_boundary_postgres import TOKENS, _invoke, _stored_accounts
from tests.services.test_pasted_secret_web_entries import stored_rows

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _principals(db, owner, agent, session):
    return await db.get(Agent, agent), await db.get(User, owner), await db.get(ChatSession, session)


async def test_goal_objective_and_transition_never_persist_own_token(owner_sessionmaker):
    from app.api.session_goals import StartGoalIn, start_session_goal

    tenant, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await _principals(db, owner, agent_id, session_id)
        await start_session_goal(agent.id, session.id,
            StartGoalIn(objective=TOKENS[0], content=TOKENS[0], start_immediately=False), user, db)
        await db.commit()
        goal = await db.scalar(select(AgentSessionGoal).where(AgentSessionGoal.chat_session_id == session.id))
        assert goal.objective == "[REDACTED_SECRET]"
        assert TOKENS[0] not in json.dumps(await stored_rows(db, session.id, agent.id))


async def test_slash_command_argument_never_persists_own_token(owner_sessionmaker, monkeypatch):
    from app.api import commands as api

    _, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)

    async def gate(*args, **kwargs):
        pass

    async def fire(*args, **kwargs):
        return {"fired": True}

    monkeypatch.setattr(api, "enforce_plan_gate", gate)
    monkeypatch.setattr(api, "_fire_loop_trigger_now", fire)
    async with owner_sessionmaker() as db:
        agent, user, session = await _principals(db, owner, agent_id, session_id)
        await api.execute_agent_command(agent.id, "loop",
            api.ExecuteCommandIn(arguments={"input": "5m " + TOKENS[0]}, session_id=str(session.id), origin="web"), user, db)
        await db.commit()
        rows = await stored_rows(db, session.id, agent.id)
        assert rows["agent_triggers"]
        assert TOKENS[0] not in json.dumps(rows)
        assert "[REDACTED_SECRET]" in json.dumps(rows["agent_triggers"])


@pytest.mark.parametrize("entry", ["create", "rename", "first_input", "branch"])
async def test_session_titles_never_persist_own_token(owner_sessionmaker, entry):
    from app.api.chat_sessions import CreateSessionIn, CreateSessionRunIn, PatchSessionIn, create_session, create_session_run, rename_session
    from app.services.chat_transcript import append_session_event
    from app.services.conversation_branch_service import create_conversation_branch

    _, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await _principals(db, owner, agent_id, session_id)
        if entry == "create":
            result = await create_session(agent.id, CreateSessionIn(title=TOKENS[0]), user, db)
            title = result.title
        elif entry == "rename":
            result = await rename_session(agent.id, session.id, PatchSessionIn(title=TOKENS[0]), user, db)
            title = result["title"]
        elif entry == "first_input":
            result = await create_session_run(agent.id, CreateSessionRunIn(title=TOKENS[0], content="Ordinary input"), user, db)
            title = result.session.title
        else:
            anchor = await append_session_event(db=db, agent_id=agent.id, tenant_id=agent.tenant_id,
                session_id=session.id, actor_type="user", event_type="message", role="user", user_id=user.id,
                content="Anchor", source="web")
            result = await create_conversation_branch(db=db, agent=agent, user=user, source_session=session,
                mode="branch", anchor_event_id=anchor.event_id, title=TOKENS[0])
            title = result.session.title
        await db.commit()
        assert title == "[REDACTED_SECRET]"
        assert TOKENS[0] not in json.dumps(await stored_rows(db, session.id, agent.id))


async def test_goal_continuation_uses_goal_owner_and_updates_persisted_objective(owner_sessionmaker, monkeypatch):
    from app.services.goal_continuation_service import continue_session_goal

    tenant, owner, other, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    submitted = []

    async def submit(**kwargs):
        submitted.append(kwargs)
        return {"ok": True, "run_id": str(uuid.uuid4())}

    monkeypatch.setattr("app.services.goal_continuation_service._submit_goal_runtime_input", submit)
    async with owner_sessionmaker() as db:
        agent, _, session = await _principals(db, owner, agent_id, session_id)
        goal = AgentSessionGoal(tenant_id=tenant, agent_id=agent.id, chat_session_id=session.id,
            created_by_user_id=owner, objective=TOKENS[0])
        db.add(goal)
        await db.flush()
        await continue_session_goal(db=db, agent=agent, user=await db.get(User, other), session=session, goal=goal)
        await db.commit()
        assert submitted
        assert goal.objective == "[REDACTED_SECRET]"
        assert TOKENS[0] not in goal.metadata_json["last_continuation_prompt"]
        assert TOKENS[0] not in str(submitted)


async def test_team_member_mailbox_and_preview_never_persist_own_token(owner_sessionmaker):
    from app.api.agent_teams import MessageAgentTeamMemberIn, message_agent_team_member

    tenant, owner, _, agent_id, parent_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, parent = await _principals(db, owner, agent_id, parent_id)
        child = ChatSession(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent.id, user_id=user.id,
            title="D9s round2 teammate", session_kind="team_member", parent_session_id=parent.id)
        db.add(child)
        team = AgentTeam(id=uuid.uuid4(), tenant_id=tenant, lead_agent_id=agent.id, parent_session_id=parent.id,
            created_by_user_id=user.id, name="D9s round2 team")
        db.add(team)
        await db.flush()
        member = AgentTeamMember(id=uuid.uuid4(), team_id=team.id, member_name="Teammate", chat_session_id=child.id)
        db.add(member)
        await db.commit()
        result = await message_agent_team_member(agent.id, team.id, member.id,
            MessageAgentTeamMemberIn(message=TOKENS[0], display_content=TOKENS[0]), user, db)
        await db.commit()
        events = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == child.id))).all()
        previews = (await db.scalars(select(AgentTeamEvent).where(AgentTeamEvent.team_id == team.id))).all()
        assert any(e.event_type == "agent_session_message" for e in events), result
        assert any((e.payload_json or {}).get("message_preview") for e in previews), result
        assert TOKENS[0] not in str([e.content for e in events])
        assert TOKENS[0] not in str([e.payload_json for e in previews])
        assert "[REDACTED_SECRET]" in str([e.content for e in events])
        assert "[REDACTED_SECRET]" in str([e.payload_json for e in previews])


async def test_personal_basic_username_remains_normal_member_text(owner_sessionmaker, monkeypatch):
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_live_input import submit_live_human_input

    tenant, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await _principals(db, owner, agent_id, session_id)
        account = await db.scalar(select(CustomApiAccount).where(CustomApiAccount.owner_user_id == owner))
        account.config = {"basic_username": user.email, "basic_password": TOKENS[0]}
        await db.commit()
        content = f"Send the report to {user.email}."
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=content, source="web")
        row = await db.get(SessionTurnInput, uuid.UUID(receipt["input_id"]))
        assert row.content_parts_json[0]["text"] == content
        assert not receipt.get("pasted_credential_notice")
    captured, prepared = await _invoke(monkeypatch, tenant=tenant, user=owner, agent=agent_id, content=content)
    assert content in captured and content in prepared
