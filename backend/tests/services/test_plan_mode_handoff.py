"""Integration tests for the ``objective_trigger`` handoff handler (§13, Phase 4).

A confirmed ``objective_trigger`` plan must:

* create/update an :class:`AgentObjective` (``status=active``) whose
  ``metadata_json`` carries ``plan_id`` / ``plan_version`` / ``plan_hash``;
* create an enabled :class:`AgentTrigger` whose ``config`` carries
  ``objective_id`` **and** ``plan_id`` (the latter is the contract the trigger
  daemon backstop in task #4 reads to recognise a legitimate confirmed-plan
  trigger and let it run);
* reuse the existing ``ensure_objective_for_trigger`` /
  ``build_objective_trigger_payload`` helpers rather than re-implementing wake
  policy translation;
* surface failures by *raising* so :class:`PlanModeService` records
  ``handoff_status="failed"`` with the error in ``handoff_payload`` and leaves
  ``status`` at ``confirmed`` (§13) — never a silent success.

The handler does its own DB work in a fresh session; these tests drive it with
the project's hand-rolled async-session fakes (same shape as
``test_objective_service.py`` / ``test_plan_mode_service.py``), so no LLM / no
real engine is involved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.objective import AgentObjective
from app.models.trigger import AgentTrigger


# ---------------------------------------------------------------------------
# Fake async session mirroring the AsyncSession surface the handler uses.
# ---------------------------------------------------------------------------


class _Result:
    """Supports both the single-row (``scalar_one_or_none``) and the
    multi-row (``scalars().all()``) access shapes the handler / its reused
    helpers use against the same fake session."""

    def __init__(self, values):
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _HandoffSession:
    """In-memory stand-in.

    ``ensure_objective_for_trigger`` queries objectives by id / by
    ``(agent_id, objective_key)``; the wake reconciler helpers query all
    objectives + all triggers for an agent. We answer each ``execute`` by
    sniffing the entity the statement targets via a column attribute we record
    on the statement, matching the established fake style in this repo.
    """

    def __init__(self, *, objectives=None, triggers=None):
        self.objectives = list(objectives or [])
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
        if isinstance(value, AgentObjective):
            self.objectives.append(value)
        elif isinstance(value, AgentTrigger):
            self.triggers.append(value)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def execute(self, stmt):
        entity = _statement_entity(stmt)
        if entity is AgentObjective:
            rows = self.objectives
        elif entity is AgentTrigger:
            rows = self.triggers
        else:
            rows = []
        return _Result([r for r in rows if _matches_where(stmt, r)])


def _statement_entity(stmt):
    """Best-effort: which mapped class is this SELECT over?"""
    try:
        desc = stmt.column_descriptions
        if desc:
            return desc[0].get("entity")
    except Exception:  # noqa: BLE001 - test fake, fall through to None
        return None
    return None


def _matches_where(stmt, row) -> bool:
    """Evaluate a statement's ``col == literal`` AND-chain against ``row``.

    Honouring the WHERE clause is what lets ``ensure_objective_for_trigger``'s
    by-key lookup correctly miss (create path) or hit (idempotent path) instead
    of always returning the first row.
    """
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter

    clause = getattr(stmt, "whereclause", None)
    if clause is None:
        return True

    def _check(node) -> bool:
        if isinstance(node, BinaryExpression):
            if node.operator is operators.and_:
                return _check(node.left) and _check(node.right)
            if node.operator is operators.eq:
                col = node.left
                bind = node.right
                key = getattr(col, "key", None)
                if key is None or not isinstance(bind, BindParameter):
                    return True  # unknown shape → don't over-filter
                return str(getattr(row, key, None)) == str(bind.value)
            return True
        # BooleanClauseList (AND of several comparisons)
        clauses = getattr(node, "clauses", None)
        if clauses is not None:
            return all(_check(c) for c in clauses)
        return True

    return _check(clause)


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
        "handoff": {"target": "objective_trigger", "create_objective": True, "create_trigger": True},
    }
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id or uuid4(),
        tenant_id=tenant_id,
        plan_version=version,
        plan_hash="sha256:deadbeef",
        status="confirmed",
        plan_json=plan_json,
        original_request="每天 9 点帮我整理新闻",
    )


def _agent(plan):
    return SimpleNamespace(id=plan.agent_id, tenant_id=plan.tenant_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_creates_objective_and_enabled_trigger_with_plan_id(monkeypatch):
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()

    monkeypatch.setattr(mod, "async_session", lambda: session)

    async def fake_load_agent(_db, agent_id):
        assert str(agent_id) == str(plan.agent_id)
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    payload = await mod.handoff_objective_trigger(plan)

    # One objective + one trigger created.
    objectives = [o for o in session.added if isinstance(o, AgentObjective)]
    triggers = [t for t in session.added if isinstance(t, AgentTrigger)]
    assert len(objectives) == 1
    assert len(triggers) == 1

    objective = objectives[0]
    trigger = triggers[0]

    # Objective is active and stamped with the confirmed plan provenance.
    assert objective.status == "active"
    assert objective.metadata_json["plan_id"] == str(plan.id)
    assert objective.metadata_json["plan_version"] == plan.plan_version
    assert objective.metadata_json["plan_hash"] == plan.plan_hash

    # Trigger is enabled and carries BOTH objective_id and the load-bearing
    # plan_id contract.
    assert trigger.is_enabled is True
    assert trigger.config["objective_id"] == str(objective.id)
    assert trigger.config["plan_id"] == str(plan.id)
    assert trigger.config["plan_version"] == plan.plan_version
    assert trigger.config["plan_hash"] == plan.plan_hash
    assert trigger.config["trigger_class"] == "objective_task"

    # Audit payload returned to PlanModeService.
    assert payload["created_objective_id"] == str(objective.id)
    assert payload["created_trigger_id"] == str(trigger.id)
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_handoff_can_run_inside_caller_transaction_without_own_commit(monkeypatch):
    """PlanModeService owns the handoff transaction; the concrete handler must
    be able to reuse that session instead of opening and committing another one."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()

    monkeypatch.setattr(mod, "async_session", lambda: (_ for _ in ()).throw(AssertionError("must use caller session")))

    async def fake_load_agent(db, agent_id):
        assert db is session
        assert str(agent_id) == str(plan.agent_id)
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    payload = await mod.handoff_objective_trigger(plan, db=session)

    assert payload["created_objective_id"]
    assert payload["created_trigger_id"]
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


