"""REST confirmation-gate wiring.

These tests pin the behaviour of the *REST gate*: every endpoint that creates or
enables an autonomous artifact (an enabled trigger, an activated wake objective,
an async delegation, or an auto-executing task) must consult
:class:`PlanModeGate` *after* the existing permission / tenant / validation
checks, and return **409 ``requires_confirmation``** when no confirmed plan
authorises the action. The same request carrying a matching confirmed plan must
pass through.

Read-only GETs, disables, deletes, and low-risk updates must **not** be gated —
they keep their existing contract verbatim (covered by the pre-existing API
tests, asserted here only where a branch is shared).

The fakes follow the project's established hand-rolled async-session pattern
(see ``test_triggers_p6_api.py`` / ``test_objectives_api.py``). The gate itself is
stubbed per-router via ``get_plan_mode_gate`` so these tests exercise *wiring*,
not the gate's internal decision logic (separately covered by
``test_plan_mode_gate.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.database import get_db
from app.services.plan_mode_gate import PlanGateDecision


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _ListResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _QueuedDB:
    """Returns queued results in order; records adds/commits/flushes."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.added = []
        self.committed = False
        self.rollbacks = 0
        self.flushed = False
        self.deleted = []

    async def execute(self, _stmt):
        if not self.results:
            raise AssertionError("Unexpected execute() call")
        return self.results.pop(0)

    def add(self, obj):
        # Mirror a real session assigning a server-default PK on flush.
        if getattr(obj, "id", None) is None:
            try:
                obj.id = uuid4()
            except (AttributeError, TypeError):
                pass
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rollbacks += 1

    async def flush(self):
        self.flushed = True

    async def refresh(self, _obj):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)


class _StubGate:
    """Records the gate call and returns a canned decision."""

    def __init__(self, decision: PlanGateDecision):
        self._decision = decision
        self.calls: list[dict] = []

    async def check(self, db, **kwargs):
        self.calls.append(kwargs)
        return self._decision


async def _fake_enrich_task_out(task, _db):
    """Return a valid ``TaskOut`` for the created task.

    ``TaskOut.model_validate`` reads server-default columns (status, assignee,
    timestamps) that an un-flushed in-memory ``Task`` lacks; populating them is
    orthogonal to the gate decision under test, so the task-create "pass-through"
    tests stub the enrichment with this helper.
    """
    from datetime import datetime, timezone

    from app.schemas.schemas import TaskOut

    now = datetime.now(timezone.utc)
    return TaskOut(
        id=getattr(task, "id", None) or uuid4(),
        agent_id=task.agent_id,
        title=task.title,
        description=task.description,
        type=task.type,
        status="pending",
        priority=task.priority or "medium",
        assignee="agent",
        created_by=task.created_by,
        request_id=task.request_id,
        request_hash=task.request_hash,
        active_runtime_task_id=getattr(task, "active_runtime_task_id", None),
        execution_attempt=getattr(task, "execution_attempt", 0) or 0,
        created_at=now,
        updated_at=now,
    )


def _requires_confirmation_decision() -> PlanGateDecision:
    return PlanGateDecision(
        allowed=False,
        reason="no_confirmed_plan",
        needs_plan_payload={
            "ok": False,
            "status": "requires_confirmation",
            "requires_confirmation": True,
            "summary": "Confirm a plan before starting this autonomous action.",
            "next_action": "STOP and create/show a plan, then WAIT for confirmation.",
        },
    )


def _allow_decision() -> PlanGateDecision:
    return PlanGateDecision(allowed=True, reason="confirmed_plan_handoff")


def _make_client(router_module, *, db, is_creator: bool = True, user=None):
    app = FastAPI()
    app.include_router(router_module.router)
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")

    async def override_user():
        return user

    async def override_db():
        yield db

    async def allow_access(_db, _user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=_user.tenant_id, creator_id=_user.id), "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return app, user, allow_access


async def _require_agent(allow_access, db, user, agent_id):
    agent, access_level = await allow_access(db, user, agent_id)
    if access_level != "manage":
        raise AssertionError("test fixture expected manage access")
    return agent


