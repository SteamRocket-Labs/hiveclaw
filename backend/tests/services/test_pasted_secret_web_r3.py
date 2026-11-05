"""Round-three probes at persistence, command and Web message boundaries."""
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent_session_goal import AgentSessionGoal
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.services.session_human_input import input_parts_to_runtime_messages, revise_unbound_human_input
from app.services.session_live_input import submit_live_human_input
from tests.services.pasted_secret_corpus_r3 import KEY, negative_cases, positive_cases
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments
from tests.services.test_pasted_secret_web_entries import principals, stored_rows
from tests.services.test_session_input_control_v2 import _seed_session, _authority, _accepted_input
from tests.integration.conftest import _route_default_app_engine  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("channel,source,notice", [
    ("web", "web", True),
    ("feishu", "channel_ingress", False),
    ("web", "agent_session_mailbox", False),
])
async def test_dispatch_preserves_ingress_redaction_and_notice(owner_sessionmaker, monkeypatch, channel, source, notice):
    from app.services.privacy_layer import PrivacyLayer

    paste_inputs = []
    redact = PrivacyLayer.redact_pasted_credentials

    def capture(payload):
        paste_inputs.append(payload)
        return redact(payload)

    monkeypatch.setattr(PrivacyLayer, "redact_pasted_credentials", capture)
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids, channel)
        receipt = await submit_live_human_input(
            db=db, agent=agent, user=user, session=session, content=KEY,
            source=source, source_channel=channel,
        )
        assert receipt["dispatch_status"] == "dispatched"
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        task = await db.get(RuntimeTask, UUID(receipt["run"]["run_id"]))
        expected = "[REDACTED_SECRET]" if notice else KEY
        assert row.content_parts_json[0]["text"] == task.prompt == expected
        assert receipt["pasted_credential_notice"] is notice
        assert bool(row.content_parts_json[0].get("metadata", {}).get("pasted_credential_notice")) is notice
        assert bool(task.metadata_json.get("pasted_credential_notice")) is notice
        assert len(paste_inputs) == int(notice)
        if notice:
            assert_no_fragments(await stored_rows(db, session.id, agent.id), KEY)


@pytest.mark.parametrize("kind,content,secret", list(positive_cases()) + [(k, c, None) for k, c in negative_cases()], ids=[x[0] for x in positive_cases()] + [x[0] for x in negative_cases()])
async def test_every_review_probe_at_storage_and_model_input(owner_sessionmaker, kind, content, secret):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=content, display_content=content, source="web")
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        model = input_parts_to_runtime_messages([row])
        if secret:
            assert_no_fragments(await stored_rows(db, session.id, agent.id), secret)
            assert_no_fragments(model, secret)
            assert receipt["pasted_credential_notice"] is True
        else:
            assert row.content_parts_json[0]["text"] == content, kind
            assert model[0]["content"] == content
            assert not receipt["pasted_credential_notice"]


@pytest.mark.parametrize("revision", [False, True])
@pytest.mark.parametrize("secret", [False, True])
async def test_client_parts_cannot_carry_side_text_or_notice(owner_sessionmaker, revision, secret):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        content = KEY if secret else "hello"
        parts = [None, {"type": "text", "text": content, "note": KEY, "metadata": {"pasted_credential_notice": True}}, {"type": "text", "text": "world"}]
        if revision:
            authority = await _authority(db, user_id=user.id, agent_id=agent.id, session_id=session.id)
            accepted = await _accepted_input(db, authority=authority, session_id=session.id)
            receipt = asdict(await revise_unbound_human_input(db, authority=authority, user=user, input_id=accepted.input_id, expected_revision=1, content_parts=parts))
        else:
            receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content="", parts=parts, source="web")
        row = await db.get(SessionTurnInput, UUID(str(receipt["input_id"])))
        assert_no_fragments(row.content_parts_json, KEY)
        assert_no_fragments(input_parts_to_runtime_messages([row]), KEY)
        assert receipt["pasted_credential_notice"] is secret
        assert row.content_parts_json[0]["text"] == ("[REDACTED_SECRET]" if secret else "hello")


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "slack", "telegram", "discord", "microsoft_teams", "wechat_personal"])
async def test_im_channel_is_selected_by_server_not_source_name(owner_sessionmaker, channel):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids, channel)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content=KEY, source="channel_ingress", source_channel=channel)
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        assert row.content_parts_json[0]["text"] == KEY
        assert not receipt["pasted_credential_notice"]


async def test_team_runtime_source_is_overridden_by_server(owner_sessionmaker):
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session, content="hello", source="agent_team_member_run", runtime_metadata={"source": "agent_team"})
        row = await db.get(SessionTurnInput, UUID(receipt["input_id"]))
        command = await db.get(SessionCommand, row.command_id)
        assert command.target_json["runtime_metadata"]["source"] == "agent_team_member_run"


