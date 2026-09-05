"""Real PostgreSQL proof for atomic, replay-safe User offboarding."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.users import UserOffboardingRequest, offboard_user
from app.database import pin_rls_tenant_context, tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
from app.models.ai_asset import AIAssetRecord
from app.models.audit import ApprovalRequest, AuditLog
from app.models.local_bridge import LocalAgentBridgeConnection, LocalAgentBridgePairingSession
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.user import User
from app.services.local_bridge_service import (
    exchange_pairing_session,
    hash_secret,
    normalize_user_code,
)
from app.services.user_offboarding_service import find_user_offboarding_replay, offboard_loaded_user


async def test_user_offboarding_transfers_all_agents_and_replays_receipt(owner_sessionmaker) -> None:
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    agent_ids = [uuid.uuid4(), uuid.uuid4()]
    queued_runtime_task_id = uuid.uuid4()
    running_runtime_task_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    business_task_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Offboarding Tenant", slug=f"offboarding-{suffix}"))
        target = User(
            id=target_id,
            username=f"departing-{suffix}",
            email=f"departing-{suffix}@example.test",
            password_hash="x",
            display_name="Departing Member",
            tenant_id=tenant_id,
            role="member",
        )
        successor = User(
            id=successor_id,
            username=f"admin-{suffix}",
            email=f"admin-{suffix}@example.test",
            password_hash="x",
            display_name="Company Admin",
            tenant_id=tenant_id,
            role="org_admin",
        )
        db.add_all([target, successor])
        await db.flush()
        for index, agent_id in enumerate(agent_ids):
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    creator_id=target_id,
                    sponsor_user_id=target_id,
                    owner_user_id=target_id,
                    name=f"Departing Agent {index}",
                    role_description="Offboarding integration proof",
                )
            )
        await db.flush()
        group = AgentCollaborationGroup(
            tenant_id=tenant_id,
            name="Offboarding A2A group",
            purpose="Owner transfer proof",
            created_by_user_id=target_id,
            created_by_agent_id=agent_ids[0],
            status="active",
        )
        db.add(group)
        await db.flush()
        db.add(
            AgentCollaborationGroupMember(
                tenant_id=tenant_id,
                group_id=group.id,
                agent_id=agent_ids[0],
                agent_owner_user_id=target_id,
                role="owner",
                status="active",
                approved_by_user_id=target_id,
            )
        )
        db.add_all(
            [
                RuntimeTask(
                    id=queued_runtime_task_id,
                    task_type="subagent",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_ids[0],
                    root_user_id=target_id,
                    status="pending",
                ),
                RuntimeTask(
                    id=running_runtime_task_id,
                    task_type="business_task",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_ids[1],
                    root_user_id=target_id,
                    status="running",
                    claimed_by="departing-user-worker",
                    metadata_json={
                        "business_task_id": str(business_task_id),
                        "requester_user_id": str(target_id),
                        "phase": "invoking",
                    },
                ),
                ApprovalRequest(
                    id=approval_id,
                    agent_id=agent_ids[0],
                    tenant_id=tenant_id,
                    action_type="tool_execution",
                    details={"tool_name": "send_email"},
                    status="pending",
                    execution_status="pending",
                    requested_by=target_id,
                ),
            ]
        )
        await db.flush()
        db.add(
            Task(
                id=business_task_id,
                agent_id=agent_ids[1],
                tenant_id=tenant_id,
                title="Departing user business task",
                status="doing",
                created_by=target_id,
                request_id=f"business-{suffix}",
                request_hash="business-task-hash",
                active_runtime_task_id=running_runtime_task_id,
                last_execution_status="running",
            )
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        target = (await db.execute(select(User).where(User.id == target_id).with_for_update())).scalar_one()
        successor = (await db.execute(select(User).where(User.id == successor_id).with_for_update())).scalar_one()
        receipt = await offboard_loaded_user(
            db,
            target_user=target,
            successor=successor,
            actor=successor,
            expected_agent_ids=agent_ids,
            reason="Employment ended",
            request_id=f"offboard-{suffix}",
        )
        assert set(receipt.transferred_agent_ids) == set(agent_ids)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        target = (await db.execute(select(User).where(User.id == target_id).with_for_update())).scalar_one()
        agents = list(
            (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)).order_by(Agent.id))).scalars().all()
        )
        assert target.is_active is False
        assert {agent.owner_user_id for agent in agents} == {successor_id}
        assert {agent.creator_id for agent in agents} == {target_id}
        assert {agent.sponsor_user_id for agent in agents} == {target_id}
        asset_owners = set(
            (
                await db.execute(
                    select(AIAssetRecord.owner_id).where(
                        AIAssetRecord.tenant_id == tenant_id,
                        AIAssetRecord.native_entity_id.in_(agent_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert asset_owners == {successor_id}
        membership = (
            await db.execute(
                select(AgentCollaborationGroupMember).where(AgentCollaborationGroupMember.agent_id == agent_ids[0])
            )
        ).scalar_one()
        assert membership.agent_owner_user_id == successor_id
        assert membership.status == "pending_owner_confirmation"
        assert membership.approved_by_user_id is None
        runtime_tasks = {
            task.id: task
            for task in (
                await db.execute(
                    select(RuntimeTask).where(RuntimeTask.id.in_((queued_runtime_task_id, running_runtime_task_id)))
                )
            )
            .scalars()
            .all()
        }
        assert runtime_tasks[queued_runtime_task_id].status == "killed"
        assert runtime_tasks[running_runtime_task_id].status == "needs_reconciliation"
        assert runtime_tasks[running_runtime_task_id].claimed_by is None
        assert runtime_tasks[running_runtime_task_id].claim_version == 1
        business_task = await db.get(Task, business_task_id)
        assert business_task is not None
        assert business_task.status == "needs_reconciliation"
        assert business_task.last_execution_status == "needs_reconciliation"
        approval = await db.get(ApprovalRequest, approval_id)
        assert approval is not None
        assert approval.status == "rejected"
        assert approval.execution_status == "rejected"
        assert approval.resolved_by == successor_id
        assert receipt.revocations.runtime_tasks == 2
        assert receipt.revocations.pending_approvals == 1

        replay = await find_user_offboarding_replay(
            db,
            target_user=target,
            successor_user_id=successor_id,
            expected_agent_ids=agent_ids,
            reason="Employment ended",
            request_id=f"offboard-{suffix}",
        )
        assert replay is not None
        assert set(replay.transferred_agent_ids) == set(agent_ids)
        audits = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action == "user:offboarded",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1


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


async def _user_row_is_locked(owner_sessionmaker, user_id: uuid.UUID) -> bool:
    """Same live-lock observation for a User row (the offboard identity lock)."""

    async with owner_sessionmaker() as db:
        result = await db.execute(select(User.id).where(User.id == user_id).with_for_update(skip_locked=True))
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


async def _seed_offboarding_pairing_race(
    owner_sessionmaker,
    app_user_sessionmaker,
    *,
    bind_agent: bool,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID], str, str, str, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a tenant with an offboardable member, an admin successor, and an
    approved bridge pairing bound to the member.

    bind_agent routes the pairing through the real agent-binding approval
    path so the exchange's connection INSERT also takes the implicit Agent
    FK lock; otherwise the synthetic shape (agent_id NULL, Tenant/User FKs
    only) is used.

    Three extra pairing rows are planted with explicitly ORDERED ids so the
    race test can prove on live PostgreSQL that the offboarding prelock
    locks exactly the member's claimable set — rows lock in ``id`` order, so
    once the lowest rows are passed their lock fate is final:

    * ``sentinel_other_member_id`` (lowest): an approved pairing of a
      DIFFERENT member of the same tenant — a prelock that dropped the user
      filter would lock it; the correct predicate must leave it lockable.
    * ``sentinel_claimed_id``: a claimed pairing of the target member — a
      prelock that dropped the status filter would lock it; the correct
      predicate must leave it lockable.
    * ``observer_pairing_id``: a second approved pairing of the target
      member that the racing exchange never touches — the prelock MUST lock
      it for the pairing→identity serialization to be real.

    Returns (tenant_id, target_id, successor_id, agent_ids, user_code,
    device_code, request_id, observer_pairing_id, sentinel_other_member_id,
    sentinel_claimed_id).
    """
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    other_member_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    user_code = f"user-{suffix}"
    device_code = f"device-{suffix}"
    sentinel_other_member_code = f"user-other-{suffix}"
    sentinel_claimed_code = f"user-claimed-{suffix}"
    observer_code = f"user-observer-{suffix}"
    (
        sentinel_other_member_id_pairing,
        sentinel_claimed_id_pairing,
        observer_pairing_id,
        exchange_pairing_id,
    ) = sorted((uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))

    def pairing_row(
        *, pairing_id: uuid.UUID | None, code: str, status: str, user: uuid.UUID
    ) -> LocalAgentBridgePairingSession:
        return LocalAgentBridgePairingSession(
            id=pairing_id,
            tenant_id=tenant_id,
            user_id=user,
            pairing_code_hash=hash_secret(normalize_user_code(code)),
            device_code_hash=hash_secret(f"device-{code}"),
            device_name=f"Offboarding Race {code}",
            client_kind="hive-connect",
            device_fingerprint=f"race-{code}",
            scopes=["local_agent:connect"],
            status=status,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Offboarding Race Tenant", slug=f"offboard-race-{suffix[:10]}"))
        db.add_all(
            [
                User(
                    id=target_id,
                    username=f"race-member-{suffix[:10]}",
                    email=f"race-member-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Race Member",
                    tenant_id=tenant_id,
                    role="member",
                ),
                User(
                    id=successor_id,
                    username=f"race-admin-{suffix[:10]}",
                    email=f"race-admin-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Race Admin",
                    tenant_id=tenant_id,
                    role="org_admin",
                ),
                User(
                    id=other_member_id,
                    username=f"race-other-{suffix[:10]}",
                    email=f"race-other-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Race Other Member",
                    tenant_id=tenant_id,
                    role="member",
                ),
            ]
        )
        await db.flush()
        db.add(
            LocalAgentBridgePairingSession(
                id=exchange_pairing_id,
                tenant_id=tenant_id,
                user_id=target_id,
                pairing_code_hash=hash_secret(normalize_user_code(user_code)),
                device_code_hash=hash_secret(device_code),
                device_name="Offboarding Race",
                client_kind="hive-connect",
                device_fingerprint=f"race-{suffix}",
                scopes=["local_agent:connect"],
                status="pending",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        db.add(
            pairing_row(
                pairing_id=sentinel_other_member_id_pairing,
                code=sentinel_other_member_code,
                status="approved",
                user=other_member_id,
            )
        )
        db.add(
            pairing_row(
                pairing_id=sentinel_claimed_id_pairing, code=sentinel_claimed_code, status="claimed", user=target_id
            )
        )
        db.add(pairing_row(pairing_id=observer_pairing_id, code=observer_code, status="approved", user=target_id))

    agent_ids: list[uuid.UUID] = []
    from app.services import local_bridge_service as bridge_service

    if bind_agent:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            agent = await bridge_service.ensure_default_local_agent_for_pairing(
                db, user_code=user_code, user_id=target_id, tenant_id=tenant_id
            )
            await bridge_service.approve_pairing_session(
                db, user_code=user_code, user_id=target_id, tenant_id=tenant_id, agent_id=agent.id
            )
        agent_ids.append(agent.id)
    else:
        async with app_user_sessionmaker() as db:
            await bridge_service.approve_pairing_session(
                db, user_code=user_code, user_id=target_id, tenant_id=tenant_id
            )
    request_id = f"offboard-race-{suffix[:10]}"
    return (
        tenant_id,
        target_id,
        successor_id,
        agent_ids,
        user_code,
        device_code,
        request_id,
        observer_pairing_id,
        sentinel_other_member_id_pairing,
        sentinel_claimed_id_pairing,
    )


@pytest.mark.parametrize("variant", ["synthetic", "real_agent"])
async def test_offboarding_and_exchange_serialize_on_pairing_locks_without_deadlock(
    owner_sessionmaker,
    app_user_sessionmaker,
    variant: str,
) -> None:
    """The supported single-user offboard route vs an exchange is a lock-order
    serialization, never PostgreSQL SQLSTATE 40P01.

    The exchange holds its pairing row FOR UPDATE past the live-identity
    read; the offboard route previously locked the target User (and owned
    Agent) rows first and only reached its claimable-pairing UPDATE inside
    authority revocation, so the connection INSERT's implicit FK KEY SHARE
    locks closed a real ABBA cycle — the single-user sibling of the tenant
    retirement defect reproduced 2026-09-04. Offboarding now locks the
    member's claimable pairings FIRST, so an in-flight exchange either
    commits and its fresh connection is revoked by the subsequent
    offboarding, or offboarding commits first and the exchange re-reads a
    rejected pairing. The real_agent variant additionally covers the Agent
    FK lock path.
    """
    (
        tenant_id,
        target_id,
        successor_id,
        agent_ids,
        user_code,
        device_code,
        request_id,
        observer_pairing_id,
        sentinel_other_member_pairing_id,
        sentinel_claimed_pairing_id,
    ) = await _seed_offboarding_pairing_race(
        owner_sessionmaker, app_user_sessionmaker, bind_agent=variant == "real_agent"
    )

    identity_read = asyncio.Event()
    release_exchange = asyncio.Event()
    offboarding_at_pairing = asyncio.Event()

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

    async def offboard():
        async with app_user_sessionmaker() as db:
            original = db.execute

            async def execute(statement, *args, **kwargs):
                sql = str(statement).lower()
                if "local_agent_bridge_pairing_sessions" in sql and ("for update" in sql or sql.startswith("update ")):
                    offboarding_at_pairing.set()
                return await original(statement, *args, **kwargs)

            db.execute = execute  # type: ignore[method-assign]
            return await offboard_user(
                user_id=target_id,
                data=UserOffboardingRequest(
                    successor_user_id=successor_id,
                    expected_agent_ids=agent_ids,
                    reason="Weekend RC pairing race regression",
                    request_id=request_id,
                ),
                tenant_id=str(tenant_id),
                current_user=SimpleNamespace(id=successor_id, role="org_admin", tenant_id=tenant_id),
                db=db,
            )

    exchange_task: asyncio.Task = asyncio.create_task(exchange())
    offboarding_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(identity_read.wait(), timeout=10)
        offboarding_task = asyncio.create_task(offboard())
        await asyncio.wait_for(offboarding_at_pairing.wait(), timeout=10)
        # Prove the prelock locks the REAL intended row set on live
        # PostgreSQL — not merely that a pairing-shaped statement was issued.
        # An independent FOR UPDATE SKIP LOCKED session must observe the
        # observer pairing row as held (the racing exchange never touches
        # it), while the two mis-scope sentinels — another member's approved
        # pairing and the target's own claimed pairing — stay lockable.
        # Rows lock in id order, so the sentinels' fates are final once the
        # observer is observed locked. A missing prelock, a dropped FOR
        # UPDATE, or a wrong tenant/user/status predicate leaves the observer
        # lockable and fails here; the old statement-text marker could not
        # distinguish any of those.
        await _await_pairing_row_locked(owner_sessionmaker, observer_pairing_id)
        assert not await _pairing_row_is_locked(owner_sessionmaker, sentinel_other_member_pairing_id), (
            "The offboarding prelock locked another member's pairing row: the user scope was widened"
        )
        assert not await _pairing_row_is_locked(owner_sessionmaker, sentinel_claimed_pairing_id), (
            "The offboarding prelock locked a non-claimable (claimed) pairing row: the status scope was widened"
        )
        # The route must still be INSIDE its prelock: the prelock cannot
        # complete while the exchange holds the highest-id pairing row, so
        # the target User lock cannot have been taken yet. If it has, the
        # route passed a prelock that locked nothing (or the wrong rows) —
        # the observer observation then came from the later revocation
        # UPDATE, not the prelock, and the pairing→identity order is broken.
        assert not await _user_row_is_locked(owner_sessionmaker, target_id), (
            "The offboard route reached its User identity lock before its pairing prelock completed"
        )
        release_exchange.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(exchange_task, offboarding_task, return_exceptions=True), timeout=20
        )
    finally:
        release_exchange.set()
        pending = [exchange_task, *([offboarding_task] if offboarding_task is not None else [])]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert errors == [], [{"type": type(error).__name__, "message": str(error).splitlines()[0]} for error in errors]

    exchange_result, offboarding_receipt = outcomes
    # The in-flight exchange wins its claim and finishes cleanly...
    assert exchange_result["status"] == "active"
    assert exchange_result["access_token"].startswith("hb_")
    # ...and the offboarding that raced it still completes atomically and
    # revokes the fresh connection in the same transaction: no unrevoked
    # ghost pairing/connection remains for the departed member.
    assert offboarding_receipt["status"] == "deactivated"
    assert offboarding_receipt["user_id"] == str(target_id)
    assert offboarding_receipt["successor_user_id"] == str(successor_id)
    assert offboarding_receipt["revocations"]["local_bridge_connections"] == 1
    assert offboarding_receipt["transferred_agent_ids"] == [str(agent_id) for agent_id in agent_ids]

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        connection = await db.get(LocalAgentBridgeConnection, pairing.connection_id)
        target_user = await db.get(User, target_id)
        successor = await db.get(User, successor_id)
        agents = list((await db.execute(select(Agent).where(Agent.tenant_id == tenant_id))).scalars().all())
        audit_count = await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "user:offboarded",
            )
        )
    assert pairing.status == "claimed"
    assert connection is not None and connection.status == "revoked"
    assert target_user is not None and target_user.is_active is False
    assert successor is not None and successor.is_active is True
    assert {agent.owner_user_id for agent in agents} == ({successor_id} if agent_ids else set())
    assert audit_count == 1


