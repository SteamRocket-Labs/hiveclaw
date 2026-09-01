from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Result:
    def __init__(self, permissions):
        self.permissions = permissions

    def scalars(self):
        return SimpleNamespace(all=lambda: self.permissions)


class _DB:
    def __init__(self, permissions):
        self.permissions = permissions
        self.executed = False

    async def execute(self, _statement):
        self.executed = True
        return _Result(self.permissions)


def _permission(*, effect="allow", expires_at=None, revoked_at=None, conditions=None):
    return SimpleNamespace(
        actions=["read"],
        conditions={} if conditions is None else conditions,
        effect=effect,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


@pytest.mark.asyncio
async def test_resource_permission_deny_precedes_live_allow_and_ignores_inactive_rows():
    from app.core.policy import check_permission

    now = datetime.now(timezone.utc)
    principal_id = uuid4()
    db = _DB(
        [
            _permission(effect="allow"),
            _permission(effect="deny"),
            _permission(effect="deny", expires_at=now - timedelta(seconds=1)),
            _permission(effect="deny", revoked_at=now),
        ]
    )

    allowed = await check_permission(
        db,
        principal_type="user",
        principal_id=principal_id,
        additional_principals=[("department", uuid4())],
        resource_type="task",
        resource_id=uuid4(),
        action="read",
        context={"tenant_id": str(uuid4())},
    )

    assert allowed is False


def test_resource_permission_allow_requires_live_matching_row():
    from app.core.policy import permission_allows

    now = datetime.now(timezone.utc)
    assert permission_allows(_permission(), action="read") is True
    assert permission_allows(_permission(effect="deny"), action="read") is False
    assert permission_allows(_permission(revoked_at=now), action="read") is False
    assert (
        permission_allows(
            _permission(expires_at=now - timedelta(seconds=1)),
            action="read",
        )
        is False
    )
    assert (
        permission_allows(
            _permission(conditions={"environment": "production"}),
            action="read",
            context={"environment": "staging"},
        )
        is False
    )
    assert (
        permission_allows(
            _permission(conditions={"ip_ranges": ["10.0.0.0/8"]}),
            action="read",
            context={"ip_address": "10.2.3.4"},
        )
        is True
    )
    assert (
        permission_allows(
            _permission(conditions={"ip_ranges": ["10.0.0.0/8"]}),
            action="read",
            context={"ip_address": "192.0.2.1"},
        )
        is False
    )
    assert (
        permission_allows(
            _permission(conditions={"unknown_condition": True}),
            action="read",
        )
        is False
    )
    for malformed_conditions in (["environment"], [], "", 0, False):
        assert permission_allows(_permission(conditions=malformed_conditions), action="read") is False
    assert (
        permission_allows(
            _permission(conditions={"time_range": {"start": "25:00", "end": "26:00"}}),
            action="read",
        )
        is False
    )


@pytest.mark.asyncio
async def test_resource_permission_invalid_tenant_context_fails_closed():
    from app.core.policy import check_permission

    db = _DB([_permission()])
    allowed = await check_permission(
        db,
        principal_type="user",
        principal_id=uuid4(),
        resource_type="task",
        resource_id=uuid4(),
        action="read",
        context={"tenant_id": "not-a-uuid"},
    )

    assert allowed is False
    assert db.executed is False
