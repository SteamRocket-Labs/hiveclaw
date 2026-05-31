"""REST early-intercept layer for Plan Mode (``docs/plan-mode-design.md`` §9.3 / §9.0).

These tests pin the behaviour of the *REST gate*: every endpoint that creates or
enables an autonomous artifact (an enabled trigger, an activated wake objective,
an async delegation, or an auto-executing task) must consult
:class:`PlanModeGate` *after* the existing permission / tenant / validation
checks, and return **409 ``plan_required``** when no confirmed plan authorises the
action. The same request carrying a matching confirmed plan must pass through.

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
        created_at=now,
        updated_at=now,
    )


def _needs_plan_decision() -> PlanGateDecision:
    return PlanGateDecision(
        allowed=False,
        reason="no_confirmed_plan",
        needs_plan_payload={
            "ok": False,
            "status": "needs_plan",
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
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/triggers",
        json={"name": "daily", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "Send daily report"},
    )

    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["status"] == "needs_plan"
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
        focus_ref=None,
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
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{trigger.agent_id}/triggers/{trigger.id}",
        json={"is_enabled": True},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
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
        focus_ref=None,
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
        focus_ref=None,
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
        focus_ref=None,
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
    monkeypatch.setattr(mod, "is_agent_creator", lambda _u, _a: True)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/schedules/",
        json={"name": "nightly", "instruction": "do it", "cron_expr": "0 9 * * *"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    assert db.added == []
    assert db.flushed is False
    assert gate.calls[0]["action_kind"] == "create_enabled_trigger"


def test_create_schedule_disabled_draft_is_not_gated(monkeypatch):
    """A schedule created with is_enabled=False is a draft, not an autonomous wake."""
    import app.api.schedules as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "is_agent_creator", lambda _u, _a: True)
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
    monkeypatch.setattr(mod, "is_agent_creator", lambda _u, _a: True)
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
    monkeypatch.setattr(mod, "is_agent_creator", lambda _u, _a: True)
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
    monkeypatch.setattr(mod, "is_agent_creator", lambda _u, _a: True)
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
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.post(f"/agents/{schedule.agent_id}/schedules/{schedule.id}/run")

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
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
# objectives REST — proposal (active), update (activate), approve
# ===========================================================================


def test_propose_objective_active_without_plan_returns_409(monkeypatch):
    """An explicit-user-request proposal would activate autonomous wake -> gate."""
    import app.api.objectives as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    upsert_called = {"n": 0}

    async def fake_upsert(_db, _agent, _candidate):  # pragma: no cover - must not run
        upsert_called["n"] += 1
        return SimpleNamespace()

    monkeypatch.setattr(mod.objective_intake, "upsert_objective_candidate", fake_upsert)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/objectives/proposals",
        json={
            "description": "每天帮我整理新闻",
            "autonomy_class": "explicit_user_request",
            "wake_policy": {"type": "cron", "config": {"expr": "0 9 * * *"}},
            "evidence": {"message": "每天帮我整理新闻"},
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    assert upsert_called["n"] == 0  # objective not persisted
    assert gate.calls[0]["action_kind"] == "activate_objective_wake"


def test_propose_objective_proposed_preview_is_not_gated(monkeypatch):
    """An inferred (proposed) objective is a preview, not an active wake -> no gate."""
    import app.api.objectives as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    async def fake_upsert(_db, _agent, candidate):
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

    monkeypatch.setattr(mod.objective_intake, "upsert_objective_candidate", fake_upsert)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/objectives/proposals",
        json={
            "description": "关注一下这个方向",
            "autonomy_class": "implicit_inference",
            "evidence": {"message": "关注一下这个方向"},
        },
    )

    assert resp.status_code == 201
    assert resp.json()["status"] == "proposed"


def test_update_objective_activate_without_plan_returns_409(monkeypatch):
    import app.api.objectives as mod

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        description="Send report",
        status="proposed",
        priority=1,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    db = _QueuedDB([_ScalarResult(objective)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{uuid4()}/objectives/{objective.id}",
        json={"status": "active"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    assert objective.status == "proposed"  # not activated
    assert db.committed is False
    assert gate.calls[0]["action_kind"] == "activate_objective_wake"


def test_update_objective_non_activation_is_not_gated(monkeypatch):
    """Editing priority/description (no status activation) keeps its contract."""
    import app.api.objectives as mod

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        description="Send report",
        status="active",
        priority=1,
        source="conversation",
        success_criteria=None,
        blocked_reason=None,
        metadata_json={},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    db = _QueuedDB([_ScalarResult(objective)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.patch(
        f"/agents/{uuid4()}/objectives/{objective.id}",
        json={"priority": 5},
    )

    assert resp.status_code == 200
    assert objective.priority == 5
    assert db.committed is True


def test_approve_objective_without_plan_returns_409(monkeypatch):
    import app.api.objectives as mod

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        description="Send report",
        status="proposed",
        priority=1,
        source="conversation",
        success_criteria=None,
        blocked_reason="waiting",
        metadata_json={"requires_approval": True},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    db = _QueuedDB([_ScalarResult(objective)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    resp = client.post(
        f"/agents/{uuid4()}/objectives/{objective.id}/approve",
        json={"reason": "ok"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    assert objective.status == "proposed"  # not activated
    assert db.committed is False
    assert gate.calls[0]["action_kind"] == "activate_objective_wake"


def test_reject_objective_is_not_gated(monkeypatch):
    """Rejection is terminal, never opens autonomous behaviour."""
    import app.api.objectives as mod

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="risky",
        description="x",
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
    db = _QueuedDB([_ScalarResult(objective)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))

    client = TestClient(app)
    resp = client.post(
        f"/agents/{uuid4()}/objectives/{objective.id}/reject",
        json={"reason": "too risky"},
    )

    assert resp.status_code == 200
    assert objective.status == "rejected"
    assert db.committed is True


# ===========================================================================
# advanced REST — /collaborate/delegate
# ===========================================================================


def test_delegate_without_plan_returns_409(monkeypatch):
    import app.api.advanced as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_needs_plan_decision())
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
    assert resp.json()["detail"]["status"] == "needs_plan"
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

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/tasks/",
        json={"title": "auto research", "description": "go", "type": "todo"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    # No task persisted, no background execution fired.
    assert db.added == []
    assert db.committed is False
    assert gate.calls[0]["action_kind"] == "start_long_task"


def test_create_non_todo_task_is_not_gated(monkeypatch):
    """A plain (non-todo) task does not auto-execute -> not gated."""
    import app.api.tasks as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: (_ for _ in ()).throw(AssertionError("not gated")))
    monkeypatch.setattr(mod, "_enrich_task_out", _fake_enrich_task_out)

    client = TestClient(app)
    agent_id = uuid4()
    resp = client.post(
        f"/agents/{agent_id}/tasks/",
        json={"title": "note", "description": "remember", "type": "supervision"},
    )

    assert resp.status_code == 201
    assert db.added and db.committed is True


def test_create_todo_task_with_confirmed_plan_passes(monkeypatch):
    import app.api.tasks as mod

    db = _QueuedDB()
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr(mod, "_enrich_task_out", _fake_enrich_task_out)
    gate = _StubGate(_allow_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    executed = {"n": 0}

    async def fake_execute(_task_id, _agent_id):
        executed["n"] += 1

    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute)

    client = TestClient(app)
    agent_id = uuid4()
    plan_id = str(uuid4())
    resp = client.post(
        f"/agents/{agent_id}/tasks/",
        json={
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


def test_trigger_task_without_plan_returns_409(monkeypatch):
    import app.api.tasks as mod

    task = SimpleNamespace(id=uuid4(), agent_id=uuid4())
    # check_agent_access returns a non-expired agent; then the task lookup.
    db = _QueuedDB([_ScalarResult(task)])
    app, _user, allow_access = _make_client(mod, db=db)
    monkeypatch.setattr(mod, "check_agent_access", allow_access)
    monkeypatch.setattr("app.core.permissions.is_agent_expired", lambda _a: False)
    gate = _StubGate(_needs_plan_decision())
    monkeypatch.setattr(mod, "get_plan_mode_gate", lambda: gate)

    executed = {"n": 0}

    async def fake_execute(_task_id, _agent_id):  # pragma: no cover - must not run
        executed["n"] += 1

    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute)

    client = TestClient(app)
    resp = client.post(f"/agents/{task.agent_id}/tasks/{task.id}/trigger")

    assert resp.status_code == 409
    assert resp.json()["detail"]["status"] == "needs_plan"
    assert executed["n"] == 0
    assert gate.calls[0]["action_kind"] == "start_long_task"
