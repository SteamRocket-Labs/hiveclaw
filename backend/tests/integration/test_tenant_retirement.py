"""Real PostgreSQL proof for exact, replay-safe tenant identity retirement."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select, update

from app.api import auth as auth_api
from app.api import desktop_auth
from app.api.tenants import TenantRetirementRequest, delete_tenant
from app.core.security import create_access_token, create_refresh_token, get_current_user, hash_password
from app.database import pin_rls_tenant_context
from app.models.audit import AuditLog
from app.models.local_agent_channel import LocalAgentChannelWsTicket
from app.models.local_bridge import LocalAgentBridgeConnection, LocalAgentBridgePairingSession
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.schemas import UserLogin
from app.services.local_agent_channel_service import resolve_ws_ticket
from app.services.local_bridge_service import (
    exchange_pairing_session,
    hash_secret,
    normalize_user_code,
    resolve_bridge_auth_context,
)


async def test_platform_tenant_retirement_serializes_replay_and_revokes_login_authority(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    target_tenant_id = uuid.uuid4()
    fallback_tenant_id = uuid.uuid4()
    platform_admin_id = uuid.uuid4()
    last_admin_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    raw_bridge_token = f"hb_{uuid.uuid4().hex}"
    raw_ws_ticket = f"hbt_{uuid.uuid4().hex}"
    suffix = uuid.uuid4().hex[:10]
    request_id = f"retire-{suffix}"
    now = datetime.now(timezone.utc)
    last_admin_username = f"last-admin-{suffix}"
    last_admin_password = f"fixture-password-{suffix}"
    stale_access_token = create_access_token(
        str(last_admin_id),
        "org_admin",
        tenant_id=str(target_tenant_id),
    )

    async with owner_sessionmaker() as db:
        target_tenant = Tenant(
            id=target_tenant_id,
            name="Retirement Target",
            slug=f"retirement-target-{suffix}",
        )
        fallback_tenant = Tenant(
            id=fallback_tenant_id,
            name="Platform Home",
            slug=f"platform-home-{suffix}",
        )
        platform_admin = User(
            id=platform_admin_id,
            username=f"platform-owner-{suffix}",
            email=f"platform-owner-{suffix}@example.test",
            password_hash="x",
            display_name="Platform Owner",
            tenant_id=fallback_tenant_id,
            role="platform_admin",
        )
        last_admin = User(
            id=last_admin_id,
            username=last_admin_username,
            email=f"last-admin-{suffix}@example.test",
            password_hash=hash_password(last_admin_password),
            display_name="Last Company Admin",
            tenant_id=target_tenant_id,
            role="org_admin",
        )
        db.add_all([target_tenant, fallback_tenant, platform_admin, last_admin])
        await db.flush()
        raw_refresh_token = await create_refresh_token(db, last_admin_id, "fixture-device")
        db.add_all(
            [
                LocalAgentBridgeConnection(
                    id=connection_id,
                    tenant_id=target_tenant_id,
                    user_id=last_admin_id,
                    device_name="Fixture Mac",
                    client_kind="hive-connect",
                    device_fingerprint=f"fixture-{suffix}",
                    token_hash=hash_secret(raw_bridge_token),
                    scopes=["local_agent:connect"],
                    status="active",
                    expires_at=now + timedelta(days=1),
                ),
            ]
        )
        await db.flush()
        db.add(
            LocalAgentChannelWsTicket(
                tenant_id=target_tenant_id,
                user_id=last_admin_id,
                connection_id=connection_id,
                ticket_hash=hash_secret(raw_ws_ticket),
                scopes=["local_agent:connect"],
                expires_at=now + timedelta(minutes=5),
            )
        )
        await db.commit()

    async def retire_once():
        async with app_user_sessionmaker() as db:
            return await delete_tenant(
                tenant_id=target_tenant_id,
                retirement=TenantRetirementRequest(
                    expected_user_ids=[last_admin_id],
                    reason="Weekend RC fixture cleanup",
                    request_id=request_id,
                ),
                current_user=SimpleNamespace(
                    id=platform_admin_id,
                    role="platform_admin",
                    tenant_id=fallback_tenant_id,
                ),
                db=db,
            )

    first, second = await asyncio.gather(retire_once(), retire_once())
    assert {first.retirement_status, second.retirement_status} == {"retired", "already_retired"}
    receipt = first if first.retirement_status == "retired" else second
    assert receipt.retirement_request_id == request_id
    assert [row.user_id for row in receipt.retired_users] == [last_admin_id]
    assert receipt.retired_users[0].revocations["refresh_tokens"] == 1
    assert receipt.retired_users[0].revocations["local_bridge_connections"] == 1

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as password_error:
            await auth_api.login(
                UserLogin(username=last_admin_username, password=last_admin_password),
                db=db,
            )
    assert password_error.value.status_code == 403
    assert password_error.value.detail == "Account is disabled"

    request = Request({"type": "http", "method": "GET", "path": "/api/auth/me", "headers": []})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=stale_access_token)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, target_tenant_id)
        with pytest.raises(HTTPException) as access_error:
            await get_current_user(request=request, credentials=credentials, db=db)
    assert access_error.value.status_code == 401
    assert access_error.value.detail == "User not found or inactive"

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as refresh_error:
            await desktop_auth.exchange_refresh_token(
                desktop_auth.DesktopExchangeRequest(
                    refresh_token=raw_refresh_token,
                    device_id="fixture-device",
                ),
                db=db,
            )
    assert refresh_error.value.status_code == 401
    assert refresh_error.value.detail == "Invalid refresh token"

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as bridge_error:
            await resolve_bridge_auth_context(db, authorization=f"Bearer {raw_bridge_token}")
    assert bridge_error.value.status_code == 401
    assert bridge_error.value.detail == "Invalid bridge token"

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as ws_error:
            await resolve_ws_ticket(db, ticket=raw_ws_ticket)
    assert ws_error.value.status_code == 401
    assert ws_error.value.detail == "Invalid local agent channel ticket"

    async with owner_sessionmaker() as db:
        target_tenant = await db.get(Tenant, target_tenant_id)
        platform_admin = await db.get(User, platform_admin_id)
        last_admin = await db.get(User, last_admin_id)
        refresh_token = (
            await db.execute(select(RefreshToken).where(RefreshToken.user_id == last_admin_id))
        ).scalar_one()
        connection = await db.get(LocalAgentBridgeConnection, connection_id)
        ws_ticket = (
            await db.execute(
                select(LocalAgentChannelWsTicket).where(LocalAgentChannelWsTicket.connection_id == connection_id)
            )
        ).scalar_one()
        audit_count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == target_tenant_id,
                AuditLog.action == "tenant:retired",
                AuditLog.details["request_id"].as_string() == request_id,
            )
        )

    assert target_tenant is not None and target_tenant.is_active is False
    assert platform_admin is not None and platform_admin.is_active is True
    assert platform_admin.role == "platform_admin"
    assert platform_admin.tenant_id == fallback_tenant_id
    assert last_admin is not None and last_admin.is_active is False
    assert last_admin.role == "org_admin"
    assert last_admin.tenant_id == target_tenant_id
    assert refresh_token.revoked is True
    assert connection is not None and connection.status == "revoked"
    assert ws_ticket.consumed_at is not None
    assert audit_count == 1


@pytest.mark.parametrize("invalid_state", ["inactive_user", "wrong_tenant", "inactive_tenant"])
async def test_local_bridge_credentials_require_live_user_tenant_binding(
    owner_sessionmaker, invalid_state: str
) -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    raw_bridge_token = f"hb_{uuid.uuid4().hex}"
    raw_ws_ticket = f"hbt_{uuid.uuid4().hex}"
    suffix = uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)

    async with owner_sessionmaker() as db:
        tenant = Tenant(
            id=tenant_id,
            name="Bridge Tenant",
            slug=f"bridge-tenant-{suffix}",
        )
        other_tenant = Tenant(
            id=other_tenant_id,
            name="Other Tenant",
            slug=f"bridge-other-{suffix}",
        )
        user = User(
            id=user_id,
            username=f"bridge-user-{suffix}",
            email=f"bridge-user-{suffix}@example.test",
            password_hash="x",
            display_name="Bridge User",
            tenant_id=tenant_id,
            role="member",
        )
        db.add_all([tenant, other_tenant, user])
        await db.flush()
        db.add(
            LocalAgentBridgeConnection(
                id=connection_id,
                tenant_id=tenant_id,
                user_id=user_id,
                device_name="Fixture Mac",
                client_kind="hive-connect",
                device_fingerprint=f"fixture-{suffix}",
                token_hash=hash_secret(raw_bridge_token),
                scopes=["local_agent:connect"],
                status="active",
                expires_at=now + timedelta(days=1),
            )
        )
        await db.flush()
        db.add(
            LocalAgentChannelWsTicket(
                tenant_id=tenant_id,
                user_id=user_id,
                connection_id=connection_id,
                ticket_hash=hash_secret(raw_ws_ticket),
                scopes=["local_agent:connect"],
                expires_at=now + timedelta(minutes=5),
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        cached_user = await db.get(User, user_id)
        cached_tenant = await db.get(Tenant, tenant_id)
        assert cached_user is not None and cached_user.is_active is True
        assert cached_user.tenant_id == tenant_id
        assert cached_tenant is not None and cached_tenant.is_active is True
        if invalid_state == "inactive_user":
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
            assert cached_user.is_active is True
        elif invalid_state == "wrong_tenant":
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(tenant_id=other_tenant_id)
                .execution_options(synchronize_session=False)
            )
            assert cached_user.tenant_id == tenant_id
        else:
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
            assert cached_tenant.is_active is True

        with pytest.raises(HTTPException) as bearer_error:
            await resolve_bridge_auth_context(db, authorization=f"Bearer {raw_bridge_token}")
        assert bearer_error.value.status_code == 401
        assert bearer_error.value.detail == "Invalid bridge token"

        with pytest.raises(HTTPException) as ws_error:
            await resolve_ws_ticket(db, ticket=raw_ws_ticket)
        assert ws_error.value.status_code == 401
        assert ws_error.value.detail == "Local agent identity is inactive"
        connection = await db.get(LocalAgentBridgeConnection, connection_id)
        ws_ticket = (
            await db.execute(
                select(LocalAgentChannelWsTicket).where(LocalAgentChannelWsTicket.connection_id == connection_id)
            )
        ).scalar_one()
    assert connection is not None and connection.status == "active"
    assert ws_ticket.consumed_at is None


async def _pairing_row_is_locked(owner_sessionmaker, pairing_id: uuid.UUID) -> bool:
    """Observe on live PostgreSQL whether a pairing row lock is held.

    ``FOR UPDATE SKIP LOCKED`` returns the row only when NO other transaction
    holds it, so an empty result is direct evidence that an open transaction
    (here: the lifecycle route under test) locked exactly this row. The
    probe's own lock is released by the rollback before the session closes.
    """

    async with owner_sessionmaker() as db:
        result = await db.execute(
            select(LocalAgentBridgePairingSession.id)
            .where(LocalAgentBridgePairingSession.id == pairing_id)
            .with_for_update(skip_locked=True)
        )
        locked = result.scalar_one_or_none() is None
        await db.rollback()
        return locked


async def _tenant_row_is_locked(owner_sessionmaker, tenant_id: uuid.UUID) -> bool:
    """Same live-lock observation for a Tenant row (the retirement identity lock)."""

    async with owner_sessionmaker() as db:
        result = await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(skip_locked=True))
        locked = result.scalar_one_or_none() is None
        await db.rollback()
        return locked


async def _await_pairing_row_locked(
    owner_sessionmaker,
    pairing_id: uuid.UUID,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Block until the pairing row is observably held, bounded by a deadline.

    The prelock under test acquires its row locks within microseconds of the
    statement being issued, so the bounded poll only covers statement
    dispatch — a prelock that locks nothing (missing, FOR UPDATE dropped, or
    mis-scoped predicate) never satisfies it and fails the test.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if await _pairing_row_is_locked(owner_sessionmaker, pairing_id):
            return
        await asyncio.sleep(0.05)
    pytest.fail(
        f"The lifecycle route never locked its claimable pairing row {pairing_id}: "
        "the pairing prelock is missing, dropped its FOR UPDATE, or is scoped to "
        "the wrong tenant/user/status predicate."
    )


async def _seed_approved_pairing(
    owner_sessionmaker,
    app_user_sessionmaker,
    *,
    bind_agent: bool,
) -> tuple[uuid.UUID, uuid.UUID, str, str, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a tenant, an org-admin user, and an approved pairing.

    bind_agent routes the pairing through the real agent-binding approval
    path so its connection INSERT also takes the implicit Agent FK lock;
    otherwise the synthetic shape (agent_id NULL, Tenant/User FKs only) is
    used.

    Three extra pairing rows are planted with explicitly ORDERED ids so the
    race test can prove on live PostgreSQL that the retirement prelock
    locks exactly the tenant's claimable set — rows lock in ``id`` order, so
    once lower rows are passed their lock fate is final:

    * ``sentinel_other_tenant_id`` (lowest): a pending pairing in a
      DIFFERENT tenant — a prelock that dropped the tenant filter would
      lock it (the retirement scope runs under an audited bypass, so RLS
      would not mask that); the correct predicate must leave it lockable.
    * ``sentinel_claimed_id``: a claimed pairing in the target tenant — a
      prelock that dropped the status filter would lock it; the correct
      predicate must leave it lockable.
    * ``observer_pairing_id``: a second approved pairing in the target
      tenant that the racing exchange never touches — the prelock MUST lock
      it for the pairing→identity serialization to be real.

    Returns (tenant_id, user_id, user_code, device_code, observer_pairing_id,
    sentinel_other_tenant_id, sentinel_claimed_id).
    """
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    user_code = f"user-{suffix}"
    device_code = f"device-{suffix}"
    sentinel_other_tenant_code = f"user-other-tenant-{suffix}"
    sentinel_claimed_code = f"user-claimed-{suffix}"
    observer_code = f"user-observer-{suffix}"
    (
        sentinel_other_tenant_pairing_id,
        sentinel_claimed_pairing_id,
        observer_pairing_id,
        exchange_pairing_id,
    ) = sorted((uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))

    def pairing_row(
        *,
        pairing_id: uuid.UUID,
        code: str,
        status: str,
        tenant: uuid.UUID,
        user: uuid.UUID,
        device: str | None = None,
    ) -> LocalAgentBridgePairingSession:
        return LocalAgentBridgePairingSession(
            id=pairing_id,
            tenant_id=tenant,
            user_id=user,
            pairing_code_hash=hash_secret(normalize_user_code(code)),
            device_code_hash=hash_secret(device or f"device-{code}"),
            device_name=f"Exchange Race {code}",
            client_kind="hive-connect",
            device_fingerprint=f"race-{code}",
            scopes=["local_agent:connect"],
            status=status,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    async with owner_sessionmaker() as db:
        db.add_all(
            [
                Tenant(id=tenant_id, name="Exchange Race Tenant", slug=f"exchange-race-{suffix}"),
                Tenant(id=other_tenant_id, name="Exchange Race Other Tenant", slug=f"exchange-other-{suffix}"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                User(
                    id=user_id,
                    username=f"exchange-{suffix}",
                    email=f"exchange-{suffix}@example.test",
                    password_hash="x",
                    display_name="Exchange Member",
                    tenant_id=tenant_id,
                    role="org_admin",
                ),
                User(
                    id=other_user_id,
                    username=f"exchange-other-{suffix}",
                    email=f"exchange-other-{suffix}@example.test",
                    password_hash="x",
                    display_name="Exchange Other Member",
                    tenant_id=other_tenant_id,
                    role="org_admin",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                pairing_row(
                    pairing_id=exchange_pairing_id,
                    code=user_code,
                    status="pending",
                    tenant=tenant_id,
                    user=user_id,
                    device=device_code,
                ),
                pairing_row(
                    pairing_id=sentinel_other_tenant_pairing_id,
                    code=sentinel_other_tenant_code,
                    status="pending",
                    tenant=other_tenant_id,
                    user=other_user_id,
                ),
                pairing_row(
                    pairing_id=sentinel_claimed_pairing_id,
                    code=sentinel_claimed_code,
                    status="claimed",
                    tenant=tenant_id,
                    user=user_id,
                ),
                pairing_row(
                    pairing_id=observer_pairing_id,
                    code=observer_code,
                    status="approved",
                    tenant=tenant_id,
                    user=user_id,
                ),
            ]
        )
        await db.commit()

    from app.services import local_bridge_service as bridge_service

    if bind_agent:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            agent = await bridge_service.ensure_default_local_agent_for_pairing(
                db, user_code=user_code, user_id=user_id, tenant_id=tenant_id
            )
            await bridge_service.approve_pairing_session(
                db, user_code=user_code, user_id=user_id, tenant_id=tenant_id, agent_id=agent.id
            )
    else:
        async with app_user_sessionmaker() as db:
            await bridge_service.approve_pairing_session(db, user_code=user_code, user_id=user_id, tenant_id=tenant_id)
    return (
        tenant_id,
        user_id,
        user_code,
        device_code,
        observer_pairing_id,
        sentinel_other_tenant_pairing_id,
        sentinel_claimed_pairing_id,
    )


@pytest.mark.parametrize("invalid_state", ["inactive_user", "wrong_tenant", "inactive_tenant"])
async def test_exchange_denies_inactive_identity_before_token_or_connection(
    owner_sessionmaker,
    app_user_sessionmaker,
    invalid_state: str,
) -> None:
    """Real-PG negative proof for the exchange live-identity gate.

    An approved pairing alone is a past owner decision, not live authority:
    an inactive User, an inactive Tenant, or a changed membership must deny
    with the typed 403 before any token is returned or any connection row
    is written. The SQL semantics run against real PostgreSQL, not a fake
    that only matches SQL substrings.
    """
    (
        tenant_id,
        user_id,
        _user_code,
        device_code,
        _observer,
        _sentinel_other_tenant,
        _sentinel_claimed,
    ) = await _seed_approved_pairing(owner_sessionmaker, app_user_sessionmaker, bind_agent=False)
    other_tenant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=other_tenant_id, name="Moved Tenant", slug=f"moved-{uuid.uuid4().hex[:10]}"))
        await db.flush()
        if invalid_state == "inactive_user":
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
        elif invalid_state == "wrong_tenant":
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(tenant_id=other_tenant_id)
                .execution_options(synchronize_session=False)
            )
        else:
            await db.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
        await db.commit()

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as exchange_error:
            await exchange_pairing_session(db, device_code=device_code)
    assert exchange_error.value.status_code == 403
    assert exchange_error.value.detail == {"code": "pairing_identity_inactive", "status": "approved"}

    async with owner_sessionmaker() as db:
        connections = await db.scalar(
            select(func.count())
            .select_from(LocalAgentBridgeConnection)
            .where(LocalAgentBridgeConnection.tenant_id == tenant_id)
        )
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.device_code_hash == hash_secret(device_code)
                )
            )
        ).scalar_one()
    assert connections == 0
    assert pairing.status == "approved"
    assert pairing.connection_id is None


