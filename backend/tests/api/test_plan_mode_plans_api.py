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


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _RecommendationDB:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        if not self.results:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self.results.pop(0))

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _plan_namespace(*, agent_id, status="awaiting_confirmation", version=1, requester=None):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        agent_id=agent_id,
        session_id=str(uuid4()),
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


def _client(monkeypatch, *, service, user=None, db=None):
    app = FastAPI()
    app.include_router(plans_api.router)
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield db or _FakeDB()

    access = {"calls": 0}

    async def allow_access(_db, _user, agent_id):
        access["calls"] += 1
        return SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id), "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(plans_api, "check_agent_access", allow_access)

    async def allow_plan_action(*_args, **_kwargs):
        return "session_owner"

    async def allow_session_action(*_args, **kwargs):
        return SimpleNamespace(
            agent=SimpleNamespace(id=kwargs["agent_id"], tenant_id=user.tenant_id),
            session=SimpleNamespace(id=kwargs["session_id"], user_id=user.id),
            authority_source="session_owner",
        )

    monkeypatch.setattr(plans_api, "_authorize_plan_action", allow_plan_action)
    monkeypatch.setattr(plans_api, "authorize_session_action", allow_session_action)
    monkeypatch.setattr(plans_api, "_service", service)
    return TestClient(app), user, access


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# cut ④: every authoring entry launches a main-loop Plan Mode run (the agent
# authors plan_json via exit_plan_mode) — the single plan path. The isolated RPC
# planner was removed; there is no flag.
# ---------------------------------------------------------------------------


def _stub_launcher(monkeypatch, *, launched):
    """Stub launch_system_plan_run to capture calls (no real agent run / DB)."""
    import app.services.plan_mode_system_run as system_run

    async def fake_launch(plan, *, seed_context=None):
        launched.append({"plan_id": plan.id, "seed_context": seed_context})
        return plan

    monkeypatch.setattr(system_run, "launch_system_plan_run", fake_launch)


