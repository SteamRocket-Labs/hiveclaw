from __future__ import annotations

import contextlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import local_bridge_service as service


def test_local_agent_default_policies_are_action_deny_by_default() -> None:
    defaults = dict(service.LOCAL_AGENT_POLICY_DEFAULTS)
    assert defaults["local_agent.execute"] == (True, True)
    assert defaults["local_agent.file_download"] == (True, True)
    assert defaults["local_agent.file_upload"] == (True, True)
    assert defaults["local_agent.event_stream"] == (True, False)
    assert defaults["local_agent.result_report"] == (True, False)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _PolicyScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _PolicyResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _PolicyScalars(self._rows)


class _PolicyDB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _PolicyResult(self.rows)


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
    assert db.added[0].expires_at is not None
    lifetime = db.added[0].expires_at - service.utcnow()
    assert timedelta(days=29) < lifetime <= timedelta(days=service.DEFAULT_BRIDGE_TOKEN_TTL_DAYS)
    assert result["expires_at"] == db.added[0].expires_at.isoformat()
    assert result["expires_in"] > 0


@pytest.mark.asyncio
async def test_bridge_auth_rejects_legacy_permanent_token_without_expiry(monkeypatch) -> None:
    tenant_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=uuid4(),
        user_id=uuid4(),
        scopes=["local_agent:connect"],
        client_kind="hive-connect",
        device_name="Legacy Mac",
        status="active",
        expires_at=None,
        last_seen_at=None,
        last_seen_ip=None,
        last_seen_user_agent=None,
    )
    db = _FakeDB(connection)

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        assert reason == "local bridge bearer token lookup"
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(_session, pinned_tenant_id):
        assert pinned_tenant_id == tenant_id

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)

    with pytest.raises(service.HTTPException) as exc_info:
        await service.resolve_bridge_auth_context(db, authorization="Bearer hb_legacy")

    assert exc_info.value.status_code == 401
    assert connection.status == "expired"
    assert db.committed is True


@pytest.mark.asyncio
async def test_file_policy_resolver_prefers_agent_deny_over_tenant_allow() -> None:
    tenant_id = uuid4()
    agent_id = uuid4()
    tenant_policy = SimpleNamespace(agent_id=None, allowed=True)
    agent_policy = SimpleNamespace(agent_id=agent_id, allowed=False)

    with pytest.raises(service.HTTPException) as exc_info:
        await service.require_local_agent_capability_policy(
            _PolicyDB([tenant_policy, agent_policy]),
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability="local_agent.file_download",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Local Agent capability denied by live policy: local_agent.file_download"


@pytest.mark.asyncio
async def test_file_policy_resolver_fails_closed_for_unbound_connection() -> None:
    with pytest.raises(service.HTTPException) as exc_info:
        await service.require_local_agent_capability_policy(
            _PolicyDB([]),
            tenant_id=uuid4(),
            agent_id=None,
            capability="local_agent.file_upload",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Local bridge connection is not bound to an Agent"
