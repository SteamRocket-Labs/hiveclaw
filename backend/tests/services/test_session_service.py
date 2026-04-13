from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.flushes = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()

    async def flush(self):
        self.flushes += 1


@pytest.mark.asyncio
async def test_create_chat_session_flushes_and_preserves_fields():
    from app.services.session_service import create_chat_session, session_conversation_id

    db = _SequenceDB([])
    session_id = uuid4()
    created_at = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)

    session = await create_chat_session(
        db,
        session_id=session_id,
        agent_id=uuid4(),
        user_id=uuid4(),
        title="Reflection Session",
        source_channel="trigger",
        participant_id=uuid4(),
        peer_agent_id=uuid4(),
        external_conv_id="web_alice",
        delivery_target_json={"channel": "web"},
        created_at=created_at,
    )

    assert db.flushes == 1
    assert db.added == [session]
    assert session.id == session_id
    assert session.title == "Reflection Session"
    assert session.source_channel == "trigger"
    assert session.external_conv_id == "web_alice"
    assert session.delivery_target_json == {"channel": "web"}
    assert session.created_at == created_at
    assert session_conversation_id(session) == str(session_id)


@pytest.mark.asyncio
async def test_find_or_create_agent_pair_session_orders_ids_and_reuses_existing():
    from app.services.session_service import find_or_create_agent_pair_session

    source_agent_id = uuid4()
    target_agent_id = uuid4()
    existing = SimpleNamespace(id=uuid4(), agent_id=min(source_agent_id, target_agent_id, key=str))
    db = _SequenceDB([existing])

    session = await find_or_create_agent_pair_session(
        db,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        owner_user_id=uuid4(),
        source_name="Source",
        target_name="Target",
        source_participant_id=uuid4(),
    )

    assert session is existing
    assert db.added == []


@pytest.mark.asyncio
async def test_find_or_create_agent_pair_session_creates_ordered_session():
    from app.services.session_service import find_or_create_agent_pair_session

    source_agent_id = uuid4()
    target_agent_id = uuid4()
    owner_user_id = uuid4()
    participant_id = uuid4()
    db = _SequenceDB([None])

    session = await find_or_create_agent_pair_session(
        db,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        owner_user_id=owner_user_id,
        source_name="Source",
        target_name="Target",
        source_participant_id=participant_id,
    )

    assert db.flushes == 1
    assert db.added == [session]
    assert session.agent_id == min(source_agent_id, target_agent_id, key=str)
    assert session.peer_agent_id == max(source_agent_id, target_agent_id, key=str)
    assert session.user_id == owner_user_id
    assert session.participant_id == participant_id
    assert session.source_channel == "agent"


@pytest.mark.asyncio
async def test_find_or_create_web_chat_session_reuses_requested_session(monkeypatch):
    from app.services.session_service import find_or_create_web_chat_session

    requested = SimpleNamespace(id=uuid4(), user_id=uuid4(), agent_id=uuid4(), source_channel="web")
    db = _SequenceDB([requested])
    target = {"channel": "web", "username": "alice", "session_id": str(requested.id)}

    async def fake_apply(db_arg, *, session, agent_id, user):
        session.delivery_target_json = target
        return target

    monkeypatch.setattr("app.services.session_service.apply_web_session_contract", fake_apply)

    user = SimpleNamespace(id=requested.user_id, username="alice", display_name="Alice")
    session = await find_or_create_web_chat_session(
        db,
        agent_id=requested.agent_id,
        user=user,
        requested_session_id=str(requested.id),
    )

    assert session is requested
    assert session.delivery_target_json == target


@pytest.mark.asyncio
async def test_find_or_create_web_chat_session_creates_default_session(monkeypatch):
    from app.services.session_service import find_or_create_web_chat_session

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), username="alice", display_name="Alice")
    db = _SequenceDB([None, None])
    target = {"channel": "web", "username": "alice"}

    async def fake_apply(db_arg, *, session, agent_id, user):
        session.delivery_target_json = target
        return target

    monkeypatch.setattr("app.services.session_service.apply_web_session_contract", fake_apply)

    session = await find_or_create_web_chat_session(
        db,
        agent_id=agent_id,
        user=user,
    )

    assert db.flushes == 1
    assert db.added == [session]
    assert session.agent_id == agent_id
    assert session.user_id == user.id
    assert session.source_channel == "web"
    assert session.title.startswith("Session ")
    assert session.delivery_target_json == target
