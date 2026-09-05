"""Real PostgreSQL regressions: an inactive Tenant's Agent is unreachable and unexecutable.

The bodyless ``DELETE /tenants/{id}`` used by the real frontend and the
platform-admin company toggle both leave Agents, channel rows, and pending
runtime work in place while marking the Tenant inactive. These tests prove on
live PostgreSQL (Testcontainers, full alembic chain, non-owner ``rls_app_user``
role) that after either producer:

* the generic owner/admin management boundary resolves the Agent exactly like
  a missing row (404, including the platform-admin bypass), while orphan
  recovery inside an ACTIVE tenant still works for an inactive owner;
* a permission-scope mutation on such an Agent has zero effect;
* the runtime bootstrap returns the typed inactive sentinel before any model
  or tool work;
* a pending ``business_task`` is not claimable by the runtime-task worker and
  the shared pre-execution executor boundary blocks with a truthful
  recoverable state — and the task becomes claimable again once the company is
  reactivated (no data loss);
* provider webhook ingress cannot resolve a previously valid configured
  channel row, and queued channel ingress events are not claimed;
* an inbox event claimed while the company is active is deferred — not
  failed, not dead-lettered — when the company is deactivated before the
  dispatcher runs, keeping its original ``available_at`` so a later arrival
  for the same company cannot overtake it, and reactivation admits the
  original event first.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.api.channel_rls import load_public_agent_channel_config
from app.api.tenants import delete_tenant
from app.core.permissions import require_agent_owner_or_admin
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.channel_config import ChannelConfig
from app.models.channel_ingress_event import ChannelIngressEvent
from app.models.runtime_task import RuntimeTask
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services.channel_ingress_inbox import ChannelIngressInboxService
from app.services.runtime_task_claim_service import RuntimeTaskClaimService


async def _seed_company(
    owner_sessionmaker,
    *,
    owner_is_active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed an active tenant, its org admin, a member, an in-target
    platform-admin Agent OWNER (the TENANT-RETIREMENT-ZOMBIE-AGENT shape whose
    ownership survives rehoming), and an out-of-tenant platform-admin caller.

    Returns (tenant_id, org_admin_id, member_id, agent_owner_admin_id,
    agent_id, caller_id, caller_tenant_id). The caller's tenant is created
    FIRST so the bodyless delete rehomes in-target platform admins into it
    (earliest active fallback).
    """

    tenant_id = uuid.uuid4()
    caller_tenant_id = uuid.uuid4()
    org_admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    agent_owner_admin_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    caller_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add_all(
            [
                Tenant(id=caller_tenant_id, name="Gate Caller Home", slug=f"gate-caller-{suffix}"),
                Tenant(id=tenant_id, name="Gate Target", slug=f"gate-target-{suffix}"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                User(
                    id=caller_id,
                    username=f"gate-caller-{suffix}",
                    email=f"gate-caller-{suffix}@example.test",
                    password_hash="x",
                    display_name="Gate Caller",
                    tenant_id=caller_tenant_id,
                    role="platform_admin",
                ),
                User(
                    id=agent_owner_admin_id,
                    username=f"gate-owner-{suffix}",
                    email=f"gate-owner-{suffix}@example.test",
                    password_hash="x",
                    display_name="In-target Platform Admin",
                    tenant_id=tenant_id,
                    role="platform_admin",
                    is_active=owner_is_active,
                ),
                User(
                    id=org_admin_id,
                    username=f"gate-admin-{suffix}",
                    email=f"gate-admin-{suffix}@example.test",
                    password_hash="x",
                    display_name="Gate Org Admin",
                    tenant_id=tenant_id,
                    role="org_admin",
                ),
                User(
                    id=member_id,
                    username=f"gate-member-{suffix}",
                    email=f"gate-member-{suffix}@example.test",
                    password_hash="x",
                    display_name="Gate Member",
                    tenant_id=tenant_id,
                    role="member",
                ),
            ]
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Gate Agent",
                role_description="owned by the in-target platform admin",
                creator_id=agent_owner_admin_id,
                sponsor_user_id=agent_owner_admin_id,
                owner_user_id=agent_owner_admin_id,
                status="idle",
            )
        )
        await db.commit()
    return (
        tenant_id,
        org_admin_id,
        member_id,
        agent_owner_admin_id,
        agent_id,
        caller_id,
        caller_tenant_id,
    )


