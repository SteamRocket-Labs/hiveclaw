"""Authored Web inputs are protected before the first database write."""
import json
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.session_live_input import submit_live_human_input
from tests.services.pasted_secret_corpus import positive_cases, review_negative_cases
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments
from tests.services.test_session_input_control_v2 import _seed_session, _authority, _accepted_input
from tests.integration.conftest import _route_default_app_engine  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
KEY = "sk-proj-R2SyntheticSeparateSecret0123456789AbCdEf"
TABLES = ("chat_sessions", "chat_messages", "chat_transcript_events", "session_commands", "session_turn_inputs", "runtime_tasks", "session_input_admissions", "session_event_outbox", "agent_session_goals", "agent_triggers")


async def principals(db, ids, channel="web"):
    _, uid, aid, sid, _ = ids
    agent, user, session = await db.get(Agent, aid), await db.get(User, uid), await db.get(ChatSession, sid)
    session.source_channel = channel
    await db.commit()
    return agent, user, session


async def stored_rows(db, sid, aid):
    rows = {}
    for table in TABLES:
        result = await db.execute(text(f"SELECT row_to_json(t)::text FROM {table} t WHERE row_to_json(t)::text LIKE :sid OR row_to_json(t)::text LIKE :aid"), {"sid": f"%{sid}%", "aid": f"%{aid}%"})
        rows[table] = [json.loads(row) for row in result.scalars()]
    return rows


@pytest.mark.parametrize("kind,content", list(review_negative_cases()))
async def test_review_negative_through_real_ingress(owner_sessionmaker, kind, content):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=content, display_content=content, parts=[{"type": "text", "text": content}], source="legacy_rest_start_session_run", runtime_metadata={"permission_mode": "plan"})
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        assert row.content_parts_json[0]["text"] == content, kind
        command = await db.get(SessionCommand, row.command_id)
        assert command.target_json["runtime_metadata"]["permission_mode"] == "plan"
        assert not receipt.get("pasted_credential_notice")


@pytest.mark.parametrize("kind,content,secret", list(positive_cases())[:42])
async def test_each_provider_through_real_ingress(owner_sessionmaker, kind, content, secret):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=content, display_content=content, source="legacy_rest_start_session_run")
        assert_no_fragments(await stored_rows(db, session.id, agent.id), secret)
        assert receipt["pasted_credential_notice"] is True, kind


@pytest.mark.parametrize("channel", ["web", "feishu"])
async def test_web_goal_protects_objective_and_continuation_copies(owner_sessionmaker, monkeypatch, channel):
    from app.api.session_goals import StartGoalIn, start_session_goal
    from app.models.agent_session_goal import AgentSessionGoal
    from app.services.goal_continuation_service import continue_session_goal

    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids, channel)
        result = await start_session_goal(agent.id, session.id, StartGoalIn(objective=KEY, content=KEY, display_content=KEY, start_immediately=True), user, db)
        assert_no_fragments(result, KEY)
        assert_no_fragments(await stored_rows(db, session.id, agent.id), KEY)
        goal = await db.scalar(select(AgentSessionGoal).where(AgentSessionGoal.chat_session_id == session.id))
        # A previously stored objective can still reach the continuation writer.
        # Keep the old row intact; only the newly written prompt must be safe.
        goal.objective = KEY
        async def capture(**kwargs):
            assert_no_fragments(kwargs["content"], KEY)
            assert_no_fragments(goal.metadata_json["last_continuation_prompt"], KEY)
            return {"run_id": str(uuid4()), "ok": True}
        monkeypatch.setattr("app.services.goal_continuation_service._submit_goal_runtime_input", capture)
        await continue_session_goal(db=db, agent=agent, user=user, session=session, goal=goal)
        await db.rollback()


@pytest.mark.parametrize("channel", ["web", "feishu"])
@pytest.mark.parametrize("command,args", [
    ("loop", {"input": "5m " + KEY}),
    ("goal", {"objective": KEY}),
    ("schedule", {"name": "R2 schedule", "instruction": KEY, "cron_expr": "0 * * * *"}),
    ("once", {"name": "R2 once", "instruction": KEY, "at": "2030-01-01T00:00:00Z"}),
])
async def test_web_commands_protect_every_early_sink(owner_sessionmaker, monkeypatch, channel, command, args):
    import app.api.commands as api
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids, channel)
        async def gate(*a, **kw):
            return None
        async def fire(*a, **kw):
            return {"fired": True}
        monkeypatch.setattr(api, "enforce_plan_gate", gate)
        monkeypatch.setattr(api, "_fire_loop_trigger_now", fire)
        result = await api.execute_agent_command(agent.id, command, api.ExecuteCommandIn(arguments=args, session_id=str(session.id)), user, db)
        assert_no_fragments(await stored_rows(db, session.id, agent.id), KEY)
        assert_no_fragments(result, KEY)


