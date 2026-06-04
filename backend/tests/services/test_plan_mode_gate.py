"""Integration tests for :class:`PlanModeGate` (imperative shell).

``PlanModeGate.check`` is the shared decision core for both the early-intercept
layer (tool / REST) and the fail-closed backstop layer (reconciler / daemon)
described in ``docs/plan-mode-design.md`` §9.0. It loads the referenced plan row
and resolves any cutover artifact exemption, then delegates the *decision* to
the pure ``plan_mode_core`` helpers.

Tested with the project's established hand-rolled async-session fakes (same
pattern as ``test_plan_mode_service.py`` / ``test_objective_service.py``). The
load-bearing decision logic is in the pure core (separately unit tested with no
fakes); here we exercise the DB-touching glue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# Fake async session over agent_plan_requests (mirrors test_plan_mode_service).
# ---------------------------------------------------------------------------


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _PlanSession:
    """In-memory stand-in answering the by-id lookup the gate performs."""

    def __init__(self):
        self.rows: list[object] = []
        self.commit_calls = 0
        self.rollback_calls = 0

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
        self.rows.append(value)

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def execute(self, stmt):
        plan_id = getattr(stmt, "_plan_lookup_id", None)
        match = next((r for r in self.rows if str(r.id) == str(plan_id)), None)
        return _ScalarOneResult(match)


def _make_confirmed_plan(
    session,
    *,
    agent_id,
    version=1,
    plan_hash="sha256:abc",
    intent_type="autonomous_wake",
    metadata_json=None,
    handoff_payload=None,
    plan_json=None,
):
    from app.models.plan_request import AgentPlanRequest

    plan = AgentPlanRequest(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        source="web_chat",
        intent_type=intent_type,
        original_request="每天 9 点帮我整理新闻",
        status="confirmed",
        plan_version=version,
        plan_hash=plan_hash,
        plan_json=plan_json or {"schema": "hive_plan.v1", "title": "Daily brief"},
        handoff_payload=handoff_payload,
        metadata_json=metadata_json,
    )
    session.add(plan)
    return plan


def _make_confirmed_long_task_plan(session, *, agent_id, version=1, plan_hash="sha256:long"):
    from app.models.plan_request import AgentPlanRequest

    plan = AgentPlanRequest(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        source="web_chat",
        intent_type="long_task",
        original_request="Run a long research task",
        status="confirmed",
        plan_version=version,
        plan_hash=plan_hash,
        plan_json={"schema": "hive_plan.v1", "title": "Long task", "handoff": {"target": "long_task"}},
    )
    session.add(plan)
    return plan


def _make_awaiting_plan(session, *, agent_id, version=1, plan_hash="sha256:abc"):
    from app.models.plan_request import AgentPlanRequest

    plan = AgentPlanRequest(
        agent_id=agent_id,
        requested_by_user_id=uuid4(),
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status="awaiting_confirmation",
        plan_version=version,
        plan_hash=plan_hash,
        plan_json={"schema": "hive_plan.v1", "title": "Daily brief"},
    )
    session.add(plan)
    return plan


@pytest.fixture()
def patched_gate(monkeypatch):
    """Return (gate, session) with the gate module's async_session patched."""
    from app.services import plan_mode_gate as mod

    session = _PlanSession()
    monkeypatch.setattr(mod, "async_session", lambda: session)
    gate = mod.PlanModeGate()
    return gate, session


# ---------------------------------------------------------------------------
# Decision: allow on confirmed-plan handoff (§9.0 step 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_allows_when_confirmed_plan_id_version_hash_match(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:abc")

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_plan_handoff"
    assert decision.needs_plan_payload is None


@pytest.mark.asyncio
async def test_check_allows_when_confirmed_plan_action_artifact_matches(patched_gate):
    """High-risk handoff is not just plan hash bound: the exact action payload
    being authorised must match the confirmed plan's recorded artifact."""
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(
        session,
        agent_id=agent_id,
        version=1,
        plan_hash="sha256:abc",
        intent_type="long_task",
        metadata_json={
            "confirmed_action_artifact": {
                "definition_hash": "wf-hash-1",
                "risk_reasons": ["step 'send' has external effects"],
            }
        },
    )

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="start_workflow",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
        action_artifact={"definition_hash": "wf-hash-1", "risk_reasons": ["step 'send' has external effects"]},
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_plan_handoff"


@pytest.mark.asyncio
async def test_check_needs_plan_when_action_artifact_missing_from_confirmed_plan(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:abc")
    plan.intent_type = "long_task"

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="start_workflow",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
        action_artifact={"definition_hash": "wf-hash-1"},
    )

    assert decision.allowed is False
    assert decision.reason == "action_artifact_missing"


@pytest.mark.asyncio
async def test_check_needs_plan_when_action_artifact_mismatches_confirmed_plan(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(
        session,
        agent_id=agent_id,
        version=1,
        plan_hash="sha256:abc",
        intent_type="long_task",
        metadata_json={"confirmed_action_artifact": {"definition_hash": "wf-hash-1"}},
    )

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="start_workflow",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
        action_artifact={"definition_hash": "wf-hash-2"},
    )

    assert decision.allowed is False
    assert decision.reason == "action_artifact_mismatch"


@pytest.mark.asyncio
async def test_check_needs_plan_with_bare_confirmed_plan_id(patched_gate):
    """A plan id alone is not enough; handoff must bind version + hash."""
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id)

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
    )

    assert decision.allowed is False
    assert decision.reason == "missing_plan_version_hash"


