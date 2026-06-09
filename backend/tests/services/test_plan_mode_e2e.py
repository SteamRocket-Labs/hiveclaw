"""End-to-end Plan Mode integration test — the full closed loop (§17).

The other Plan Mode test files each verify one layer in isolation (the pure
core, the service shell, the gate, the handoff handler, the daemon preflight).
This file wires those real components together over a *single shared in-memory
store* and proves the whole walk from a captured user request to a fired-or-
blocked trigger, with no mocks of the components under test:

    create_plan_request   (draft)
      -> generate_plan     (awaiting_confirmation, real hash + markdown)
      -> confirm_plan       (confirmed, version+hash bound, non-self user)
      -> handoff_confirmed_plan (scheduled_trigger handler creates an enabled
                                 AgentTrigger directly — no objective layer —
                                 stamped with the plan provenance)
      -> evaluate_trigger_preflight(that trigger) -> ALLOWED

plus the safety invariants the loop must never violate:

* **Counter-case** — an autonomous trigger with no ``plan_id`` and no cutover
  exemption fails ``evaluate_trigger_preflight`` closed with ``plan_required``.
* **Self-confirm ban** (§8.1) — the requester cannot confirm their own plan.
* **Version conflict** (§8.2) — confirming a stale ``plan_version`` is rejected.
* **Rejected plans leave no enabled artifact** (§17) — a rejected plan cannot
  hand off and therefore creates no objective/trigger.

The shared session is the established hand-rolled async-session fake style used
across this repo (``test_plan_mode_service.py`` / ``test_plan_mode_handoff.py``
/ ``test_trigger_preflight.py``); we union their access shapes so one object can
back ``PlanModeService`` (by-id / by-agent plan lookups), the
``objective_trigger`` handoff (entity + WHERE-matched objective/trigger/agent
queries) and the ``PlanModeGate`` plan lookup the preflight performs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.agent import Agent
from app.models.objective import AgentObjective
from app.models.plan_request import AgentPlanRequest
from app.models.trigger import AgentTrigger


# ---------------------------------------------------------------------------
# A single in-memory async session backing every real component in the loop.
#
# Path-unification cut ④: there is no RPC planner — ``generate_plan`` lands the
# caller-supplied structured fill (here ``_GOOD_FILL``) directly as the plan_json
# (the agent authors it in main-loop Plan Mode), so the loop drives create ->
# generate(fill) -> confirm -> handoff with no planner fake.
# ---------------------------------------------------------------------------


class _Result:
    """Covers both single-row and multi-row access on the same result."""

    def __init__(self, values):
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


def _statement_entity(stmt):
    """Best-effort mapped class behind a SELECT (None when undecidable)."""
    try:
        desc = stmt.column_descriptions
        if desc:
            return desc[0].get("entity")
    except Exception:  # noqa: BLE001 - test fake, degrade to None
        return None
    return None


def _matches_where(stmt, row) -> bool:
    """Evaluate a statement's ``col == literal`` AND-chain against ``row``.

    Mirrors the matcher in ``test_plan_mode_handoff.py`` so the handoff's
    by-key objective lookup correctly misses (create path) or hits (idempotent
    path) instead of always returning the first row.
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
                    return True
                return str(getattr(row, key, None)) == str(bind.value)
            return True
        clauses = getattr(node, "clauses", None)
        if clauses is not None:
            return all(_check(c) for c in clauses)
        return True

    return _check(clause)


class _E2ESession:
    """One shared store for plans, objectives, triggers and agents.

    ``PlanModeService`` / ``PlanModeGate`` look plans up by the ``_plan_lookup_id``
    / ``_plan_lookup_agent_id`` hints they stamp on the statement; the handoff
    handler queries ``Agent`` / ``AgentObjective`` / ``AgentTrigger`` by entity +
    WHERE. We answer all of those off the same lists so state created by the
    handoff is the very same state the preflight later reads.
    """

    def __init__(self, *, plans=None, objectives=None, triggers=None, agents=None):
        self.plans = list(plans or [])
        self.objectives = list(objectives or [])
        self.triggers = list(triggers or [])
        self.agents = list(agents or [])
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
        if isinstance(value, AgentPlanRequest):
            self.plans.append(value)
        elif isinstance(value, AgentObjective):
            self.objectives.append(value)
        elif isinstance(value, AgentTrigger):
            self.triggers.append(value)
        elif isinstance(value, Agent):
            self.agents.append(value)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def execute(self, stmt):
        # 1) PlanModeService / PlanModeGate by-id + by-agent hints take priority.
        plan_id = getattr(stmt, "_plan_lookup_id", None)
        if plan_id is not None:
            match = next((p for p in self.plans if str(p.id) == str(plan_id)), None)
            return _Result([match] if match else [])
        agent_id = getattr(stmt, "_plan_lookup_agent_id", None)
        if agent_id is not None:
            return _Result([p for p in self.plans if str(p.agent_id) == str(agent_id)])

        # 2) Entity + WHERE matching for the handoff's ORM queries.
        entity = _statement_entity(stmt)
        if entity is Agent:
            rows = self.agents
        elif entity is AgentObjective:
            rows = self.objectives
        elif entity is AgentTrigger:
            rows = self.triggers
        elif entity is AgentPlanRequest:
            rows = self.plans
        else:
            rows = []
        return _Result([r for r in rows if _matches_where(stmt, r)])


