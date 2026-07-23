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
        json={
            "label": "useful",
            "reason": "Good synthesis",
            "message_id": str(uuid.uuid4()),
            "decision_id": "decision/dec-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["calibration_result"]["entry_id"] == "t3-feedback-1"
    assert seen["db"] is fake_db
    assert seen["current_user"] is current_user
    assert seen["label"] == "useful"
    assert seen["reason"] == "Good synthesis"
    assert seen["decision_id"] == "decision/dec-1"


def test_record_session_feedback_hides_missing_or_out_of_scope_decision(monkeypatch) -> None:
    client, _fake_db, _current_user = _build_client()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    async def fake_get_session_and_agent(**_kwargs):
        return (
            SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            "session_owner",
        )

    async def missing_decision(**_kwargs):
        raise KeyError("decision-not-visible")

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "record_session_feedback", missing_decision)

    response = client.post(
        f"/agents/{agent_id}/sessions/{session_id}/feedback",
        json={"label": "useful", "decision_id": "decision-not-visible"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Decision not found in this session"}


def test_get_session_activation_feedback_api_calls_read_model(monkeypatch) -> None:
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

    def fake_read_sidecar(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {
            "schema": "hive.ccplus.activation_feedback_read_model.v1",
            "path": "memory/control/activation_feedback.jsonl",
            "entries": [{"session_id": str(session_id), "label": "useful"}],
            "total_lines": 1,
            "skipped_lines": 0,
            "matched_entries": 1,
            "truncated": False,
            "retention": {"max_entries": 5000},
        }

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "read_activation_feedback_sidecar", fake_read_sidecar)

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/feedback/activation-sidecar?limit=25")

    assert response.status_code == 200
    assert response.json()["entries"] == [{"session_id": str(session_id), "label": "useful"}]
    assert seen["args"] == ()
    assert seen["kwargs"]["agent_id"] == agent_id
    assert seen["kwargs"]["session_id"] == session_id
    assert seen["kwargs"]["limit"] == 25
    assert seen["kwargs"]["newest_first"] is True


def test_list_session_decisions_api_uses_authorized_session_scope(monkeypatch) -> None:
    client, fake_db, current_user = _build_client()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    decision_id = f"decision-{uuid.uuid4().hex}"
    seen = {}

    async def fake_get_session_and_agent(**kwargs):
        seen["authority"] = kwargs
        return (
            SimpleNamespace(id=session_id, agent_id=agent_id, tenant_id=tenant_id, source_channel="web"),
            SimpleNamespace(id=agent_id, tenant_id=tenant_id),
            "session_owner",
        )

    async def fake_list_decisions(**kwargs):
        seen["list"] = kwargs
        return [
            {
                "id": decision_id,
                "action": "send_feishu_message",
                "tool_name": "send_feishu_message",
                "outcome": "ask",
                "reason_codes": ["charter_confirm_first"],
                "created_at": "2026-07-24T01:00:00+00:00",
                "feedback_count": 0,
            }
        ]

    monkeypatch.setattr(chat_sessions_api, "_get_run_session_and_agent", fake_get_session_and_agent)
    monkeypatch.setattr(chat_sessions_api, "list_session_decision_traces", fake_list_decisions, raising=False)

    response = client.get(f"/agents/{agent_id}/sessions/{session_id}/decisions?limit=25")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": decision_id,
            "action": "send_feishu_message",
            "tool_name": "send_feishu_message",
            "outcome": "ask",
            "reason_codes": ["charter_confirm_first"],
            "created_at": "2026-07-24T01:00:00+00:00",
            "feedback_count": 0,
        }
    ]
    assert seen["authority"]["action"] == "read_decision_history"
    assert seen["list"] == {
        "db": fake_db,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "limit": 25,
    }


def test_list_session_decisions_api_has_typed_product_response_contract() -> None:
    client, _fake_db, _current_user = _build_client()

    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/agents/{agent_id}/sessions/{session_id}/decisions"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert response_schema == {
        "items": {"$ref": "#/components/schemas/SessionDecisionTraceOut"},
        "type": "array",
        "title": "Response List Decisions For Session Agents  Agent Id  Sessions  Session Id  Decisions Get",
    }