async def test_goal_update_notice_is_on_updated_event(owner_sessionmaker, monkeypatch):
    monkeypatch.setattr(AgentSessionGoal.__mapper__, "eager_defaults", True)
    import app.api.commands as api
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        await api.execute_agent_command(agent.id, "goal_start", api.ExecuteCommandIn(arguments={"objective": "ordinary objective"}, session_id=str(session.id), origin="agent"), user, db)
        await api.execute_agent_command(agent.id, "goal_update", api.ExecuteCommandIn(arguments={"objective": KEY}, session_id=str(session.id), origin="agent"), user, db)
        rows = await stored_rows(db, session.id, agent.id)
        assert_no_fragments(rows, KEY)
        assert rows["agent_session_goals"][0]["metadata_json"]["pasted_credential_notice"] is True
        event = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session.id).order_by(ChatTranscriptEvent.sequence.desc()))).first()
        assert event.metadata_json["pasted_credential_notice"] is True


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
async def test_goal_notice_does_not_repeat_on_later_transitions(owner_sessionmaker, action):
    from app.api.session_goals import _append_goal_transition_event
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        goal = AgentSessionGoal(tenant_id=agent.tenant_id, agent_id=agent.id, chat_session_id=session.id, created_by_user_id=user.id, objective="[REDACTED_SECRET]", metadata_json={"pasted_credential_notice": True})
        db.add(goal)
        await db.flush()
        await _append_goal_transition_event(db=db, agent=agent, user=user, session_id=session.id, goal=goal, action=action)
        event = (await db.scalars(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session.id).order_by(ChatTranscriptEvent.sequence.desc()))).first()
        assert not event.metadata_json.get("pasted_credential_notice")


@pytest.mark.parametrize("entry", ["create", "rename", "session", "branch", "command"])
async def test_boundary_unavailable_returns_503_for_writes(owner_sessionmaker, monkeypatch, entry):
    from app.api.chat_sessions import CreateSessionIn, CreateSessionRunIn, PatchSessionIn, create_session, create_session_run, rename_session
    from app.services.conversation_branch_service import create_conversation_branch
    from app.services.chat_transcript import append_session_event
    from app.services.credential_boundary_loader import RuntimeIngressSecretBoundaryUnavailable
    import app.api.commands as api
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        event = await append_session_event(db=db, agent_id=agent.id, tenant_id=agent.tenant_id, session_id=session.id, actor_type="user", event_type="message", role="user", user_id=user.id, content="anchor", source="web")
        async def unavailable(*a, **kw):
            raise RuntimeIngressSecretBoundaryUnavailable("test inventory unavailable")
        monkeypatch.setattr("app.services.credential_boundary_loader.load_runtime_ingress_secret_boundary", unavailable)
        with pytest.raises(HTTPException) as exc:
            if entry == "create":
                await create_session(agent.id, CreateSessionIn(title=KEY), user, db)
            elif entry == "rename":
                await rename_session(agent.id, session.id, PatchSessionIn(title=KEY), user, db)
            elif entry == "session":
                await create_session_run(agent.id, CreateSessionRunIn(title=KEY, content="hello"), user, db)
            elif entry == "branch":
                await create_conversation_branch(db=db, agent=agent, user=user, source_session=session, mode="branch", anchor_event_id=event.event_id, title=KEY)
            else:
                await api.execute_agent_command(agent.id, "goal_start", api.ExecuteCommandIn(arguments={"objective": KEY}, session_id=str(session.id), origin="agent"), user, db)
        assert exc.value.status_code == 503


async def test_goal_continuation_carries_this_prompt_notice(owner_sessionmaker, monkeypatch):
    from app.services.goal_continuation_service import continue_session_goal
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        goal = AgentSessionGoal(tenant_id=agent.tenant_id, agent_id=agent.id,
            chat_session_id=session.id, created_by_user_id=user.id, objective=KEY)
        db.add(goal)
        await db.flush()
        captured = []

        async def capture(**kwargs):
            captured.append(kwargs)
            return {"run_id": str(uuid4()), "ok": True}

        monkeypatch.setattr("app.services.goal_continuation_service._submit_goal_runtime_input", capture)
        await continue_session_goal(db=db, agent=agent, user=user, session=session, goal=goal)
        assert captured
        assert_no_fragments(captured[0]["content"], KEY)
        assert captured[0]["extra_runtime_metadata"]["pasted_credential_notice"] is True
        assert goal.metadata_json["pasted_credential_notice"] is True
        await db.rollback()


async def test_runtime_payload_cannot_choose_a_system_channel(owner_sessionmaker):
    from app.services.web_chat_runtime import start_web_chat_run
    ids = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await principals(db, ids)
        await start_web_chat_run(db=db, agent=agent, user=user, session=session, content=KEY,
            extra_metadata={"source": "agent_session_mailbox", "channel": "agent_session_mailbox"})
        assert_no_fragments(await stored_rows(db, session.id, agent.id), KEY)
