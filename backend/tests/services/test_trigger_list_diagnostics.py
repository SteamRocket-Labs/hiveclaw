from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _SequenceSession:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_list_triggers_includes_no_model_diagnostic(monkeypatch):
    from app.services.agent_tool_domains import triggers as trigger_domain

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="scheduled_trigger",
        type="cron",
        config={"expr": "0 9 * * *", "trigger_class": "scheduled_job"},
        reason="Run scheduled job",
        is_enabled=True,
        fire_count=3,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        tenant_id=uuid4(),
        heartbeat_enabled=True,
        primary_model_id=None,
    )
    session = _SequenceSession(
        [
            _ScalarsResult([trigger]),
            _ScalarsResult([agent]),
        ]
    )

    # RLS 阶段1: _handle_list_triggers now resolves the agent's tenant and opens
    # a tenant-scoped session. Yield the fake session (no real SET LOCAL, so the
    # result sequence is unchanged) and stub the tenant resolver. triggers.py
    # imports both at module level.
    async def _resolve(*_a, **_k):
        return agent.tenant_id

    monkeypatch.setattr(trigger_domain, "tenant_scoped_session", lambda *_a, **_k: session, raising=False)
    monkeypatch.setattr(trigger_domain, "resolve_tenant_for_agent", _resolve, raising=False)

    requester_id = uuid4()

    async def _load_requester(_db, user_id):
        assert user_id == requester_id
        return SimpleNamespace(id=requester_id, tenant_id=agent.tenant_id, role="member")

    async def _filter(_db, _user, *, triggers, **_kwargs):
        return [
            (
                item,
                SimpleNamespace(authority_source="resource_owner", operator_view=False),
            )
            for item in triggers
        ]

    monkeypatch.setattr(trigger_domain, "load_trigger_requester", _load_requester)
    monkeypatch.setattr(trigger_domain, "filter_authorized_triggers", _filter)

    result = await trigger_domain._handle_list_triggers(agent_id, user_id=requester_id)

    assert "Trigger Diagnostics" in result
    assert "agent_no_model_blocking_autonomy" in result


@pytest.mark.asyncio
async def test_list_triggers_filters_foreign_resource_before_render_and_diagnostics(monkeypatch):
    from app.services.agent_tool_domains import triggers as trigger_domain

    agent_id = uuid4()
    requester_id = uuid4()
    owned = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="owned-trigger",
        type="cron",
        config={"expr": "0 9 * * *", "created_by": str(requester_id)},
        reason="owned",
        is_enabled=True,
        fire_count=0,
    )
    foreign = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="foreign-trigger",
        type="cron",
        config={"expr": "0 10 * * *", "created_by": str(uuid4())},
        reason="foreign secret",
        is_enabled=True,
        fire_count=0,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=uuid4(), primary_model_id=uuid4())
    session = _SequenceSession([_ScalarsResult([owned, foreign]), _ScalarsResult([agent])])

    async def _resolve(*_args, **_kwargs):
        return agent.tenant_id

    async def _load_requester(_db, user_id):
        assert user_id == requester_id
        return SimpleNamespace(id=requester_id, tenant_id=agent.tenant_id, role="member")

    async def _filter(_db, _user, *, triggers, **_kwargs):
        assert triggers == [owned, foreign]
        return [(owned, SimpleNamespace(authority_source="resource_owner", operator_view=False))]

    audited = {}

    def _audit(*, agent, triggers):
        audited["agent"] = agent
        audited["triggers"] = triggers
        return {"findings": []}

    monkeypatch.setattr(trigger_domain, "tenant_scoped_session", lambda *_a, **_k: session)
    monkeypatch.setattr(trigger_domain, "resolve_tenant_for_agent", _resolve)
    monkeypatch.setattr(trigger_domain, "load_trigger_requester", _load_requester)
    monkeypatch.setattr(trigger_domain, "filter_authorized_triggers", _filter)
    monkeypatch.setattr("app.services.autonomous_audit.audit_agent_autonomy_snapshot", _audit)

    result = await trigger_domain._handle_list_triggers(agent_id, user_id=requester_id)

    assert "owned-trigger" in result
    assert "foreign-trigger" not in result
    assert "foreign secret" not in result
    assert audited["triggers"] == [owned]


@pytest.mark.asyncio
async def test_list_triggers_fails_closed_without_runtime_requester():
    from app.services.agent_tool_domains import triggers as trigger_domain

    result = await trigger_domain._handle_list_triggers(uuid4())

    assert "auth_or_permission" in result
