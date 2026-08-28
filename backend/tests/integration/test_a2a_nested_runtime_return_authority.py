"""Nested A→B→C runtime result return into an ACTIVE read-only delegation parent.

Production defect (DAY1-A2A-NESTED-RUNTIME-RETURN-AUTH-001, Codex review of
the DAY1-A2A-ACTIVE-STEER-AUTH-001 chain): in a nested A→B→C delegation, B's
``delegation_run`` session is product read-only.  When C completes, the live
completion path ``RuntimeNotificationOutboxWorker._deliver_page`` ->
``continue_parent_session_with_result_page`` ->
``continue_agent_session_from_mailbox`` -> ``_submit_active_session_input`` ->
``submit_live_human_input`` has no internal runtime authority lane, so it
falls into ``resolve_session_mutation_authority`` with the root user and hits
typed HTTP 409 ``session_read_only`` for the ACTIVE B parent.  The C result is
NOT a parent peer message (B's peer is A; the completion source is the
runtime result integration page), so the fix is a distinct narrow
server-derived ``runtime_result_integration`` authority — never the
``a2a_peer_agent_id`` lane and never a generic system bypass.

These tests drive the real terminal seam, real outbox, real integration page
preparation/delivery and the canonical Session V2 input path against
Testcontainers PostgreSQL.  Only DB session factories are rebound.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultMailboxCursor, RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.models.tenant import Tenant
from app.models.user import User
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService


async def _mk_tenant(db, *, prefix: str = "nest") -> uuid.UUID:
    tenant = Tenant(name=f"{prefix} Tenant", slug=f"{prefix}-{uuid.uuid4().hex[:10]}")
    db.add(tenant)
    await db.flush()
    return tenant.id


async def _mk_user(db, tenant_id: uuid.UUID) -> uuid.UUID:
    user = User(
        username=f"nr-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="Nested Return Owner",
        tenant_id=tenant_id,
        role="org_admin",
    )
    db.add(user)
    await db.flush()
    return user.id


async def _mk_agent(db, *, tenant_id: uuid.UUID, user_id: uuid.UUID, name: str) -> uuid.UUID:
    agent = Agent(
        tenant_id=tenant_id,
        creator_id=user_id,
        owner_user_id=user_id,
        name=name,
        role_description="Nested return regression agent.",
        status="idle",
    )
    db.add(agent)
    await db.flush()
    return agent.id


def _delegation_session(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    peer_agent_id: uuid.UUID,
    parent_session_id: uuid.UUID,
    root_session_id: uuid.UUID,
    runtime_task_id: uuid.UUID | None,
    title: str,
) -> ChatSession:
    return ChatSession(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        peer_agent_id=peer_agent_id,
        source_channel="agent",
        session_kind="delegation_run",
        actor_type="agent",
        runtime_source="delegation",
        visibility_scope="agent_owner",
        listed_surface="chat",
        parent_session_id=parent_session_id,
        root_session_id=root_session_id,
        runtime_task_id=runtime_task_id,
        title=title,
        transcript_metadata_json={"interaction_type": "delegation"},
    )


async def _seed_nested(owner_sessionmaker) -> dict[str, uuid.UUID | str]:
    """Seed the realistic nested A→B→C durable shape.

    B owns a real delegation_run session P proven by its own durable
    delegation RuntimeTask (session.runtime_task_id), an ACTIVE
    a2a_continuation run, and C's a2a_continuation run whose completion must
    return into P.
    """

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        user_id = await _mk_user(db, tenant_id)
        agent_a_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Root Coordinator A")
        agent_b_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Middle Worker B")
        agent_c_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Leaf Worker C")

        root_session = ChatSession(
            agent_id=agent_a_id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"root-{uuid.uuid4().hex[:8]}",
        )
        db.add(root_session)
        await db.flush()

        task_ab = RuntimeTask(
            task_type="delegation",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=agent_a_id,
            child_agent_id=agent_b_id,
            child_agent_name="Middle Worker B",
            prompt="Coordinate the leaf task",
            parent_session_id=str(root_session.id),
            child_session_id=None,
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id)],
            depth=1,
        )
        db.add(task_ab)
        await db.flush()

        parent_session = _delegation_session(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_b_id,
            peer_agent_id=agent_a_id,
            parent_session_id=root_session.id,
            root_session_id=root_session.id,
            runtime_task_id=task_ab.id,
            title="Delegation: Middle Worker B",
        )
        db.add(parent_session)
        await db.flush()
        task_ab.child_session_id = str(parent_session.id)

        b_turn_id = f"turn-{uuid.uuid4().hex}"
        b_active_run = RuntimeTask(
            task_type="a2a_continuation",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=agent_b_id,
            child_agent_id=agent_b_id,
            child_agent_name="Middle Worker B",
            prompt="Coordinate the leaf task",
            parent_session_id=str(parent_session.id),
            child_session_id=str(parent_session.id),
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id)],
            depth=1,
            metadata_json={"turn_id": b_turn_id},
        )
        db.add(b_active_run)

        task_bc = RuntimeTask(
            task_type="delegation",
            status="completed",
            tenant_id=tenant_id,
            parent_agent_id=agent_b_id,
            child_agent_id=agent_c_id,
            child_agent_name="Leaf Worker C",
            prompt="Draft the leaf report",
            parent_session_id=str(parent_session.id),
            child_session_id=None,
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id), str(agent_c_id)],
            depth=2,
        )
        db.add(task_bc)
        await db.flush()

        child_session = _delegation_session(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_c_id,
            peer_agent_id=agent_b_id,
            parent_session_id=parent_session.id,
            root_session_id=parent_session.id,
            runtime_task_id=task_bc.id,
            title="Delegation: Leaf Worker C",
        )
        db.add(child_session)
        await db.flush()
        task_bc.child_session_id = str(child_session.id)

        c_run = RuntimeTask(
            task_type="a2a_continuation",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=agent_c_id,
            child_agent_id=agent_c_id,
            child_agent_name="Leaf Worker C",
            prompt="Draft the leaf report",
            parent_session_id=str(child_session.id),
            child_session_id=str(child_session.id),
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id), str(agent_c_id)],
            depth=2,
            metadata_json={"turn_id": f"turn-{uuid.uuid4().hex}"},
        )
        db.add(c_run)
        await db.commit()

    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_a_id": agent_a_id,
        "agent_b_id": agent_b_id,
        "agent_c_id": agent_c_id,
        "root_session_id": root_session.id,
        "parent_session_id": parent_session.id,
        "child_session_id": child_session.id,
        "b_active_run_id": b_active_run.id,
        "b_active_turn_id": b_turn_id,
        "c_run_id": c_run.id,
    }


async def _clear_outbox(owner_sessionmaker) -> None:
    async with owner_sessionmaker() as db:
        await db.execute(delete(RuntimeNotificationOutbox))
        await db.execute(delete(RuntimeResultIntegrationPage))
        await db.execute(delete(RuntimeResultMailboxCursor))
        await db.execute(delete(RuntimeResultObject))
        await db.commit()


async def _drive_terminal_seam(owner_sessionmaker, *, tenant_id: uuid.UUID, task_id: uuid.UUID) -> None:
    """Terminalize a run through the one shared web-chat terminal seam."""

    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        assert task is not None
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status="completed",
            result_summary="NESTED-RETURN-MARKER leaf result ready",
            metadata_json=None,
            terminal_source="assistant_message_finalizer",
        )
        await db.commit()


@pytest.mark.asyncio
async def test_nested_completion_delivers_into_active_delegation_parent(owner_sessionmaker, monkeypatch) -> None:
    """C completes while B's delegation parent is ACTIVE: the durable result
    page must be admitted into B's session through the exact
    runtime_result_integration authority — not the user writable gate (409)
    and not the a2a peer lane (B's peer is A; the source is the runtime)."""

    from app.services.session_v2_persistence import resolve_session_command_authority

    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    seeded = await _seed_nested(owner_sessionmaker)
    tenant_id = seeded["tenant_id"]

    await _drive_terminal_seam(owner_sessionmaker, tenant_id=tenant_id, task_id=seeded["c_run_id"])
    async with owner_sessionmaker() as db:
        outbox_row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(seeded["c_run_id"])
                )
            )
        ).scalar_one()
        assert outbox_row.parent_session_id == seeded["parent_session_id"]
        assert outbox_row.parent_agent_id == seeded["agent_b_id"]
        assert outbox_row.source_kind == "a2a_continuation"

    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    counts = await service.drain_once(worker_id="nested-return-worker")
    async with owner_sessionmaker() as db:
        page_error = (
            await db.execute(
                select(RuntimeResultIntegrationPage.last_error).where(
                    RuntimeResultIntegrationPage.parent_session_id == seeded["parent_session_id"]
                )
            )
        ).scalar_one()
    assert counts["delivered"] == 1, (counts, page_error)

    async with owner_sessionmaker() as db:
        page = (
            await db.execute(
                select(RuntimeResultIntegrationPage).where(
                    RuntimeResultIntegrationPage.parent_session_id == seeded["parent_session_id"]
                )
            )
        ).scalar_one()
        assert page.status == "delivered"

        wake_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == seeded["parent_session_id"],
                        ChatTranscriptEvent.event_type == "agent_task_notification",
                    )
                )
            ).scalars()
        )
        assert len(wake_events) == 1

        # The accepted input lives on the canonical Session V2 plane: one
        # steer command/input into B's ACTIVE run, system role preserved,
        # principal compatibility kept (user / root user), and the typed
        # runtime_result_integration stamp selecting the recovery lane.
        row = (
            await db.execute(
                select(SessionTurnInput).where(
                    SessionTurnInput.session_id == seeded["parent_session_id"],
                    SessionTurnInput.intent == "steer_current_turn",
                )
            )
        ).scalar_one()
        assert row.target_run_id == seeded["b_active_run_id"]
        assert row.status in {"queued", "bound", "applied"}
        parts = list(row.content_parts_json or [])
        assert parts and parts[0].get("role") == "system"

        command = await db.get(SessionCommand, row.command_id)
        assert command is not None
        assert command.principal_type == "user"
        assert command.principal_id == seeded["user_id"]
        stamp = dict((command.target_json or {}).get("session_command_authority") or {})
        assert stamp.get("schema") == "hive.session_command_authority.runtime_result_integration.v1"
        assert stamp.get("authority_source") == "runtime_result_integration"
        assert stamp.get("integration_page_id") == str(page.id)
        # Volatile claim data never enters the durable stamp.
        assert "claim_token" not in stamp and "claimed_by" not in stamp

        # Fresh-worker recovery AFTER the page reached its durable delivered
        # terminal status: immutable route/authority facts still revalidate.
        session = await db.get(ChatSession, seeded["parent_session_id"])
        context = await resolve_session_command_authority(
            db, command=command, session=session, action="mutate_session_input"
        )
        assert context.authority.authority_source == "runtime_result_integration"
        assert context.authority.principal_type == "user"
        assert context.authority.principal_id == seeded["user_id"]
        assert context.actor.id == seeded["user_id"]

    # Delivery replay is deduplicated: no second event, no second input.
    replay = await service.drain_once(worker_id="nested-return-worker")
    assert replay["delivered"] == 0
    async with owner_sessionmaker() as db:
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == seeded["parent_session_id"],
                    ChatTranscriptEvent.event_type == "agent_task_notification",
                )
            )
        ).scalar_one()
        input_count = (
            await db.execute(
                select(func.count())
                .select_from(SessionTurnInput)
                .where(SessionTurnInput.session_id == seeded["parent_session_id"])
            )
        ).scalar_one()
    assert event_count == 1
    assert input_count == 1


