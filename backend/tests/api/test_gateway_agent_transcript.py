from __future__ import annotations

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
        return self._value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _FakeDB:
    def __init__(self, execute_results: list[object]) -> None:
        self._execute_results = list(execute_results)
        self.added: list[object] = []
        self.commit_count = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, _stmt):
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


@pytest.mark.asyncio
async def test_gateway_send_message_persists_openclaw_agent_request_to_chat_transcript(monkeypatch) -> None:
    from app.api import gateway as gateway_mod

    source_agent = SimpleNamespace(
        id=uuid4(),
        name="Source OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        openclaw_last_seen=None,
    )
    target_owner_id = uuid4()
    target_agent = SimpleNamespace(
        id=uuid4(),
        name="Target OpenClaw",
        agent_type="openclaw",
        creator_id=target_owner_id,
        openclaw_last_seen=None,
    )
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)
    db = _FakeDB([target_agent])

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
        db=db,
    )

    assert result["status"] == "accepted"
    outbound_gateway = next(obj for obj in db.added if isinstance(obj, GatewayMessage))
    outbound_chat = next(obj for obj in db.added if isinstance(obj, ChatMessage))
    assert outbound_gateway.conversation_id == "agent-pair-conv"
    assert outbound_chat.agent_id == chat_session.agent_id
    assert outbound_chat.conversation_id == "agent-pair-conv"
    assert outbound_chat.role == "user"
    assert outbound_chat.content == "Need the release summary."
    assert outbound_chat.user_id == target_owner_id


@pytest.mark.asyncio
async def test_gateway_report_result_persists_openclaw_agent_reply_to_chat_transcript(monkeypatch) -> None:
    from app.api import gateway as gateway_mod

    current_agent = SimpleNamespace(
        id=uuid4(),
        name="Target OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
        openclaw_last_seen=None,
    )
    sender_agent = SimpleNamespace(
        id=uuid4(),
        name="Source OpenClaw",
        agent_type="openclaw",
        creator_id=uuid4(),
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
    reply_db = _FakeDB([sender_agent])
    chat_session = SimpleNamespace(id=uuid4(), agent_id=uuid4(), last_message_at=None)

    async def fake_get_agent_by_key(_api_key, _db):
        return current_agent

    async def fake_find_or_create_agent_pair_session(*_args, **_kwargs):
        return chat_session

    monkeypatch.setattr(gateway_mod, "_get_agent_by_key", fake_get_agent_by_key)
    monkeypatch.setattr(gateway_mod, "find_or_create_agent_pair_session", fake_find_or_create_agent_pair_session)
    monkeypatch.setattr(gateway_mod, "session_conversation_id", lambda _session: "agent-pair-conv")
    monkeypatch.setattr(gateway_mod, "async_session", lambda: _AsyncSessionContext(reply_db))

    result = await gateway_mod.report_result(
        GatewayReportRequest(message_id=queued_message.id, result="Here is the release summary."),
        x_api_key="test-key",
        db=primary_db,
    )

    assert result == {"status": "ok"}
    reply_gateway = next(obj for obj in reply_db.added if isinstance(obj, GatewayMessage))
    reply_chat = next(obj for obj in reply_db.added if isinstance(obj, ChatMessage))
    assert reply_gateway.conversation_id == "agent-pair-conv"
    assert reply_chat.agent_id == chat_session.agent_id
    assert reply_chat.conversation_id == "agent-pair-conv"
    assert reply_chat.role == "assistant"
    assert reply_chat.content == "Here is the release summary."
