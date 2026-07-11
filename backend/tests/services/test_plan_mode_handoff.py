"""Integration tests for the ``scheduled_trigger`` handoff handler.

Exec/automation CC-alignment (2026-06-08): a confirmed recurring plan
(``intent_type=autonomous_wake``) must:

* create an enabled :class:`AgentTrigger` **directly** from the plan's
  ``wake_policy`` — no intermediate ``AgentObjective`` row (that concept was
  retired);
* stamp ``config.plan_id`` / ``plan_version`` / ``plan_hash`` (the contract the
  trigger daemon backstop reads to recognise a legitimate confirmed-plan trigger)
  and ``config.trigger_class="scheduled_job"`` (the non-objective autonomous class);
* be idempotent on ``config.plan_id`` — re-running updates the existing trigger
  rather than duplicating;
* surface failures by *raising* so :class:`PlanModeService` records
  ``handoff_status="failed"`` and leaves ``status`` at ``confirmed`` — never a
  silent success.

The handler does its own DB work in a fresh session; these tests drive it with
the project's hand-rolled async-session fakes, so no LLM / no real engine is
involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.trigger import AgentTrigger


# ---------------------------------------------------------------------------
# Fake async session mirroring the AsyncSession surface the handler uses.
# ---------------------------------------------------------------------------


class _Result:
    """Supports both the single-row (``scalar_one_or_none``) and the multi-row
    (``scalars().all()``) access shapes the handler uses against the fake."""

    def __init__(self, values):
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _HandoffSession:
    """In-memory stand-in. The handler only queries triggers by ``agent_id``."""

    def __init__(self, *, triggers=None):
        self.triggers = list(triggers or [])
        self.added: list[object] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid4()
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)
        value.updated_at = datetime.now(timezone.utc)
        self.added.append(value)
        if isinstance(value, AgentTrigger):
            self.triggers.append(value)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def execute(self, stmt):
        # The handler issues exactly one SELECT: all triggers for an agent.
        return _Result(list(self.triggers))


# ---------------------------------------------------------------------------
# Plan + agent factories
# ---------------------------------------------------------------------------


def _confirmed_wake_plan(*, agent_id=None, tenant_id=None, version=1):
    plan_json = {
        "schema": "hive_plan.v1",
        "title": "Daily industry brief",
        "intent_type": "autonomous_wake",
        "objective": "Produce a useful daily industry brief for the user.",
        "motivation": "User asked for a recurring morning industry news summary.",
        "steps": [{"order": 1, "description": "Collect sources"}],
        "success_criteria": ["Brief includes 5-10 material updates with links."],
        "wake_policy": {"type": "cron", "config": {"expr": "0 9 * * 1-5"}, "timezone": "Asia/Shanghai"},
        "required_capabilities": ["web_search"],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": ["recurring autonomous wake"]},
        "estimated_cost": {"tokens_per_run": "medium", "expected_duration": "1-3 minutes"},
        "stop_conditions": ["User cancels the plan."],
        "handoff": {"target": "scheduled_trigger", "create_trigger": True},
    }
    plan = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id or uuid4(),
        tenant_id=tenant_id,
        plan_version=version,
        plan_hash="sha256:deadbeef",
        status="confirmed",
        plan_json=plan_json,
        original_request="每天 9 点帮我整理新闻",
    )
    plan.metadata_json = {
        "active_plan_authorization": {
            "schema": "hive.plan_authorization_evidence.v1",
            "lease_id": str(uuid4()),
            "canonical_args_hash": "args-hash",
            "target_ref": f"plan:{plan.id}:handoff:scheduled_trigger",
            "requester_user_id": str(uuid4()),
            "session_id": "session-1",
            "runtime_task_id": None,
            "evidence_id": f"plan-handoff:{plan.id}:scheduled_trigger",
        }
    }
    return plan


def _agent(plan):
    return SimpleNamespace(id=plan.agent_id, tenant_id=plan.tenant_id)


async def _fake_resolve_tenant(_agent_id, **_kwargs):
    # The bare branch now resolves the plan's tenant via an audited bypass read
    # before pinning the session; stub it so unit tests never touch a real DB.
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_creates_enabled_trigger_with_plan_id_and_no_objective(monkeypatch):
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()

    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)

    async def fake_load_agent(_db, agent_id):
        assert str(agent_id) == str(plan.agent_id)
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    payload = await mod.handoff_scheduled_trigger(plan)

    # Exactly one trigger created; the AgentObjective concept is gone.
    triggers = [t for t in session.added if isinstance(t, AgentTrigger)]
    assert len(triggers) == 1
    trigger = triggers[0]

    # Trigger is the recurring cron from the plan's wake_policy.
    assert trigger.is_enabled is True
    assert trigger.type == "cron"
    assert trigger.config["expr"] == "0 9 * * 1-5"
    assert trigger.config["timezone"] == "Asia/Shanghai"

    # The load-bearing confirmed-plan backstop contract + non-objective class.
    assert trigger.config["plan_id"] == str(plan.id)
    assert trigger.config["plan_version"] == plan.plan_version
    assert trigger.config["plan_hash"] == plan.plan_hash
    assert trigger.config["plan_authorization"] == plan.metadata_json["active_plan_authorization"]
    assert trigger.config["trigger_class"] == "scheduled_job"
    assert "objective_id" not in trigger.config

    # Audit payload returned to PlanModeService.
    assert payload["created_trigger_id"] == str(trigger.id)
    assert "created_objective_id" not in payload
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_handoff_can_run_inside_caller_transaction_without_own_commit(monkeypatch):
    """PlanModeService owns the handoff transaction; the concrete handler must be
    able to reuse that session instead of opening and committing another one."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()

    monkeypatch.setattr(
        mod,
        "tenant_scoped_session",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must use caller session")),
    )

    async def fake_load_agent(db, agent_id):
        assert db is session
        assert str(agent_id) == str(plan.agent_id)
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    payload = await mod.handoff_scheduled_trigger(plan, db=session)

    assert payload["created_trigger_id"]
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


