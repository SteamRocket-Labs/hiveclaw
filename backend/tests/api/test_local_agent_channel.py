from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.local_agent_channel as local_agent_channel_api
from app.core.security import get_current_user
from app.database import get_db
from app.services.local_bridge_service import BridgeAuthContext


def test_main_app_exposes_unprefixed_local_agent_browser_websocket_alias() -> None:
    from app.main import app

    websocket_paths = {getattr(route, "path", "") for route in app.routes}

    assert "/ws/local-agents/sessions/{session_id}" in websocket_paths


class _FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


def _context(
    *,
    connection_id=None,
    tenant_id=None,
    agent_id=None,
    user_id=None,
    scopes=("local_agent:connect", "local_agent:receive", "local_agent:send", "local_agent:report"),
):
    return BridgeAuthContext(
        connection_id=connection_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        agent_id=agent_id or uuid4(),
        user_id=user_id or uuid4(),
        scopes=tuple(scopes),
        client_kind="codex",
        device_name="Codex local runner",
    )


def _client(monkeypatch, *, db=None, current_user=None, context=None, agent=None) -> TestClient:
    app = FastAPI()
    app.include_router(local_agent_channel_api.router)
    db = db or _FakeDB()
    current_user = current_user or SimpleNamespace(id=uuid4(), tenant_id=uuid4(), role="member")
    context = context or _context(user_id=current_user.id, tenant_id=current_user.tenant_id)
    agent = agent or SimpleNamespace(id=context.agent_id, tenant_id=context.tenant_id)

    async def override_user():
        return current_user

    async def override_db():
        yield db

    async def override_context():
        return context

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        assert db_arg is db
        assert user_arg is current_user
        assert requested_agent_id == agent.id
        return agent, "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[local_agent_channel_api.get_bridge_auth_context] = override_context
    monkeypatch.setattr(local_agent_channel_api, "check_agent_access", fake_check_agent_access)
    return TestClient(app)


def test_create_ws_ticket_uses_bridge_context_and_short_lived_ticket(monkeypatch) -> None:
    context = _context()
    db = _FakeDB()
    captured = {}

    async def fake_create_ws_ticket(db_arg, *, context, ttl_seconds):
        captured["db"] = db_arg
        captured["context"] = context
        captured["ttl_seconds"] = ttl_seconds
        return {"ticket": "hbt_test_ticket", "expires_in": ttl_seconds, "single_use": True}

    monkeypatch.setattr(local_agent_channel_api.channel_service, "create_ws_ticket", fake_create_ws_ticket)
    client = _client(monkeypatch, db=db, context=context)

    response = client.post("/local-bridge/channel/ws-ticket", headers={"Authorization": "Bearer hb_secret"})

    assert response.status_code == 201
    assert response.json() == {"ticket": "hbt_test_ticket", "expires_in": 60, "single_use": True}
    assert captured == {"db": db, "context": context, "ttl_seconds": 60}


def test_local_agent_channel_ping_refreshes_presence(monkeypatch) -> None:
    context = _context()
    db = _FakeDB()
    captured = {}

    async def fake_resolve_ws_ticket(db_arg, *, ticket, last_seen_ip=None, user_agent=None):
        captured["resolve"] = {
            "db": db_arg,
            "ticket": ticket,
            "last_seen_ip": last_seen_ip,
            "user_agent": user_agent,
        }
        return context

    async def fake_mark_channel_seen(db_arg, *, context):
        captured["seen"] = {"db": db_arg, "context": context}

    async def fake_poll_pending_channel_messages(db_arg, *, context):
        captured["poll"] = {"db": db_arg, "context": context}
        return [{"id": "approved-message", "status": "delivered"}]

    monkeypatch.setattr(local_agent_channel_api.channel_service, "resolve_ws_ticket", fake_resolve_ws_ticket)
    monkeypatch.setattr(local_agent_channel_api.channel_service, "mark_channel_seen", fake_mark_channel_seen)
    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "poll_pending_channel_messages",
        fake_poll_pending_channel_messages,
    )
    client = _client(monkeypatch, db=db, context=context)

    with client.websocket_connect("/local-bridge/channel/ws?ticket=hbt_test") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "message",
            "message": {"id": "approved-message", "status": "delivered"},
        }
        assert websocket.receive_json() == {"type": "pong"}

    assert captured["resolve"]["ticket"] == "hbt_test"
    assert captured["seen"] == {"db": db, "context": context}
    assert captured["poll"] == {"db": db, "context": context}


