from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.objectives as objectives_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    pass


def _client(monkeypatch, db=None):
    app = FastAPI()
    app.include_router(objectives_api.router)
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield db or _FakeDB()

    async def allow_access(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id), "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(objectives_api, "check_agent_access", allow_access)
    return TestClient(app), user


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ObjectiveUpdateDB:
    def __init__(self, objective):
        self.objective = objective
        self.committed = False

    async def execute(self, _stmt):
        return _ScalarResult(self.objective)

    async def commit(self):
        self.committed = True


def test_list_objectives_api_returns_objective_ledger_rows(monkeypatch):
    agent_id = uuid4()
    objective_id = uuid4()
    now = datetime.now(timezone.utc)

    async def fake_list(_db, _agent_id):
        assert _agent_id == agent_id
        return [
            SimpleNamespace(
                id=objective_id,
                objective_key="daily_report",
                description="Send report",
                status="active",
                priority=2,
                source="conversation",
                success_criteria="Sent with evidence",
                blocked_reason=None,
                metadata_json={"autonomy_class": "explicit_user_request"},
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        ]

    monkeypatch.setattr(objectives_api.objective_service, "list_objectives_for_agent", fake_list)
    client, _user = _client(monkeypatch)

    response = client.get(f"/agents/{agent_id}/objectives")

    assert response.status_code == 200
    data = response.json()
    assert data[0]["id"] == str(objective_id)
    assert data[0]["status"] == "active"
    assert data[0]["metadata"]["autonomy_class"] == "explicit_user_request"


def test_propose_objective_api_uses_intake_gate(monkeypatch):
    agent_id = uuid4()
    captured = {}

    async def fake_upsert(db, agent, candidate):
        captured["agent"] = agent
        captured["candidate"] = candidate
        return SimpleNamespace(
            id=uuid4(),
            objective_key=candidate.objective_key,
            description=candidate.description,
            status="proposed",
            priority=0,
            source=candidate.source,
            success_criteria=None,
            blocked_reason=None,
            metadata_json={"autonomy_class": candidate.autonomy_class},
            created_at=None,
            updated_at=None,
            completed_at=None,
        )

    monkeypatch.setattr(objectives_api.objective_intake, "upsert_objective_candidate", fake_upsert)
    client, _user = _client(monkeypatch)

    response = client.post(
        f"/agents/{agent_id}/objectives/proposals",
        json={
            "description": "之后关注一下这个方向",
            "autonomy_class": "implicit_inference",
            "evidence": {"message": "之后关注一下这个方向"},
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "proposed"
    assert captured["candidate"].suggested_status == "proposed"


def test_update_objective_api_rejects_completion_without_evidence(monkeypatch):
    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        description="Send report",
        status="active",
        priority=1,
        source="conversation",
        success_criteria="Sent with evidence",
        blocked_reason=None,
        metadata_json={},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    fake_db = _ObjectiveUpdateDB(objective)
    client, _user = _client(monkeypatch, db=fake_db)

    response = client.patch(
        f"/agents/{agent_id}/objectives/{objective.id}",
        json={"status": "completed"},
    )

    assert response.status_code == 400
    assert objective.status == "active"
    assert fake_db.committed is False


def test_approve_objective_api_activates_pending_goal(monkeypatch):
    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        description="Send report",
        status="proposed",
        priority=1,
        source="conversation",
        success_criteria="Sent with evidence",
        blocked_reason="waiting for approval",
        metadata_json={"requires_approval": True},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    fake_db = _ObjectiveUpdateDB(objective)
    client, _user = _client(monkeypatch, db=fake_db)

    response = client.post(
        f"/agents/{agent_id}/objectives/{objective.id}/approve",
        json={"reason": "approved by user"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert objective.metadata_json["requires_approval"] is False
    assert fake_db.committed is True


def test_reject_objective_api_records_rejection(monkeypatch):
    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="risky_message",
        description="Send risky message",
        status="proposed",
        priority=1,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={"requires_approval": True},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    fake_db = _ObjectiveUpdateDB(objective)
    client, _user = _client(monkeypatch, db=fake_db)

    response = client.post(
        f"/agents/{agent_id}/objectives/{objective.id}/reject",
        json={"reason": "too risky"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert objective.metadata_json["rejection"]["reason"] == "too risky"
    assert fake_db.committed is True
