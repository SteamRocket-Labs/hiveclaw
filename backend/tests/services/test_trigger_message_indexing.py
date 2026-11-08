from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services import trigger_daemon as daemon
from tests.services.test_session_input_control_v2 import _seed_session
from tests.services.test_trigger_daemon import _route_scoped_session


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["identity", "name", "agent"])
@pytest.mark.parametrize("scenario", ["normal", "hot", "empty"])
async def test_legacy_message_results_equal_original_query(
    owner_sessionmaker, monkeypatch, branch, scenario
):
    tenant, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    now = datetime.now(UTC)
    since = now - timedelta(hours=1)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        source = Agent(id=uuid4(), tenant_id=tenant, creator_id=user_id, name="Source employee")
        db.add(source)
        session = await db.get(ChatSession, session_id)
        session.source_channel = "feishu"
        session.delivery_target_json = {"channel": "feishu", "user_id": "alice"}
        others = [
            ChatSession(id=uuid4(), tenant_id=tenant, agent_id=agent_id, user_id=user_id,
                source_channel=channel, delivery_target_json={"channel": channel, "user_id": sender})
            for channel, sender in [("feishu", "bob"), ("slack", "alice")]
        ]
        # Empty watched sessions must contribute no messages or fabricated match.
        db.add_all(others + [
            ChatSession(id=uuid4(), tenant_id=tenant, agent_id=agent_id, user_id=user_id)
            for _ in range(5)
        ])
        await db.flush()
        outside = ChatSession(id=uuid4(), tenant_id=tenant, agent_id=source.id, user_id=user_id)
        db.add(outside)
        await db.flush()

        def message(sid, role, at, *, participant=None, target_agent=agent_id):
            return ChatMessage(id=uuid4(), tenant_id=tenant, agent_id=target_agent, user_id=user_id,
                conversation_id=str(sid), role=role, content=f"{role} at {at.isoformat()}",
                participant_id=participant, created_at=at)

        if scenario != "empty":
            db.add_all([
                message(session_id if i % 2 == 0 else others[0].id, "user", now + timedelta(seconds=i))
                for i in range(24)
            ])
            db.add(message(others[1].id, "user", now + timedelta(seconds=24)))
            db.add(message(session_id, "assistant", now + timedelta(seconds=25),
                participant=source.participant_id))
            db.add(message(session_id, "user", since - timedelta(seconds=1)))
        if scenario == "hot":
            db.add_all([
                message(session_id, "assistant", now + timedelta(seconds=100, microseconds=i))
                for i in range(1000)
            ])
        db.add(message(outside.id, "user", now + timedelta(seconds=200), target_agent=source.id))
        db.add(message(outside.id, "assistant", now + timedelta(seconds=201),
            participant=source.participant_id, target_agent=source.id))
        db.add(message("web", "user", now + timedelta(seconds=202)))
        await db.commit()
        username, source_id, participant_id = user.username, source.id, source.participant_id

    # This is the original query, including its global sort, limit and join.
    original = select(ChatMessage, ChatSession).join(
        ChatSession, ChatMessage.conversation_id == cast(ChatSession.id, String)
    ).where(ChatSession.agent_id == agent_id, ChatMessage.created_at > since)
    if branch == "agent":
        original = original.where(ChatMessage.role == "assistant", ChatMessage.participant_id == participant_id)
        config = {"from_agent_id": str(source_id)}
    elif branch == "name":
        original = original.where(ChatMessage.role == "user", ChatSession.user_id == user_id)
        config = {"from_user_name": username}
    else:
        original = original.where(ChatMessage.role == "user")
        config = {"from_user_identity": "feishu:alice", "from_channel": "feishu"}
    original = original.order_by(ChatMessage.created_at.desc()).limit(20 if branch == "identity" else 1)
    async with owner_sessionmaker() as db:
        expected = (await db.execute(original)).all()
    expected_ids = [row[0].id for row in expected]
    eligible = expected
    if branch == "identity":
        eligible = [row for row in expected if row[1].source_channel == "feishu"
            and (row[1].delivery_target_json or {}).get("user_id") == "alice"]

    observed = []

    class RecordingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if getattr(statement, "is_select", False) and "chat_messages" in str(statement):
                frozen = result.freeze()
                observed.append([row[0].id for row in frozen().all()])
                return frozen()
            return result

    factory = async_sessionmaker(owner_sessionmaker.kw["bind"], class_=RecordingSession, expire_on_commit=False)
    _route_scoped_session(monkeypatch, daemon, factory, tenant_id=tenant)
    trigger = SimpleNamespace(agent_id=agent_id, name="legacy match", created_at=since,
        last_fired_at=None, fire_count=0, config=config, reply_context=None)
    assert await daemon._check_new_agent_messages(trigger) is bool(eligible)
    assert observed == [expected_ids]
    if eligible:
        assert config["_matched_message"] == eligible[0][0].content
        assert config["_matched_event_key"] == f"chat_message:{eligible[0][0].id}"
    else:
        assert "_matched_event_key" not in config
