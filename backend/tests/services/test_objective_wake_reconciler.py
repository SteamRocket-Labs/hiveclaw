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
        metadata_json={},
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
        metadata_json={},
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
