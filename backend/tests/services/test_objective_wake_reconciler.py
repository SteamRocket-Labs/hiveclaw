from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


class _FakeSession:
    def __init__(self, objectives=None, triggers=None):
        self._responses = [list(objectives or []), list(triggers or [])]
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _stmt):
        values = self._responses.pop(0) if self._responses else []
        return _ScalarsResult(values)

    def add(self, value):
        self.added.append(value)
        if not getattr(value, "id", None):
            value.id = uuid4()

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


def test_build_objective_trigger_payload_binds_objective_id_and_focus_ref():
    from app.services.objective_wake_reconciler import build_objective_trigger_payload

    objective_id = uuid4()
    objective = SimpleNamespace(
        id=objective_id,
        objective_key="daily_report",
        description="Send the daily report",
        success_criteria="Report sent with source links",
        metadata_json={"wake_policy": {"type": "cron", "config": {"expr": "0 9 * * *"}}},
    )

    payload = build_objective_trigger_payload(objective)

    assert payload["name"] == "objective_daily_report"
    assert payload["type"] == "cron"
    assert payload["config"]["expr"] == "0 9 * * *"
    assert payload["config"]["trigger_class"] == "objective_task"
    assert payload["config"]["objective_id"] == str(objective_id)
    assert payload["focus_ref"] == "daily_report"
    assert "Report sent with source links" in payload["reason"]


@pytest.mark.asyncio
async def test_reconcile_agent_objective_wake_policies_creates_missing_primary_wake():
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    tenant_id = uuid4()
    objective_id = uuid4()
    objective = SimpleNamespace(
        id=objective_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        objective_key="first_report",
        description="Create the first report",
        status="active",
        priority=0,
        success_criteria=None,
        # Plan provenance (§9.0): this objective traces to a confirmed plan, so
        # the reconciler is allowed to auto-create its enabled wake.
        metadata_json={"plan_id": str(uuid4())},
    )
    session = _FakeSession(objectives=[objective], triggers=[])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 1
    assert len(session.added) == 1
    trigger = session.added[0]
    assert trigger.agent_id == agent_id
    assert trigger.focus_ref == "first_report"
    assert trigger.config["trigger_class"] == "objective_task"
    assert trigger.config["objective_id"] == str(objective_id)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reconcile_agent_objective_wake_policies_does_not_duplicate_existing_wake():
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective_id = uuid4()
    objective = SimpleNamespace(
        id=objective_id,
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="first_report",
        description="Create the first report",
        status="active",
        priority=0,
        success_criteria=None,
        metadata_json={"plan_id": str(uuid4())},
    )
    trigger = SimpleNamespace(
        is_enabled=True,
        focus_ref="first_report",
        config={"trigger_class": "objective_task", "objective_id": str(objective_id)},
    )
    session = _FakeSession(objectives=[objective], triggers=[trigger])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 0
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_reconcile_agent_objective_wake_policies_reuses_existing_trigger_name():
    from app.services.objective_wake_reconciler import build_objective_trigger_payload, reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective_id = uuid4()
    objective = SimpleNamespace(
        id=objective_id,
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="task_1",
        description="Create the first report",
        status="active",
        priority=0,
        success_criteria=None,
        metadata_json={"plan_id": str(uuid4())},
    )
    payload = build_objective_trigger_payload(objective)
    existing = SimpleNamespace(
        agent_id=agent_id,
        name=payload["name"],
        type="once",
        config={"at": "2026-04-27T09:00:00+00:00"},
        reason="Old wake policy",
        focus_ref=None,
        is_enabled=False,
    )
    session = _FakeSession(objectives=[objective], triggers=[existing])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 0
    assert report["updated"] == 1
    assert session.added == []
    assert session.commits == 1
    assert existing.is_enabled is True
    assert existing.focus_ref == "task_1"
    assert existing.config["trigger_class"] == "objective_task"
    assert existing.config["objective_id"] == str(objective_id)


@pytest.mark.asyncio
async def test_reconcile_skips_conversation_intent_objective_without_plan():
    """§9.0 backstop: an active objective born from a conversation intent (no
    plan_id, not platform-internal) must NOT get an auto-created enabled wake.
    This closes the "chat intent -> active objective -> auto enabled trigger" hole."""
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="follow_up_client",
        description="Keep following up with the client",
        status="active",
        priority=0,
        success_criteria=None,
        metadata_json={"autonomy_class": "explicit_user_request"},
    )
    session = _FakeSession(objectives=[objective], triggers=[])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 0
    assert report["updated"] == 0
    assert report["skipped"] == 1
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_reconcile_creates_wake_for_confirmed_plan_objective():
    """An objective carrying plan provenance (confirmed-plan handoff) is exempt
    and still gets its enabled wake from the reconciler."""
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="planned_brief",
        description="Daily planned brief",
        status="active",
        priority=0,
        success_criteria=None,
        metadata_json={"plan_id": str(uuid4()), "plan_version": 1},
    )
    session = _FakeSession(objectives=[objective], triggers=[])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 1
    assert len(session.added) == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reconcile_creates_wake_for_platform_internal_objective():
    """The self-evolution / recovery loop (internal_self_improvement) must keep
    auto-waking without a confirmed plan — the backstop must not break it."""
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="recover_daily_report",
        description="Recover failing trigger",
        status="active",
        priority=1,
        success_criteria=None,
        metadata_json={"autonomy_class": "internal_self_improvement"},
    )
    session = _FakeSession(objectives=[objective], triggers=[])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 1
    assert len(session.added) == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reconcile_creates_wake_for_cutover_exempt_objective():
    """An objective explicitly tagged with a cutover exemption is honoured."""
    from app.services.objective_wake_reconciler import reconcile_agent_objective_wake_policies

    agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=uuid4(),
        objective_key="legacy_objective",
        description="Pre-existing objective",
        status="active",
        priority=0,
        success_criteria=None,
        metadata_json={"plan_exempt_reason": "preexisting_before_cutover"},
    )
    session = _FakeSession(objectives=[objective], triggers=[])

    report = await reconcile_agent_objective_wake_policies(session, agent_id=agent_id)

    assert report["created"] == 1
    assert len(session.added) == 1


def test_objective_has_enabled_wake_ignores_other_agents_same_focus_ref():
    from app.services.objective_wake_reconciler import objective_has_enabled_wake

    agent_id = uuid4()
    other_agent_id = uuid4()
    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        objective_key="task_1",
    )
    other_agent_trigger = SimpleNamespace(
        agent_id=other_agent_id,
        is_enabled=True,
        focus_ref="task_1",
        config={"trigger_class": "objective_task"},
    )

    assert objective_has_enabled_wake(objective, [other_agent_trigger]) is False
