from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.audit import ChatMessage
from app.models.gateway_message import GatewayMessage
from app.schemas.schemas import GatewayReportRequest, GatewaySendMessageRequest


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        if isinstance(self._value, list):
            if not self._value:
                return None
            if len(self._value) > 1:
                raise AssertionError("scalar_one_or_none() received multiple rows")
            return self._value[0]
        return self._value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._value, list):
            return list(self._value)
        if self._value is None:
            return []
        return [self._value]

    def first(self):
        values = self.all()
        return values[0] if values else None


class _FakeDB:
    def __init__(self, execute_results: list[object]) -> None:
        self._execute_results = list(execute_results)
        self.added: list[object] = []
        self.commit_count = 0
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[object] = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def flush(self) -> None:
        return None

    async def execute(self, _stmt):
        self.statements.append(_stmt)
        sql = getattr(_stmt, "text", None) or str(_stmt)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _FakeScalarResult(None)
        if not self._execute_results:
            raise AssertionError("Unexpected execute call")
        return _FakeScalarResult(self._execute_results.pop(0))


class _AsyncSessionContext:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeDB:
        return self._db

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _AsyncSessionFactory:
    def __init__(self, dbs: list[_FakeDB]) -> None:
        self._dbs = list(dbs)

    def __call__(self, *args, **kwargs):
        if not self._dbs:
            raise AssertionError("Unexpected session call")
        return _AsyncSessionContext(self._dbs.pop(0))


@pytest.mark.asyncio
async def test_gateway_api_key_lookup_pins_agent_tenant() -> None:
    from app import database
    from app.api import gateway as gateway_mod

    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        api_key_hash="ignored-by-fake-db",
        agent_type="openclaw",
    )
    db = _FakeDB([agent])

    result = await gateway_mod._get_agent_by_key("test-key", db)

    assert result is agent
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{tenant_id}'" in str(stmt) for stmt in db.statements)


@pytest.mark.asyncio
async def test_gateway_send_message_persists_openclaw_agent_request_to_chat_transcript(monkeypatch) -> None:
    from app.api import gateway as gateway_mod

    tenant_id = uuid4()
    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        tenant_id=tenant_id,
        openclaw_last_seen=None,
    )
    target_owner_id = uuid4()
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target OpenClaw",
        agent_type="openclaw",
        creator_id=target_owner_id,
        tenant_id=tenant_id,
        openclaw_last_seen=None,
    )
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)
    source_participant = SimpleNamespace(id=uuid4(), display_name=source_agent.name)
    db = _FakeDB([target_agent, source_participant.id])

    async def fake_get_agent_by_key(_api_key, _db):
        return source_agent

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        return chat_session

    monkeypatch.setattr(gateway_mod, "_get_agent_by_key", fake_get_agent_by_key)
    monkeypatch.setattr(gateway_mod, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)
    monkeypatch.setattr(gateway_mod, "session_conversation_id", lambda _session: "agent-pair-conv")

    result = await gateway_mod.send_message(
        GatewaySendMessageRequest(target=target_agent.name, content="Need the release summary."),
        x_api_key="test-key",
        authorization=None,
        db=db,
    )

    assert result["status"] == "accepted"
    outbound_gateway = next(obj for obj in db.added if isinstance(obj, GatewayMessage))
    outbound_chat = next(obj for obj in db.added if isinstance(obj, ChatMessage))
    assert outbound_gateway.conversation_id == "agent-pair-conv"
    assert outbound_gateway.tenant_id == tenant_id
    assert outbound_chat.agent_id == chat_session.agent_id
    assert outbound_chat.tenant_id == tenant_id
    assert outbound_chat.conversation_id == "agent-pair-conv"
    assert outbound_chat.role == "user"
    assert outbound_chat.content == "Need the release summary."
    assert outbound_chat.user_id == target_owner_id
    assert outbound_chat.participant_id == source_participant.id


@pytest.mark.asyncio
async def test_gateway_report_result_persists_openclaw_agent_reply_to_chat_transcript(monkeypatch) -> None:
    from app.api import gateway as gateway_mod

    current_agent = SimpleNamespace(
        id=uuid4(),
        name="Target OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        openclaw_last_seen=None,
        tenant_id=uuid4(),
    )
    sender_agent = SimpleNamespace(
        id=uuid4(),
        name="Source OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        tenant_id=current_agent.tenant_id,
    )
    queued_message = SimpleNamespace(
        id=uuid4(),
        agent_id=current_agent.id,
        sender_agent_id=sender_agent.id,
        sender_user_id=None,
        conversation_id="agent-pair-conv",
        status="delivered",
        result=None,
        completed_at=None,
    )
    primary_db = _FakeDB([queued_message])
    current_participant = SimpleNamespace(id=uuid4(), display_name=current_agent.name)
    reply_db = _FakeDB([sender_agent, current_participant.id])
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)

    async def fake_get_agent_by_key(_api_key, _db):
        return current_agent

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        return chat_session

    monkeypatch.setattr(gateway_mod, "_get_agent_by_key", fake_get_agent_by_key)
    monkeypatch.setattr(gateway_mod, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)
    monkeypatch.setattr(gateway_mod, "session_conversation_id", lambda _session: "agent-pair-conv")
    monkeypatch.setattr(gateway_mod, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(reply_db))

    result = await gateway_mod.report_result(
        GatewayReportRequest(message_id=queued_message.id, result="Here is the release summary."),
        x_api_key="test-key",
        authorization=None,
        db=primary_db,
    )

    assert result == {"status": "ok"}
    reply_gateway = next(obj for obj in reply_db.added if isinstance(obj, GatewayMessage))
    reply_chat = next(obj for obj in reply_db.added if isinstance(obj, ChatMessage))
    assert reply_gateway.conversation_id == "agent-pair-conv"
    assert reply_gateway.tenant_id == current_agent.tenant_id
    assert reply_chat.agent_id == chat_session.agent_id
    assert reply_chat.tenant_id == current_agent.tenant_id
    assert reply_chat.conversation_id == "agent-pair-conv"
    assert reply_chat.role == "assistant"
    assert reply_chat.content == "Here is the release summary."
    assert reply_chat.participant_id == current_participant.id