@pytest.mark.asyncio
async def test_terminal_race_successor_stays_a2a_continuation(owner_sessionmaker, monkeypatch) -> None:
    """If B's active run terminalizes after the result input was accepted,
    the deterministic FIFO successor on the delegation parent must stay
    completion-outbox eligible (a2a_continuation) and replay-safe."""

    from app.services.session_input_dispatch import recover_dispatched_terminal_steers_once

    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    seeded = await _seed_nested(owner_sessionmaker)
    tenant_id = seeded["tenant_id"]

    await _drive_terminal_seam(owner_sessionmaker, tenant_id=tenant_id, task_id=seeded["c_run_id"])
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    counts = await service.drain_once(worker_id="nested-return-worker")
    assert counts["delivered"] == 1, counts

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(SessionTurnInput).where(SessionTurnInput.session_id == seeded["parent_session_id"]))
        ).scalar_one()
        input_id = row.id
        b_run = await db.get(RuntimeTask, seeded["b_active_run_id"])
        b_run.status = "completed"
        await db.commit()

    async with owner_sessionmaker() as db:
        recovery = await recover_dispatched_terminal_steers_once(
            db, worker_id="nested-return-recovery", stale_after=timedelta(seconds=0), tenant_id=tenant_id
        )
        assert recovery["dispatched"] == 1, recovery
        successor = await db.get(RuntimeTask, uuid.uuid5(input_id, "session-v2-successor-run"))
        assert successor is not None
        assert successor.task_type == "a2a_continuation"
        assert successor.parent_agent_id == seeded["agent_b_id"]
        assert successor.parent_session_id == str(seeded["parent_session_id"])

    async with owner_sessionmaker() as db:
        replay = await recover_dispatched_terminal_steers_once(
            db, worker_id="nested-return-recovery-2", stale_after=timedelta(seconds=0), tenant_id=tenant_id
        )
        assert replay["claimed"] == 0, replay


