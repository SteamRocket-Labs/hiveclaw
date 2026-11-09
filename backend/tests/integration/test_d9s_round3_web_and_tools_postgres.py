"""Final team-run, tool-sequence and continuation persistence regressions."""

import html
from pathlib import Path
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_session_goal import AgentSessionGoal
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.custom_api_account import CustomApiAccount, CustomApiAccountGrant
from app.models.tool import Tool
from app.models.user import User
from tests.integration.test_d9s_stored_secret_boundary_postgres import TOKENS, _stored_accounts

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_member_run_preview_and_input_share_redacted_content(owner_sessionmaker):
    from app.api.agent_teams import StartAgentTeamMemberRunIn, start_agent_team_member_run
    from app.models.session_v2 import SessionTurnInput

    tenant, owner, _, agent_id, parent_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user = await db.get(Agent, agent_id), await db.get(User, owner)
        child = ChatSession(id=uuid.uuid4(), tenant_id=tenant, agent_id=agent.id, user_id=user.id,
            title="D9s final teammate", session_kind="team_member", parent_session_id=parent_id)
        team = AgentTeam(id=uuid.uuid4(), tenant_id=tenant, lead_agent_id=agent.id, parent_session_id=parent_id,
            created_by_user_id=user.id, name="D9s final team")
        db.add_all([child, team])
        await db.flush()
        member = AgentTeamMember(id=uuid.uuid4(), team_id=team.id, member_name="Teammate", chat_session_id=child.id)
        db.add(member)
        await db.commit()
        result = await start_agent_team_member_run(agent.id, team.id, member.id,
            StartAgentTeamMemberRunIn(content=TOKENS[0], display_content=TOKENS[0]),
            current_user=user, db=db)
        await db.commit()
        events = (await db.scalars(select(AgentTeamEvent).where(AgentTeamEvent.team_id == team.id))).all()
        inputs = (await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == child.id))).all()
        assert events and inputs, result
        assert all(TOKENS[0] not in str(event.payload_json) for event in events)
        assert any((event.payload_json or {}).get("content_preview") == "[REDACTED_SECRET]" for event in events)
        assert all(TOKENS[0] not in str(row.content_parts_json) for row in inputs)


async def test_personal_basic_call_does_not_deny_later_member_email_arguments(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import agent_tools, custom_api_connectors
    from app.services.custom_api_accounts import bind_account, connector_revision
    from app.tools.runtime import ToolExecutionContext

    tenant, owner, _, agent_id, session_id, tool_id = await _stored_accounts(owner_sessionmaker)
    password = TOKENS[0]
    async with owner_sessionmaker() as db:
        agent, user, tool = await db.get(Agent, agent_id), await db.get(User, owner), await db.get(Tool, tool_id)
        username = user.email
        tool.config = {**tool.config, "auth": {"scheme": "basic"},
            "action": {"path": "/read", "query": {"assignee": "{member_email}"}}}
        tool.parameters_schema = {"type": "object", "properties": {"member_email": {"type": "string"}}}
        account = await db.scalar(select(CustomApiAccount).where(CustomApiAccount.owner_user_id == owner))
        account.config = {"basic_username": username, "basic_password": password}
        account.connector_revision = connector_revision(tool)
        grant = CustomApiAccountGrant(tenant_id=tenant, account_id=account.id, agent_id=agent_id,
            requester_user_id=owner, enabled=True, version=1)
        db.add(grant)
        await db.flush()
        await bind_account(db, agent=agent, tool=tool, session_id=session_id, user=user,
            account_id=account.id, account_version=account.version, grant_version=grant.version)
        await db.commit()
        name = tool.name
    sent = []

    async def provider(method, url, **kwargs):
        sent.append(kwargs)
        return httpx.Response(200, json={"member": username, "echo": password})

    scoped = lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    monkeypatch.setattr(custom_api_connectors, "tenant_scoped_session", scoped)
    monkeypatch.setattr("app.services.governed_egress.request_governed_http", provider)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    context = ToolExecutionContext(agent_id=agent_id, user_id=owner, tenant_id=str(tenant),
        session_id=str(session_id), workspace=Path(tmp_path))
    service = agent_tools._get_tool_runtime_service()
    first = await custom_api_connectors.execute_custom_api_tool(name, {"member_email": "self"},
        agent_id=agent_id, execution_context=context)
    assert len(sent) == 1, first
    assert password not in str(first) and "[REDACTED_SECRET]" in str(first)
    second = await service.execute_with_context(name, {"member_email": username}, context)
    assert len(sent) == 2, second
    assert "unauthorized_secret_bytes" not in str(second)
    assert username in str(second)
    assert password not in str(second) and "[REDACTED_SECRET]" in str(second)
    assert context.exact_secret_boundary.redact_text(password).text == "[REDACTED_SECRET]"


@pytest.mark.parametrize("secret", ['D9sRound3Opaque&Token012345', 'D9sRound3Opaque"Token012345'])
async def test_goal_objective_is_redacted_before_html_escaping(owner_sessionmaker, monkeypatch, secret):
    from app.services.goal_continuation_service import continue_session_goal

    tenant, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    submitted = []

    async def submit(**kwargs):
        submitted.append(kwargs)
        return {"ok": True, "run_id": str(uuid.uuid4())}

    monkeypatch.setattr("app.services.goal_continuation_service._submit_goal_runtime_input", submit)
    async with owner_sessionmaker() as db:
        agent, user, session = await db.get(Agent, agent_id), await db.get(User, owner), await db.get(ChatSession, session_id)
        account = await db.scalar(select(CustomApiAccount).where(CustomApiAccount.owner_user_id == owner))
        account.config = {"bearer_token": secret}
        goal = AgentSessionGoal(tenant_id=tenant, agent_id=agent.id, chat_session_id=session.id,
            created_by_user_id=owner, objective=f"Continue with {secret}.")
        db.add(goal)
        await db.flush()
        await continue_session_goal(db=db, agent=agent, user=user, session=session, goal=goal)
        await db.commit()
        assert submitted
        copies = str(submitted) + str(goal.metadata_json["last_continuation_prompt"])
        assert secret not in copies and html.escape(secret) not in copies
        assert "[REDACTED_SECRET]" in copies
        assert secret not in goal.objective