def test_local_agent_waiting_approval_is_not_fanned_out_to_runner() -> None:
    assert local_agent_channel_api._runner_dispatch_allowed({"status": "pending"}) is True
    assert local_agent_channel_api._runner_dispatch_allowed({"status": "delivered"}) is True
    assert local_agent_channel_api._runner_dispatch_allowed({"status": "waiting_approval"}) is False
    assert local_agent_channel_api._runner_dispatch_allowed({"status": "rejected"}) is False


def test_web_user_creates_local_agent_channel_session_and_message(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=None, user_id=user_id)
    captured = {}

    async def fake_create_channel_session(db_arg, *, tenant_id, owner_user_id, source_agent_id, source, title):
        captured["session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "source_agent_id": source_agent_id,
            "source": source,
            "title": title,
        }
        return {
            "id": session_id,
            "chat_session_id": None,
            "status": "active",
            "source": source,
            "created_at": None,
        }

    async def fake_enqueue_channel_message(
        db_arg,
        *,
        session_id,
        owner_user_id,
        sender_user_id,
        sender_agent_id=None,
        content,
        attachments,
        metadata,
        idempotency_key=None,
    ):
        captured["message"] = {
            "db": db_arg,
            "session_id": session_id,
            "owner_user_id": owner_user_id,
            "sender_user_id": sender_user_id,
            "sender_agent_id": sender_agent_id,
            "content": content,
            "attachments": attachments,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
        }
        return {
            "id": message_id,
            "session_id": session_id,
            "status": "pending",
            "content": content,
            "attachments": attachments,
            "metadata": metadata,
            "created_at": None,
        }

    monkeypatch.setattr(local_agent_channel_api.channel_service, "create_channel_session", fake_create_channel_session)
    monkeypatch.setattr(
        local_agent_channel_api.channel_service, "enqueue_channel_message", fake_enqueue_channel_message
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context)

    session_response = client.post(
        "/local-agents/sessions",
        json={"source": "web", "title": "Local chat"},
    )
    message_response = client.post(
        f"/local-agents/sessions/{session_id}/messages",
        json={
            "content": "hello local codex",
            "metadata": {"purpose": "smoke"},
            "idempotency_key": "browser:message-1",
        },
    )

    assert session_response.status_code == 201
    assert session_response.json()["id"] == str(session_id)
    assert session_response.json()["chat_session_id"] is None
    assert message_response.status_code == 201
    assert message_response.json()["id"] == str(message_id)
    assert captured["session"]["tenant_id"] == tenant_id
    assert captured["session"]["owner_user_id"] == user_id
    assert captured["session"]["source_agent_id"] is None
    assert captured["message"]["owner_user_id"] == user_id
    assert captured["message"]["sender_user_id"] == user_id
    assert captured["message"]["sender_agent_id"] is None
    assert captured["message"]["content"] == "hello local codex"
    assert captured["message"]["idempotency_key"] == "browser:message-1"


