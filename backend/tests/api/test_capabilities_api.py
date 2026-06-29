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
