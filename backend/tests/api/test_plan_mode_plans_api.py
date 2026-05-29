"""Contract tests for the Plan Mode REST API (``app.api.plans``, §11).

These exercise the router against ``PlanModeService`` stubbed on the module, so
they assert the HTTP contract (status codes, request/response shapes, error
mapping) rather than the service internals (which are covered in
``tests/services/test_plan_mode_service.py``). All endpoints must enforce
``check_agent_access``; the confirm endpoint must map the service's typed
errors onto 403 (self-confirm / missing user) and 409 (version/hash/state).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.plans as plans_api
from app.core.security import get_current_user
from app.database import get_db
from app.services.plan_mode_service import PlanConflictError


class _FakeDB:
    pass


def _plan_namespace(*, agent_id, status="awaiting_confirmation", version=1, requester=None):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        agent_id=agent_id,
        session_id="sess-1",
        runtime_task_id=None,
        requested_by_user_id=requester or uuid4(),
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status=status,
        plan_version=version,
        plan_hash="sha256:abc",
        plan_markdown_path=f"/data/{agent_id}/plans/x.md",
        plan_json={"title": "Daily brief", "intent_type": "autonomous_wake"},
        handoff_payload=None,
        handoff_status="not_started" if status == "confirmed" else None,
        confirmed_by_user_id=None,
        confirmed_at=None,
        rejected_by_user_id=None,
        rejected_at=None,
        superseded_by_plan_id=None,
        expires_at=None,
        created_at=None,
        updated_at=None,
        metadata_json={},
    )


def _client(monkeypatch, *, service, user=None):
    app = FastAPI()
    app.include_router(plans_api.router)
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield _FakeDB()

    access = {"calls": 0}

    async def allow_access(_db, _user, agent_id):
        access["calls"] += 1
        return SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id), "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(plans_api, "check_agent_access", allow_access)
    monkeypatch.setattr(plans_api, "_service", service)
    return TestClient(app), user, access


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_plan_runs_create_then_generate_and_returns_201(monkeypatch):
    agent_id = uuid4()
    created = {}

    class _Service:
        async def create_plan_request(self, **kwargs):
            created.update(kwargs)
            return _plan_namespace(agent_id=agent_id, status="draft")

        async def generate_plan(self, *, plan_id, fill):
            created["fill"] = fill
            return _plan_namespace(agent_id=agent_id, status="awaiting_confirmation")

    client, user, access = _client(monkeypatch, service=_Service())

    resp = client.post(
        f"/agents/{agent_id}/plans",
        json={
            "original_request": "每天 9 点帮我整理新闻",
            "intent_type": "autonomous_wake",
            "session_id": "sess-1",
            "fill": {"objective": "Daily brief"},
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["intent_type"] == "autonomous_wake"
    # requested_by_user_id is derived from the authenticated user, not the body.
    assert created["requested_by_user_id"] == user.id
    assert created["intent_type"] == "autonomous_wake"
    assert created["fill"] == {"objective": "Daily brief"}
    assert access["calls"] == 1


def test_create_plan_rejects_unknown_intent_with_400(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def create_plan_request(self, **kwargs):
            raise ValueError("unknown intent_type 'bogus'")

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans",
        json={"original_request": "x", "intent_type": "bogus"},
    )
    assert resp.status_code == 400
    assert "intent_type" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# list + get
# ---------------------------------------------------------------------------


def test_list_plans_returns_rows(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def list_plans_for_agent(self, _agent_id, *, limit=50):
            assert _agent_id == agent_id
            return [_plan_namespace(agent_id=agent_id), _plan_namespace(agent_id=agent_id)]

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.get(f"/agents/{agent_id}/plans")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_plan_404_when_missing(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return None

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.get(f"/agents/{agent_id}/plans/{uuid4()}")
    assert resp.status_code == 404


def test_get_plan_404_when_belongs_to_other_agent(monkeypatch):
    agent_id = uuid4()
    other_agent = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=other_agent)

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.get(f"/agents/{agent_id}/plans/{uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_confirm_success_returns_confirmed_payload(monkeypatch):
    agent_id = uuid4()
    captured = {}

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def confirm_plan(self, **kwargs):
            captured.update(kwargs)
            return _plan_namespace(agent_id=agent_id, status="confirmed")

    client, user, _ = _client(monkeypatch, service=_Service())
    plan_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/plans/{plan_id}/confirm",
        json={"plan_version": 1, "plan_hash": "sha256:abc", "reason": "Looks good"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "confirmed"
    assert body["handoff_status"] == "not_started"
    # The confirming user is the authenticated user.
    assert captured["confirming_user_id"] == user.id
    assert captured["plan_version"] == 1
    assert captured["plan_hash"] == "sha256:abc"


def test_confirm_version_mismatch_maps_to_409(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def confirm_plan(self, **kwargs):
            raise PlanConflictError("version_mismatch", "submitted plan_version 1 does not match current version 2")

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans/{uuid4()}/confirm",
        json={"plan_version": 1, "plan_hash": "sha256:abc"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "version_mismatch"


def test_confirm_self_confirm_maps_to_403(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def confirm_plan(self, **kwargs):
            raise PermissionError("the plan requester cannot confirm their own plan")

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans/{uuid4()}/confirm",
        json={"plan_version": 1, "plan_hash": "sha256:abc"},
    )
    assert resp.status_code == 403


def test_confirm_404_when_plan_belongs_to_other_agent(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=uuid4())  # different agent

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans/{uuid4()}/confirm",
        json={"plan_version": 1, "plan_hash": "sha256:abc"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# revise / reject / handoff
# ---------------------------------------------------------------------------


def test_revise_returns_new_version(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def revise_plan(self, *, plan_id, fill):
            return _plan_namespace(agent_id=agent_id, status="awaiting_confirmation", version=2)

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans/{uuid4()}/revise",
        json={"fill": {"objective": "revised"}},
    )
    assert resp.status_code == 200
    assert resp.json()["plan_version"] == 2


def test_reject_returns_rejected(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def reject_plan(self, **kwargs):
            return _plan_namespace(agent_id=agent_id, status="rejected")

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(
        f"/agents/{agent_id}/plans/{uuid4()}/reject",
        json={"reason": "not needed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_handoff_returns_handoff_status(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id, status="confirmed")

        async def handoff_confirmed_plan(self, *, plan_id):
            plan = _plan_namespace(agent_id=agent_id, status="confirmed")
            plan.handoff_status = "completed"
            plan.handoff_payload = {"created_objective_id": str(uuid4()), "created_trigger_id": str(uuid4())}
            return plan

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(f"/agents/{agent_id}/plans/{uuid4()}/handoff", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["handoff_status"] == "completed"
    assert body["handoff_payload"]["created_objective_id"]


def test_handoff_not_confirmed_maps_to_409(monkeypatch):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id, status="confirmed")

        async def handoff_confirmed_plan(self, *, plan_id):
            raise PlanConflictError("not_confirmed", "only confirmed plans can be handed off")

    client, *_ = _client(monkeypatch, service=_Service())
    resp = client.post(f"/agents/{agent_id}/plans/{uuid4()}/handoff", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_confirmed"


@pytest.mark.parametrize("path", ["confirm", "revise", "reject", "handoff"])
def test_mutations_404_when_plan_missing(monkeypatch, path):
    agent_id = uuid4()

    class _Service:
        async def get_plan(self, _plan_id):
            return None

    client, *_ = _client(monkeypatch, service=_Service())
    body = {"plan_version": 1, "plan_hash": "sha256:abc"} if path == "confirm" else {}
    resp = client.post(f"/agents/{agent_id}/plans/{uuid4()}/{path}", json=body)
    assert resp.status_code == 404