def test_create_plan_launches_system_run_and_returns_201(monkeypatch):
    agent_id = uuid4()
    draft = _plan_namespace(agent_id=agent_id, status="draft")
    authored = _plan_namespace(agent_id=agent_id, status="awaiting_confirmation")
    authored.id = draft.id  # launcher fills the same draft id
    launched: list = []
    created = {}

    class _Service:
        async def create_plan_request(self, **kwargs):
            created.update(kwargs)
            return draft

        async def generate_plan(self, **_kwargs):
            raise AssertionError("RPC planner removed — create must launch a plan run")

        async def get_plan(self, plan_id):
            assert plan_id == draft.id
            return authored

    _stub_launcher(monkeypatch, launched=launched)
    client, user, access = _client(monkeypatch, service=_Service())

    resp = client.post(
        f"/agents/{agent_id}/plans",
        json={
            "original_request": "每天 9 点帮我整理新闻",
            "intent_type": "autonomous_wake",
            "session_id": str(uuid4()),
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
    assert access["calls"] == 1
    # The agent run authored it (stable draft id); fill is carried as seed context.
    assert len(launched) == 1
    assert launched[0]["plan_id"] == draft.id
    assert launched[0]["seed_context"] == {"objective": "Daily brief"}


def test_regenerate_launches_system_run(monkeypatch):
    agent_id = uuid4()
    existing = _plan_namespace(agent_id=agent_id, status="planning_failed")
    authored = _plan_namespace(agent_id=agent_id, status="awaiting_confirmation")
    authored.id = existing.id  # launcher fills the same plan id (stable for UI)
    launched: list = []
    get_calls = {"n": 0}

    class _Service:
        async def get_plan(self, plan_id):
            # First call: _load_plan_for_agent returns the failed plan. Second
            # call: the post-launch reload returns the authored result (same id).
            assert plan_id == existing.id
            get_calls["n"] += 1
            return existing if get_calls["n"] == 1 else authored

        async def generate_plan(self, **_kwargs):
            raise AssertionError("RPC planner removed — regenerate must launch a plan run")

    _stub_launcher(monkeypatch, launched=launched)
    client, *_ = _client(monkeypatch, service=_Service())

    resp = client.post(
        f"/agents/{agent_id}/plans/{existing.id}/regenerate",
        json={"fill": {"revision_request": "focus on RWA"}},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "awaiting_confirmation"  # authored result returned
    assert len(launched) == 1
    assert launched[0]["plan_id"] == existing.id
    assert launched[0]["seed_context"] == {"revision_request": "focus on RWA"}


def test_revise_supersedes_to_draft_then_launches(monkeypatch):
    agent_id = uuid4()
    old = _plan_namespace(agent_id=agent_id, status="awaiting_confirmation", version=1)
    draft = _plan_namespace(agent_id=agent_id, status="draft", version=2)
    authored = _plan_namespace(agent_id=agent_id, status="awaiting_confirmation", version=2)
    authored.id = draft.id
    launched: list = []
    calls = {"supersede": 0, "revise": 0}

    class _Service:
        async def get_plan(self, plan_id):
            if plan_id == old.id:
                return old
            return authored

        async def supersede_to_draft(self, *, plan_id):
            calls["supersede"] += 1
            assert plan_id == old.id
            return draft

        async def revise_plan(self, **_kwargs):
            calls["revise"] += 1
            raise AssertionError("legacy revise_plan removed from the REST path — must launch a plan run")

    _stub_launcher(monkeypatch, launched=launched)
    client, *_ = _client(monkeypatch, service=_Service())

    resp = client.post(
        f"/agents/{agent_id}/plans/{old.id}/revise",
        json={"fill": {"objective": "revised"}},
    )

    assert resp.status_code == 200
    assert resp.json()["plan_version"] == 2
    assert calls == {"supersede": 1, "revise": 0}
    assert launched[0]["plan_id"] == draft.id


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


def test_create_plan_recommendation_records_authenticated_user(monkeypatch):
    agent_id = uuid4()
    db = _RecommendationDB()

    class _Service:
        pass

    client, user, access = _client(monkeypatch, service=_Service(), db=db)
    resp = client.post(
        f"/agents/{agent_id}/plan-recommendations",
        json={
            "original_request": "每天 9 点提醒我",
            "session_id": "sess-1",
            "source": "web_chat",
        },
    )

    assert resp.status_code == 201
    assert db.committed is True
    recommendation = db.added[0]
    assert recommendation.agent_id == agent_id
    assert recommendation.recommended_to_user_id == user.id
    assert recommendation.session_id == "sess-1"
    assert recommendation.status == "recommended"
    assert access["calls"] == 1


def test_decline_plan_recommendation_requires_owner_user(monkeypatch):
    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    recommendation = SimpleNamespace(
        id=uuid4(),
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        session_id="sess-1",
        runtime_task_id=None,
        recommended_to_user_id=user.id,
        source="web_chat",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        title="Daily",
        original_request="每天 9 点提醒我",
        status="recommended",
        declined_by_user_id=None,
        declined_at=None,
        accepted_by_user_id=None,
        accepted_at=None,
        created_at=None,
        updated_at=None,
        metadata_json={},
    )
    db = _RecommendationDB([recommendation])

    class _Service:
        pass

    client, _user, _access = _client(monkeypatch, service=_Service(), user=user, db=db)
    resp = client.post(f"/agents/{agent_id}/plan-recommendations/{recommendation.id}/decline")

    assert resp.status_code == 200
    assert recommendation.status == "declined"
    assert recommendation.declined_by_user_id == user.id
    assert db.committed is True


# ---------------------------------------------------------------------------
# list + get
# ---------------------------------------------------------------------------


def test_list_plans_returns_rows(monkeypatch):
    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    class _Service:
        async def list_plans_for_agent(self, _agent_id, *, limit=50):
            assert _agent_id == agent_id
            return [
                _plan_namespace(agent_id=agent_id, requester=user.id),
                _plan_namespace(agent_id=agent_id, requester=user.id),
            ]

    client, *_ = _client(monkeypatch, service=_Service(), user=user)
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


def test_confirm_and_handoff_success_returns_handoff_payload(monkeypatch):
    agent_id = uuid4()
    captured = {}

    class _Service:
        async def get_plan(self, _plan_id):
            return _plan_namespace(agent_id=agent_id)

        async def confirm_and_handoff_plan(self, **kwargs):
            captured.update(kwargs)
            plan = _plan_namespace(agent_id=agent_id, status="confirmed")
            plan.handoff_status = "completed"
            plan.handoff_payload = {"runtime_task_id": "run-123", "execution": "current_session"}
            return plan

    client, user, _ = _client(monkeypatch, service=_Service())
    plan_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/plans/{plan_id}/confirm-and-handoff",
        json={"plan_version": 1, "plan_hash": "sha256:abc", "reason": "Looks good"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "confirmed"
    assert body["handoff_status"] == "completed"
    assert body["handoff_payload"]["runtime_task_id"] == "run-123"
    assert captured["confirming_user_id"] == user.id
    assert captured["plan_version"] == 1
    assert captured["plan_hash"] == "sha256:abc"


def test_confirm_version_mismatch_maps_to_stale_confirmation_409(monkeypatch):
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
    assert resp.json()["detail"]["error"] == "stale_confirmation"
    assert resp.json()["detail"]["reason_code"] == "version_mismatch"
    assert resp.json()["detail"]["current"]["plan_version"] == 1


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