def _declined_recommendation(
    *,
    agent_id,
    user,
    status="declined",
    session_id="session-1",
    action_kind="create_enabled_trigger",
):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=getattr(user, "tenant_id", None),
        agent_id=agent_id,
        session_id=session_id,
        recommended_to_user_id=user.id,
        source="web_chat",
        intent_type="autonomous_wake",
        action_kind=action_kind,
        tool_name="set_trigger",
        title="Daily",
        original_request="每天 9 点",
        status=status,
        declined_by_user_id=user.id if status == "declined" else None,
        declined_at=None,
        accepted_by_user_id=None,
        accepted_at=None,
        created_at=None,
        updated_at=None,
        metadata_json={},
    )


# ===========================================================================
# triggers REST — POST create + PATCH enable
# ===========================================================================


def _trigger_view_stub(monkeypatch, mod):
    monkeypatch.setattr(
        mod,
        "build_trigger_view",
        lambda *_a, **_k: {"display_kind": "scheduled_job", "display_title": "x"},
    )


def test_create_trigger_without_plan_returns_409(monkeypatch):
    import app.api.triggers as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    _trigger_view_stub(monkeypatch, mod)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/triggers",
        json={"name": "daily", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "Send daily report"},
    )

    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["status"] == "requires_confirmation"
    # No trigger persisted on the blocked branch.
    assert db.added == []
    assert db.committed is False
    # Gate was asked about the right action with the agent id.
    assert gate.calls[0]["action_kind"] == "create_enabled_trigger"
    assert str(gate.calls[0]["agent_id"]) == str(agent_id)


def test_create_trigger_with_confirmed_plan_passes(monkeypatch):
    import app.api.triggers as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    _trigger_view_stub(monkeypatch, mod)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{agent_id}/triggers",
        json={
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "Send daily report",
            "confirmed_plan_id": plan_id,
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
    )

    assert resp.status_code == 201
    assert db.committed is True
    assert db.added and db.added[0].is_enabled is True
    # Confirmed-plan fields flowed through to the gate.
    assert gate.calls[0]["confirmed_plan_id"] == plan_id
    assert gate.calls[0]["plan_version"] == 1
    assert gate.calls[0]["plan_hash"] == "sha256:abc"


