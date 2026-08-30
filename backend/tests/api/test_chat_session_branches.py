from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
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


@pytest.mark.parametrize("mode", ["edit", "insert_before", "insert_after", "reply", "side_question"])
def test_branch_endpoint_starts_run_through_canonical_session_v2_input(monkeypatch, mode):
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

    async def fake_submit(**kwargs):
        captured_run.update(kwargs)
        return {"run": {"run_id": "run-1", "status": "running"}}

    async def fail_legacy_start(**_kwargs):
        raise AssertionError("content-bearing branches must not bypass Session V2 input admission")

    monkeypatch.setattr(chat_sessions_api, "create_conversation_branch", fake_create_branch)
    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fake_submit)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fail_legacy_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/branches",
        json={
            "mode": mode,
            "anchor_event_id": str(anchor_event_id),
            "content": "replacement",
            "display_content": "replacement",
            "start_run": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["session"]["id"] == str(branch_session_id)
    assert payload["branch"]["mode"] == mode
    assert payload["run"] == {"run_id": "run-1", "status": "running"}
    assert captured_branch["source_session"] is source_session
    assert captured_branch["mode"] == mode
    assert captured_branch["anchor_event_id"] == anchor_event_id
    assert captured_run["session"] is branch_session
    assert captured_run["content"] == "replacement"
    assert captured_run["requested_kind"] == "start_turn"
    assert captured_run["source"] == f"conversation_branch_{mode}"
    assert captured_run["input_id"] == uuid5(
        branch_session_id,
        f"conversation-branch:{mode}:initial-input",
    )
    assert captured_run["idempotency_key"] == (f"conversation-branch:{branch_session_id}:{mode}:initial-input")
    assert captured_run["runtime_metadata"]["branch_mode"] == mode
    assert captured_run["runtime_metadata"]["permission_mode"] == "default"


def test_regenerate_branch_reuses_canonical_prefix_without_duplicate_human_input(monkeypatch):
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
        title="Regenerate",
        created_at=SimpleNamespace(isoformat=lambda: "2026-06-22T00:00:00+00:00"),
        last_message_at=None,
        peer_agent_id=None,
    )
    db = _FakeDB(source_session)
    captured_run = {}

    async def fake_create_branch(**kwargs):
        return SimpleNamespace(
            session=branch_session,
            branch={"mode": kwargs["mode"], "anchor_event_id": str(kwargs["anchor_event_id"])},
            run_request=SimpleNamespace(
                content="copied canonical prompt",
                display_content="copied canonical prompt",
                file_name="",
                append_user_message=False,
                extra_metadata={"branch_mode": "regenerate"},
            ),
        )

    async def fail_submit(**_kwargs):
        raise AssertionError("regenerate must not create a duplicate HumanInput checkpoint")

    async def fake_start(**kwargs):
        captured_run.update(kwargs)
        return {"run_id": "run-regenerate", "status": "running"}

    monkeypatch.setattr(chat_sessions_api, "create_conversation_branch", fake_create_branch)
    monkeypatch.setattr(chat_sessions_api, "submit_live_human_input", fail_submit)
    monkeypatch.setattr(chat_sessions_api, "start_web_chat_run", fake_start)
    client = _client(monkeypatch, db=db, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/branches",
        json={
            "mode": "regenerate",
            "anchor_event_id": str(anchor_event_id),
            "start_run": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["run"] == {"run_id": "run-regenerate", "status": "running"}
    assert captured_run["content"] == "copied canonical prompt"
    assert captured_run["append_user_message"] is False


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