def test_web_user_restores_default_local_agent_channel_session_and_timeline(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    event_id = uuid4()
    message_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=None, user_id=user_id)
    captured = {}

    async def fake_get_or_create_default_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id=None,
        title=None,
    ):
        captured["default_session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "title": title,
        }
        return {
            "id": session_id,
            "chat_session_id": None,
            "status": "active",
            "source": "web",
            "created_at": None,
        }

    async def fake_list_channel_events(db_arg, *, session_id, owner_user_id=None, after_sequence=0, limit=100):
        captured["timeline"] = {
            "db": db_arg,
            "session_id": session_id,
            "owner_user_id": owner_user_id,
            "after_sequence": after_sequence,
            "limit": limit,
        }
        return [
            {
                "id": str(event_id),
                "sequence": 7,
                "session_id": str(session_id),
                "message_id": str(message_id),
                "direction": "hive_to_local",
                "type": "message",
                "payload": {"content": "persist this chat"},
                "created_at": None,
            }
        ]

    async def fake_get_channel_session_for_actor(db_arg, *, session_id, actor_user_id):
        captured["timeline_session"] = {
            "db": db_arg,
            "session_id": session_id,
            "actor_user_id": actor_user_id,
        }
        return (
            {
                "id": session_id,
                "chat_session_id": None,
                "status": "active",
                "source": "web",
                "created_at": None,
            },
            actor_user_id,
        )

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_or_create_default_channel_session",
        fake_get_or_create_default_channel_session,
    )
    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_channel_session_for_actor",
        fake_get_channel_session_for_actor,
    )
    monkeypatch.setattr(local_agent_channel_api.channel_service, "list_channel_events", fake_list_channel_events)
    client = _client(monkeypatch, db=db, current_user=current_user, context=context)

    session_response = client.post("/local-agents/sessions/default")
    timeline_response = client.get(f"/local-agents/sessions/{session_id}/timeline?after_sequence=6")

    assert session_response.status_code == 200
    assert session_response.json()["id"] == str(session_id)
    assert timeline_response.status_code == 200
    assert timeline_response.json()["session"]["id"] == str(session_id)
    assert timeline_response.json()["events"][0]["payload"]["content"] == "persist this chat"
    assert timeline_response.json()["next_cursor"] == 7
    assert captured["default_session"]["tenant_id"] == tenant_id
    assert captured["default_session"]["owner_user_id"] == user_id
    assert captured["default_session"]["actor_user_id"] is None
    assert captured["default_session"]["source_agent_id"] is None
    assert captured["timeline"]["owner_user_id"] == user_id
    assert captured["timeline"]["after_sequence"] == 6


def test_web_user_restores_agent_scoped_default_local_agent_channel_session(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        name="Codex on Mac",
        owner_user_id=user_id,
        creator_id=user_id,
    )
    captured = {}

    async def fake_get_or_create_default_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id=None,
        title=None,
    ):
        captured["default_session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "title": title,
        }
        return {
            "id": session_id,
            "chat_session_id": chat_session_id,
            "status": "active",
            "source": "web",
            "created_at": None,
        }

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_or_create_default_channel_session",
        fake_get_or_create_default_channel_session,
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context, agent=agent)

    response = client.post(f"/agents/{agent_id}/local-agent/sessions/default")

    assert response.status_code == 200
    assert response.json()["id"] == str(session_id)
    assert response.json()["chat_session_id"] == str(chat_session_id)
    assert captured["default_session"] == {
        "db": db,
        "tenant_id": tenant_id,
        "owner_user_id": user_id,
        "actor_user_id": user_id,
        "source_agent_id": agent_id,
        "title": "Codex on Mac",
    }


def test_agent_scoped_local_agent_channel_lists_and_resolves_sessions(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    channel_session_id = uuid4()
    chat_session_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        name="Codex on Mac",
        owner_user_id=user_id,
        creator_id=user_id,
    )
    captured = {}

    async def fake_list_agent_channel_sessions(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id,
        limit,
    ):
        captured["list"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "limit": limit,
        }
        return [
            {
                "id": channel_session_id,
                "chat_session_id": chat_session_id,
                "agent_id": agent_id,
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

    async def fake_resolve_agent_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id,
        session_id,
    ):
        captured["resolve"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "session_id": session_id,
        }
        return {
            "id": channel_session_id,
            "chat_session_id": chat_session_id,
            "agent_id": agent_id,
            "title": "Codex on Mac",
            "source": "web",
            "source_channel": "local_agent",
            "session_kind": "local_agent_channel",
            "status": "active",
            "created_at": None,
            "updated_at": None,
            "last_message_at": None,
        }

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "list_agent_channel_sessions",
        fake_list_agent_channel_sessions,
    )
    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "resolve_agent_channel_session",
        fake_resolve_agent_channel_session,
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context, agent=agent)

    list_response = client.get(f"/agents/{agent_id}/local-agent/sessions")
    resolve_response = client.get(f"/agents/{agent_id}/local-agent/sessions/{chat_session_id}")

    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == str(channel_session_id)
    assert list_response.json()[0]["chat_session_id"] == str(chat_session_id)
    assert resolve_response.status_code == 200
    assert resolve_response.json()["id"] == str(channel_session_id)
    assert captured["list"]["source_agent_id"] == agent_id
    assert captured["list"]["actor_user_id"] == user_id
    assert captured["resolve"]["session_id"] == chat_session_id
    assert captured["resolve"]["actor_user_id"] == user_id