async def _bodyless_delete_tenant(
    app_user_sessionmaker,
    *,
    tenant_id: uuid.UUID,
    caller_id: uuid.UUID,
) -> None:
    """The real frontend producer: DELETE /tenants/{id} without a body."""

    async with app_user_sessionmaker() as db:
        await delete_tenant(
            tenant_id=tenant_id,
            retirement=None,
            current_user=SimpleNamespace(id=caller_id, role="platform_admin", tenant_id=None),
            db=db,
        )
        await db.commit()


async def _load_user(db, user_id: uuid.UUID) -> User:
    return (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()


async def _load_user_in_tenant(db, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """Pin the session to the user's own tenant, as the request path does.

    The read-only transaction stays open so the returned identity keeps its
    loaded attributes (a rollback would expire them); every later statement on
    this session, including the audited bypass scopes, joins the same
    transaction with the pinned GUC.
    """

    await pin_rls_tenant_context(db, tenant_id)
    return await _load_user(db, user_id)


async def test_inactive_tenant_agent_is_unreachable_through_owner_admin_boundary(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """After the bodyless delete, ``require_agent_owner_or_admin`` must return
    the same 404 as a missing Agent — including through the platform-admin
    bypass lookup — for both the rehomed platform admin and the detached
    org-admin/owner identities."""

    (
        tenant_id,
        org_admin_id,
        _member_id,
        agent_owner_admin_id,
        agent_id,
        caller_id,
        caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)

    # Control: while the tenant is active, the org admin resolves the Agent
    # through the request-scoped branch and the in-target platform-admin OWNER
    # resolves it through the audited bypass lookup.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        org_admin = await _load_user(db, org_admin_id)
        resolved = await require_agent_owner_or_admin(db, org_admin, agent_id)
        assert str(resolved.id) == str(agent_id)
        await db.rollback()

    async with app_user_sessionmaker() as db:
        owner_admin = await _load_user_in_tenant(db, tenant_id, agent_owner_admin_id)
        resolved = await require_agent_owner_or_admin(db, owner_admin, agent_id, lock=True)
        assert str(resolved.id) == str(agent_id)

    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    async with owner_sessionmaker() as db:
        tenant = await db.get(Tenant, tenant_id)
        rehomed_owner = await db.get(User, agent_owner_admin_id)
    assert tenant is not None and tenant.is_active is False
    assert rehomed_owner.is_active is True and rehomed_owner.role == "platform_admin"
    assert rehomed_owner.tenant_id is not None and rehomed_owner.tenant_id != tenant_id

    # The decisive red/green boundary: the rehomed platform-admin owner must
    # get the same 404 as a missing Agent through the bypass lookup — on the
    # frozen candidate this call resolved the Agent and returned ownership
    # authority over the retired company's row.
    async with app_user_sessionmaker() as db:
        rehomed_identity = await _load_user_in_tenant(db, rehomed_owner.tenant_id, agent_owner_admin_id)
        with pytest.raises(HTTPException) as bypass_error:
            await require_agent_owner_or_admin(db, rehomed_identity, agent_id, lock=True)
        assert bypass_error.value.status_code == 404
        assert bypass_error.value.detail == "Agent not found"

    # An unrelated platform administrator must not learn the Agent exists
    # either (frozen candidate: 403 existence leak; corrected: missing-row 404).
    async with app_user_sessionmaker() as db:
        platform_caller = await _load_user_in_tenant(db, caller_tenant_id, caller_id)
        with pytest.raises(HTTPException) as caller_error:
            await require_agent_owner_or_admin(db, platform_caller, agent_id)
        assert caller_error.value.status_code == 404
        assert caller_error.value.detail == "Agent not found"

    # Same-tenant callers of a disabled company are detached at deactivation
    # (tenant_id NULL), so the non-bypass branch is exercised with the
    # detached identity exactly as an authenticated request would present it;
    # the Agent must still resolve like a missing row under the request scope.
    async with owner_sessionmaker() as db:
        detached_admin = await db.get(User, org_admin_id)
    assert detached_admin.is_active is True and detached_admin.tenant_id is None
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        with pytest.raises(HTTPException) as owner_branch_error:
            await require_agent_owner_or_admin(db, detached_admin, agent_id, lock=True)
        assert owner_branch_error.value.status_code == 404
        assert owner_branch_error.value.detail == "Agent not found"


async def test_active_tenant_inactive_owner_recovery_still_works(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """The orphan-recovery purpose survives: inside an ACTIVE tenant, an
    org admin (and the platform-admin bypass) can still resolve an Agent whose
    owner is inactive."""

    (
        tenant_id,
        org_admin_id,
        _member_id,
        agent_owner_admin_id,
        agent_id,
        _caller_id,
        _caller_tenant_id,
    ) = await _seed_company(
        owner_sessionmaker,
        owner_is_active=False,
    )

    async with owner_sessionmaker() as db:
        tenant = await db.get(Tenant, tenant_id)
        inactive_owner = await db.get(User, agent_owner_admin_id)
    assert tenant.is_active is True
    assert inactive_owner.is_active is False

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        org_admin = await _load_user(db, org_admin_id)
        resolved = await require_agent_owner_or_admin(db, org_admin, agent_id, lock=True)
        assert str(resolved.id) == str(agent_id)


async def test_update_agent_permissions_on_inactive_tenant_agent_has_zero_effect(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """The live ``update_agent_permissions`` endpoint must abort with 404
    before deleting or inserting any AgentPermission row, leaving the
    previously granted scope exactly as it was."""

    from app.api.agents import AgentPermissionUpdateIn, update_agent_permissions

    (
        tenant_id,
        _org_admin_id,
        member_id,
        agent_owner_admin_id,
        agent_id,
        caller_id,
        caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)

    async with owner_sessionmaker() as db:
        db.add(
            AgentPermission(
                agent_id=agent_id,
                tenant_id=tenant_id,
                scope_type="user",
                scope_id=member_id,
                access_level="manage",
            )
        )
        await db.commit()

    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    # The rehomed platform-admin owner drives the live endpoint exactly as
    # Codex's red probe did: on the frozen candidate the call returned and its
    # body deleted the old grant and inserted a company grant.
    async with owner_sessionmaker() as db:
        rehomed_owner = await db.get(User, agent_owner_admin_id)
    assert rehomed_owner.tenant_id is not None and rehomed_owner.tenant_id != tenant_id
    async with app_user_sessionmaker() as db:
        rehomed_identity = await _load_user_in_tenant(db, rehomed_owner.tenant_id, agent_owner_admin_id)
        with pytest.raises(HTTPException) as permission_error:
            await update_agent_permissions(
                agent_id,
                AgentPermissionUpdateIn(scope_type="company", access_level="use"),
                current_user=rehomed_identity,
                db=db,
            )
        assert permission_error.value.status_code == 404
        assert permission_error.value.detail == "Agent not found"
        await db.rollback()

    async with owner_sessionmaker() as db:
        rows = (
            await db.execute(
                select(AgentPermission.scope_type, AgentPermission.scope_id, AgentPermission.access_level).where(
                    AgentPermission.agent_id == agent_id
                )
            )
        ).all()
    assert rows == [("user", member_id, "manage")]


async def test_resolve_runtime_config_reports_inactive_tenant_before_model_work(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
) -> None:
    """``_resolve_runtime_config`` must return the typed inactive sentinel
    (which the kernel turns into an early-exit error before any tool runs)
    for an Agent whose company was deactivated by the bodyless delete."""

    from app.runtime import invoker

    (
        tenant_id,
        _org_admin_id,
        _member_id,
        _agent_owner_admin_id,
        agent_id,
        caller_id,
        _caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)

    resolved_tenant = await invoker.resolve_tenant_for_agent(agent_id, session_factory=app_user_sessionmaker)
    assert str(resolved_tenant) == str(tenant_id)

    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    monkeypatch.setattr(invoker, "async_session", app_user_sessionmaker)
    config = await invoker._resolve_runtime_config(agent_id)
    assert config.tenant_resolution_error is not None
    assert "inactive" in config.tenant_resolution_error
    assert str(config.tenant_id) == str(tenant_id)

    # Control: an ACTIVE tenant's Agent still bootstraps without the sentinel.
    (
        _active_tenant_id,
        _admin,
        _member,
        _agent_owner_admin_id,
        active_agent_id,
        _caller,
        _caller_home,
    ) = await _seed_company(owner_sessionmaker)
    active_config = await invoker._resolve_runtime_config(active_agent_id)
    assert active_config.tenant_resolution_error is None


async def test_inactive_tenant_pending_business_task_is_unclaimable_and_recoverable(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """A pending ``business_task`` staged before the bodyless delete is not
    claimable by the runtime-task worker while the company is inactive — and
    becomes claimable again after reactivation, proving the truthful
    recoverable state (no killed rows, no fake terminal status)."""

    from app.services.business_task_runtime import stage_business_task_runtime

    (
        tenant_id,
        _org_admin_id,
        member_id,
        _agent_owner_admin_id,
        agent_id,
        caller_id,
        _caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)

    request_id = f"gate-task-{uuid.uuid4().hex[:10]}"
    task_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            title="Gate pending business task",
            created_by=member_id,
            request_id=request_id,
            request_hash=uuid.uuid4().hex,
            status="pending",
        )
        db.add(task)
        await db.flush()
        runtime_task = await stage_business_task_runtime(
            db=db,
            task=task,
            requester_user_id=member_id,
            agent_name="Gate Agent",
            request_id=request_id,
        )
        await db.commit()
        runtime_task_id = runtime_task.id

    async def claim_business_tasks() -> list[RuntimeTask]:
        async with app_user_sessionmaker() as db:
            async with enter_rls_bypass(db, reason="test claim pending business tasks") as bypass_db:
                service = RuntimeTaskClaimService(
                    db=bypass_db,
                    worker_id="gate-test-worker",
                    task_types=("business_task",),
                )
                return await service.claim_available()

    # Control: while the tenant is active the pending business task IS claimable.
    claimed_while_active = await claim_business_tasks()
    assert [str(task.id) for task in claimed_while_active] == [str(runtime_task_id)]
    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeTask)
            .where(RuntimeTask.id == runtime_task_id)
            .values(status="pending", claimed_by=None, claim_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    claimed_while_inactive = await claim_business_tasks()
    assert claimed_while_inactive == []

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTask, runtime_task_id)
    assert row is not None and row.status == "pending", "the task must stay durably pending, not faked terminal"

    # Reactivation (the recoverable path) makes the task claimable again.
    async with owner_sessionmaker() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(is_active=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    reclaimed = await claim_business_tasks()
    assert [str(task.id) for task in reclaimed] == [str(runtime_task_id)]


async def test_execute_task_blocks_inactive_tenant_before_model_invocation(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
) -> None:
    """The shared pre-execution executor boundary must block with the typed
    recoverable ``tenant_inactive`` state before any mutation or model work —
    distinguishable from the downstream ``model_not_configured`` gate that the
    frozen candidate fell through to."""

    from app.services import task_executor
    from app.services.tenant_resolver import clear_tenant_resolution_cache

    (
        tenant_id,
        _org_admin_id,
        member_id,
        _agent_owner_admin_id,
        agent_id,
        caller_id,
        _caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)

    request_id = f"gate-exec-{uuid.uuid4().hex[:10]}"
    task_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            Task(
                id=task_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                title="Gate executor task",
                created_by=member_id,
                request_id=request_id,
                request_hash=uuid.uuid4().hex,
                status="pending",
            )
        )
        await db.commit()

    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    real_scoped_session = task_executor.tenant_scoped_session

    def scoped_session_on_container(tenant, **kwargs):
        kwargs.setdefault("session_factory", app_user_sessionmaker)
        return real_scoped_session(tenant, **kwargs)

    monkeypatch.setattr(task_executor, "tenant_scoped_session", scoped_session_on_container)
    # Serve the agent→tenant mapping from the process-local cache — the exact
    # multi-worker shape where another process resolved the mapping before the
    # company was deactivated. Only a fresh liveness read at admission can
    # close it; a cache clear in the deactivating worker would prove nothing.
    from app.services import tenant_resolver as tenant_resolver_module

    tenant_resolver_module._cache_agent_tenant(agent_id, tenant_id)
    try:
        outcome = await task_executor.execute_task(task_id, agent_id, requester_user_id=member_id)
    finally:
        clear_tenant_resolution_cache()
    assert outcome.status == "blocked"
    assert outcome.error_code == "tenant_inactive"
    assert outcome.retryable is True

    # Zero execution effect inside the disabled company.
    async with owner_sessionmaker() as db:
        task = await db.get(Task, task_id)
        logs = await db.scalar(select(func.count()).select_from(TaskLog).where(TaskLog.task_id == task_id))
    assert task is not None and task.status == "pending"
    assert logs == 0


async def test_post_claim_tenant_deactivation_defers_dispatch_until_reactivation(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
) -> None:
    """The post-claim deactivation race: an inbox event is claimed while the
    company is active, the company is then deactivated, and only afterwards
    does the dispatcher run. The shared dispatch boundary must re-read Tenant
    liveness inside its own transaction and stop before materialized-message
    recovery or any payload handler (whose ``call_agent_llm`` path applies
    permission-mode, tool-permission, and plan-confirmation effects before
    RuntimeTask staging), and the inbox must release the claim as a deferral —
    not a failure — so the event survives for reactivation without consuming
    its finite attempt budget. The deferral must also keep the row's original
    ``available_at``: claim order is ``(available_at, received_at)``, so a
    defer-time rewrite would let an event that arrived after the deferred one
    overtake it once the company is reactivated."""

    from app.runtime.tenant_admission import RuntimeTenantPreconditionError
    from app.services import channel_ingress_dispatcher
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event

    (
        tenant_id,
        _org_admin_id,
        _member_id,
        _agent_owner_admin_id,
        agent_id,
        _caller_id,
        _caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)
    suffix = uuid.uuid4().hex[:10]
    first_event_id = f"gate-race-{suffix}"
    later_event_id = f"gate-race-later-{suffix}"
    # Pinned arrivals: the later event's durable timestamps sit strictly
    # between the first event's original availability and the deferral, so a
    # defer-time rewrite of the first row's available_at reorders the pair.
    first_arrival = datetime.now(timezone.utc) - timedelta(seconds=2)
    later_arrival = datetime.now(timezone.utc) - timedelta(seconds=1)
    ingress_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChannelIngressEvent(
                id=ingress_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                provider="feishu",
                installation_ref=str(uuid.uuid4()),
                provider_event_id=first_event_id,
                handler_key="feishu.event",
                payload_digest=uuid.uuid4().hex,
                payload_json={"provider": "feishu", "body": {"event_id": first_event_id}},
                metadata_json={"transport": "webhook"},
                status="received",
                available_at=first_arrival,
                received_at=first_arrival,
            )
        )
        await db.commit()

    # Observable sentinels at the only two payload-admission seams; the real
    # dispatcher, sessions, row claim, and transaction lifecycle all execute
    # against live PostgreSQL underneath them.
    admissions: list[str] = []

    async def sentinel_resume(db, item):
        admissions.append(f"resume:{item.provider_event_id}")
        return None

    async def sentinel_payload(db, item):
        admissions.append(f"payload:{item.provider_event_id}")
        return {"status": "processed", "reply_text": "sentinel"}

    monkeypatch.setattr(channel_ingress_dispatcher, "_resume_materialized_user_message", sentinel_resume)
    monkeypatch.setattr(channel_ingress_dispatcher, "_dispatch_verified_payload", sentinel_payload)

    real_scoped_session = channel_ingress_dispatcher.tenant_scoped_session

    def scoped_session_on_container(tenant, **kwargs):
        kwargs.setdefault("session_factory", app_user_sessionmaker)
        return real_scoped_session(tenant, **kwargs)

    monkeypatch.setattr(channel_ingress_dispatcher, "tenant_scoped_session", scoped_session_on_container)

    captured: dict[str, RuntimeTenantPreconditionError] = {}

    async def deactivate_then_dispatch(item):
        # The production race: the company is disabled after the inbox claim
        # committed but before the shared dispatch boundary reads the row.
        async with owner_sessionmaker() as db:
            await db.execute(
                update(Tenant)
                .where(Tenant.id == item.tenant_id)
                .values(is_active=False)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        try:
            return await dispatch_channel_ingress_event(item)
        except RuntimeTenantPreconditionError as exc:
            captured["precondition"] = exc
            raise

    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)
    deferred = await inbox.drain_once(worker_id="gate-race-worker", dispatch=deactivate_then_dispatch)

    assert deferred == {"claimed": 1, "processed": 0, "retried": 0, "dead_lettered": 0, "deferred": 1}
    assert admissions == [], "no payload admission may run for the disabled company"
    precondition = captured["precondition"]
    assert precondition.reason_code == "tenant_inactive"
    assert precondition.source == "channel_ingress_dispatch"

    async with owner_sessionmaker() as db:
        row = await db.get(ChannelIngressEvent, ingress_id)
    assert row is not None
    assert row.status == "received", "the deferral must release the claim, not fake a terminal state"
    assert row.locked_by is None and row.locked_at is None
    assert row.attempt_count == 0, "a deactivation must not consume the event's finite attempts"
    assert row.last_error is None
    assert row.available_at == first_arrival, (
        "the deferral must keep the availability the row already had at claim time"
    )

    # A later arrival for the same company queues behind the deferred event;
    # its durable timestamps precede the deferral, exactly like an event that
    # was durably received after the first event's claim but before the
    # company was reactivated.
    later_ingress_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChannelIngressEvent(
                id=later_ingress_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                provider="feishu",
                installation_ref=str(uuid.uuid4()),
                provider_event_id=later_event_id,
                handler_key="feishu.event",
                payload_digest=uuid.uuid4().hex,
                payload_json={"provider": "feishu", "body": {"event_id": later_event_id}},
                metadata_json={"transport": "webhook"},
                status="received",
                available_at=later_arrival,
                received_at=later_arrival,
            )
        )
        await db.commit()

    # While the company stays disabled neither the released row nor the later
    # arrival is re-claimed, so the deferral cannot churn attempts (the
    # claim-time semi-join holds).
    assert await inbox.claim_batch(worker_id="gate-race-worker") == []

    # Reactivation restores the full claim → dispatch → admission path, and
    # the deferred original is admitted before the later arrival.
    async with owner_sessionmaker() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(is_active=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    resumed = await inbox.drain_once(worker_id="gate-race-worker", dispatch=dispatch_channel_ingress_event)

    assert resumed == {"claimed": 2, "processed": 2, "retried": 0, "dead_lettered": 0, "deferred": 0}
    assert admissions == [
        f"resume:{first_event_id}",
        f"payload:{first_event_id}",
        f"resume:{later_event_id}",
        f"payload:{later_event_id}",
    ], "reactivation must admit the deferred original before the later arrival"
    async with owner_sessionmaker() as db:
        row = await db.get(ChannelIngressEvent, ingress_id)
        later_row = await db.get(ChannelIngressEvent, later_ingress_id)
    assert row is not None and row.status == "processed"
    assert row.attempt_count == 1
    assert later_row is not None and later_row.status == "processed"
    assert later_row.attempt_count == 1


async def test_provider_ingress_cannot_enter_inactive_tenant_agent_runtime(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Both deactivation producers close provider ingress that authenticates
    with only channel identity: the public webhook channel lookup resolves a
    previously valid configured channel row to None (the route then 404s
    before any signature or ingestion), and the durable inbox does not claim
    the company's queued events while it is disabled."""

    (
        tenant_id,
        _org_admin_id,
        member_id,
        _agent_owner_admin_id,
        agent_id,
        caller_id,
        _caller_tenant_id,
    ) = await _seed_company(owner_sessionmaker)
    suffix = uuid.uuid4().hex[:10]
    config_id = uuid.uuid4()
    ingress_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChannelConfig(
                id=config_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                channel_type="feishu",
                is_configured=True,
                is_connected=True,
                extra_config={"connection_status": "connected"},
            )
        )
        await db.flush()
        db.add(
            ChannelIngressEvent(
                id=ingress_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                provider="feishu",
                installation_ref=str(config_id),
                provider_event_id=f"gate-event-{suffix}",
                handler_key="feishu.event",
                payload_digest=uuid.uuid4().hex,
                payload_json={"provider": "feishu", "body": {"event_id": f"gate-event-{suffix}"}},
                metadata_json={"transport": "webhook"},
                status="received",
                available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    inbox = ChannelIngressInboxService(session_factory=app_user_sessionmaker)

    # Controls while the company is active.
    async with app_user_sessionmaker() as db:
        config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="feishu")
        assert config is not None and str(config.id) == str(config_id)
        await db.rollback()
    claimed_active = await inbox.claim_batch(worker_id="gate-ingress-worker")
    assert [str(item.id) for item in claimed_active] == [str(ingress_id)]
    async with owner_sessionmaker() as db:
        await db.execute(
            update(ChannelIngressEvent)
            .where(ChannelIngressEvent.id == ingress_id)
            .values(status="received", locked_by=None, locked_at=None, attempt_count=0)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    # Producer 1: the platform-admin company toggle keeps channel credentials
    # valid, so only the liveness gate can hold the boundary.
    async with owner_sessionmaker() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    async with app_user_sessionmaker() as db:
        config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="feishu")
        assert config is None
        await db.rollback()
    claimed_toggled = await inbox.claim_batch(worker_id="gate-ingress-worker")
    assert claimed_toggled == []
    async with owner_sessionmaker() as db:
        row = await db.get(ChannelIngressEvent, ingress_id)
    assert row is not None and row.status == "received", "queued events stay durable, not dead-lettered"

    # Producer 2: the bodyless DELETE (scrubs credentials and detaches users)
    # must leave the same two gates closed.
    async with owner_sessionmaker() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(is_active=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    await _bodyless_delete_tenant(app_user_sessionmaker, tenant_id=tenant_id, caller_id=caller_id)

    async with app_user_sessionmaker() as db:
        config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="feishu")
        assert config is None
        await db.rollback()
    claimed_deleted = await inbox.claim_batch(worker_id="gate-ingress-worker")
    assert claimed_deleted == []
