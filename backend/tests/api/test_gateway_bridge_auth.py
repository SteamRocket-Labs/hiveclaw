from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.gateway as gateway_api
import app.api.local_bridge as local_bridge_api
from app.core.security import get_current_user
from app.database import get_db


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, scalar_values=None):
        self.committed = False
        self.added = []
        self.scalar_values = list(scalar_values or [])

    async def execute(self, _stmt):
        if self.scalar_values:
            return _ScalarOneResult(self.scalar_values.pop(0))
        return _ScalarListResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_poll_accepts_bridge_bearer_without_x_api_key(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        name="Web3研究员",
        status="idle",
        openclaw_last_seen=None,
    )
    captured = {}

    async def fake_get_gateway_actor(*, x_api_key, authorization, db):
        captured["x_api_key"] = x_api_key
        captured["authorization"] = authorization
        return gateway_api.GatewayActor(
            agent=agent,
            bridge_context=gateway_api.BridgeAuthContext(
                connection_id=connection_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                scopes=("gateway:poll",),
                client_kind="generic_mcp_stdio",
                device_name="Workstation",
            ),
        )

    monkeypatch.setattr(gateway_api, "_get_gateway_actor", fake_get_gateway_actor)
    db = _FakeDB()

    result = await gateway_api.poll_messages(
        x_api_key=None,
        authorization="Bearer hb_secret",
        db=db,
    )

    assert captured == {"x_api_key": None, "authorization": "Bearer hb_secret"}
    assert result.messages == []
    assert db.committed is True
    assert agent.openclaw_last_seen is not None
    assert agent.status == "running"


@pytest.mark.asyncio
async def test_poll_requires_some_gateway_auth() -> None:
    with pytest.raises(HTTPException) as exc:
        await gateway_api._get_gateway_actor(x_api_key=None, authorization=None, db=_FakeDB())

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_gateway_actor_keeps_legacy_x_api_key_path(monkeypatch) -> None:
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), name="legacy")

    async def fake_get_agent_by_key(api_key, db):
        assert api_key == "legacy-key"
        return agent

    monkeypatch.setattr(gateway_api, "_get_agent_by_key", fake_get_agent_by_key)

    actor = await gateway_api._get_gateway_actor(x_api_key="legacy-key", authorization=None, db=_FakeDB())

    assert actor.agent is agent
    assert actor.bridge_context is None


def test_enqueue_work_request_uses_checked_agent_identity(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    captured = {}

    async def override_user():
        return SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member")

    async def fake_check_agent_access(db, user, requested_agent_id):
        assert user.id == user_id
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_create_or_bind_chat_session(**kwargs):
        captured["session_kwargs"] = kwargs
        return SimpleNamespace(id=session_id)

    async def fake_enqueue_work_request(db, *, agent_id, tenant_id, sender_user_id, content, metadata, conversation_id):
        captured.update(
            {
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "sender_user_id": sender_user_id,
                "content": content,
                "metadata": metadata,
                "conversation_id": conversation_id,
            }
        )
        return {"status": "pending", "message_id": str(uuid4()), "conversation_id": conversation_id}

    app = FastAPI()
    app.include_router(local_bridge_api.router)
    app.dependency_overrides[get_current_user] = override_user
    db = _FakeDB()

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(local_bridge_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(local_bridge_api, "create_or_bind_chat_session", fake_create_or_bind_chat_session)
    monkeypatch.setattr(local_bridge_api.bridge_service, "enqueue_work_request", fake_enqueue_work_request)
    client = TestClient(app)

    resp = client.post(
        f"/agents/{agent_id}/local-bridge/work-requests",
        json={"content": "Research this Web3 topic", "metadata": {"priority": "normal"}},
    )

    assert resp.status_code == 201
    assert captured == {
        "session_kwargs": {
            "db": db,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "runtime_source": "local_bridge_work_request",
            "actor_type": "local_agent_bridge",
            "external_conversation_id": f"local_bridge:{agent_id}:{user_id}",
            "source_channel": "local_bridge",
            "title_seed": "Local Agent Bridge",
            "session_kind": "local_agent_bridge",
            "visibility_scope": "direct_user",
            "listed_surface": "chat",
        },
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "sender_user_id": user_id,
        "content": "Research this Web3 topic",
        "metadata": {"priority": "normal"},
        "conversation_id": str(session_id),
    }
    assert resp.json()["conversation_id"] == str(session_id)
    assert db.added[0].conversation_id == str(session_id)
    assert db.added[0].content == "Research this Web3 topic"


@pytest.mark.asyncio
async def test_bridge_report_persists_assistant_message_and_refreshes_local_bridge_session(monkeypatch) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    message_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        name="Web3研究员",
        status="idle",
        openclaw_last_seen=None,
    )
    gateway_message = SimpleNamespace(
        id=message_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        sender_user_id=user_id,
        sender_agent_id=None,
        conversation_id=str(session_id),
        status="delivered",
        result=None,
        attachments_json=[],
        metadata_json={"kind": "work_request"},
        completed_at=None,
    )
    session = SimpleNamespace(id=session_id, last_message_at=None)
    db = _FakeDB(scalar_values=[gateway_message, session])

    async def fake_get_gateway_actor(*, x_api_key, authorization, db):
        return gateway_api.GatewayActor(agent=agent, bridge_context=None)

    monkeypatch.setattr(gateway_api, "_get_gateway_actor", fake_get_gateway_actor)

    result = await gateway_api.report_result(
        body=gateway_api.GatewayReportRequest(
            message_id=message_id,
            result="done by codex",
            attachments=[{"path": "workspace/local-bridge/done.md", "filename": "done.md"}],
            metadata={"runtime": "command", "exit_code": 0},
        ),
        x_api_key=None,
        authorization="Bearer hb_secret",
        db=db,
    )

    assert result["status"] == "ok"
    assert gateway_message.status == "completed"
    assert gateway_message.result == "done by codex"
    assert gateway_message.attachments_json == [
        {"path": "workspace/local-bridge/done.md", "filename": "done.md", "direction": "result"}
    ]
    assert gateway_message.metadata_json["report"] == {"runtime": "command", "exit_code": 0}
    assert session.last_message_at is not None
    assert db.committed is True
    assert db.added[0].role == "assistant"
    assert db.added[0].conversation_id == str(session_id)
    assert db.added[0].content == "done by codex"