def test_agent_scoped_local_agent_channel_delete_archives_session(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    channel_session_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        name="Codex on Mac",
        owner_user_id=user_id,
        creator_id=user_id,
    )
    captured = {}

    async def fake_archive_agent_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id,
        session_id,
    ):
        captured.update(
            {
                "db": db_arg,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
                "actor_user_id": actor_user_id,
                "source_agent_id": source_agent_id,
                "session_id": session_id,
            }
        )
        return {"status": "archived"}

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "archive_agent_channel_session",
        fake_archive_agent_channel_session,
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context, agent=agent)

    response = client.delete(f"/agents/{agent_id}/local-agent/sessions/{channel_session_id}")

    assert response.status_code == 204
    assert captured == {
        "db": db,
        "tenant_id": tenant_id,
        "owner_user_id": user_id,
        "actor_user_id": user_id,
        "source_agent_id": agent_id,
        "session_id": channel_session_id,
    }


def test_shared_local_agent_channel_uses_host_owner_for_delivery_and_caller_for_session(monkeypatch, tmp_path) -> None:
    tenant_id = uuid4()
    caller_user_id = uuid4()
    host_owner_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    message_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=caller_user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=host_owner_id)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        name="Teammate Mac",
        owner_user_id=host_owner_id,
        creator_id=host_owner_id,
    )
    monkeypatch.setattr(local_agent_channel_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    actor_upload = (
        tmp_path
        / "local_agents"
        / str(tenant_id)
        / "users"
        / str(caller_user_id)
        / "workspace"
        / "uploads"
        / "proof.md"
    )
    actor_upload.parent.mkdir(parents=True)
    actor_upload.write_text("shared local agent proof", encoding="utf-8")
    captured = {}

    async def fake_get_or_create_default_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id=None,
        title=None,
    ):
        captured["default_session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "title": title,
        }
        return {
            "id": session_id,
            "chat_session_id": uuid4(),
            "agent_id": agent_id,
            "status": "active",
            "source": "web",
            "created_at": None,
        }

    async def fake_enqueue_channel_message(
        db_arg,
        *,
        session_id,
        owner_user_id,
        sender_user_id,
        sender_agent_id=None,
        content,
        attachments,
        metadata,
        idempotency_key=None,
    ):
        captured["message"] = {
            "db": db_arg,
            "session_id": session_id,
            "owner_user_id": owner_user_id,
            "sender_user_id": sender_user_id,
            "sender_agent_id": sender_agent_id,
            "content": content,
            "attachments": attachments,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
        }
        return {
            "id": message_id,
            "session_id": session_id,
            "status": "pending",
            "content": content,
            "attachments": attachments,
            "metadata": metadata,
            "created_at": None,
        }

    async def fake_resolve_agent_channel_session(
        db_arg,
        *,
        tenant_id,
        owner_user_id,
        actor_user_id=None,
        source_agent_id,
        session_id,
    ):
        captured["resolve"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "actor_user_id": actor_user_id,
            "source_agent_id": source_agent_id,
            "session_id": session_id,
        }
        return {
            "id": session_id,
            "chat_session_id": uuid4(),
            "agent_id": source_agent_id,
            "title": "Teammate Mac",
            "source": "web",
            "source_channel": "local_agent",
            "session_kind": "local_agent_channel",
            "status": "active",
            "created_at": None,
            "updated_at": None,
            "last_message_at": None,
        }

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_or_create_default_channel_session",
        fake_get_or_create_default_channel_session,
    )
    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "resolve_agent_channel_session",
        fake_resolve_agent_channel_session,
    )
    monkeypatch.setattr(
        local_agent_channel_api.channel_service, "enqueue_channel_message", fake_enqueue_channel_message
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context, agent=agent)

    session_response = client.post(f"/agents/{agent_id}/local-agent/sessions/default")
    message_response = client.post(
        f"/agents/{agent_id}/local-agent/sessions/{session_id}/messages",
        json={
            "content": "shared user task",
            "attachments": [{"path": "workspace/uploads/proof.md", "filename": "proof.md"}],
            "idempotency_key": "shared:message-1",
        },
    )

    assert session_response.status_code == 200
    assert message_response.status_code == 201
    assert captured["default_session"]["owner_user_id"] == host_owner_id
    assert captured["default_session"]["actor_user_id"] == caller_user_id
    assert captured["default_session"]["source_agent_id"] == agent_id
    assert captured["resolve"]["owner_user_id"] == host_owner_id
    assert captured["resolve"]["actor_user_id"] == caller_user_id
    assert captured["message"]["owner_user_id"] == host_owner_id
    assert captured["message"]["sender_user_id"] == caller_user_id
    assert captured["message"]["sender_agent_id"] == agent_id
    assert captured["message"]["idempotency_key"] == "shared:message-1"
    materialized_attachment = captured["message"]["attachments"][0]
    assert materialized_attachment["path"] == f"workspace/shared_uploads/{caller_user_id}/proof.md"
    assert materialized_attachment["workspace_path"] == f"workspace/shared_uploads/{caller_user_id}/proof.md"
    assert materialized_attachment["source_workspace_path"] == "workspace/uploads/proof.md"
    assert materialized_attachment["source_user_id"] == str(caller_user_id)
    assert materialized_attachment["materialized_for_user_id"] == str(host_owner_id)
    host_copy = (
        tmp_path
        / "local_agents"
        / str(tenant_id)
        / "users"
        / str(host_owner_id)
        / "workspace"
        / "shared_uploads"
        / str(caller_user_id)
        / "proof.md"
    )
    assert host_copy.read_text(encoding="utf-8") == "shared local agent proof"

    host_result = (
        tmp_path
        / "local_agents"
        / str(tenant_id)
        / "users"
        / str(host_owner_id)
        / "workspace"
        / "local-bridge"
        / "result.md"
    )
    host_result.parent.mkdir(parents=True)
    host_result.write_text("shared caller can download host result", encoding="utf-8")

    download_response = client.get(
        f"/agents/{agent_id}/local-agent/sessions/{session_id}/workspace/download",
        params={"path": "workspace/local-bridge/result.md"},
    )

    assert download_response.status_code == 200
    assert download_response.text == "shared caller can download host result"


