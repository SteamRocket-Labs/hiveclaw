from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import local_bridge_service as service


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, row):
        self.row = row
        self.in_bypass = False
        self.added = []
        self.committed = False

    async def execute(self, _statement):
        return _ScalarResult(self.row if self.in_bypass else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


@pytest.mark.asyncio
async def test_exchange_pairing_reads_approved_pairing_with_audited_bypass_then_pins_tenant(monkeypatch):
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    pairing = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        connection_id=None,
        device_name="Codex",
        client_kind="codex",
        device_fingerprint="fp",
        scopes=["local_agent:receive"],
        status="approved",
        expires_at=service.utcnow() + service.timedelta(minutes=5),
        claimed_at=None,
    )
    db = _FakeDB(pairing)
    bypass_reasons: list[str] = []
    pinned_tenants: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        assert session is db
        assert actor_id is None
        bypass_reasons.append(reason)
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(session, pinned_tenant_id):
        assert session is db
        pinned_tenants.append(str(pinned_tenant_id))

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)
    monkeypatch.setattr(service, "generate_bridge_token", lambda: "hb_test_token")

    result = await service.exchange_pairing_session(db, device_code="dev_secret")

    assert result["status"] == "active"
    assert result["access_token"] == "hb_test_token"
    assert pairing.status == "claimed"
    assert db.committed is True
    assert bypass_reasons == ["local bridge device-code pairing lookup"]
    assert pinned_tenants == [str(tenant_id)]
    assert db.added[0].tenant_id == tenant_id
    assert db.added[0].agent_id == agent_id
    assert db.added[0].user_id == user_id