@pytest.mark.asyncio
async def test_check_needs_plan_when_confirmed_plan_belongs_to_other_agent(patched_gate):
    gate, session = patched_gate
    current_agent_id = uuid4()
    other_agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=other_agent_id, version=1, plan_hash="sha256:abc")

    decision = await gate.check(
        session,
        agent_id=current_agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
    )

    assert decision.allowed is False
    assert decision.reason == "plan_agent_mismatch"


@pytest.mark.asyncio
async def test_check_needs_plan_when_confirmed_plan_intent_does_not_match_action(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_long_task_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:long")

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:long",
    )

    assert decision.allowed is False
    assert decision.reason == "plan_intent_mismatch"


# ---------------------------------------------------------------------------
# Decision: needs_plan when plan missing / unconfirmed / mismatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_needs_plan_when_no_plan_referenced(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
    )

    assert decision.allowed is False
    assert decision.reason == "no_confirmed_plan"
    payload = decision.needs_plan_payload
    assert payload is not None
    assert payload["ok"] is False
    assert payload["status"] == "needs_plan"
    assert payload["next_action"]


@pytest.mark.asyncio
async def test_check_needs_plan_when_confirmed_plan_id_unknown(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="enable_autonomous_wake",
        confirmed_plan_id=uuid4(),  # not in the session
        plan_version=1,
        plan_hash="sha256:abc",
    )

    assert decision.allowed is False
    assert decision.reason == "plan_not_found"
    assert decision.needs_plan_payload["status"] == "needs_plan"


@pytest.mark.asyncio
async def test_check_needs_plan_when_referenced_plan_not_confirmed(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_awaiting_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:abc")

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
    )

    assert decision.allowed is False
    assert decision.reason == "plan_not_confirmed"
    payload = decision.needs_plan_payload
    assert payload["plan_id"] == str(plan.id)
    assert payload["plan_version"] == 1


@pytest.mark.asyncio
async def test_check_needs_plan_on_version_mismatch(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id, version=2, plan_hash="sha256:abc")

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,  # stale
        plan_hash="sha256:abc",
    )

    assert decision.allowed is False
    assert decision.reason == "version_mismatch"
    assert decision.needs_plan_payload["status"] == "needs_plan"


@pytest.mark.asyncio
async def test_check_needs_plan_on_hash_mismatch(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:abc")

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:tampered",
    )

    assert decision.allowed is False
    assert decision.reason == "hash_mismatch"


# ---------------------------------------------------------------------------
# Decision: allow on cutover exemption (§9.0 step 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_allows_when_artifact_carries_cutover_exemption(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        action_artifact={"config": {"metadata": {"plan_exempt_reason": "preexisting_before_cutover"}}},
    )

    assert decision.allowed is True
    assert decision.reason == "plan_exempt"
    assert decision.exempt_reason == "preexisting_before_cutover"
    assert decision.needs_plan_payload is None


@pytest.mark.asyncio
async def test_check_exemption_via_resolver_callback(patched_gate):
    """When the caller can't pass the artifact inline, it may supply a resolver
    keyed by ``action_ref`` (used by the backstop layer where only an id is in
    hand)."""
    gate, session = patched_gate
    agent_id = uuid4()
    trigger_id = uuid4()

    async def resolver(ref):
        assert ref == trigger_id
        return {"metadata": {"plan_exempt_reason": "preexisting_before_cutover"}}

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        action_ref=trigger_id,
        artifact_resolver=resolver,
    )

    assert decision.allowed is True
    assert decision.reason == "plan_exempt"
    assert decision.exempt_reason == "preexisting_before_cutover"


@pytest.mark.asyncio
async def test_check_no_exemption_when_artifact_blank(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        action_artifact={"config": {"metadata": {}}},
    )

    assert decision.allowed is False
    assert decision.reason == "no_confirmed_plan"


# ---------------------------------------------------------------------------
# Confirmed plan beats a present-but-irrelevant exemption probe; precedence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_confirmed_plan_takes_precedence_over_exemption_lookup(patched_gate):
    gate, session = patched_gate
    agent_id = uuid4()
    plan = _make_confirmed_plan(session, agent_id=agent_id, version=1, plan_hash="sha256:abc")

    resolver_called = {"n": 0}

    async def resolver(ref):  # pragma: no cover - must not be called
        resolver_called["n"] += 1
        return None

    decision = await gate.check(
        session,
        agent_id=agent_id,
        action_kind="create_enabled_trigger",
        confirmed_plan_id=plan.id,
        plan_version=1,
        plan_hash="sha256:abc",
        action_ref=uuid4(),
        artifact_resolver=resolver,
    )

    assert decision.allowed is True
    assert decision.reason == "confirmed_plan_handoff"
    assert resolver_called["n"] == 0  # short-circuited before exemption lookup


# ---------------------------------------------------------------------------
# Validation: unknown action_kind fails fast (programmer error, §error_handling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rejects_unknown_action_kind(patched_gate):
    gate, session = patched_gate

    with pytest.raises(ValueError, match="action_kind"):
        await gate.check(
            session,
            agent_id=uuid4(),
            action_kind="rm_-rf_slash",
        )


# ---------------------------------------------------------------------------
# needs_plan payload carries an intent-appropriate summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_plan_payload_summary_mentions_the_action(patched_gate):
    gate, session = patched_gate

    decision = await gate.check(
        session,
        agent_id=uuid4(),
        action_kind="start_delegation",
    )

    assert decision.allowed is False
    # The summary should be human/agent-actionable and reference confirming a plan.
    assert "plan" in decision.needs_plan_payload["summary"].lower()