@pytest.mark.parametrize("variant", ["synthetic", "real_agent"])
async def test_exchange_and_retirement_serialize_on_pairing_locks_without_deadlock(
    owner_sessionmaker,
    app_user_sessionmaker,
    variant: str,
) -> None:
    """Exchange vs retirement is a lock-order serialization, never 40P01.

    The exchange holds its pairing row FOR UPDATE past the live-identity
    read; retirement previously locked Tenant/User/Agent rows first and then
    waited at its pairing UPDATE, so the connection INSERT's implicit FK
    KEY SHARE locks closed a real ABBA cycle (PostgreSQL SQLSTATE 40P01,
    reproduced 2026-09-04). Retirement now takes the tenant's claimable
    pairing rows FIRST, so an in-flight exchange either commits and its
    fresh connection is revoked by the subsequent retirement, or retirement
    commits first and the exchange re-reads a rejected pairing.
    The real_agent variant additionally covers the Agent FK lock path.
    """
    (
        tenant_id,
        user_id,
        user_code,
        device_code,
        observer_pairing_id,
        sentinel_other_tenant_pairing_id,
        sentinel_claimed_pairing_id,
    ) = await _seed_approved_pairing(owner_sessionmaker, app_user_sessionmaker, bind_agent=variant == "real_agent")
    if variant == "real_agent":
        # The retirement active-Agent blocker requires owned Agents to be
        # soft-deleted first; the Agent FK row itself still exists, so the
        # connection INSERT still takes its implicit KEY SHARE lock.
        from app.models.agent import Agent

        async with owner_sessionmaker() as db:
            await db.execute(
                update(Agent)
                .where(Agent.tenant_id == tenant_id)
                .values(deleted_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )
            await db.commit()

    home_tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home_tenant_id, name="Retirement Home", slug=f"home-{uuid.uuid4().hex[:10]}"))
        db.add(
            User(
                id=owner_id,
                username=f"retire-owner-{uuid.uuid4().hex[:10]}",
                email=f"retire-owner-{uuid.uuid4().hex[:10]}@example.test",
                password_hash="x",
                display_name="Retirement Owner",
                tenant_id=home_tenant_id,
                role="platform_admin",
            )
        )
        await db.commit()

    identity_read = asyncio.Event()
    release_exchange = asyncio.Event()
    retirement_at_pairing = asyncio.Event()

    async def exchange():
        async with app_user_sessionmaker() as db:
            original = db.execute

            async def execute(statement, *args, **kwargs):
                sql = str(statement).lower()
                if sql.startswith("select users.id") and "join tenants" in sql and "users.is_active" in sql:
                    identity_read.set()
                    await release_exchange.wait()
                return await original(statement, *args, **kwargs)

            db.execute = execute  # type: ignore[method-assign]
            return await exchange_pairing_session(db, device_code=device_code)

    async def retire():
        async with app_user_sessionmaker() as db:
            original = db.execute

            async def execute(statement, *args, **kwargs):
                sql = str(statement).lower()
                if "local_agent_bridge_pairing_sessions" in sql and ("for update" in sql or sql.startswith("update ")):
                    retirement_at_pairing.set()
                return await original(statement, *args, **kwargs)

            db.execute = execute  # type: ignore[method-assign]
            return await delete_tenant(
                tenant_id=tenant_id,
                retirement=TenantRetirementRequest(
                    expected_user_ids=[user_id],
                    reason="Weekend RC pairing race regression",
                    request_id=f"pairing-race-{uuid.uuid4().hex[:10]}",
                ),
                current_user=SimpleNamespace(id=owner_id, role="platform_admin", tenant_id=home_tenant_id),
                db=db,
            )

    exchange_task: asyncio.Task = asyncio.create_task(exchange())
    retirement_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(identity_read.wait(), timeout=10)
        retirement_task = asyncio.create_task(retire())
        await asyncio.wait_for(retirement_at_pairing.wait(), timeout=10)
        # Prove the retirement prelock locks the REAL intended row set on
        # live PostgreSQL — not merely that a pairing-shaped statement was
        # issued. An independent FOR UPDATE SKIP LOCKED session must observe
        # the observer pairing row as held (the racing exchange never
        # touches it), while the two mis-scope sentinels — another tenant's
        # pending pairing and this tenant's own claimed pairing — stay
        # lockable. Rows lock in id order, so the sentinels' fates are final
        # once the observer is observed locked. A missing prelock, a dropped
        # FOR UPDATE, or a wrong tenant/status predicate leaves the observer
        # lockable and fails here.
        await _await_pairing_row_locked(owner_sessionmaker, observer_pairing_id)
        assert not await _pairing_row_is_locked(owner_sessionmaker, sentinel_other_tenant_pairing_id), (
            "The retirement prelock locked another tenant's pairing row: the tenant scope was widened"
        )
        assert not await _pairing_row_is_locked(owner_sessionmaker, sentinel_claimed_pairing_id), (
            "The retirement prelock locked a non-claimable (claimed) pairing row: the status scope was widened"
        )
        # The route must still be INSIDE its prelock: the prelock cannot
        # complete while the exchange holds the highest-id pairing row, so
        # the Tenant identity lock cannot have been taken yet. If it has,
        # the route passed a prelock that locked nothing (or the wrong
        # rows) — the observer observation then came from the later
        # revocation UPDATE, not the prelock, and the pairing→identity
        # order is broken.
        assert not await _tenant_row_is_locked(owner_sessionmaker, tenant_id), (
            "The retirement route reached its Tenant identity lock before its pairing prelock completed"
        )
        release_exchange.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(exchange_task, retirement_task, return_exceptions=True), timeout=15
        )
    finally:
        release_exchange.set()
        pending = [exchange_task, *([retirement_task] if retirement_task is not None else [])]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert errors == [], [{"type": type(error).__name__, "message": str(error).splitlines()[0]} for error in errors]

    exchange_result, retirement_receipt = outcomes
    # The in-flight exchange wins its claim and finishes cleanly...
    assert exchange_result["status"] == "active"
    assert exchange_result["access_token"].startswith("hb_")
    # ...and the retirement that raced it still completes and revokes the
    # fresh connection in the same transaction: no unrevoked ghost remains.
    assert retirement_receipt.retirement_status == "retired"
    assert retirement_receipt.retired_users[0].revocations["local_bridge_connections"] == 1

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        connection = await db.get(LocalAgentBridgeConnection, pairing.connection_id)
        retired_user = await db.get(User, user_id)
        retired_tenant = await db.get(Tenant, tenant_id)
    assert pairing.status == "claimed"
    assert connection is not None and connection.status == "revoked"
    assert retired_user is not None and retired_user.is_active is False
    assert retired_tenant is not None and retired_tenant.is_active is False


