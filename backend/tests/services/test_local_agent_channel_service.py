from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import local_agent_channel_service as service


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _FakeDB:
    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.added = []
        self.flushed = False
        self.committed = False

    async def execute(self, _stmt):
        value = self.execute_values.pop(0) if self.execute_values else None
        if hasattr(value, "all") or hasattr(value, "scalar_one_or_none"):
            return value
        return _ScalarResult(value)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_reuses_existing_web_session() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    existing_session = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=None,
        chat_session_id=None,
        source="web",
        status="active",
        created_at=None,
    )
    db = _FakeDB([existing_session])

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )

    assert payload["id"] == existing_session.id
    assert payload["source"] == "web"
    assert payload["status"] == "active"
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_creates_when_missing() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    db = _FakeDB([None])

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
    )

    assert payload["source"] == "web"
    assert payload["status"] == "active"
    assert len(db.added) == 1
    assert db.flushed is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_creates_agent_scoped_chat_session(monkeypatch) -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB([None])
    captured = {}

    async def fake_create_or_bind_chat_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=chat_session_id)

    monkeypatch.setattr(service, "create_or_bind_chat_session", fake_create_or_bind_chat_session)

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        title="Codex on Mac",
    )

    assert payload["chat_session_id"] == chat_session_id
    assert payload["source"] == "web"
    assert len(db.added) == 1
    assert db.added[0].source_agent_id == source_agent_id
    assert db.added[0].chat_session_id == chat_session_id
    assert captured["db"] is db
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == source_agent_id
    assert captured["user_id"] == owner_user_id
    assert captured["external_conversation_id"] == f"local_agent:{owner_user_id}:{source_agent_id}:web"
    assert captured["source_channel"] == "local_agent"


@pytest.mark.asyncio
async def test_get_or_create_default_channel_session_separates_shared_actor_from_host_owner(monkeypatch) -> None:
    tenant_id = uuid4()
    host_owner_id = uuid4()
    actor_user_id = uuid4()
    source_agent_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB([_RowsResult([])])
    captured = {}

    async def fake_create_or_bind_chat_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=chat_session_id)

    monkeypatch.setattr(service, "create_or_bind_chat_session", fake_create_or_bind_chat_session)

    payload = await service.get_or_create_default_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        actor_user_id=actor_user_id,
        source_agent_id=source_agent_id,
        title="Shared Codex",
    )

    assert payload["chat_session_id"] == chat_session_id
    assert len(db.added) == 1
    channel_session = db.added[0]
    assert channel_session.owner_user_id == host_owner_id
    assert channel_session.source_agent_id == source_agent_id
    assert captured["user_id"] == actor_user_id
    assert captured["external_conversation_id"] == (
        f"local_agent:{host_owner_id}:{source_agent_id}:{actor_user_id}:web"
    )


@pytest.mark.asyncio
async def test_list_agent_channel_sessions_returns_sidebar_ready_sessions() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
        source="web",
        status="active",
        created_at=None,
        updated_at=None,
    )
    chat_session = SimpleNamespace(
        id=chat_session_id,
        agent_id=source_agent_id,
        title="Codex on Mac",
        source_channel="local_agent",
        session_kind="local_agent_channel",
        last_message_at=None,
        created_at=None,
    )
    db = _FakeDB([_RowsResult([(channel_session, chat_session)])])

    rows = await service.list_agent_channel_sessions(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        actor_user_id=owner_user_id,
        source_agent_id=source_agent_id,
    )

    assert rows == [
        {
            "id": channel_session_id,
            "chat_session_id": chat_session_id,
            "agent_id": source_agent_id,
            "title": "Codex on Mac",
            "source": "web",
            "source_channel": "local_agent",
            "session_kind": "local_agent_channel",
            "status": "active",
            "created_at": None,
            "updated_at": None,
            "last_message_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_agent_channel_session_accepts_chat_session_id() -> None:
    tenant_id = uuid4()
    owner_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
        source="web",
        status="active",
        created_at=None,
        updated_at=None,
    )
    chat_session = SimpleNamespace(
        id=chat_session_id,
        agent_id=source_agent_id,
        title="Local debug session",
        source_channel="local_agent",
        session_kind="local_agent_channel",
        last_message_at=None,
        created_at=None,
    )
    db = _FakeDB([_RowsResult([(channel_session, chat_session)])])

    payload = await service.resolve_agent_channel_session(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        source_agent_id=source_agent_id,
        session_id=chat_session_id,
    )

    assert payload["id"] == channel_session_id
    assert payload["chat_session_id"] == chat_session_id
    assert payload["title"] == "Local debug session"


@pytest.mark.asyncio
async def test_record_channel_result_writes_assistant_message_for_actor_chat_session() -> None:
    tenant_id = uuid4()
    host_owner_id = uuid4()
    actor_user_id = uuid4()
    source_agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    message_id = uuid4()
    channel_session = SimpleNamespace(
        id=channel_session_id,
        tenant_id=tenant_id,
        owner_user_id=host_owner_id,
        source_agent_id=source_agent_id,
        chat_session_id=chat_session_id,
    )
    channel_message = SimpleNamespace(
        id=message_id,
        session_id=channel_session_id,
        owner_user_id=host_owner_id,
        source_agent_id=source_agent_id,
        tenant_id=tenant_id,
        direction="hive_to_local",
        content="shared caller request",
        status="delivered",
        result=None,
        attachments_json=[],
        metadata_json={},
        created_at=None,
        delivered_at=None,
        completed_at=None,
    )
    mirrored_chat_session = SimpleNamespace(id=chat_session_id, user_id=actor_user_id, last_message_at=None)

    class _DbWithGet(_FakeDB):
        async def get(self, _model, obj_id):
            assert obj_id == chat_session_id
            return mirrored_chat_session

    db = _DbWithGet([channel_session, channel_message])
    context = SimpleNamespace(
        tenant_id=tenant_id,
        user_id=host_owner_id,
        scopes=("local_agent:report",),
    )

    result = await service.record_channel_result(
        db,
        context=context,
        session_id=channel_session_id,
        message_id=message_id,
        result_status="completed",
        output="shared caller result",
        artifacts=[],
        metadata={},
    )

    chat_messages = [obj for obj in db.added if obj.__class__.__name__ == "ChatMessage"]
    assert result["status"] == "completed"
    assert len(chat_messages) == 1
    assert chat_messages[0].user_id == actor_user_id
    assert chat_messages[0].conversation_id == str(chat_session_id)
