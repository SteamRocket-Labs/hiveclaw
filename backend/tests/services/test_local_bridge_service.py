from __future__ import annotations

import contextlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Update

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


class _UpdateResult:
    """Models a production UPDATE result (rowcount semantics)."""

    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeDB:
    def __init__(self, row):
        self.row = row
        self.in_bypass = False
        self.added = []
        self.committed = False
        self.executed_statements = []

    async def execute(self, statement):
        self.executed_statements.append(statement)
        if isinstance(statement, Update):
            # Model the claim UPDATE semantics: the row is claimed only when
            # it was approved (mirrors the production status predicate), and
            # the SET values are applied to the row like a real UPDATE.
            applies = getattr(self.row, "status", None) == "approved"
            if applies:
                for key, value in statement.compile().params.items():
                    if hasattr(self.row, key):
                        setattr(self.row, key, value)
            return _UpdateResult(1 if applies else 0)
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


class _LiveBridgeAuthDB(_FakeDB):
    def __init__(self, connection, *, user, tenant):
        super().__init__(connection)
        self.user = user
        self.tenant = tenant

    async def execute(self, statement):
        self.executed_statements.append(statement)
        if not self.in_bypass:
            return _ScalarResult(None)
        sql = str(statement).lower()
        has_live_identity_gate = (
            "join users" in sql
            and "join tenants" in sql
            and "users.is_active" in sql
            and "tenants.is_active" in sql
            and "users.tenant_id = local_agent_bridge_connections.tenant_id" in sql
        )
        if not has_live_identity_gate:
            return _ScalarResult(self.row)
        is_live = (
            self.user.id == self.row.user_id
            and self.user.is_active
            and self.user.tenant_id == self.row.tenant_id
            and self.tenant.id == self.row.tenant_id
            and self.tenant.is_active
        )
        return _ScalarResult(self.row if is_live else None)


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
    # The claim is a status-predicated UPDATE with rowcount semantics.
    update_statements = [s for s in db.executed_statements if isinstance(s, Update)]
    assert len(update_statements) == 1
    claim_statement = update_statements[0]
    where_sql = str(claim_statement.whereclause)
    assert "id" in where_sql and "status" in where_sql
    claim_params = claim_statement.compile().params
    assert claim_params["status"] == "claimed"
    assert claim_params.get("status_1") == "approved"
    assert db.committed is True
    assert bypass_reasons == [
        "local bridge device-code pairing lookup",
        "local bridge pairing live identity check",
    ]
    assert pinned_tenants == [str(tenant_id)]
    assert db.added[0].tenant_id == tenant_id
    assert db.added[0].agent_id == agent_id
    assert db.added[0].user_id == user_id
    assert db.added[0].expires_at is not None
    lifetime = db.added[0].expires_at - service.utcnow()
    assert timedelta(days=29) < lifetime <= timedelta(days=service.DEFAULT_BRIDGE_TOKEN_TTL_DAYS)
    assert result["expires_at"] == db.added[0].expires_at.isoformat()
    assert result["expires_in"] > 0


