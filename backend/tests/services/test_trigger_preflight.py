"""Tests for the trigger preflight Plan Mode backstop (§9.0).

``evaluate_trigger_preflight`` is the deterministic fail-closed guard the trigger
daemon runs *before* mutating fire counters / launching an invocation. The Plan
Mode addition: an enabled *autonomous* trigger (cron/interval/once/poll) may only
fire if it proves it came from a confirmed plan (``config.plan_id`` -> a
``confirmed`` :class:`AgentPlanRequest`) or carries an explicit cutover exemption
(``config.metadata.plan_exempt_reason``). Otherwise preflight returns
``TriggerPreflightResult(False, "plan_required", ...)`` and the daemon skips it.

These exercise the preflight glue with the project's hand-rolled async-session
fakes; the pure classification + gate-decision logic is unit tested in
``test_plan_mode_gate_core.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _PlanLookupSession:
    """Answers the by-id AgentPlanRequest lookup the PlanModeGate performs.

    The real preflight passes its own ``db`` to the gate; the gate issues a
    ``select(AgentPlanRequest).where(id == ...)`` carrying a ``_plan_lookup_id``
    hint (mirrors PlanModeService / PlanModeGate). We resolve against an in-memory
    list of plan rows keyed by id.
    """

    def __init__(self, plans=None):
        self._plans = {str(p.id): p for p in (plans or [])}

    async def execute(self, stmt):
        plan_id = getattr(stmt, "_plan_lookup_id", None)
        return _ScalarOneResult(self._plans.get(str(plan_id)))


def _agent():
    return SimpleNamespace(id=uuid4(), tenant_id=uuid4(), status="active")


def _model():
    return SimpleNamespace(id=uuid4())


def _trigger(*, trigger_type="cron", config=None, name="daily_brief"):
    cfg = {"trigger_class": "scheduled_job"}
    cfg.update(config or {})
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        type=trigger_type,
        config=cfg,
        focus_ref=None,
        max_fires=None,
        expires_at=None,
    )


def _confirmed_plan(*, agent_id, version=1, plan_hash="sha256:abc"):
    from app.models.plan_request import AgentPlanRequest

    return AgentPlanRequest(
        id=uuid4(),
        agent_id=agent_id,
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status="confirmed",
        plan_version=version,
        plan_hash=plan_hash,
        plan_json={"schema": "hive_plan.v1", "title": "Daily brief"},
    )


def _awaiting_plan(*, agent_id):
    from app.models.plan_request import AgentPlanRequest

    return AgentPlanRequest(
        id=uuid4(),
        agent_id=agent_id,
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status="awaiting_confirmation",
        plan_version=1,
        plan_hash="sha256:abc",
        plan_json={"schema": "hive_plan.v1", "title": "Daily brief"},
    )


@pytest.mark.asyncio
async def test_autonomous_trigger_without_plan_is_blocked():
    """A cron trigger with no plan_id and no exemption fails closed."""
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    trigger = _trigger(trigger_type="cron", config={})  # no plan_id
    db = _PlanLookupSession(plans=[])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is False
    assert result.skip_reason == "plan_required"
    assert str(trigger.id) in str(result.metadata.get("trigger_id", ""))


@pytest.mark.asyncio
async def test_autonomous_trigger_with_confirmed_plan_passes():
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    plan = _confirmed_plan(agent_id=agent.id)
    trigger = _trigger(
        trigger_type="cron",
        config={"plan_id": str(plan.id), "plan_version": plan.plan_version, "plan_hash": plan.plan_hash},
    )
    db = _PlanLookupSession(plans=[plan])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is True
    assert result.skip_reason is None


@pytest.mark.asyncio
async def test_autonomous_trigger_with_unconfirmed_plan_is_blocked():
    """config.plan_id pointing at a not-yet-confirmed plan must NOT pass."""
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    plan = _awaiting_plan(agent_id=agent.id)
    trigger = _trigger(
        trigger_type="interval",
        config={"plan_id": str(plan.id), "plan_version": plan.plan_version, "plan_hash": plan.plan_hash},
    )
    db = _PlanLookupSession(plans=[plan])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is False
    assert result.skip_reason == "plan_required"


@pytest.mark.asyncio
async def test_autonomous_trigger_with_cutover_exemption_passes():
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    trigger = _trigger(
        trigger_type="cron",
        config={"metadata": {"plan_exempt_reason": "preexisting_before_cutover"}},
    )
    db = _PlanLookupSession(plans=[])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is True
    assert result.skip_reason is None


@pytest.mark.asyncio
async def test_reactive_on_message_trigger_is_not_plan_gated():
    """Reactive triggers (on_message/webhook) are cutover legacy, not the
    autonomy the backstop gates — they pass without a plan."""
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    trigger = _trigger(trigger_type="on_message", config={})
    db = _PlanLookupSession(plans=[])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is True


@pytest.mark.asyncio
async def test_objective_task_trigger_is_not_double_gated_by_plan():
    """objective_task triggers keep their existing objective-approval gate; the
    plan classifier must not additionally block them on plan_id. With an active,
    approved objective and no objective bound (None), preflight passes."""
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    # objective_task with no resolvable objective → existing logic `continue`s;
    # the plan gate must not block it for lacking config.plan_id.
    trigger = _trigger(trigger_type="cron", config={"trigger_class": "objective_task"})
    db = _PlanLookupSession(plans=[])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is True


@pytest.mark.asyncio
async def test_system_maintenance_trigger_is_not_plan_gated():
    from app.services.trigger_preflight import evaluate_trigger_preflight

    agent = _agent()
    trigger = _trigger(trigger_type="cron", config={"trigger_class": "system_maintenance"})
    db = _PlanLookupSession(plans=[])

    result = await evaluate_trigger_preflight(
        db, agent=agent, model=_model(), triggers=[trigger], now=datetime.now(timezone.utc)
    )

    assert result.ok is True