def test_web_user_creates_browser_session_ws_ticket(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=None, user_id=user_id)
    captured = {}

    async def fake_create_browser_session_ws_ticket(
        db_arg,
        *,
        tenant_id,
        actor_user_id,
        session_id,
        ttl_seconds,
    ):
        captured.update(
            {
                "db": db_arg,
                "tenant_id": tenant_id,
                "actor_user_id": actor_user_id,
                "session_id": session_id,
                "ttl_seconds": ttl_seconds,
            }
        )
        return {"ticket": "hbwt_browser", "expires_in": ttl_seconds, "single_use": False}

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "create_browser_session_ws_ticket",
        fake_create_browser_session_ws_ticket,
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context)

    response = client.post(f"/local-agents/sessions/{session_id}/ws-ticket")

    assert response.status_code == 201
    assert response.json() == {"ticket": "hbwt_browser", "expires_in": 60, "single_use": False}
    assert captured == {
        "db": db,
        "tenant_id": tenant_id,
        "actor_user_id": user_id,
        "session_id": session_id,
        "ttl_seconds": 60,
    }


@pytest.mark.asyncio
async def test_browser_channel_manager_fans_out_only_to_matching_user_session() -> None:
    owner_user_id = uuid4()
    session_id = uuid4()
    other_session_id = uuid4()
    matching = SimpleNamespace(sent=[])
    other = SimpleNamespace(sent=[])

    async def send_json(payload):
        matching.sent.append(payload)

    async def send_other_json(payload):
        other.sent.append(payload)

    matching.send_json = send_json
    other.send_json = send_other_json
    manager = local_agent_channel_api.LocalAgentBrowserChannelManager()

    await manager.connect(owner_user_id=owner_user_id, session_id=session_id, websocket=matching)
    await manager.connect(owner_user_id=owner_user_id, session_id=other_session_id, websocket=other)
    await manager.send_to_session(owner_user_id, session_id, {"type": "event", "event": {"id": "event-1"}})

    assert matching.sent == [{"type": "event", "event": {"id": "event-1"}}]
    assert other.sent == []