def test_create_trigger_after_user_declines_plan_recommendation_passes(monkeypatch):
    import app.api.triggers as mod

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    recommendation = _declined_recommendation(agent_id=agent_id, user=user)
    db = _QueuedDB([_ScalarResult(recommendation)])
    app, _user, allow_access = _make_client(mod, db=db, user=user)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    _trigger_view_stub(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.post(
        f"/agents/{agent_id}/triggers",
        json={
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "Send daily report",
            "plan_mode_decision": "declined",
            "plan_recommendation_id": str(recommendation.id),
        },
    )

    assert resp.status_code == 201
    assert db.committed is True
    assert db.added[0].config["metadata"]["plan_exempt_reason"] == "user_declined_plan_mode"
    assert db.added[0].config["metadata"]["plan_recommendation_id"] == str(recommendation.id)


def test_create_trigger_rejects_bare_declined_without_recommendation(monkeypatch):
    import app.api.triggers as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    _trigger_view_stub(monkeypatch, mod)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/triggers",
        json={
            "name": "daily",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "reason": "Send daily report",
            "plan_mode_decision": "declined",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "plan_recommendation_required"
    assert db.added == []
    assert db.committed is False


def test_update_trigger_enable_without_plan_returns_409(monkeypatch):
    import app.api.triggers as mod

    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="r",
        is_enabled=False,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    db = _QueuedDB([_ScalarResult(trigger)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{trigger.agent_id}/triggers/{trigger.id}",
        json={"is_enabled": True},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    # The enable must not have been applied or committed.
    assert trigger.is_enabled is False
    assert db.committed is False
    assert gate.calls[0]["action_kind"] == "enable_autonomous_wake"


def test_update_trigger_disable_is_not_gated(monkeypatch):
    """Disabling a trigger is low-risk: it must NOT consult the gate."""
    import app.api.triggers as mod

    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="r",
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    db = _QueuedDB([_ScalarResult(trigger)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)

    def _boom():
        raise AssertionError("gate must not be consulted for a disable")

    monkeypatch.setattr(mod, "get_plan_mode_gate", _boom)

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{trigger.agent_id}/triggers/{trigger.id}",
        json={"is_enabled": False},
    )

    assert resp.status_code == 200
    assert trigger.is_enabled is False
    assert db.committed is True


def test_update_trigger_enable_after_user_declines_plan_recommendation_passes(monkeypatch):
    import app.api.triggers as mod

    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="r",
        is_enabled=False,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    recommendation = _declined_recommendation(
        agent_id=trigger.agent_id,
        user=user,
        action_kind="enable_autonomous_wake",
    )
    db = _QueuedDB([_ScalarResult(trigger), _ScalarResult(recommendation)])
    app, _user, allow_access = _make_client(mod, db=db, user=user)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{trigger.agent_id}/triggers/{trigger.id}",
        json={
            "is_enabled": True,
            "plan_mode_decision": "declined",
            "plan_recommendation_id": str(recommendation.id),
        },
    )

    assert resp.status_code == 200
    assert trigger.is_enabled is True
    assert trigger.config["metadata"]["plan_exempt_reason"] == "user_declined_plan_mode"
    assert trigger.config["metadata"]["plan_recommendation_id"] == str(recommendation.id)
    assert db.committed is True


def test_update_trigger_reason_only_is_not_gated(monkeypatch):
    """A config/reason-only edit (no enable) keeps its existing contract."""
    import app.api.triggers as mod

    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="old",
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    db = _QueuedDB([_ScalarResult(trigger)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{trigger.agent_id}/triggers/{trigger.id}",
        json={"reason": "new"},
    )

    assert resp.status_code == 200
    assert trigger.reason == "new"
    assert db.committed is True


# ===========================================================================
# schedules REST — POST create + PATCH enable + POST /run
# ===========================================================================


def test_create_schedule_without_plan_returns_409(monkeypatch):
    import app.api.schedules as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(
        mod, "require_agent_manage_access", lambda db, user, agent_id: _require_agent(allow_access, db, user, agent_id)
    )
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/schedules/",
        json={"name": "nightly", "instruction": "do it", "cron_expr": "0 9 * * *"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    assert db.added == []
    assert db.flushed is False
    assert gate.calls[0]["action_kind"] == "create_enabled_trigger"


def test_create_schedule_disabled_draft_is_not_gated(monkeypatch):
    """A schedule created with is_enabled=False is a draft, not an autonomous wake."""
    import app.api.schedules as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(
        mod, "require_agent_manage_access", lambda db, user, agent_id: _require_agent(allow_access, db, user, agent_id)
    )
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/schedules/",
        json={"name": "nightly", "instruction": "do it", "cron_expr": "0 9 * * *", "is_enabled": False},
    )

    assert resp.status_code == 201
    assert db.added and db.added[0].is_enabled is False
    assert db.flushed is True


def test_create_schedule_with_confirmed_plan_passes(monkeypatch):
    import app.api.schedules as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(
        mod, "require_agent_manage_access", lambda db, user, agent_id: _require_agent(allow_access, db, user, agent_id)
    )
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{agent_id}/schedules/",
        json={
            "name": "nightly",
            "instruction": "do it",
            "cron_expr": "0 9 * * *",
            "confirmed_plan_id": plan_id,
            "confirmed_plan_version": 2,
            "confirmed_plan_hash": "sha256:def",
        },
    )

    assert resp.status_code == 201
    assert db.added and db.added[0].is_enabled is True
    assert gate.calls[0]["confirmed_plan_id"] == plan_id
    assert gate.calls[0]["plan_version"] == 2


def test_create_schedule_after_user_declines_plan_recommendation_passes(monkeypatch):
    import app.api.schedules as mod

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    recommendation = _declined_recommendation(agent_id=agent_id, user=user)
    db = _QueuedDB([_ScalarResult(recommendation)])
    app, _user, allow_access = _make_client(mod, db=db, user=user)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(
        mod, "require_agent_manage_access", lambda db, user, agent_id: _require_agent(allow_access, db, user, agent_id)
    )
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.post(
        f"/agents/{agent_id}/schedules/",
        json={
            "name": "nightly",
            "instruction": "do it",
            "cron_expr": "0 9 * * *",
            "plan_mode_decision": "declined",
            "plan_recommendation_id": str(recommendation.id),
        },
    )

    assert resp.status_code == 201
    assert db.added and db.added[0].is_enabled is True
    assert db.added[0].config["metadata"]["plan_exempt_reason"] == "user_declined_plan_mode"
    assert db.added[0].config["metadata"]["plan_recommendation_id"] == str(recommendation.id)
    assert db.flushed is True


def test_update_schedule_enable_after_user_declines_plan_recommendation_passes(monkeypatch):
    import app.api.schedules as mod

    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    schedule = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="nightly",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="do it",
        reply_context=None,
        last_fired_at=None,
        created_at=None,
        fire_count=0,
        is_enabled=False,
    )
    recommendation = _declined_recommendation(
        agent_id=schedule.agent_id,
        user=user,
        action_kind="enable_autonomous_wake",
    )
    db = _QueuedDB([_ScalarResult(schedule), _ScalarResult(recommendation)])
    app, _user, allow_access = _make_client(mod, db=db, user=user)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(
        mod, "require_agent_manage_access", lambda db, user, agent_id: _require_agent(allow_access, db, user, agent_id)
    )
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{schedule.agent_id}/schedules/{schedule.id}",
        json={
            "is_enabled": True,
            "plan_mode_decision": "declined",
            "plan_recommendation_id": str(recommendation.id),
        },
    )

    assert resp.status_code == 200
    assert schedule.is_enabled is True
    assert schedule.config["metadata"]["plan_exempt_reason"] == "user_declined_plan_mode"
    assert schedule.config["metadata"]["plan_recommendation_id"] == str(recommendation.id)
    assert db.flushed is True


def test_schedule_run_without_plan_returns_409(monkeypatch):
    import app.api.schedules as mod

    schedule = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="nightly",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="do it",
        reply_context=None,
        last_fired_at=None,
        created_at=None,
        fire_count=0,
        is_enabled=True,
    )
    db = _QueuedDB([_ScalarResult(schedule)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.post(f"/agents/{schedule.agent_id}/schedules/{schedule.id}/run")

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    # No manual one-shot trigger queued.
    assert db.added == []
    assert gate.calls[0]["action_kind"] == "create_enabled_trigger"


def test_schedule_run_with_confirmed_plan_passes(monkeypatch):
    import app.api.schedules as mod

    schedule = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="nightly",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="do it",
        reply_context=None,
        last_fired_at=None,
        created_at=None,
        fire_count=0,
        is_enabled=True,
    )
    db = _QueuedDB([_ScalarResult(schedule)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{schedule.agent_id}/schedules/{schedule.id}/run",
        json={"confirmed_plan_id": plan_id, "confirmed_plan_version": 1, "confirmed_plan_hash": "sha256:abc"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert db.added and db.added[0].type == "once"
    assert gate.calls[0]["confirmed_plan_id"] == plan_id


def test_schedule_run_after_user_declines_plan_recommendation_passes(monkeypatch):
    import app.api.schedules as mod

    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), username="member")
    schedule = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="nightly",
        type="cron",
        config={"expr": "0 9 * * *"},
        reason="do it",
        reply_context=None,
        last_fired_at=None,
        created_at=None,
        fire_count=0,
        is_enabled=True,
    )
    recommendation = _declined_recommendation(agent_id=schedule.agent_id, user=user)
    db = _QueuedDB([_ScalarResult(schedule), _ScalarResult(recommendation)])
    app, _user, allow_access = _make_client(mod, db=db, user=user)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.post(
        f"/agents/{schedule.agent_id}/schedules/{schedule.id}/run",
        json={
            "plan_mode_decision": "declined",
            "plan_recommendation_id": str(recommendation.id),
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert db.added and db.added[0].type == "once"
    assert db.added[0].config["metadata"]["plan_exempt_reason"] == "user_declined_plan_mode"
    assert db.added[0].config["metadata"]["plan_recommendation_id"] == str(recommendation.id)


# ===========================================================================
# advanced REST — /collaborate/delegate
# ===========================================================================


def test_delegate_without_plan_returns_409(monkeypatch):
    import app.api.advanced as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    delegate_called = {"n": 0}

    async def fake_delegate(*_a, **_k):  # pragma: no cover - must not run
        delegate_called["n"] += 1
        return {}

    monkeypatch.setattr(mod.collaboration_service, "delegate_task", fake_delegate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/collaborate/delegate",
        json={"to_agent_id": str(uuid4()), "task_title": "Do research", "task_description": "deep dive"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    assert delegate_called["n"] == 0
    assert gate.calls[0]["action_kind"] == "start_delegation"


def test_delegate_with_confirmed_plan_passes(monkeypatch):
    import app.api.advanced as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    captured = {}

    async def fake_delegate(_db, _from, _to, _title, _desc, **kwargs):
        captured.update(kwargs)
        return {"task_id": "t1", "status": "delegated"}

    monkeypatch.setattr(mod.collaboration_service, "delegate_task", fake_delegate)

    client = TestClient(app)
    agent_id = uuid4()
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{agent_id}/collaborate/delegate",
        json={
            "to_agent_id": str(uuid4()),
            "task_title": "Do research",
            "confirmed_plan_id": plan_id,
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "delegated"
    assert gate.calls[0]["confirmed_plan_id"] == plan_id
    assert captured["confirmed_plan_id"] == plan_id
    assert captured["confirmed_plan_version"] == 1
    assert captured["confirmed_plan_hash"] == "sha256:abc"


def test_send_inter_agent_message_is_not_gated(monkeypatch):
    """A2A messaging is not autonomous task delegation -> no gate."""
    import app.api.advanced as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    async def fake_msg(_db, _from, _to, _msg, _type):
        return {"ok": True}

    monkeypatch.setattr(mod.collaboration_service, "send_message_between_agents", fake_msg)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/collaborate/message",
        json={"to_agent_id": str(uuid4()), "message": "hi"},
    )

    assert resp.status_code == 200


# ===========================================================================
# tasks REST — auto-executing todo create + manual trigger
# ===========================================================================


def test_create_todo_task_without_plan_returns_409(monkeypatch):
    import app.api.tasks as mod

    db = _QueuedDB([_ScalarResult(None)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/tasks/",
        json={
            "request_id": "create-no-plan-1",
            "title": "auto research",
            "description": "go",
            "type": "todo",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    # No task persisted, no background execution fired.
    assert db.added == []
    assert db.committed is False
    assert gate.calls[0]["action_kind"] == "start_long_task"


def test_create_todo_task_with_confirmed_plan_passes(monkeypatch):
    import app.api.tasks as mod

    db = _QueuedDB([_ScalarResult(None)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "_enrich_task_out", _fake_enrich_task_out)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    staged = {}

    async def fake_stage(**kwargs):
        staged.update(kwargs)
        runtime_task = SimpleNamespace(id=uuid4())
        kwargs["task"].active_runtime_task_id = runtime_task.id
        kwargs["task"].execution_attempt = 1
        return runtime_task

    async def fake_notify(**_kwargs):
        return None

    monkeypatch.setattr(mod, "stage_business_task_runtime", fake_stage)
    monkeypatch.setattr(mod, "notify_runtime_task_worker", fake_notify)

    client = TestClient(app)
    agent_id = uuid4()
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{agent_id}/tasks/",
        json={
            "request_id": "create-confirmed-1",
            "title": "auto research",
            "description": "go",
            "type": "todo",
            "confirmed_plan_id": plan_id,
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
    )

    assert resp.status_code == 201
    assert db.committed is True
    assert gate.calls[0]["confirmed_plan_id"] == plan_id
    assert staged["task"].agent_id == agent_id
    assert staged["task"].description == "go"
    assert staged["request_id"] == "create-confirmed-1"


def test_trigger_task_without_plan_returns_409(monkeypatch):
    import app.api.tasks as mod

    task = SimpleNamespace(id=uuid4(), agent_id=uuid4())
    # check_agent_access returns a non-expired agent; then the task lookup.
    db = _QueuedDB([_ScalarResult(task), _ScalarResult(None)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _a: False)
    gate = _StubGate(_requires_confirmation_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    executed = {"n": 0}

    async def fake_execute(_task_id, _agent_id):  # pragma: no cover - must not run
        executed["n"] += 1

    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute)

    client = TestClient(app)
    resp = client.post(
        f"/agents/{task.agent_id}/tasks/{task.id}/trigger",
        json={"request_id": "trigger-no-plan-1"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "requires_confirmation"
    assert executed["n"] == 0
    assert gate.calls[0]["action_kind"] == "start_long_task"


def test_trigger_task_with_confirmed_plan_enqueues_runtime_task(monkeypatch):
    import app.api.tasks as mod

    task = SimpleNamespace(id=uuid4(), agent_id=uuid4(), description="go")
    db = _QueuedDB([_ScalarResult(task), _ScalarResult(None)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _a: False)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    staged = {}

    async def fake_stage(**kwargs):
        staged.update(kwargs)
        return SimpleNamespace(id=uuid4())

    async def fake_execute(_task_id, _agent_id):  # pragma: no cover - must not run
        raise AssertionError("trigger_task must enqueue RuntimeTask instead of spawning execute_task")

    async def fake_notify(**_kwargs):
        return None

    monkeypatch.setattr(mod, "stage_business_task_runtime", fake_stage)
    monkeypatch.setattr(mod, "notify_runtime_task_worker", fake_notify)
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute)

    client = TestClient(app)
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{task.agent_id}/tasks/{task.id}/trigger",
        json={
            "request_id": "trigger-confirmed-1",
            "confirmed_plan_id": plan_id,
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:abc",
        },
    )

    assert resp.status_code == 200
    assert staged["task"] is task
    assert staged["request_id"] == "trigger-confirmed-1"
    assert gate.calls[0]["confirmed_plan_id"] == plan_id


def test_create_task_recovers_concurrent_same_request(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    import app.api.tasks as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "_enrich_task_out", _fake_enrich_task_out)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: _StubGate(_allow_decision()))
    load_calls = 0

    async def fake_load(*_args, **_kwargs):
        nonlocal load_calls
        load_calls += 1
        return None if load_calls == 1 else db.added[0]

    async def duplicate_stage(**_kwargs):
        raise IntegrityError("INSERT tasks", {}, RuntimeError("duplicate request"))

    monkeypatch.setattr(mod, "_load_matching_task_request", fake_load)
    monkeypatch.setattr(mod, "stage_business_task_runtime", duplicate_stage)

    response = TestClient(app).post(
        f"/agents/{uuid4()}/tasks/",
        json={
            "request_id": "concurrent-create-1",
            "title": "one logical task",
            "confirmed_plan_id": str(uuid4()),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:plan",
        },
    )

    assert response.status_code == 201
    assert db.rollbacks == 1
    assert load_calls == 2


def test_trigger_task_recovers_concurrent_same_request(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    import app.api.tasks as mod

    task = SimpleNamespace(id=uuid4(), agent_id=uuid4(), description="go")
    db = _QueuedDB([_ScalarResult(task)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _a: False)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: _StubGate(_allow_decision()))
    winning_runtime = SimpleNamespace(id=uuid4())
    load_calls = 0

    async def fake_load(*_args, **_kwargs):
        nonlocal load_calls
        load_calls += 1
        return None if load_calls == 1 else winning_runtime

    async def duplicate_stage(**_kwargs):
        raise IntegrityError("INSERT runtime_tasks", {}, RuntimeError("duplicate request"))

    monkeypatch.setattr(mod, "_load_matching_runtime_request", fake_load)
    monkeypatch.setattr(mod, "stage_business_task_runtime", duplicate_stage)

    response = TestClient(app).post(
        f"/agents/{task.agent_id}/tasks/{task.id}/trigger",
        json={
            "request_id": "concurrent-trigger-1",
            "confirmed_plan_id": str(uuid4()),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:plan",
        },
    )

    assert response.status_code == 200
    assert response.json()["runtime_task_id"] == winning_runtime.id.hex
    assert db.rollbacks == 1
    assert load_calls == 2


def test_trigger_task_rejects_a_different_request_while_task_is_active(monkeypatch):
    from app.services.business_task_runtime import BusinessTaskInvariantError

    import app.api.tasks as mod

    task = SimpleNamespace(id=uuid4(), agent_id=uuid4(), description="go")
    db = _QueuedDB([_ScalarResult(task), _ScalarResult(None)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _a: False)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: _StubGate(_allow_decision()))

    async def active_stage(**_kwargs):
        raise BusinessTaskInvariantError("business task already has an active run")

    monkeypatch.setattr(mod, "stage_business_task_runtime", active_stage)

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/agents/{task.agent_id}/tasks/{task.id}/trigger",
        json={
            "request_id": "different-trigger-request",
            "confirmed_plan_id": str(uuid4()),
            "confirmed_plan_version": 1,
            "confirmed_plan_hash": "sha256:plan",
        },
    )

    assert response.status_code == 409
    assert "active run" in response.json()["detail"]
    assert db.rollbacks == 1
