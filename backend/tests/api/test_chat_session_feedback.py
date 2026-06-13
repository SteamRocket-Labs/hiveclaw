from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.chat_sessions as chat_sessions_api
from app.api.chat_sessions import router
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def commit(self) -> None:
        return None


def _build_client():
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_record_session_feedback_api_calls_persistent_service(monkeypatch) -> None:
    client, fake_db, current_user = _build_client()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    seen = {}

    async def fake_get_session_and_agent(**kwargs):
        assert kwargs["db"] is fake_db
        assert kwargs["agent_id"] == agent_id
        assert kwargs["session_id"] == session_id
        return (
            SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            "use",
        )

    async def fake_record_feedback(**kwargs):
        seen.update(kwargs)
        return {
            "id": str(uuid.uuid4()),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "label": "useful",
            "calibration_result": {"t3_status": "accepted", "entry_id": "t3-feedback-1"},
        }

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "record_session_feedback", fake_record_feedback)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/feedback",
        json={"label": "useful", "reason": "Good synthesis", "message_id": str(uuid.uuid4())},
    )

    assert response.status_code == 200
    assert response.json()["calibration_result"]["entry_id"] == "t3-feedback-1"
    assert seen["db"] is fake_db
    assert seen["current_user"] is current_user
    assert seen["label"] == "useful"
    assert seen["reason"] == "Good synthesis"