def test_user_scoped_local_agent_workspace_lists_reads_and_downloads(monkeypatch, tmp_path) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    workspace_root = tmp_path / "local_agents" / str(tenant_id) / "users" / str(user_id)
    uploads_dir = workspace_root / "workspace" / "uploads"
    uploads_dir.mkdir(parents=True)
    report = uploads_dir / "codex-report.md"
    report.write_text("# Local Codex\n\nhello from local workspace\n", encoding="utf-8")

    monkeypatch.setattr(local_agent_channel_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client = _client(monkeypatch, current_user=current_user)

    root_response = client.get("/local-agents/workspace/files?path=workspace")
    uploads_response = client.get("/local-agents/workspace/files?path=workspace/uploads")
    content_response = client.get("/local-agents/workspace/content?path=workspace/uploads/codex-report.md")
    download_response = client.get("/local-agents/workspace/download?path=workspace/uploads/codex-report.md")

    assert root_response.status_code == 200
    assert root_response.json() == [
        {
            "name": "uploads",
            "path": "workspace/uploads",
            "is_dir": True,
            "size": 0,
            "modified_at": str((workspace_root / "workspace" / "uploads").stat().st_mtime),
        }
    ]
    assert uploads_response.status_code == 200
    assert uploads_response.json()[0]["path"] == "workspace/uploads/codex-report.md"
    assert uploads_response.json()[0]["is_dir"] is False
    assert content_response.status_code == 200
    assert content_response.json() == {
        "path": "workspace/uploads/codex-report.md",
        "content": "# Local Codex\n\nhello from local workspace\n",
    }
    assert download_response.status_code == 200
    assert download_response.content == b"# Local Codex\n\nhello from local workspace\n"


def test_user_scoped_local_agent_workspace_upload_saves_to_uploads(monkeypatch, tmp_path) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    monkeypatch.setattr(local_agent_channel_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client = _client(monkeypatch, current_user=current_user)

    response = client.post(
        "/local-agents/workspace/upload",
        files={"file": ("agent-proof.md", "# Proof\n\n我是agent\n".encode("utf-8"), "text/markdown")},
    )

    saved_file = (
        tmp_path / "local_agents" / str(tenant_id) / "users" / str(user_id) / "workspace" / "uploads" / "agent-proof.md"
    )
    assert response.status_code == 200
    assert response.json()["workspace_path"] == "workspace/uploads/agent-proof.md"
    assert response.json()["filename"] == "agent-proof.md"
    assert saved_file.read_text(encoding="utf-8") == "# Proof\n\n我是agent\n"


def test_user_scoped_local_agent_workspace_rejects_path_traversal(monkeypatch, tmp_path) -> None:
    current_user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), role="member")
    monkeypatch.setattr(local_agent_channel_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client = _client(monkeypatch, current_user=current_user)

    response = client.get("/local-agents/workspace/files?path=../secret")

    assert response.status_code == 403
    assert response.json()["detail"] == "Path traversal not allowed"


def test_bridge_token_downloads_user_scoped_channel_workspace_file(monkeypatch, tmp_path) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    context = _context(tenant_id=tenant_id, agent_id=None, user_id=user_id)
    workspace_root = tmp_path / "local_agents" / str(tenant_id) / "users" / str(user_id)
    uploads_dir = workspace_root / "workspace" / "uploads"
    uploads_dir.mkdir(parents=True)
    (uploads_dir / "cloud-brief.md").write_text("from Hive to local runner\n", encoding="utf-8")

    monkeypatch.setattr(local_agent_channel_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client = _client(monkeypatch, context=context)

    response = client.get("/local-bridge/channel/workspace/download?path=workspace/uploads/cloud-brief.md")

    assert response.status_code == 200
    assert response.content == b"from Hive to local runner\n"


def test_local_agent_channel_websocket_sends_pending_messages_and_accepts_result(monkeypatch) -> None:
    context = _context()
    pending_message_id = uuid4()
    session_id = uuid4()
    captured = {"ready": None, "acks": [], "results": []}

    async def fake_resolve_ws_ticket(db, *, ticket, user_agent=None, last_seen_ip=None):
        assert ticket == "hbt_test"
        return context

    async def fake_mark_channel_ready(db, *, context, runtime_kind, capabilities):
        captured["ready"] = {"runtime_kind": runtime_kind, "capabilities": capabilities}
        return {
            "status": "online",
            "snapshot_hash": "a" * 64,
            "effective_capabilities": ["event_stream", "execute", "result_report"],
            "expires_at": "2026-07-10T14:00:00+00:00",
        }

    async def fake_poll_pending_channel_messages(db, *, context, limit=10):
        return [
            {
                "id": pending_message_id,
                "session_id": session_id,
                "source": "web",
                "content": "do this locally",
                "attachments": [],
                "metadata": {"priority": "normal"},
            }
        ]

    async def fake_ack_channel_message(db, *, context, message_id):
        captured["acks"].append(message_id)
        return {"status": "delivered"}

    async def fake_record_channel_result(
        db,
        *,
        context,
        session_id,
        message_id,
        result_status,
        output,
        artifacts,
        metadata,
    ):
        captured["results"].append(
            {
                "session_id": session_id,
                "message_id": message_id,
                "status": result_status,
                "output": output,
                "artifacts": artifacts,
                "metadata": metadata,
            }
        )
        return {
            "status": "completed",
            "receipt": {
                "schema": "hive.execution_receipt.v1",
                "request_hash": "b" * 64,
                "capability_snapshot_hash": "a" * 64,
                "result_refs": ["workspace/local/done.md"],
                "status": "completed",
                "replay_key": "local:message-1",
                "trace_id": "local-agent:message-1",
                "span_id": "remote-action:message-1",
            },
        }

    monkeypatch.setattr(local_agent_channel_api.channel_service, "resolve_ws_ticket", fake_resolve_ws_ticket)
    monkeypatch.setattr(local_agent_channel_api.channel_service, "mark_channel_ready", fake_mark_channel_ready)
    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "poll_pending_channel_messages",
        fake_poll_pending_channel_messages,
    )
    monkeypatch.setattr(local_agent_channel_api.channel_service, "ack_channel_message", fake_ack_channel_message)
    monkeypatch.setattr(local_agent_channel_api.channel_service, "record_channel_result", fake_record_channel_result)

    app = FastAPI()
    app.include_router(local_agent_channel_api.router)

    async def override_db():
        yield _FakeDB()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    with client.websocket_connect("/local-bridge/channel/ws?ticket=hbt_test") as ws:
        assert ws.receive_json() == {
            "type": "hello",
            "connection_id": str(context.connection_id),
            "owner_user_id": str(context.user_id),
        }
        ws.send_json({"type": "ready", "runtime_kind": "codex", "capabilities": {"file_upload": True}})
        assert ws.receive_json() == {
            "type": "ready_ack",
            "status": "online",
            "snapshot_hash": "a" * 64,
            "effective_capabilities": ["event_stream", "execute", "result_report"],
            "expires_at": "2026-07-10T14:00:00+00:00",
        }
        message = ws.receive_json()
        assert message["type"] == "message"
        assert message["message"]["id"] == str(pending_message_id)
        ws.send_json({"type": "ack", "message_id": str(pending_message_id)})
        assert ws.receive_json() == {"type": "ack_ack", "message_id": str(pending_message_id)}
        ws.send_json(
            {
                "type": "result",
                "session_id": str(session_id),
                "message_id": str(pending_message_id),
                "status": "completed",
                "output": "done by local codex",
                "artifacts": [{"path": "workspace/local/done.md"}],
                "metadata": {"runtime": "codex"},
            }
        )
        result_ack = ws.receive_json()
        assert result_ack["type"] == "result_ack"
        assert result_ack["message_id"] == str(pending_message_id)
        assert result_ack["status"] == "completed"
        assert result_ack["receipt"]["schema"] == "hive.execution_receipt.v1"

    assert captured["ready"] == {"runtime_kind": "codex", "capabilities": {"file_upload": True}}
    assert captured["acks"] == [pending_message_id]
    assert captured["results"] == [
        {
            "session_id": session_id,
            "message_id": pending_message_id,
            "status": "completed",
            "output": "done by local codex",
            "artifacts": [{"path": "workspace/local/done.md"}],
            "metadata": {"runtime": "codex"},
        }
    ]
