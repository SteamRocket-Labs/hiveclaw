from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.api.triggers as triggers_api
from app.core.security import get_current_user
from app.database import get_db
from app.services.plan_mode_gate import PlanGateDecision


def _allow_plan_gate(monkeypatch):
    """Neutralize the Plan Mode REST gate for tests not focused on it."""

    class _AllowGate:
        async def check(self, _db, **_kwargs):
            return PlanGateDecision(allowed=True, reason="confirmed_plan_handoff")

    monkeypatch.setattr(triggers_api, "get_plan_mode_gate", lambda: _AllowGate())


class _ListResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _QueuedDB:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        if not self.values:
            raise AssertionError("Unexpected execute() call")
        return _ListResult(self.values.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _client(monkeypatch, db):
    app = FastAPI()
    app.include_router(triggers_api.router)
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield db

    async def allow_access(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id), "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(triggers_api, "check_agent_access", allow_access)
    return TestClient(app), user


@pytest.mark.asyncio
async def test_list_triggers_returns_normalized_display_without_default_diagnostics(monkeypatch):
    agent_id = uuid4()
    trigger_id = uuid4()
    trigger = SimpleNamespace(
        id=trigger_id,
        name="daily_report",
        type="cron",
        config={"expr": "0 9 * * *", "trigger_class": "scheduled_job", "model_id": str(uuid4())},
        reason="Send daily report",
        is_enabled=True,
        fire_count=2,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    db = _QueuedDB([[trigger]])
    captured = {}

    async def allow_access(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id, tenant_id=uuid4()), "manage"

    def fake_view(trigger_arg, **kwargs):
        captured["include_diagnostics"] = kwargs["include_diagnostics"]
        assert trigger_arg is trigger
        return {
            "display_kind": "scheduled_job",
            "display_title": "Send daily report",
            "attention_state": "active",
            "next_action": None,
        }

    monkeypatch.setattr(triggers_api, "build_trigger_view", fake_view)
    monkeypatch.setattr(triggers_api, "check_agent_access", allow_access)

    result = await triggers_api.list_agent_triggers(agent_id=agent_id, current_user=SimpleNamespace(), db=db)

    assert result[0].id == str(trigger_id)
    assert result[0].display_kind == "scheduled_job"
    assert result[0].display_title == "Send daily report"
    assert result[0].diagnostics is None
    assert captured["include_diagnostics"] is False


@pytest.mark.asyncio
async def test_list_triggers_can_return_diagnostics_when_requested(monkeypatch):
    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_report",
        type="cron",
        config={"expr": "0 9 * * *", "trigger_class": "scheduled_job"},
        reason="Send daily report",
        is_enabled=True,
        fire_count=2,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    db = _QueuedDB([[trigger]])

    async def allow_access(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id, tenant_id=uuid4()), "manage"

    def fake_view(_trigger, **kwargs):
        assert kwargs["include_diagnostics"] is True
        return {
            "display_kind": "scheduled_job",
            "display_title": "Send daily report",
            "attention_state": "active",
            "diagnostics": {"trigger_class": "scheduled_job"},
        }

    monkeypatch.setattr(triggers_api, "build_trigger_view", fake_view)
    monkeypatch.setattr(triggers_api, "check_agent_access", allow_access)

    result = await triggers_api.list_agent_triggers(
        agent_id=agent_id,
        current_user=SimpleNamespace(),
        db=db,
        diagnostics=True,
    )

    assert result[0].diagnostics == {"trigger_class": "scheduled_job"}


def test_create_trigger_rejects_event_wait_without_lifecycle(monkeypatch):
    db = _QueuedDB()
    client, _user = _client(monkeypatch, db)
    agent_id = uuid4()

    response = client.post(
        f"/agents/{agent_id}/triggers",
        json={
            "name": "wait_for_reply",
            "type": "on_message",
            "config": {"reply_to_current_sender": True, "trigger_class": "event_wait"},
            "reason": "Wait for a reply",
        },
    )

    assert response.status_code == 400
    assert "max_fires" in response.text
    assert db.added == []


def test_create_scheduled_trigger_defaults_class_and_returns_display(monkeypatch):
    db = _QueuedDB()
    client, _user = _client(monkeypatch, db)
    agent_id = uuid4()
    # Plan Mode gate is exercised by test_plan_mode_rest_gate.py; here we assert
    # trigger-class defaulting + display, so allow the gate through.
    _allow_plan_gate(monkeypatch)

    response = client.post(
        f"/agents/{agent_id}/triggers",
        json={
            "name": "daily_report",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "Send daily report",
        },
    )

    assert response.status_code == 201
    assert db.committed is True
    assert db.added[0].config["trigger_class"] == "scheduled_job"
    payload = response.json()
    assert payload["display_kind"] == "scheduled_job"
    assert payload["display_title"] == "Send daily report"
    assert payload.get("diagnostics") is None
