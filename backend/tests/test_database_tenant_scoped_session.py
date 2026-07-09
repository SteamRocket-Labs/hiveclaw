from __future__ import annotations

import pytest


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_tenant_scoped_session_require_tenant_blocks_missing_tenant(monkeypatch) -> None:
    from app.database import tenant_scoped_session
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError

    fake_session = _FakeSession()
    opened = False

    def _factory():
        nonlocal opened
        opened = True
        return fake_session

    async def _pin(_session, _tenant_id):
        raise AssertionError("pin_rls_tenant_context should not run when tenant is required but missing")

    monkeypatch.setattr("app.database.pin_rls_tenant_context", _pin)

    with pytest.raises(RuntimeTenantPreconditionError) as exc:
        async with tenant_scoped_session(None, session_factory=_factory, require_tenant=True, source="heartbeat"):
            raise AssertionError("session body should not run")

    assert opened is False
    assert exc.value.reason_code == "tenant_required"
    assert exc.value.status == "blocked_precondition"
    assert exc.value.source == "heartbeat"


@pytest.mark.asyncio
async def test_tenant_scoped_session_default_still_allows_fail_closed_none(monkeypatch) -> None:
    from app.database import tenant_scoped_session

    fake_session = _FakeSession()
    pinned_values: list[object] = []

    def _factory():
        return fake_session

    async def _pin(_session, tenant_id):
        pinned_values.append(tenant_id)
        return None

    monkeypatch.setattr("app.database.pin_rls_tenant_context", _pin)

    async with tenant_scoped_session(None, session_factory=_factory) as session:
        assert session is fake_session

    assert pinned_values == [None]
    assert fake_session.commits == 1