async def test_retirement_first_leaves_no_claimable_pairing_for_exchange(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """When retirement wins, a later exchange sees a rejected pairing."""
    (
        tenant_id,
        user_id,
        _user_code,
        device_code,
        _observer,
        _sentinel_other_tenant,
        _sentinel_claimed,
    ) = await _seed_approved_pairing(owner_sessionmaker, app_user_sessionmaker, bind_agent=False)
    home_tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home_tenant_id, name="Retirement Home 2", slug=f"home2-{uuid.uuid4().hex[:10]}"))
        db.add(
            User(
                id=owner_id,
                username=f"retire2-owner-{uuid.uuid4().hex[:10]}",
                email=f"retire2-owner-{uuid.uuid4().hex[:10]}@example.test",
                password_hash="x",
                display_name="Retirement Owner",
                tenant_id=home_tenant_id,
                role="platform_admin",
            )
        )
        await db.commit()

    async with app_user_sessionmaker() as db:
        receipt = await delete_tenant(
            tenant_id=tenant_id,
            retirement=TenantRetirementRequest(
                expected_user_ids=[user_id],
                reason="Weekend RC retirement-first regression",
                request_id=f"retire-first-{uuid.uuid4().hex[:10]}",
            ),
            current_user=SimpleNamespace(id=owner_id, role="platform_admin", tenant_id=home_tenant_id),
            db=db,
        )
    assert receipt.retirement_status == "retired"

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as exchange_error:
            await exchange_pairing_session(db, device_code=device_code)
    assert exchange_error.value.status_code == 403
    assert exchange_error.value.detail == "Pairing request rejected"

    async with owner_sessionmaker() as db:
        connections = await db.scalar(
            select(func.count())
            .select_from(LocalAgentBridgeConnection)
            .where(LocalAgentBridgeConnection.tenant_id == tenant_id)
        )
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.device_code_hash == hash_secret(device_code)
                )
            )
        ).scalar_one()
    assert connections == 0
    assert pairing.status == "rejected"