@pytest.mark.asyncio
async def test_forged_or_wrong_target_page_is_denied(owner_sessionmaker, monkeypatch) -> None:
    """Forged page ids, cross-tenant bindings, and wrong targets never mint
    the runtime result integration authority."""

    from app.services.session_live_input import submit_live_human_input
    from app.services.session_v2_persistence import resolve_runtime_result_integration_authority

    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    seeded = await _seed_nested(owner_sessionmaker)
    tenant_id = seeded["tenant_id"]
    await _drive_terminal_seam(owner_sessionmaker, tenant_id=tenant_id, task_id=seeded["c_run_id"])
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    counts = await service.drain_once(worker_id="nested-return-worker")
    assert counts["delivered"] == 1, counts

    async with owner_sessionmaker() as db:
        page = (
            await db.execute(
                select(RuntimeResultIntegrationPage).where(
                    RuntimeResultIntegrationPage.parent_session_id == seeded["parent_session_id"]
                )
            )
        ).scalar_one()
        agent = await db.get(Agent, seeded["agent_b_id"])
        user = await db.get(User, seeded["user_id"])
        session = await db.get(ChatSession, seeded["parent_session_id"])
        root_session = await db.get(ChatSession, seeded["root_session_id"])
        # Capture ids up front: the rollback between sub-cases expires ORM
        # objects, and expired-attribute refresh is implicit sync IO.
        page_id = page.id
        session_id = session.id
        root_session_id = root_session.id

        # 1. Forged page id.
        with pytest.raises(PermissionError, match="runtime_result_integration_page_not_found"):
            await submit_live_human_input(
                db=db,
                agent=agent,
                user=user,
                session=session,
                content="forged page",
                source="runtime_result_integration",
                runtime_result_page_id=uuid.uuid4(),
            )
        await db.rollback()

        # 2. Real page, wrong target session (the human root session R is not
        #    a durable delegation child, so the lane never activates for it).
        with pytest.raises(PermissionError, match="runtime_result_integration_target_not_delegation"):
            await resolve_runtime_result_integration_authority(
                db,
                page_id=page_id,
                agent_id=seeded["agent_a_id"],
                session_id=root_session_id,
                action="mutate_session_input",
            )
        await db.rollback()

        # 3. Real page, action outside mutate_session_input.
        with pytest.raises(PermissionError, match="runtime_result_integration_action_not_allowed"):
            await resolve_runtime_result_integration_authority(
                db,
                page_id=page_id,
                agent_id=seeded["agent_b_id"],
                session_id=session_id,
                action="mutate_session_control",
            )
        await db.rollback()

    # 4. Cross-tenant: the same page id against a different tenant's nested
    #    seed must never resolve.
    seeded2 = await _seed_nested(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        with pytest.raises(PermissionError, match="runtime_result_integration"):
            await resolve_runtime_result_integration_authority(
                db,
                page_id=page_id,
                agent_id=seeded2["agent_b_id"],
                session_id=seeded2["parent_session_id"],
                action="mutate_session_input",
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_missing_linked_outbox_evidence_is_denied(owner_sessionmaker) -> None:
    """A page row without linked outbox/source evidence is not authority."""

    from app.services.session_v2_persistence import resolve_runtime_result_integration_authority

    seeded = await _seed_nested(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        orphan_page = RuntimeResultIntegrationPage(
            tenant_id=seeded["tenant_id"],
            parent_session_id=seeded["parent_session_id"],
            parent_agent_id=seeded["agent_b_id"],
            parent_user_id=seeded["user_id"],
            root_scope_key=f"orphan-{uuid.uuid4().hex[:8]}",
            integration_epoch=1,
            delivery_mode="parent_continuation",
            mailbox_sequence_start=1,
            mailbox_sequence_end=1,
            item_count=1,
            manifest_json={"items": []},
            manifest_sha256="0" * 64,
            status="processing",
            claimed_by="forged-worker",
            claim_token=uuid.uuid4(),
        )
        db.add(orphan_page)
        await db.commit()

        with pytest.raises(PermissionError, match="runtime_result_integration_source_missing"):
            await resolve_runtime_result_integration_authority(
                db,
                page_id=orphan_page.id,
                agent_id=seeded["agent_b_id"],
                session_id=seeded["parent_session_id"],
                action="mutate_session_input",
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_tampered_stamp_is_denied_on_recovery_rebuild(owner_sessionmaker, monkeypatch) -> None:
    """The stamp only selects the recovery validator: a forged
    integration_page_id in the stamp cannot pass fresh-worker revalidation."""

    from app.services.session_v2_persistence import resolve_session_command_authority

    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    seeded = await _seed_nested(owner_sessionmaker)
    await _drive_terminal_seam(owner_sessionmaker, tenant_id=seeded["tenant_id"], task_id=seeded["c_run_id"])
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    counts = await service.drain_once(worker_id="nested-return-worker")
    assert counts["delivered"] == 1, counts

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(SessionTurnInput).where(SessionTurnInput.session_id == seeded["parent_session_id"]))
        ).scalar_one()
        command = await db.get(SessionCommand, row.command_id)
        target = dict(command.target_json or {})
        stamp = dict(target["session_command_authority"])
        stamp["integration_page_id"] = str(uuid.uuid4())
        target["session_command_authority"] = stamp
        command.target_json = target
        await db.commit()

        session = await db.get(ChatSession, seeded["parent_session_id"])
        with pytest.raises(PermissionError, match="runtime_result_integration_page_not_found"):
            await resolve_session_command_authority(db, command=command, session=session, action="mutate_session_input")
        await db.rollback()


@pytest.mark.asyncio
async def test_admission_requires_actual_processing_delivery_state(owner_sessionmaker, monkeypatch) -> None:
    """New-command admission requires the durable claimed processing delivery
    state; an unclaimed prepared page is not admission authority, while
    already-accepted recovery stays lifecycle-tolerant."""

    from app.services.session_v2_persistence import resolve_runtime_result_integration_authority

    monkeypatch.setattr("app.services.runtime_budget_service.async_session", owner_sessionmaker)
    await _clear_outbox(owner_sessionmaker)
    seeded = await _seed_nested(owner_sessionmaker)
    tenant_id = seeded["tenant_id"]
    await _drive_terminal_seam(owner_sessionmaker, tenant_id=tenant_id, task_id=seeded["c_run_id"])

    # Prepare the page WITHOUT delivering: claim + prepare only.
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    claimed = await service.claim_batch(worker_id="nested-return-prepare", limit=10)
    assert claimed, "expected one claimed outbox row"
    pages = await service.prepare_integration_pages(worker_id="nested-return-prepare", claimed=claimed)
    assert len(pages) == 1

    async with owner_sessionmaker() as db:
        stored = (
            await db.execute(
                select(RuntimeResultIntegrationPage).where(
                    RuntimeResultIntegrationPage.parent_session_id == seeded["parent_session_id"]
                )
            )
        ).scalar_one()
        assert stored.status == "processing"
        # Simulate an unclaimed prepared page: durable claim evidence absent.
        stored.status = "prepared"
        stored.claimed_by = None
        stored.claim_token = None
        await db.commit()

        with pytest.raises(PermissionError, match="runtime_result_integration_not_in_delivery"):
            await resolve_runtime_result_integration_authority(
                db,
                page_id=stored.id,
                agent_id=seeded["agent_b_id"],
                session_id=seeded["parent_session_id"],
                action="mutate_session_input",
            )
        await db.rollback()