# ---------------------------------------------------------------------------
# Fixture: a service wired to the real objective_trigger handoff + a shared
# session patched into every module that opens one.
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e(monkeypatch, tmp_path):
    """Return (service, session, agent) with the full real wiring.

    * ``PlanModeService`` from the real class, with the real
      ``objective_trigger`` handler registered (``plan_mode_registry``).
    * ``async_session`` patched to the *same* shared store in the service, the
      handoff module and the gate module.
    * ``AGENT_DATA_DIR`` patched to ``tmp_path`` so markdown is written for real.
    """
    from app.services import plan_mode_gate as gate_mod
    from app.services import plan_mode_handoff as handoff_mod
    from app.services import plan_mode_service as service_mod
    from app.services.plan_mode_registry import register_plan_mode_handoffs

    agent = Agent(
        id=uuid4(),
        tenant_id=uuid4(),
        name="news-bot",
        status="active",
        primary_model_id=uuid4(),
    )
    session = _E2ESession(agents=[agent])

    monkeypatch.setattr(service_mod, "async_session", lambda: session)
    monkeypatch.setattr(service_mod, "_agent_data_dir", lambda: tmp_path)
    monkeypatch.setattr(handoff_mod, "async_session", lambda: session)
    monkeypatch.setattr(gate_mod, "async_session", lambda: session)

    service = service_mod.PlanModeService()
    register_plan_mode_handoffs(service)
    return service, session, agent


_GOOD_FILL = {
    "objective": "Produce a useful daily industry brief for the user.",
    "motivation": "User asked for a recurring morning industry news summary.",
    "steps": [{"order": 1, "description": "Collect official high-signal sources", "expected_output": "source list"}],
    "success_criteria": ["Brief includes 5-10 material updates with source links."],
    "stop_conditions": ["User cancels the plan.", "Three consecutive failed runs."],
    "wake_policy": {"type": "cron", "config": {"expr": "0 9 * * 1-5"}, "timezone": "Asia/Shanghai"},
    "required_capabilities": ["web_search", "web_fetch"],
}


async def _drive_to_awaiting(service, *, agent, requester):
    """create -> generate, returning the awaiting_confirmation plan."""
    draft = await service.create_plan_request(
        agent_id=agent.id,
        requested_by_user_id=requester,
        original_request="每天 9 点帮我整理新闻",
        intent_type="autonomous_wake",
        tenant_id=agent.tenant_id,
        source="web_chat",
    )
    assert draft.status == "draft"
    plan = await service.generate_plan(plan_id=draft.id, fill=dict(_GOOD_FILL))
    assert plan.status == "awaiting_confirmation"
    assert plan.plan_hash and plan.plan_hash.startswith("sha256:")
    return plan


# ---------------------------------------------------------------------------
# Happy path — the full loop ending in an ALLOWED preflight.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_loop_confirmed_plan_yields_passing_trigger(e2e):
    """create -> generate -> confirm -> handoff -> preflight ALLOW.

    Proves the canonical §17 closed loop: a confirmed plan produces an enabled
    trigger (``config.plan_id``) directly — no intermediate objective (that
    concept was retired) — and that very trigger then clears
    ``evaluate_trigger_preflight`` *because* it carries a confirmed plan id.
    """
    from app.services.trigger_preflight import evaluate_trigger_preflight

    service, session, agent = e2e
    requester = uuid4()
    confirmer = uuid4()  # a different real user (§8.1)

    plan = await _drive_to_awaiting(service, agent=agent, requester=requester)

    # -- confirm with the exact stored version + hash (§8.2) ----------------
    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=confirmer,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
        reason="Looks good",
    )
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by_user_id == confirmer
    assert confirmed.handoff_status == "not_started"

    # -- handoff to the real scheduled_trigger handler ----------------------
    handed = await service.handoff_confirmed_plan(plan_id=plan.id)
    assert handed.status == "confirmed"  # status never mutated by handoff (§13)
    assert handed.handoff_status == "completed"
    trigger_id = handed.handoff_payload["created_trigger_id"]
    assert trigger_id
    assert "created_objective_id" not in handed.handoff_payload  # no objective layer

    # The enabled trigger carries the load-bearing config.plan_id contract,
    # created directly from the plan with no objective row.
    assert session.objectives == []
    trigger = next(t for t in session.triggers if str(t.id) == trigger_id)
    assert trigger.is_enabled is True
    assert trigger.type == "cron"
    assert trigger.config["plan_id"] == str(plan.id)
    assert trigger.config["plan_version"] == plan.plan_version
    assert trigger.config["plan_hash"] == plan.plan_hash
    assert trigger.config["trigger_class"] == "scheduled_job"
    assert "objective_id" not in trigger.config

    # -- preflight on the produced trigger: ALLOWED (it has a confirmed plan) --
    model = SimpleNamespace(id=agent.primary_model_id)
    result = await evaluate_trigger_preflight(
        session, agent=agent, model=model, triggers=[trigger], now=datetime.now(timezone.utc)
    )
    assert result.ok is True, result.skip_reason
    assert result.skip_reason is None