@pytest.mark.asyncio
async def test_gateway_poll_history_prefers_participant_display_names(monkeypatch) -> None:
    from app.api import gateway as gateway_mod

    current_agent = SimpleNamespace(
        id=uuid4(),
        name="Target OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        openclaw_last_seen=None,
        status="idle",
    )
    queued_message = SimpleNamespace(
        id=uuid4(),
        agent_id=current_agent.id,
        sender_agent_id=uuid4(),
        sender_user_id=None,
        content="Need the release summary.",
        conversation_id="agent-pair-conv",
        status="pending",
        delivered_at=None,
        created_at=datetime.now(timezone.utc),
    )
    source_participant = SimpleNamespace(id=uuid4(), display_name="Source OpenClaw")
    target_participant = SimpleNamespace(id=uuid4(), display_name="Target OpenClaw")
    history_messages = [
        SimpleNamespace(
            role="user",
            content="Need the release summary.",
            participant_id=source_participant.id,
            user_id=current_agent.creator_id,
            created_at=datetime.now(timezone.utc),
        ),
        SimpleNamespace(
            role="assistant",
            content="Here is the release summary.",
            participant_id=target_participant.id,
            user_id=current_agent.creator_id,
            created_at=datetime.now(timezone.utc),
        ),
    ]
    db = _FakeDB(
        [
            [queued_message],
            "Source OpenClaw",
            history_messages,
            source_participant.display_name,
            target_participant.display_name,
            [],
            [],
        ]
    )

    async def fake_get_agent_by_key(_api_key, _db):
        return current_agent

    monkeypatch.setattr(gateway_mod, "_get_agent_by_key", fake_get_agent_by_key)

    response = await gateway_mod.poll_messages(x_api_key="test-key", authorization=None, db=db)

    assert queued_message.status == "delivered"
    assert response.messages[0].history[0].sender_name == source_participant.display_name
    assert response.messages[0].history[1].sender_name == target_participant.display_name


@pytest.mark.asyncio
async def test_send_to_agent_background_persists_participant_ids_for_native_agent_transcript(
    monkeypatch,
    tmp_path,
) -> None:
    from app.api import gateway as gateway_mod
    from app.api import websocket as websocket_mod
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent

    source_agent_id = uuid4()
    target_agent_id = uuid4()
    owner_user_id = uuid4()
    source_participant = SimpleNamespace(id=uuid4(), display_name="Source OpenClaw")
    target_participant = SimpleNamespace(id=uuid4(), display_name="Native Target")
    model = SimpleNamespace(id=uuid4())
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)
    first_db = _FakeDB([model, source_participant.id, target_participant.id, []])
    second_db = _FakeDB([])

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        return chat_session

    async def fake_call_llm(**kwargs):
        assert kwargs["auto_close_session"] is False
        await kwargs["on_tool_call"]({"tool": "lookup", "status": "running", "args": {"q": "release"}})
        await kwargs["on_tool_call"](
            {"tool": "lookup", "status": "done", "result": "full gateway result END_OF_GATEWAY_TOOL"}
        )
        return "Native reply"

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(gateway_mod, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)
    monkeypatch.setattr(gateway_mod, "session_conversation_id", lambda session: str(session.id))
    monkeypatch.setattr(gateway_mod, "tenant_scoped_session", _AsyncSessionFactory([first_db, second_db]))
    monkeypatch.setattr(websocket_mod, "call_llm", fake_call_llm)

    await gateway_mod._send_to_agent_background(
        source_agent_id=str(source_agent_id),
        source_agent_name="Source OpenClaw",
        target_agent_id=str(target_agent_id),
        target_agent_name="Native Target",
        target_primary_model_id=str(uuid4()),
        target_role_description="Summarize releases",
        target_creator_id=str(owner_user_id),
        target_tenant_id=str(uuid4()),
        content="Need the release summary.",
    )

    request_chat = next(obj for obj in first_db.added if isinstance(obj, ChatMessage))
    reply_chat = next(obj for obj in first_db.added if isinstance(obj, ChatMessage) and obj.role == "assistant")
    transcript_events = [
        obj for db in (first_db, second_db) for obj in db.added if isinstance(obj, ChatTranscriptEvent)
    ]
    assert request_chat.participant_id == source_participant.id
    assert reply_chat.participant_id == target_participant.id
    assert [event.event_type for event in transcript_events] == [
        "user_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]
    events = replay_t0_session_events(agent_id=target_agent_id, session_id=chat_session.id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [
        ("user_message", "user"),
        ("tool_call", "tool"),
        ("tool_result", "tool"),
        ("assistant_message", "assistant"),
        ("segment_boundary", "system"),
    ]
    assert events[0].content == "[Message from agent: Source OpenClaw]\nNeed the release summary."
    assert "END_OF_GATEWAY_TOOL" in events[2].content
    assert events[3].content == "Native reply"