async def test_offboarding_first_leaves_no_claimable_pairing_for_exchange(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """When offboarding wins, a later device-code exchange sees a rejected pairing."""
    (
        tenant_id,
        target_id,
        successor_id,
        _agent_ids,
        _user_code,
        device_code,
        request_id,
        _observer_pairing_id,
        _sentinel_other_member_pairing_id,
        _sentinel_claimed_pairing_id,
    ) = await _seed_offboarding_pairing_race(owner_sessionmaker, app_user_sessionmaker, bind_agent=False)

    async with app_user_sessionmaker() as db:
        receipt = await offboard_user(
            user_id=target_id,
            data=UserOffboardingRequest(
                successor_user_id=successor_id,
                expected_agent_ids=[],
                reason="Weekend RC offboarding-first regression",
                request_id=request_id,
            ),
            tenant_id=str(tenant_id),
            current_user=SimpleNamespace(id=successor_id, role="org_admin", tenant_id=tenant_id),
            db=db,
        )
    assert receipt["status"] == "deactivated"

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


async def _seed_fresh_unbound_approval(
    owner_sessionmaker,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str, str]:
    """Seed the CC probe shape: member + admin and a QUARANTINE-held pending
    pairing that offboarding's pairing prelock and revocation UPDATE cannot
    see (user_id NULL, quarantine scope), so only the approve-time
    live-identity gate can stop a fresh binding onto a departed member.

    Returns (tenant_id, target_id, successor_id, user_code, device_code,
    device_name).
    """
    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID, TENANT_SCOPE_QUARANTINE_SLUG

    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    user_code = f"user-{suffix}"
    device_code = f"device-{suffix}"
    device_name = f"Fresh Approval {suffix[:8]}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Fresh Approval Tenant", slug=f"fresh-approval-{suffix[:10]}"))
        db.add_all(
            [
                User(
                    id=target_id,
                    username=f"fresh-member-{suffix[:10]}",
                    email=f"fresh-member-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Fresh Approval Member",
                    tenant_id=tenant_id,
                    role="member",
                ),
                User(
                    id=successor_id,
                    username=f"fresh-admin-{suffix[:10]}",
                    email=f"fresh-admin-{suffix[:10]}@example.test",
                    password_hash="x",
                    display_name="Fresh Approval Admin",
                    tenant_id=tenant_id,
                    role="org_admin",
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
                device_fingerprint=f"fresh-approval-{suffix}",
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
    return tenant_id, target_id, successor_id, user_code, device_code, device_name


async def test_fresh_unbound_approval_cannot_bind_offboarded_identity(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """A fresh approval must not commit onto an already-offboarded identity.

    Deterministic interleaving with two statement-completion barriers: the
    offboard route parks right after its target-User FOR UPDATE, the real
    user-level approve route parks right after its pairing FOR UPDATE
    loader, offboarding then commits first. The approve request's Agent /
    Participant / ai-asset bootstrap is flushed but uncommitted at that
    point, so the approve-time live-identity gate must roll the whole
    request back typed — no approved pairing for the inactive member, no
    orphan Agent rows, and a truthful pending poll for the device.
    """
    from app.api.local_bridge import approve_current_user_bridge_pairing
    from app.core.tenant_scope import TENANT_SCOPE_QUARANTINE_ID
    from app.models.capability_policy import CapabilityPolicy
    from app.models.participant import Participant

    (
        tenant_id,
        target_id,
        successor_id,
        user_code,
        device_code,
        device_name,
    ) = await _seed_fresh_unbound_approval(owner_sessionmaker)

    offboarding_holds_user = asyncio.Event()
    release_offboarding = asyncio.Event()
    approval_loaded_pairing = asyncio.Event()
    release_approval = asyncio.Event()

    async def offboard():
        async with app_user_sessionmaker() as db:
            original = db.execute

            async def execute(statement, *args, **kwargs):
                result = await original(statement, *args, **kwargs)
                sql = str(statement).lower()
                if not offboarding_holds_user.is_set() and sql.startswith("select users") and "for update" in sql:
                    offboarding_holds_user.set()
                    await release_offboarding.wait()
                return result

            db.execute = execute  # type: ignore[method-assign]
            return await offboard_user(
                user_id=target_id,
                data=UserOffboardingRequest(
                    successor_user_id=successor_id,
                    expected_agent_ids=[],
                    reason="Weekend RC fresh approval race regression",
                    request_id=f"fresh-approval-{uuid.uuid4().hex[:10]}",
                ),
                tenant_id=str(tenant_id),
                current_user=SimpleNamespace(id=successor_id, role="org_admin", tenant_id=tenant_id),
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
                current_user=SimpleNamespace(id=target_id, role="member", tenant_id=tenant_id),
                db=db,
            )

    offboard_task: asyncio.Task = asyncio.create_task(offboard())
    approve_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(offboarding_holds_user.wait(), timeout=10)
        approve_task = asyncio.create_task(approve())
        await asyncio.wait_for(approval_loaded_pairing.wait(), timeout=10)
        # Offboarding commits while the approve request holds only its
        # quarantine pairing row — invisible to the offboarding predicates.
        release_offboarding.set()
        offboarding_receipt = await asyncio.wait_for(offboard_task, timeout=20)
        release_approval.set()
        with pytest.raises(HTTPException) as approve_error:
            await asyncio.wait_for(approve_task, timeout=20)
    finally:
        release_offboarding.set()
        release_approval.set()
        for task in (offboard_task, *([approve_task] if approve_task is not None else [])):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            offboard_task, *([approve_task] if approve_task is not None else []), return_exceptions=True
        )

    assert approve_error.value.status_code == 409
    assert approve_error.value.detail == {"code": "pairing_identity_inactive", "status": "pending"}
    assert offboarding_receipt["status"] == "deactivated"

    async with owner_sessionmaker() as db:
        pairing = (
            await db.execute(
                select(LocalAgentBridgePairingSession).where(
                    LocalAgentBridgePairingSession.pairing_code_hash == hash_secret(normalize_user_code(user_code))
                )
            )
        ).scalar_one()
        target = await db.get(User, target_id)
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
    assert target is not None and target.is_active is False
    assert agent_count == 0, "ensure_default_local_agent_for_pairing rows survived as orphans"
    assert asset_count == 0
    assert policy_count == 0
    assert orphan_participants == 0

    async with app_user_sessionmaker() as db:
        poll = await exchange_pairing_session(db, device_code=device_code)
    assert poll == {"status": "pending", "interval": 3}


async def test_fresh_approval_before_offboarding_is_rejected_by_offboarding(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """When the fresh approval wins the race and commits first, the pairing
    becomes visible to offboarding's revocation UPDATE and must end
    rejected, with the flushed Agent transferred to the successor."""

    from app.api.local_bridge import approve_current_user_bridge_pairing

    (
        tenant_id,
        target_id,
        successor_id,
        user_code,
        device_code,
        _device_name,
    ) = await _seed_fresh_unbound_approval(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        approved = await approve_current_user_bridge_pairing(
            user_code=user_code,
            current_user=SimpleNamespace(id=target_id, role="member", tenant_id=tenant_id),
            db=db,
        )
    assert approved["status"] == "approved"
    agent_id = uuid.UUID(approved["agent_id"])

    async with app_user_sessionmaker() as db:
        receipt = await offboard_user(
            user_id=target_id,
            data=UserOffboardingRequest(
                successor_user_id=successor_id,
                expected_agent_ids=[agent_id],
                reason="Weekend RC fresh approval regression",
                request_id=f"fresh-approval-win-{uuid.uuid4().hex[:10]}",
            ),
            tenant_id=str(tenant_id),
            current_user=SimpleNamespace(id=successor_id, role="org_admin", tenant_id=tenant_id),
            db=db,
        )
    assert receipt["status"] == "deactivated"

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
    assert agent is not None and agent.owner_user_id == successor_id