class _LivePairingIdentityDB(_FakeDB):
    """Routes the exchange live-identity gate like the production join."""

    def __init__(self, row, *, user, tenant):
        super().__init__(row)
        self.user = user
        self.tenant = tenant

    async def execute(self, statement):
        self.executed_statements.append(statement)
        if isinstance(statement, Update):
            return await super().execute(statement)
        sql = str(statement).lower()
        if "join tenants" in sql and "users.is_active" in sql and "tenants.is_active" in sql:
            live = (
                self.user.id == self.row.user_id
                and self.user.is_active
                and self.user.tenant_id == self.row.tenant_id
                and self.tenant.id == self.row.tenant_id
                and self.tenant.is_active
            )
            return _ScalarResult(self.user.id if live else None)
        return await super().execute(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_active,tenant_active,membership_matches,denied",
    [
        (True, True, True, False),
        (False, True, True, True),
        (True, False, True, True),
        (True, True, False, True),
    ],
)
async def test_exchange_pairing_revalidates_live_identity_before_issuance(
    monkeypatch,
    user_active,
    tenant_active,
    membership_matches,
    denied,
):
    tenant_id = uuid4()
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        is_active=user_active,
        tenant_id=tenant_id if membership_matches else uuid4(),
    )
    tenant = SimpleNamespace(id=tenant_id, is_active=tenant_active)
    pairing = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=uuid4(),
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
    db = _LivePairingIdentityDB(pairing, user=user, tenant=tenant)

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(_session, _pinned_tenant_id):
        return None

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)
    monkeypatch.setattr(service, "generate_bridge_token", lambda: "hb_test_token")

    if denied:
        with pytest.raises(service.HTTPException) as excinfo:
            await service.exchange_pairing_session(db, device_code="dev_secret")

        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == {"code": "pairing_identity_inactive", "status": "approved"}
        # No token issuance, connection mutation or claim happened.
        assert db.added == []
        assert db.committed is False
        assert not [statement for statement in db.executed_statements if isinstance(statement, Update)]
        assert pairing.status == "approved"
        return

    result = await service.exchange_pairing_session(db, device_code="dev_secret")

    assert result["status"] == "active"
    assert result["access_token"] == "hb_test_token"
    assert pairing.status == "claimed"
    identity_sql = " ".join(
        str(statement).lower()
        for statement in db.executed_statements
        if not isinstance(statement, Update) and "join tenants" in str(statement).lower()
    )
    assert "users.is_active" in identity_sql
    assert "tenants.is_active" in identity_sql


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
@pytest.mark.parametrize("invalid_state", ["inactive_user", "wrong_tenant", "inactive_tenant"])
async def test_bridge_auth_requires_live_user_tenant_binding(monkeypatch, invalid_state: str) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=uuid4(),
        user_id=user_id,
        scopes=["local_agent:connect"],
        client_kind="hive-connect",
        device_name="Fixture Mac",
        status="active",
        expires_at=service.utcnow() + service.timedelta(minutes=5),
        last_seen_at=None,
        last_seen_ip=None,
        last_seen_user_agent=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    tenant = SimpleNamespace(id=tenant_id, is_active=True)
    if invalid_state == "inactive_user":
        user.is_active = False
    elif invalid_state == "wrong_tenant":
        user.tenant_id = uuid4()
    else:
        tenant.is_active = False
    db = _LiveBridgeAuthDB(connection, user=user, tenant=tenant)
    pinned_tenants = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        assert session is db
        assert reason == "local bridge bearer token lookup"
        assert actor_id is None
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(_session, pinned_tenant_id):
        pinned_tenants.append(pinned_tenant_id)

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)

    with pytest.raises(service.HTTPException) as exc_info:
        await service.resolve_bridge_auth_context(db, authorization="Bearer hb_fixture")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid bridge token"
    assert pinned_tenants == []
    assert connection.last_seen_at is None


@pytest.mark.asyncio
async def test_bridge_auth_returns_context_for_live_exact_identity(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    connection = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        scopes=["local_agent:connect"],
        client_kind="hive-connect",
        device_name="Fixture Mac",
        status="active",
        expires_at=service.utcnow() + service.timedelta(minutes=5),
        last_seen_at=None,
        last_seen_ip=None,
        last_seen_user_agent=None,
    )
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    tenant = SimpleNamespace(id=tenant_id, is_active=True)
    db = _LiveBridgeAuthDB(connection, user=user, tenant=tenant)
    pinned_tenants = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        assert session is db
        assert reason == "local bridge bearer token lookup"
        assert actor_id is None
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(_session, pinned_tenant_id):
        pinned_tenants.append(pinned_tenant_id)

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)

    context = await service.resolve_bridge_auth_context(
        db,
        authorization="Bearer hb_fixture",
        last_seen_ip="127.0.0.1",
        user_agent="pytest",
    )

    assert context.connection_id == connection.id
    assert context.tenant_id == tenant_id
    assert context.user_id == user_id
    assert context.agent_id == agent_id
    assert connection.last_seen_at is not None
    assert connection.last_seen_ip == "127.0.0.1"
    assert connection.last_seen_user_agent == "pytest"
    assert pinned_tenants == [tenant_id]


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


