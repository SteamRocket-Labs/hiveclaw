from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self.values)


class _DB:
    def __init__(self, permissions):
        self.permissions = permissions

    async def execute(self, _statement):
        return _Result(self.permissions)


def _permission(
    agent_id,
    *,
    effect="allow",
    expires_at=None,
    revoked_at=None,
    governed=True,
    extra_conditions=None,
):
    conditions = {"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}} if governed else {}
    conditions.update(extra_conditions or {})
    return SimpleNamespace(
        resource_id=agent_id,
        actions=["operator.inspect"],
        conditions=conditions,
        effect=effect,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


@pytest.mark.asyncio
async def test_operator_inspection_is_independent_live_and_deny_aware():
    from app.core.permissions import load_agent_operator_inspection_ids

    now = datetime.now(timezone.utc)
    allowed_id = uuid4()
    denied_id = uuid4()
    expired_id = uuid4()
    revoked_id = uuid4()
    ungoverned_id = uuid4()
    constrained_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    permissions = [
        _permission(allowed_id, expires_at=now + timedelta(hours=1)),
        _permission(denied_id),
        _permission(denied_id, effect="deny"),
        _permission(expired_id, expires_at=now - timedelta(seconds=1)),
        _permission(revoked_id, revoked_at=now),
        _permission(ungoverned_id, governed=False),
        _permission(constrained_id, extra_conditions={"environment": "production"}),
    ]

    result = await load_agent_operator_inspection_ids(
        _DB(permissions),
        user=user,
        agent_ids=[allowed_id, denied_id, expired_id, revoked_id, ungoverned_id, constrained_id],
    )

    assert result == {allowed_id}


@pytest.mark.asyncio
async def test_operator_reachability_does_not_widen_generic_agent_access(monkeypatch):
    import app.core.permissions as permissions

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=agent.tenant_id)

    async def deny_generic_access(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No access to this agent")

    async def load_agent(*_args, **_kwargs):
        return agent

    async def allow_operator(*_args, **_kwargs):
        return True

    monkeypatch.setattr(permissions, "check_agent_access", deny_generic_access)
    monkeypatch.setattr(permissions, "_load_agent_for_user", load_agent)
    monkeypatch.setattr(permissions, "has_agent_operator_inspect", allow_operator)

    resolved_agent, access_level = await permissions.check_agent_operator_reachability(
        object(),
        user,
        agent.id,
    )

    assert resolved_agent is agent
    assert access_level == "operator"
    with pytest.raises(HTTPException) as exc:
        await permissions.check_agent_access(object(), user, agent.id)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_operator_inspection_audit_is_transactional_and_reason_is_not_semantically_scanned(monkeypatch):
    import app.core.permissions as permissions

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=agent.tenant_id)
    resource_id = uuid4()
    calls = []

    async def fake_has_grant(*_args, **_kwargs):
        return True

    async def fake_write_audit_event(_db, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(permissions, "has_agent_operator_inspect", fake_has_grant)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    source = await permissions.authorize_agent_operator_inspection(
        object(),
        user=user,
        agent=agent,
        reason="Review keyword: delete, secret, allow; this is still just audit context.",
        action="chat_session:read",
        resource_type="chat_session",
        resource_id=resource_id,
    )

    assert source == "operator_inspect_grant"
    assert calls[0]["resource_id"] == resource_id
    assert calls[0]["details"]["authority_source"] == "operator_inspect_grant"

    async def failing_write(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.core.policy.write_audit_event", failing_write)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await permissions.authorize_agent_operator_inspection(
            object(),
            user=user,
            agent=agent,
            reason="Incident review",
            action="chat_session:read",
            resource_type="chat_session",
            resource_id=resource_id,
        )


@pytest.mark.asyncio
async def test_operator_inspection_rejects_missing_reason_and_missing_grant(monkeypatch):
    import app.core.permissions as permissions

    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4(), tenant_id=agent.tenant_id)

    async def no_grant(*_args, **_kwargs):
        return False

    monkeypatch.setattr(permissions, "has_agent_operator_inspect", no_grant)

    for reason in (None, "   "):
        with pytest.raises(HTTPException) as exc:
            await permissions.authorize_agent_operator_inspection(
                object(),
                user=user,
                agent=agent,
                reason=reason,
                action="chat_session:read",
                resource_type="chat_session",
                resource_id=uuid4(),
            )
        assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await permissions.authorize_agent_operator_inspection(
            object(),
            user=user,
            agent=agent,
            reason="x" * 1001,
            action="chat_session:read",
            resource_type="chat_session",
            resource_id=uuid4(),
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await permissions.authorize_agent_operator_inspection(
            object(),
            user=user,
            agent=agent,
            reason="Incident review",
            action="chat_session:read",
            resource_type="chat_session",
            resource_id=uuid4(),
        )
    assert exc.value.status_code == 403