async def _seed_fresh_unbound_retirement(
    owner_sessionmaker,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str, str, str]:
    """Seed the CC probe shape for retirement: a retiring tenant with one
    member, a platform-admin home tenant, and a QUARANTINE-held pending
    pairing that retirement's pairing prelock and revocation UPDATE cannot
    see (user_id NULL, quarantine scope), so only the approve-time
    live-identity gate can stop a fresh binding onto a retired identity.

    Returns (tenant_id, user_id, home_tenant_id, owner_id, user_code,
    device_code, device_name).
    """
    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID, TENANT_SCOPE_QUARANTINE_SLUG

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    home_tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    user_code = f"user-{suffix}"
    device_code = f"device-{suffix}"
    device_name = f"Fresh Retirement {suffix[:8]}"
    async with owner_sessionmaker() as db:
        db.add_all(
            [
                Tenant(id=tenant_id, name="Fresh Retirement Tenant", slug=f"fresh-retire-{suffix[:10]}"),
                Tenant(id=home_tenant_id, name="Fresh Retirement Home", slug=f"fresh-retire-home-{suffix[:10]}"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                User(
                    id=user_id,
                    username=f"fresh-retire-{suffix[:10]}",
                    email=f"fresh-retire-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Fresh Retirement Member",
                    tenant_id=tenant_id,
                    role="org_admin",
                ),
                User(
                    id=owner_id,
                    username=f"fresh-retire-owner-{suffix[:10]}",
                    email=f"fresh-retire-owner-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Fresh Retirement Owner",
                    tenant_id=home_tenant_id,
                    role="platform_admin",
                ),
            ]
        )
        await db.commit()
    async with owner_sessionmaker() as db:
        # The quarantine scope row mirrors create_pairing_session's seed; it
        # may already exist when an earlier unbound-pairing test ran.
        if await db.get(Tenant, TENANT_SCOPE_QUARANTINE_ID) is None:
            db.add(
                Tenant(id=TENANT_SCOPE_QUARANTINE_ID, name="Tenant Scope Quarantine", slug=TENANT_SCOPE_QUARANTINE_SLUG)
            )
            await db.flush()
        db.add(
            LocalAgentBridgePairingSession(
                tenant_id=TENANT_SCOPE_QUARANTINE_ID,
                user_id=None,
                pairing_code_hash=hash_secret(normalize_user_code(user_code)),
                device_code_hash=hash_secret(device_code),
                device_name=device_name,
                client_kind="hive-connect",
                device_fingerprint=f"fresh-retirement-{suffix}",
                scopes=["local_agent:connect"],
                status="pending",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                metadata_json={
                    "tenant_binding": "unbound_pending_pairing",
                    "holding_scope": TENANT_SCOPE_QUARANTINE_SLUG,
                },
            )
        )
        await db.commit()
    return tenant_id, user_id, home_tenant_id, owner_id, user_code, device_code, device_name


async def test_fresh_unbound_approval_cannot_bind_retired_identity(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """A fresh approval must not commit onto an already-retired identity.

    Deterministic interleaving with two statement-completion barriers: the
    retirement route parks right after its global Tenant FOR UPDATE, the
    real user-level approve route parks right after its pairing FOR UPDATE
    loader, retirement then commits first. The approve request's Agent /
    Participant / ai-asset bootstrap is flushed but uncommitted at that
    point, so the approve-time live-identity gate must roll the whole
    request back typed — no approved pairing for the retired member, no
    orphan Agent rows, and a truthful pending poll for the device.
    """
    from app.api.local_bridge import approve_current_user_bridge_pairing
    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
    from app.models.agent import Agent
    from app.models.ai_asset import AIAssetRecord
    from app.models.capability_policy import CapabilityPolicy
    from app.models.participant import Participant

    (
        tenant_id,
        user_id,
        home_tenant_id,
        owner_id,
        user_code,
        device_code,
        device_name,
    ) = await _seed_fresh_unbound_retirement(owner_sessionmaker)

    retirement_holds_tenants = asyncio.Event()
    release_retirement = asyncio.Event()
    approval_loaded_pairing = asyncio.Event()
    release_approval = asyncio.Event()

    async def retire():
        async with app_user_sessionmaker() as db:
            original = db.execute

            async def execute(statement, *args, **kwargs):
                result = await original(statement, *args, **kwargs)
                sql = str(statement).lower()
                if not retirement_holds_tenants.is_set() and sql.startswith("select tenants") and "for update" in sql:
                    retirement_holds_tenants.set()
                    await release_retirement.wait()
                return result

            db.execute = execute  # type: ignore[method-assign]
            return await delete_tenant(
                tenant_id=tenant_id,
                retirement=TenantRetirementRequest(
                    expected_user_ids=[user_id],
                    reason="Weekend RC fresh approval retirement race regression",
                    request_id=f"fresh-retire-{uuid.uuid4().hex[:10]}",
                ),
                current_user=SimpleNamespace(id=owner_id, role="platform_admin", tenant_id=home_tenant_id),
                db=db,
            )

    async def approve():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            original = db.execute

            async def execute(statement, *args, **kwargs):
                result = await original(statement, *args, **kwargs)
                sql = str(statement).lower()
                if (
                    not approval_loaded_pairing.is_set()
                    and "local_agent_bridge_pairing_sessions" in sql
                    and "for update" in sql
                ):
                    approval_loaded_pairing.set()
                    await release_approval.wait()
                return result

            db.execute = execute  # type: ignore[method-assign]
            return await approve_current_user_bridge_pairing(
                user_code=user_code,
                current_user=SimpleNamespace(id=user_id, role="org_admin", tenant_id=tenant_id),
                db=db,
            )

    retirement_task: asyncio.Task = asyncio.create_task(retire())
    approve_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(retirement_holds_tenants.wait(), timeout=10)
        approve_task = asyncio.create_task(approve())
        await asyncio.wait_for(approval_loaded_pairing.wait(), timeout=10)
        # Retirement commits while the approve request holds only its
        # quarantine pairing row — invisible to retirement's predicates.
        release_retirement.set()
        retirement_receipt = await asyncio.wait_for(retirement_task, timeout=20)
        release_approval.set()
        with pytest.raises(HTTPException) as approve_error:
            await asyncio.wait_for(approve_task, timeout=20)
    finally:
        release_retirement.set()
        release_approval.set()
        for task in (retirement_task, *([approve_task] if approve_task is not None else [])):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            retirement_task, *([approve_task] if approve_task is not None else []), return_exceptions=True
        )

    assert approve_error.value.status_code == 409
    assert approve_error.value.detail == {"code": "pairing_identity_inactive", "status": "pending"}
    assert retirement_receipt.retirement_status == "retired"

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        retired_user = await db.get(User, user_id)
        retired_tenant = await db.get(Tenant, tenant_id)
        agent_count = await db.scalar(select(func.count()).select_from(Agent).where(Agent.tenant_id == tenant_id))
        asset_count = await db.scalar(
            select(func.count()).select_from(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id)
        )
        policy_count = await db.scalar(
            select(func.count()).select_from(CapabilityPolicy).where(CapabilityPolicy.tenant_id == tenant_id)
        )
        orphan_participants = await db.scalar(
            select(func.count())
            .select_from(Participant)
            .where(Participant.type == "agent", Participant.display_name == device_name)
        )
    assert pairing.status == "pending", "the rebind was rolled back with the request"
    assert pairing.user_id is None
    assert pairing.tenant_id == TENANT_SCOPE_QUARANTINE_ID
    assert retired_user is not None and retired_user.is_active is False
    assert retired_tenant is not None and retired_tenant.is_active is False
    assert agent_count == 0, "ensure_default_local_agent_for_pairing rows survived as orphans"
    assert asset_count == 0
    assert policy_count == 0
    assert orphan_participants == 0

    async with app_user_sessionmaker() as db:
        poll = await exchange_pairing_session(db, device_code=device_code)
    assert poll == {"status": "pending", "interval": 3}


async def test_fresh_approval_before_retirement_is_rejected_by_retirement(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """When the fresh approval wins the race and commits first, the pairing
    becomes visible to retirement's revocation UPDATE and must end
    rejected, with the flushed Agent soft-deleted by the retirement."""

    from app.api.local_bridge import approve_current_user_bridge_pairing
    from app.models.agent import Agent

    (
        tenant_id,
        user_id,
        home_tenant_id,
        owner_id,
        user_code,
        device_code,
        _device_name,
    ) = await _seed_fresh_unbound_retirement(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        approved = await approve_current_user_bridge_pairing(
            user_code=user_code,
            current_user=SimpleNamespace(id=user_id, role="org_admin", tenant_id=tenant_id),
            db=db,
        )
    assert approved["status"] == "approved"
    agent_id = uuid.UUID(approved["agent_id"])

    # The retirement active-Agent blocker requires owned Agents to be
    # soft-deleted first (mirrors the race test's real_agent variant).
    async with owner_sessionmaker() as db:
        await db.execute(
            update(Agent)
            .where(Agent.tenant_id == tenant_id)
            .values(deleted_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    async with app_user_sessionmaker() as db:
        receipt = await delete_tenant(
            tenant_id=tenant_id,
            retirement=TenantRetirementRequest(
                expected_user_ids=[user_id],
                reason="Weekend RC fresh approval retirement regression",
                request_id=f"fresh-retire-win-{uuid.uuid4().hex[:10]}",
            ),
            current_user=SimpleNamespace(id=owner_id, role="platform_admin", tenant_id=home_tenant_id),
            db=db,
        )
    assert receipt.retirement_status == "retired"

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        connections = await db.scalar(
            select(func.count())
            .select_from(LocalAgentBridgeConnection)
            .where(LocalAgentBridgeConnection.tenant_id == tenant_id)
        )
        agent = await db.get(Agent, agent_id)
    assert pairing.status == "rejected"
    assert connections == 0
    assert agent is not None and agent.deleted_at is not None


async def _seed_platform_admin_owned_agent_tenant(
    owner_sessionmaker,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed the TENANT-RETIREMENT-ZOMBIE-AGENT-001 shape: a target tenant with
    one non-platform member plus an in-target platform admin who owns an
    active Agent, a separate platform-admin caller tenant, and the Agent row.

    Returns (tenant_id, member_id, in_tenant_platform_admin_id, agent_id,
    caller_id); the caller lives in its own tenant so retirement never
    rehomes the acting administrator.
    """
    from app.models.agent import Agent

    tenant_id = uuid.uuid4()
    caller_tenant_id = uuid.uuid4()
    member_id = uuid.uuid4()
    in_tenant_admin_id = uuid.uuid4()
    caller_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add_all(
            [
                Tenant(id=tenant_id, name="Zombie Target", slug=f"zombie-target-{suffix}"),
                Tenant(id=caller_tenant_id, name="Zombie Caller Home", slug=f"zombie-caller-{suffix}"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                User(
                    id=member_id,
                    username=f"zombie-member-{suffix}",
                    email=f"zombie-member-{suffix}@example.test",
                    password_hash="x",
                    display_name="Zombie Member",
                    tenant_id=tenant_id,
                    role="member",
                ),
                User(
                    id=in_tenant_admin_id,
                    username=f"zombie-admin-{suffix}",
                    email=f"zombie-admin-{suffix}@example.test",
                    password_hash="x",
                    display_name="In-target Platform Admin",
                    tenant_id=tenant_id,
                    role="platform_admin",
                ),
                User(
                    id=caller_id,
                    username=f"zombie-caller-{suffix}",
                    email=f"zombie-caller-{suffix}@example.test",
                    password_hash="x",
                    display_name="Retirement Caller",
                    tenant_id=caller_tenant_id,
                    role="platform_admin",
                ),
            ]
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Platform Admin Owned Agent",
                role_description="owned by the in-target platform admin",
                creator_id=in_tenant_admin_id,
                sponsor_user_id=in_tenant_admin_id,
                owner_user_id=in_tenant_admin_id,
                status="idle",
            )
        )
        await db.commit()
    return tenant_id, member_id, in_tenant_admin_id, agent_id, caller_id


async def test_retirement_refuses_while_platform_admin_owned_agent_is_active(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """TENANT-RETIREMENT-ZOMBIE-AGENT-001 (a): explicit retirement must refuse
    while ANY target-tenant Agent is not soft-deleted — including one owned by
    a platform admin who would otherwise be rehomed, stay active, and keep
    ``manage`` over a retired company's Agent. The refusal is the existing
    typed zero-effect response; nothing about the tenant, its users, or the
    Agent may change."""
    from app.models.agent import Agent

    tenant_id, member_id, in_tenant_admin_id, agent_id, caller_id = await _seed_platform_admin_owned_agent_tenant(
        owner_sessionmaker
    )

    async with app_user_sessionmaker() as db:
        with pytest.raises(HTTPException) as retirement_error:
            await delete_tenant(
                tenant_id=tenant_id,
                retirement=TenantRetirementRequest(
                    expected_user_ids=[member_id],
                    reason="Weekend RC zombie-agent retirement refusal",
                    request_id=f"zombie-refuse-{uuid.uuid4().hex[:10]}",
                ),
                current_user=SimpleNamespace(id=caller_id, role="platform_admin", tenant_id=None),
                db=db,
            )
    assert retirement_error.value.status_code == 409
    assert retirement_error.value.detail == {
        "code": "tenant_retirement_owned_agents_active",
        "agent_ids": [str(agent_id)],
    }

    async with owner_sessionmaker() as db:
        tenant = await db.get(Tenant, tenant_id)
        member = await db.get(User, member_id)
        in_tenant_admin = await db.get(User, in_tenant_admin_id)
        agent = await db.get(Agent, agent_id)
        retired_audit_count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.action == "tenant:retired")
        )

    assert tenant is not None and tenant.is_active is True
    assert member is not None and member.is_active is True
    assert member.tenant_id == tenant_id and member.role == "member"
    assert in_tenant_admin is not None and in_tenant_admin.is_active is True
    assert in_tenant_admin.tenant_id == tenant_id and in_tenant_admin.role == "platform_admin"
    assert agent is not None and agent.deleted_at is None and agent.status == "idle"
    assert retired_audit_count == 0


async def test_inactive_tenant_zombie_agent_is_unreachable_to_rehomed_platform_owner(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """TENANT-RETIREMENT-ZOMBIE-AGENT-001 (b): a deliberately pre-existing
    inactive-tenant zombie Agent (no-body delete leaves agents in place) must
    be unreachable through the shared point ``check_agent_access`` even for
    its rehomed, still-active platform-admin owner — same 404 as a missing
    Agent, without leaking that the retired Agent exists."""
    from fastapi import HTTPException as FastapiHTTPException

    from app.core.permissions import check_agent_access
    from app.models.agent import Agent

    tenant_id, _member_id, in_tenant_admin_id, agent_id, caller_id = await _seed_platform_admin_owned_agent_tenant(
        owner_sessionmaker
    )

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        owner = (
            await db.execute(select(User).where(User.id == in_tenant_admin_id, User.is_active.is_(True)))
        ).scalar_one()
        # Control: while the tenant is active, the platform-admin bypass
        # lookup resolves the owned Agent with manage authority.
        agent, access_level = await check_agent_access(db, owner, agent_id)
        assert str(agent.id) == str(agent_id)
        assert access_level == "manage"
        await db.rollback()

    # The plain no-body delete deactivates the company without touching its
    # Agents — the deliberate pre-existing zombie state.
    async with app_user_sessionmaker() as db:
        await delete_tenant(
            tenant_id=tenant_id,
            retirement=None,
            current_user=SimpleNamespace(id=caller_id, role="platform_admin", tenant_id=None),
            db=db,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        tenant = await db.get(Tenant, tenant_id)
        rehomed_owner = await db.get(User, in_tenant_admin_id)
        zombie_agent = await db.get(Agent, agent_id)
    assert tenant is not None and tenant.is_active is False
    assert rehomed_owner is not None and rehomed_owner.is_active is True
    assert rehomed_owner.tenant_id is not None and rehomed_owner.tenant_id != tenant_id
    assert zombie_agent is not None and zombie_agent.deleted_at is None

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, rehomed_owner.tenant_id)
        owner = (
            await db.execute(select(User).where(User.id == in_tenant_admin_id, User.is_active.is_(True)))
        ).scalar_one()
        with pytest.raises(FastapiHTTPException) as access_error:
            await check_agent_access(db, owner, agent_id)
        assert access_error.value.status_code == 404
        assert access_error.value.detail == "Agent not found"
