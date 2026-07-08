from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, stmt):
        if "SET LOCAL app.current_tenant_id" in str(stmt):
            return _ScalarResult(None)
        if not self._results:
            raise AssertionError(f"Unexpected execute() call: {stmt}")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


@pytest.mark.asyncio
async def test_list_capability_policies_uses_selected_tenant_scope_for_platform_admin() -> None:
    import app.api.capabilities as capabilities_api

    selected_tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    policy = SimpleNamespace(
        id=uuid4(),
        capability="external.web.search",
        agent_id=None,
        allowed=True,
        requires_approval=True,
        conditions={},
    )
    db = _FakeDB([_ListResult([policy])])

    result = await capabilities_api.list_capability_policies(
        tenant_id=str(selected_tenant_id),
        current_user=current_user,
        db=db,
    )

    assert result[0].capability == "external.web.search"


@pytest.mark.asyncio
async def test_upsert_capability_policy_writes_selected_tenant_scope_for_platform_admin(monkeypatch) -> None:
    import app.api.capabilities as capabilities_api

    selected_tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    policy = SimpleNamespace(
        id=uuid4(),
        tenant_id=selected_tenant_id,
        capability="external.web.search",
        agent_id=None,
        allowed=False,
        requires_approval=False,
        conditions={},
    )
    db = _FakeDB([_ScalarResult(policy)])
    audit_calls = []

    async def fake_write_audit_event(*_args, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    result = await capabilities_api.upsert_capability_policy(
        data=capabilities_api.CapabilityPolicyUpdate(
            capability="external.web.search",
            allowed=True,
            requires_approval=True,
            conditions={},
        ),
        tenant_id=str(selected_tenant_id),
        current_user=current_user,
        db=db,
    )

    assert result.capability == "external.web.search"
    assert policy.allowed is True
    assert policy.requires_approval is True
    assert audit_calls[0]["tenant_id"] == selected_tenant_id
    assert db.committed is True


@pytest.mark.asyncio
async def test_capability_factor_api_threads_agent_access_and_tenant(monkeypatch) -> None:
    import app.api.capabilities as capabilities_api

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    current_user = SimpleNamespace(id=user_id, role="admin", tenant_id=tenant_id)
    db = _FakeDB([])
    access_calls = []
    service_calls = []

    async def fake_check_agent_access(db_arg, user_arg, agent_id_arg):
        access_calls.append((db_arg, user_arg, agent_id_arg))

    async def fake_capture(db_arg, *, tenant_id, originating_agent_id, originating_user_id, data):
        service_calls.append((db_arg, tenant_id, originating_agent_id, originating_user_id, data))
        return {"factor": {"id": "factor-1", "factor_kind": data["factor_kind"]}, "review": {"id": "review-1"}}

    monkeypatch.setattr(capabilities_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(capabilities_api, "capture_capability_factor", fake_capture)

    result = await capabilities_api.create_agent_capability_factor(
        agent_id=agent_id,
        data=capabilities_api.CapabilityFactorIn(
            factor_kind="skill_candidate",
            display_name="Research Skill",
            summary="Agent generated a reusable research skill.",
        ),
        current_user=current_user,
        db=db,
    )

    assert result["factor"]["id"] == "factor-1"
    assert access_calls == [(db, current_user, agent_id)]
    assert service_calls[0][1:4] == (tenant_id, agent_id, user_id)


@pytest.mark.asyncio
async def test_capability_promotion_api_threads_admin_decision(monkeypatch) -> None:
    import app.api.capabilities as capabilities_api

    selected_tenant_id = uuid4()
    proposal_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    db = _FakeDB([])
    decisions = []

    async def fake_resolve(db_arg, user_arg, tenant_id_arg):
        assert db_arg is db
        assert user_arg is current_user
        assert tenant_id_arg == str(selected_tenant_id)
        return selected_tenant_id

    async def fake_decide(db_arg, *, tenant_id, proposal_id, approver_id, decision, reason, resulting_snapshot_id=None):
        decisions.append((db_arg, tenant_id, proposal_id, approver_id, decision, reason, resulting_snapshot_id))
        return {"proposal": {"id": str(proposal_id), "decision": decision}}

    monkeypatch.setattr(capabilities_api, "resolve_and_pin_tenant_scope", fake_resolve)
    monkeypatch.setattr(capabilities_api, "decide_promotion_proposal", fake_decide)

    result = await capabilities_api.approve_capability_promotion_proposal(
        proposal_id=proposal_id,
        data=capabilities_api.PromotionDecisionIn(reason="ready", resulting_snapshot_id=uuid4()),
        tenant_id=str(selected_tenant_id),
        current_user=current_user,
        db=db,
    )

    assert result["proposal"]["decision"] == "approved"
    assert decisions[0][1:5] == (selected_tenant_id, proposal_id, current_user.id, "approved")
