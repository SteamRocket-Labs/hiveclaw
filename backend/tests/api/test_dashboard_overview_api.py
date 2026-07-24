from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_dashboard_overview_uses_fixed_bulk_loaders_for_any_agent_count(monkeypatch):
    import app.api.dashboard as dashboard_api

    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), department_id=None, role="member")
    agent_ids = [uuid4() for _ in range(100)]
    calls = {"agents": 0, "grants": 0, "sessions": 0, "activities": 0, "failures": 0}

    async def load_agents(*_args, **_kwargs):
        calls["agents"] += 1
        return agent_ids

    async def load_grants(*_args, **_kwargs):
        calls["grants"] += 1
        return set()

    async def load_sessions(*_args, **_kwargs):
        calls["sessions"] += 1
        return ([{"id": "session-1", "agent_id": str(agent_ids[0])}], 321)

    async def load_activities(*_args, **_kwargs):
        calls["activities"] += 1
        return [{"id": "activity-1", "agent_id": str(agent_ids[0])}]

    async def load_failures(*_args, **_kwargs):
        calls["failures"] += 1
        return ({str(agent_ids[0]): {"total_errors": 2}}, True, 500)

    monkeypatch.setattr(dashboard_api, "_load_accessible_agent_ids", load_agents)
    monkeypatch.setattr(dashboard_api, "load_explicit_resource_grant_ids", load_grants)
    monkeypatch.setattr(dashboard_api, "_load_dashboard_sessions", load_sessions)
    monkeypatch.setattr(dashboard_api, "_load_dashboard_activities", load_activities)
    monkeypatch.setattr(dashboard_api, "_load_dashboard_failures", load_failures)

    payload = await dashboard_api.get_dashboard_overview(
        tenant_id=None,
        session_limit=4,
        activity_limit=20,
        failure_hours=24,
        failure_limit=500,
        current_user=user,
        db=SimpleNamespace(),
    )

    assert calls == {"agents": 1, "grants": 1, "sessions": 1, "activities": 1, "failures": 1}
    assert payload["session_count"] == 321
    assert payload["recent_sessions"][0]["id"] == "session-1"
    assert payload["recent_activities"][0]["id"] == "activity-1"
    assert payload["tool_failures"][str(agent_ids[0])]["total_errors"] == 2
    assert payload["query_evidence"] == {
        "agent_count": 100,
        "session_limit": 4,
        "activity_limit": 20,
        "failure_hours": 24,
        "failure_limit": 500,
        "failure_rows_scanned": 500,
        "failure_rows_truncated": True,
    }


@pytest.mark.asyncio
async def test_dashboard_overview_short_circuits_without_accessible_agents(monkeypatch):
    import app.api.dashboard as dashboard_api

    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), department_id=None, role="member")

    async def no_agents(*_args, **_kwargs):
        return []

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("dashboard data loaders must not run without accessible agents")

    monkeypatch.setattr(dashboard_api, "_load_accessible_agent_ids", no_agents)
    monkeypatch.setattr(dashboard_api, "load_explicit_resource_grant_ids", unexpected)
    monkeypatch.setattr(dashboard_api, "_load_dashboard_sessions", unexpected)

    payload = await dashboard_api.get_dashboard_overview(
        tenant_id=None,
        session_limit=4,
        activity_limit=20,
        failure_hours=24,
        failure_limit=500,
        current_user=user,
        db=SimpleNamespace(),
    )

    assert payload["recent_sessions"] == []
    assert payload["recent_activities"] == []
    assert payload["tool_failures"] == {}
    assert payload["query_evidence"]["agent_count"] == 0