@pytest.mark.asyncio
async def test_handoff_is_idempotent_when_trigger_already_exists(monkeypatch):
    """Re-running the handler for the same plan must not create a duplicate
    enabled trigger; it updates the existing one (and keeps plan_id)."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)

    # Pre-seed the objective the reconciler will find + the trigger it created.
    objective = AgentObjective(
        id=uuid4(),
        agent_id=plan.agent_id,
        tenant_id=plan.tenant_id,
        objective_key="daily_industry_brief",
        description="Produce a useful daily industry brief for the user.",
        status="active",
        source="plan",
        metadata_json={"plan_id": str(plan.id)},
    )
    session = _HandoffSession(objectives=[objective])
    monkeypatch.setattr(mod, "async_session", lambda: session)

    async def fake_load_agent(_db, _agent_id):
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    # First run creates the trigger.
    first = await mod.handoff_objective_trigger(plan)
    triggers_after_first = [t for t in session.triggers if isinstance(t, AgentTrigger)]
    assert len(triggers_after_first) == 1
    assert first["created_trigger_id"] == str(triggers_after_first[0].id)

    # Second run must not add a second trigger row.
    await mod.handoff_objective_trigger(plan)
    triggers_after_second = [t for t in session.triggers if isinstance(t, AgentTrigger)]
    assert len(triggers_after_second) == 1
    assert triggers_after_second[0].config["plan_id"] == str(plan.id)
    assert triggers_after_second[0].config["plan_version"] == plan.plan_version
    assert triggers_after_second[0].config["plan_hash"] == plan.plan_hash


@pytest.mark.asyncio
async def test_handoff_raises_when_agent_missing(monkeypatch):
    """A missing agent must raise so PlanModeService records handoff failure
    rather than a partial/silent success (§13)."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    session = _HandoffSession()
    monkeypatch.setattr(mod, "async_session", lambda: session)

    async def fake_load_agent(_db, _agent_id):
        return None

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    with pytest.raises(mod.HandoffError):
        await mod.handoff_objective_trigger(plan)
    # Nothing committed on failure.
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_handoff_rejects_non_confirmed_plan(monkeypatch):
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    plan.status = "awaiting_confirmation"
    session = _HandoffSession()
    monkeypatch.setattr(mod, "async_session", lambda: session)

    with pytest.raises(mod.HandoffError):
        await mod.handoff_objective_trigger(plan)
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_registered_handler_uses_plan_mode_service_session(monkeypatch):
    """``PlanModeService`` awaits the registered handler with its own session so
    handoff side effects and ``handoff_status`` are committed atomically."""
    import app.services.plan_mode_handoff as mod

    plan = _confirmed_wake_plan()
    agent = _agent(plan)
    session = _HandoffSession()
    monkeypatch.setattr(mod, "async_session", lambda: session)

    async def fake_load_agent(_db, _agent_id):
        return agent

    monkeypatch.setattr(mod, "_load_agent", fake_load_agent)

    handler = mod.objective_trigger_handoff_handler

    payload = await handler(session, plan)
    assert payload["created_objective_id"]
    assert payload["created_trigger_id"]
    assert session.commit_calls == 0