@pytest.mark.asyncio
async def test_handoff_is_idempotent_on_plan_id(monkeypatch):
    """Re-running the handler for the same plan must not create a duplicate
    trigger; it updates the existing one (matched on config.plan_id)."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()
    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)

    async def fake_load_agent(_db, _agent_id):
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    # First run creates the trigger.
    first = await mod.handoff_scheduled_trigger(plan)
    triggers_after_first = [t for t in session.triggers if isinstance(t, AgentTrigger)]
    assert len(triggers_after_first) == 1
    assert first["created_trigger_id"] == str(triggers_after_first[0].id)

    # Second run must not add a second trigger row.
    await mod.handoff_scheduled_trigger(plan)
    triggers_after_second = [t for t in session.triggers if isinstance(t, AgentTrigger)]
    assert len(triggers_after_second) == 1
    assert triggers_after_second[0].config["plan_id"] == str(plan.id)
    assert triggers_after_second[0].is_enabled is True


@pytest.mark.asyncio
async def test_detached_handoff_creates_once_background_trigger(monkeypatch):
    """The detached target reuses the same machinery with force_once=True, so a
    confirmed 'run it and notify me' plan becomes a single ``once`` trigger."""
    import app.services.plan_mode_detached_handoff as detached
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    # Detached plans typically carry no recurring schedule.
    plan.plan_json["wake_policy"] = {"type": "none"}
    agent = _agent(plan)
    session = _HandoffSession()

    async def fake_load_agent(_db, _agent_id):
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    payload = await detached.detached_runtime_task_handoff(session, plan)

    triggers = [t for t in session.added if isinstance(t, AgentTrigger)]
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.type == "once"
    assert trigger.config.get("at")  # a concrete fire time was synthesised
    assert trigger.config["plan_id"] == str(plan.id)
    assert trigger.config["trigger_class"] == "scheduled_job"
    assert payload["created_trigger_id"] == str(trigger.id)


@pytest.mark.asyncio
async def test_handoff_raises_when_agent_missing(monkeypatch):
    """A missing agent must raise so PlanModeService records handoff failure
    rather than a partial/silent success."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    session = _HandoffSession()
    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)

    async def fake_load_agent(_db, _agent_id):
        return None

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    with pytest.raises(mod.HandoffError):
        await mod.handoff_scheduled_trigger(plan)
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_handoff_rejects_non_confirmed_plan(monkeypatch):
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    plan.status = "awaiting_confirmation"
    session = _HandoffSession()
    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)

    with pytest.raises(mod.HandoffError):
        await mod.handoff_scheduled_trigger(plan)
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_registered_handler_uses_plan_mode_service_session(monkeypatch):
    """``PlanModeService`` awaits the registered handler with its own session so
    handoff side effects and ``handoff_status`` are committed atomically."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()
    monkeypatch.setattr(mod, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(mod, "tenant_scoped_session", lambda *a, **k: session)

    async def fake_load_agent(_db, _agent_id):
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    handler = mod.scheduled_trigger_handoff_handler

    payload = await handler(session, plan)
    assert payload["created_trigger_id"]
    assert session.commit_calls == 0
