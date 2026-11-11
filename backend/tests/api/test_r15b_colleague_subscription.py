"""The colleague's conversation is observable without becoming writable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import WebSocket, WebSocketDisconnect


@pytest.fixture
def colleague_subscription(monkeypatch):
    import app.api.websocket as api

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), name="Colleague")
    user = SimpleNamespace(id=uuid4(), tenant_id=agent.tenant_id, role="member", username="member", display_name="Member")
    session = SimpleNamespace(id=uuid4(), agent_id=agent.id, user_id=user.id,
        source_channel="agent", session_kind="delegation_run", runtime_source="delegation", audience_kind=None)
    db = AsyncMock()
    db.scalar.return_value = session
    db.__aenter__.return_value = db
    monkeypatch.setattr(api, "tenant_scoped_session", lambda _: db)
    monkeypatch.setattr(api, "decode_access_token", lambda _: {"sub": str(user.id)})
    monkeypatch.setattr("app.core.security.authenticate_request_user", AsyncMock(return_value=user))
    monkeypatch.setattr(api, "resolve_tenant_for_agent", AsyncMock(return_value=agent.tenant_id))
    monkeypatch.setattr(api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(api, "is_agent_expired", lambda _: False)
    manager, rebind = AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "manager", manager)
    monkeypatch.setattr(api, "apply_web_session_contract", rebind)
    monkeypatch.setattr(api, "get_active_web_chat_run", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.session_subscription.load_session_catchup_window", AsyncMock(
        return_value=SimpleNamespace(last_committed_sequence=0, last_committed_storage_sequence=0,
            cursor=SimpleNamespace(mode="identity"))))
    socket = AsyncMock(spec=WebSocket)
    socket.receive_json.return_value = {"type": "session.subscribe", "session_id": str(session.id),
        "after_sequence": 0, "schema_version": 2, "connection_attempt_id": "colleague-reader"}
    return api, agent, session, manager, rebind, socket


@pytest.mark.parametrize("owns_session", [True, False])
async def test_colleague_subscription_uses_read_authority_without_rebinding(colleague_subscription, owns_session):
    api, agent, session, manager, rebind, socket = colleague_subscription
    if not owns_session:
        session.user_id = uuid4()

    async def disconnect(frame):
        if frame["type"] == "session.ready":
            raise WebSocketDisconnect()

    socket.send_json.side_effect = disconnect
    await api.websocket_chat(socket, agent.id, token="fixture", session_id=str(session.id))
    frame = socket.send_json.call_args.args[0]
    if owns_session:
        assert frame["type"] == "session.ready"
        manager.begin_session_subscription.assert_awaited_once_with(str(agent.id), socket, str(session.id))
    else:
        assert frame["error"]["code"] == "session_forbidden"
        manager.begin_session_subscription.assert_not_awaited()
    rebind.assert_not_awaited()
    assert session.source_channel == "agent" and session.session_kind == "delegation_run"


async def test_read_only_colleague_socket_does_not_generate_idle_summary(monkeypatch, colleague_subscription):
    import asyncio

    api, agent, session, manager, _, socket = colleague_subscription
    subscribe = socket.receive_json.return_value
    socket.receive_json.side_effect = [subscribe, asyncio.TimeoutError(), WebSocketDisconnect()]
    monkeypatch.setattr(api, "_conversation_from_history_messages", lambda _: [
        {"role": "user", "content": "Please check the report."},
        {"role": "assistant", "content": "I am checking it."},
    ])
    monkeypatch.setattr(api, "_process_role", lambda: "worker")
    monkeypatch.setattr(api, "_has_active_web_chat_run", AsyncMock(return_value=False))
    monkeypatch.setattr(api, "_emit_ws_session_lifecycle_hook", AsyncMock())
    monkeypatch.setattr(api, "_get_ws_idle_dream_seconds", lambda: 180)
    db = api.tenant_scoped_session(agent.tenant_id)
    db.execute.return_value = Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[]))))
    generate, save = AsyncMock(return_value="Idle summary"), AsyncMock()
    monkeypatch.setattr("app.services.memory_service._generate_session_summary", generate)
    monkeypatch.setattr("app.services.memory_service._save_session_summary", save)
    await api.websocket_chat(socket, agent.id, token="fixture", session_id=str(session.id))
    manager.activate_session_subscription.assert_awaited_once()
    assert api._has_active_web_chat_run.await_count == 1
    generate.assert_not_awaited()
    save.assert_not_awaited()