class _ApprovePairingDB(_FakeDB):
    """Routes the approve rebind UPDATE and live-identity gate like production."""

    def __init__(self, row, *, user, tenant):
        super().__init__(row)
        self.user = user
        self.tenant = tenant
        self.rolled_back = False
        self._before_rebind: dict[str, object] | None = None

    async def execute(self, statement):
        self.executed_statements.append(statement)
        if isinstance(statement, Update):
            # The rebind applies only while the pairing is still pending,
            # mirroring the production status predicate; the pre-image is
            # kept so rollback() can model the database undoing it.
            applies = getattr(self.row, "status", None) == "pending"
            if applies:
                self._before_rebind = dict(vars(self.row))
                for key, value in statement.compile().params.items():
                    if hasattr(self.row, key):
                        setattr(self.row, key, value)
            return _UpdateResult(1 if applies else 0)
        sql = str(statement).lower()
        if "join tenants" in sql and "users.is_active" in sql and "tenants.is_active" in sql:
            live = (
                self.user.id == self.row.user_id
                and self.user.is_active
                and self.user.tenant_id == self.row.tenant_id
                and self.tenant.id == self.row.tenant_id
                and self.tenant.is_active
            )
            return _ScalarResult(self.user.id if live else None)
        return await super().execute(statement)

    async def rollback(self):
        self.rolled_back = True
        self.committed = False
        if self._before_rebind is not None:
            for key, value in self._before_rebind.items():
                setattr(self.row, key, value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_active,tenant_active,membership_matches,denied",
    [
        (True, True, True, False),
        (False, True, True, True),
        (True, False, True, True),
        (True, True, False, True),
    ],
)
async def test_approve_pairing_revalidates_live_identity_before_commit(
    monkeypatch,
    user_active,
    tenant_active,
    membership_matches,
    denied,
):
    """A pending pairing cannot be rebound onto an inactive or nonmember identity.

    The rebind UPDATE and the whole ensure-default Agent bootstrap are
    uncommitted at the gate, so the typed 409 must also roll the request
    back instead of leaving a half-approved binding behind.
    """
    tenant_id = uuid4()
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        is_active=user_active,
        tenant_id=tenant_id if membership_matches else uuid4(),
    )
    tenant = SimpleNamespace(id=tenant_id, is_active=tenant_active)
    pairing = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        agent_id=None,
        user_id=None,
        connection_id=None,
        device_name="Codex",
        client_kind="codex",
        device_fingerprint="fp",
        scopes=["local_agent:receive"],
        status="pending",
        approved_at=None,
        metadata_json={"tenant_binding": "unbound_pending_pairing"},
        expires_at=service.utcnow() + service.timedelta(minutes=5),
        claimed_at=None,
    )
    db = _ApprovePairingDB(pairing, user=user, tenant=tenant)

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason: str, actor_id: str | None = None):
        db.in_bypass = True
        try:
            yield session
        finally:
            db.in_bypass = False

    async def fake_pin_rls_tenant_context(_session, _pinned_tenant_id):
        return None

    monkeypatch.setattr(service, "enter_rls_bypass", fake_enter_rls_bypass)
    monkeypatch.setattr(service, "pin_rls_tenant_context", fake_pin_rls_tenant_context)

    if denied:
        with pytest.raises(service.HTTPException) as excinfo:
            await service.approve_pairing_session(db, user_code="code_secret", user_id=user_id, tenant_id=tenant_id)

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == {"code": "pairing_identity_inactive", "status": "pending"}
        # The uncommitted rebind was rolled back with the request: the
        # pairing stays pending and unbound, never approved for the
        # inactive/nonmember identity.
        assert db.rolled_back is True
        assert db.committed is False
        assert pairing.status == "pending"
        assert pairing.user_id is None
        assert pairing.tenant_id is None
        return

    result = await service.approve_pairing_session(db, user_code="code_secret", user_id=user_id, tenant_id=tenant_id)

    assert result["status"] == "approved"
    assert db.committed is True
    assert db.rolled_back is False
    assert pairing.status == "approved"
    assert pairing.user_id == user_id
    assert pairing.tenant_id == tenant_id