# ---------------------------------------------------------------------------
# Counter-case — an autonomous trigger with no plan fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autonomous_trigger_without_plan_is_blocked(e2e):
    """The mirror image of the happy path: an enabled cron trigger that did NOT
    come from a confirmed plan (no ``config.plan_id``, no cutover exemption)
    must be refused by the preflight backstop with ``plan_required``."""
    from app.services.trigger_preflight import evaluate_trigger_preflight

    _service, session, agent = e2e
    model = SimpleNamespace(id=agent.primary_model_id)
    orphan = AgentTrigger(
        id=uuid4(),
        agent_id=agent.id,
        name="orphan_cron",
        type="cron",
        config={"trigger_class": "scheduled_job", "expr": "0 9 * * *"},
        is_enabled=True,
    )

    result = await evaluate_trigger_preflight(
        session, agent=agent, model=model, triggers=[orphan], now=datetime.now(timezone.utc)
    )

    assert result.ok is False
    assert result.skip_reason == "plan_required"
    assert str(orphan.id) in str(result.metadata.get("trigger_id", ""))


# ---------------------------------------------------------------------------
# Safety invariants on the confirm gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requesting_user_can_confirm_their_own_plan(e2e):
    """§8.1 forbids agent self-confirmation, not the same real user who asked
    for the plan confirming it from the UI."""
    service, session, agent = e2e
    requester = uuid4()
    plan = await _drive_to_awaiting(service, agent=agent, requester=requester)

    confirmed = await service.confirm_plan(
        plan_id=plan.id,
        confirming_user_id=requester,
        plan_version=plan.plan_version,
        plan_hash=plan.plan_hash,
    )

    reloaded = next(p for p in session.plans if str(p.id) == str(plan.id))
    assert confirmed.status == "confirmed"
    assert reloaded.status == "confirmed"
    assert reloaded.confirmed_by_user_id == requester


@pytest.mark.asyncio
async def test_confirming_stale_version_conflicts(e2e):
    """§8.2 — a submitted plan_version that does not match the stored one is a
    PlanConflictError(version_mismatch); the plan stays awaiting."""
    from app.services.plan_mode_service import PlanConflictError

    service, session, agent = e2e
    plan = await _drive_to_awaiting(service, agent=agent, requester=uuid4())

    with pytest.raises(PlanConflictError) as exc_info:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=uuid4(),
            plan_version=plan.plan_version + 1,  # stale / wrong version
            plan_hash=plan.plan_hash,
        )
    assert exc_info.value.error_code == "version_mismatch"

    reloaded = next(p for p in session.plans if str(p.id) == str(plan.id))
    assert reloaded.status == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_confirming_stale_hash_conflicts(e2e):
    """§8.2 / §8.3 — a confirmed plan binds to its exact hash; a mismatched hash
    (e.g. the plan was edited after the preview) is refused."""
    from app.services.plan_mode_service import PlanConflictError

    service, _session, agent = e2e
    plan = await _drive_to_awaiting(service, agent=agent, requester=uuid4())

    with pytest.raises(PlanConflictError) as exc_info:
        await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=uuid4(),
            plan_version=plan.plan_version,
            plan_hash="sha256:stale",
        )
    assert exc_info.value.error_code == "hash_mismatch"


# ---------------------------------------------------------------------------
# Rejected plans leave no enabled autonomous artifact (§17).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_plan_produces_no_enabled_artifact(e2e):
    """A rejected plan is terminal: it cannot hand off, so no objective/trigger
    is ever created (§17 — rejected/expired/superseded leave no enabled
    autonomous artifacts)."""
    from app.services.plan_mode_service import PlanConflictError

    service, session, agent = e2e
    plan = await _drive_to_awaiting(service, agent=agent, requester=uuid4())

    rejected = await service.reject_plan(plan_id=plan.id, rejecting_user_id=uuid4(), reason="not now")
    assert rejected.status == "rejected"

    # Handoff of a non-confirmed plan is refused (status stays rejected).
    with pytest.raises(PlanConflictError) as exc_info:
        await service.handoff_confirmed_plan(plan_id=plan.id)
    assert exc_info.value.error_code == "not_confirmed"

    # Nothing autonomous was created.
    assert [o for o in session.objectives] == []
    assert [t for t in session.triggers] == []