@pytest.mark.parametrize("source", [None, "agent_team_member_run", "session_goal", "future_web_entry"])
async def test_web_policy_defaults_on_even_for_im_origin(owner_sessionmaker, source):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids, "feishu")
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=KEY, source=source)
        assert_no_fragments(await stored_rows(db, session.id, agent.id), KEY)
        assert receipt["pasted_credential_notice"] is True


@pytest.mark.parametrize("source", ["plan_mode_handoff", "runtime_result_integration", "hr_agent_handoff", "trigger_reflection", "system_plan_run", "loop_same_session", "goal_continuation", "agent_session_mailbox", "feishu", "dingtalk", "slack", "telegram", "wecom"])
async def test_system_and_im_sources_have_no_second_paste_pass(owner_sessionmaker, source):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=KEY, source=source)
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        assert row.content_parts_json[0]["text"] == KEY
        command = await db.get(SessionCommand, row.command_id)
        assert not command.target_json["runtime_metadata"].get("pasted_credential_notice")
        assert not receipt.get("pasted_credential_notice")
        if (receipt.get("run") or {}).get("run_id"):
            task = await db.get(RuntimeTask, UUID(receipt["run"]["run_id"]))
            assert task.prompt == KEY
            assert not task.metadata_json.get("pasted_credential_notice")


async def test_mailbox_source_override_has_no_second_paste_pass(owner_sessionmaker):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=KEY, source="web", runtime_metadata={"source": "agent_session_mailbox"})
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        assert row.content_parts_json[0]["text"] == KEY
        assert not receipt.get("pasted_credential_notice")
        if (receipt.get("run") or {}).get("run_id"):
            task = await db.get(RuntimeTask, UUID(receipt["run"]["run_id"]))
            assert task.prompt == KEY
            assert not task.metadata_json.get("pasted_credential_notice")


async def test_revision_retains_flag_without_adding_notice_text(owner_sessionmaker):
    from app.services.session_human_input import revise_unbound_human_input
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        authority = await _authority(db, user_id=user.id, agent_id=agent.id, session_id=session.id)
        accepted = await _accepted_input(db, authority=authority, session_id=session.id)
        for revision in (1, 2):
            receipt = await revise_unbound_human_input(db, authority=authority, user=user, input_id=accepted.input_id, expected_revision=revision, content_parts=[{"type": "text", "text": KEY}])
            row = await db.get(SessionTurnInput, accepted.input_id)
            assert row.content_parts_json[0]["text"] == "[REDACTED_SECRET]"
            assert "我把一段密钥" not in json.dumps(row.content_parts_json, ensure_ascii=False)
            assert asdict(receipt).get("pasted_credential_notice") is True


@pytest.mark.parametrize("entry", ["session", "create", "rename", "branch"])
async def test_caller_titles_are_protected_before_storage(owner_sessionmaker, entry):
    from app.api.chat_sessions import CreateSessionRunIn, CreateSessionIn, PatchSessionIn, create_session_run, create_session, rename_session
    from app.services.chat_transcript import append_session_event
    from app.services.conversation_branch_service import create_conversation_branch
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        if entry == "session":
            result = await create_session_run(agent.id, CreateSessionRunIn(title=KEY, content="Ordinary message"), user, db)
            assert result.session.title == "[REDACTED_SECRET]"
        elif entry == "create":
            result = await create_session(agent.id, CreateSessionIn(title=KEY), user, db)
            assert result.title == "[REDACTED_SECRET]"
        elif entry == "rename":
            result = await rename_session(agent.id, session.id, PatchSessionIn(title=KEY), user, db)
            assert result["title"] == "[REDACTED_SECRET]"
        else:
            event = await append_session_event(db=db, agent_id=agent.id, tenant_id=agent.tenant_id, session_id=session.id, actor_type="user", event_type="message", role="user", user_id=user.id, content="Anchor", source="web")
            branch = await create_conversation_branch(db=db, agent=agent, user=user, source_session=session, mode="branch", anchor_event_id=event.event_id, title=KEY)
            assert branch.session.title == "[REDACTED_SECRET]"


async def test_old_goal_new_continuation_prompt_is_hidden(owner_sessionmaker, monkeypatch):
    from app.models.agent_session_goal import AgentSessionGoal
    from app.services.goal_continuation_service import continue_session_goal
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        goal = AgentSessionGoal(tenant_id=agent.tenant_id, agent_id=agent.id, chat_session_id=session.id, created_by_user_id=user.id, objective=KEY)
        db.add(goal)
        await db.flush()
        submitted = []
        async def capture(**kwargs):
            submitted.append(kwargs)
            assert_no_fragments(goal.metadata_json["last_continuation_prompt"], KEY)
            assert_no_fragments(kwargs, KEY)
            return {"ok": True, "run_id": str(uuid4())}
        monkeypatch.setattr("app.services.goal_continuation_service._submit_goal_runtime_input", capture)
        await continue_session_goal(db=db, agent=agent, user=user, session=session, goal=goal)
        assert submitted
        await db.rollback()
