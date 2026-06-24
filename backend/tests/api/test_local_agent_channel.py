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
        json={"content": "hello local codex", "metadata": {"purpose": "smoke"}},
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
        source_agent_id=None,
        title=None,
    ):
        captured["default_session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
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

    async def fake_list_channel_events(db_arg, *, session_id, owner_user_id=None, after_event_id=None, limit=100):
        captured["timeline"] = {
            "db": db_arg,
            "session_id": session_id,
            "owner_user_id": owner_user_id,
            "after_event_id": after_event_id,
            "limit": limit,
        }
        return [
            {
                "id": str(event_id),
                "session_id": str(session_id),
                "message_id": str(message_id),
                "direction": "hive_to_local",
                "type": "message",
                "payload": {"content": "persist this chat"},
                "created_at": None,
            }
        ]

    async def fake_get_channel_session(db_arg, *, session_id, owner_user_id):
        captured["timeline_session"] = {
            "db": db_arg,
            "session_id": session_id,
            "owner_user_id": owner_user_id,
        }
        return {
            "id": session_id,
            "chat_session_id": None,
            "status": "active",
            "source": "web",
            "created_at": None,
        }

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_or_create_default_channel_session",
        fake_get_or_create_default_channel_session,
    )
    monkeypatch.setattr(local_agent_channel_api.channel_service, "get_channel_session", fake_get_channel_session)
    monkeypatch.setattr(local_agent_channel_api.channel_service, "list_channel_events", fake_list_channel_events)
    client = _client(monkeypatch, db=db, current_user=current_user, context=context)

    session_response = client.post("/local-agents/sessions/default")
    timeline_response = client.get(f"/local-agents/sessions/{session_id}/timeline")

    assert session_response.status_code == 200
    assert session_response.json()["id"] == str(session_id)
    assert timeline_response.status_code == 200
    assert timeline_response.json()["session"]["id"] == str(session_id)
    assert timeline_response.json()["events"][0]["payload"]["content"] == "persist this chat"
    assert captured["default_session"]["tenant_id"] == tenant_id
    assert captured["default_session"]["owner_user_id"] == user_id
    assert captured["default_session"]["source_agent_id"] is None
    assert captured["timeline"]["owner_user_id"] == user_id


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
        source_agent_id=None,
        title=None,
    ):
        captured["default_session"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
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

    async def fake_list_agent_channel_sessions(db_arg, *, tenant_id, owner_user_id, source_agent_id, limit):
        captured["list"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
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

    async def fake_resolve_agent_channel_session(db_arg, *, tenant_id, owner_user_id, source_agent_id, session_id):
        captured["resolve"] = {
            "db": db_arg,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
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
    assert captured["resolve"]["session_id"] == chat_session_id


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

    async def fake_archive_agent_channel_session(db_arg, *, tenant_id, owner_user_id, source_agent_id, session_id):
        captured.update(
            {
                "db": db_arg,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
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
        "source_agent_id": agent_id,
        "session_id": channel_session_id,
    }


def test_agent_scoped_local_agent_channel_requires_owner_even_with_manage_access(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    db = _FakeDB()
    current_user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")
    context = _context(tenant_id=tenant_id, agent_id=agent_id, user_id=user_id)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        agent_type="local_agent",
        name="Teammate Mac",
        owner_user_id=uuid4(),
        creator_id=uuid4(),
    )

    async def should_not_create_session(*_args, **_kwargs):
        raise AssertionError("non-owner must not open local agent channel sessions")

    monkeypatch.setattr(
        local_agent_channel_api.channel_service,
        "get_or_create_default_channel_session",
        should_not_create_session,
    )
    client = _client(monkeypatch, db=db, current_user=current_user, context=context, agent=agent)

    response = client.post(f"/agents/{agent_id}/local-agent/sessions/default")

    assert response.status_code == 403
    assert response.json()["detail"] == "Only the owner can use this local agent channel"


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
        owner_user_id,
        session_id,
        ttl_seconds,
    ):
        captured.update(
            {
                "db": db_arg,
                "tenant_id": tenant_id,
                "owner_user_id": owner_user_id,
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
        "owner_user_id": user_id,
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
        return {"status": "online"}

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
        return {"status": "completed"}

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
        assert ws.receive_json() == {"type": "ready_ack", "status": "online"}
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
        assert ws.receive_json() == {"type": "result_ack", "message_id": str(pending_message_id), "status": "completed"}

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
