from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.chat_sessions as chat_sessions_api
from app.core.security import get_current_user
from app.database import get_db


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, session):
        self.session = session
        self.commits = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.session)

    async def commit(self):
        self.commits += 1


def _client(monkeypatch, *, db, user, agent, access_level="use"):
    app = FastAPI()
    app.include_router(chat_sessions_api.router)

    async def override_user():
        return user

    async def override_db():
        yield db

    async def allow_access(_db, _user, target_agent_id):
        assert target_agent_id == agent.id
        return agent, access_level

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", allow_access)
    return TestClient(app)


def test_branch_endpoint_starts_run_with_branch_runtime_contract(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    branch_session_id = uuid4()
    anchor_event_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    source_session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    branch_session = SimpleNamespace(
        id=branch_session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=session_id,
        root_session_id=session_id,
        runtime_task_id=None,
        title="Branch",
        created_at=SimpleNamespace(isoformat=lambda: "2026-06-22T00:00:00+00:00"),
        last_message_at=None,
        peer_agent_id=None,
    )
    db = _FakeDB(source_session)
    captured_branch = {}
    captured_run = {}

    async def fake_create_branch(**kwargs):
        captured_branch.update(kwargs)
        return SimpleNamespace(
            session=branch_session,
            branch={"mode": kwargs["mode"], "anchor_event_id": str(kwargs["anchor_event_id"])},
            run_request=SimpleNamespace(
                content="replacement",
                display_content="replacement",
                file_name="",
                append_user_message=True,
                extra_metadata={"branch_mode": kwargs["mode"]},
            ),
        )

    async def fake_start(**kwargs):
        captured_run.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "create_conversation_branch", fake_create_branch)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/branches",
        json={
            "mode": "edit",
            "anchor_event_id": str(anchor_event_id),
            "content": "replacement",
            "display_content": "replacement",
            "start_run": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["session"]["id"] == str(branch_session_id)
    assert payload["branch"]["mode"] == "edit"
    assert payload["run"] == {"run_id": "run-1", "status": "running"}
    assert captured_branch["source_session"] is source_session
    assert captured_branch["mode"] == "edit"
    assert captured_branch["anchor_event_id"] == anchor_event_id
    assert captured_run["session"] is branch_session
    assert captured_run["content"] == "replacement"
    assert captured_run["append_user_message"] is True


def test_branch_endpoint_accepts_branch_mode_without_starting_run(monkeypatch):
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    branch_session_id = uuid4()
    anchor_event_id = uuid4()
    agent = SimpleNamespace(id=agent_id, creator_id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, role="member")
    source_session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    branch_session = SimpleNamespace(
        id=branch_session_id,
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=session_id,
        root_session_id=session_id,
        runtime_task_id=None,
        title="Original session",
        created_at=SimpleNamespace(isoformat=lambda: "2026-06-22T00:00:00+00:00"),
        last_message_at=None,
        peer_agent_id=None,
    )
    db = _FakeDB(source_session)
    captured_branch = {}

    async def fake_create_branch(**kwargs):
        captured_branch.update(kwargs)
        return SimpleNamespace(
            session=branch_session,
            branch={"mode": kwargs["mode"], "anchor_event_id": str(kwargs["anchor_event_id"])},
            run_request=None,
        )

    monkeypatch.setattr(chat_sessions_api, "create_conversation_branch", fake_create_branch)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/branches",
        json={
            "mode": "branch",
            "anchor_event_id": str(anchor_event_id),
            "start_run": False,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["session"]["id"] == str(branch_session_id)
    assert payload["session"]["title"] == "Original session"
    assert payload["branch"]["mode"] == "branch"
    assert payload["run"] is None
    assert db.commits == 1
    assert captured_branch["mode"] == "branch"
    assert captured_branch["anchor_event_id"] == anchor_event_id
