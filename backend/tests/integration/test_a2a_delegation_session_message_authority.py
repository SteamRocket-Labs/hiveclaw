"""A2A delegation child Session V2 write admission regressions (real PostgreSQL).

Production defect (Day1): a parent agent calling ``send_agent_session_message``
against an ACTIVE ``delegation_run`` child reaches
``continue_agent_session_from_mailbox`` -> ``_submit_active_session_input`` ->
``submit_live_human_input`` -> ``resolve_session_mutation_authority``, which
applies the product read-only ``require_writable_session`` gate and returns
HTTP 409 ``session_read_only``.  The read-only boundary itself is correct for
user-facing HTTP/WS mutation and must NOT be weakened; the missing piece is a
narrow server-derived authority for the authenticated parent Agent's message
to its exact active A2A delegation child.

These tests drive the REAL tool handler and the REAL Session V2
command/input/admission/dispatch path against Testcontainers PostgreSQL.
Only DB session factories are rebound to the container; no continuation,
authority, or dispatch behavior is faked.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import tenant_scoped_session as real_tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
from app.models.tenant import Tenant
from app.models.user import User
from app.services import runtime_task_service
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


async def _mk_tenant(db, *, prefix: str = "a2a") -> uuid.UUID:
    tenant = Tenant(name=f"{prefix} Tenant", slug=f"{prefix}-{uuid.uuid4().hex[:10]}")
    db.add(tenant)
    await db.flush()
    return tenant.id


async def _mk_user(db, tenant_id: uuid.UUID, *, role: str = "org_admin") -> uuid.UUID:
    user = User(
        username=f"a2a-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="A2A Owner",
        tenant_id=tenant_id,
        role=role,
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
        role_description="A2A delegation regression agent.",
        status="idle",
    )
    db.add(agent)
    await db.flush()
    return agent.id


async def _seed_delegation(owner_sessionmaker) -> dict[str, uuid.UUID | str]:
    """Seed the exact durable A2A delegation binding the orchestrator persists.

    Mirrors ``orchestrator._ensure_peer_delegation_session`` durable columns:
    the child session is ``session_kind="delegation_run"`` /
    ``runtime_source="delegation"`` / ``source_channel="agent"`` with
    ``peer_agent_id`` = delegating parent, owned by the root user, bound to the
    root session and to the durable delegation RuntimeTask whose
    ``parent_agent_id`` is the delegating agent.
    """

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        user_id = await _mk_user(db, tenant_id)
        parent_agent_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Coordinator")
        child_agent_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Worker")

        root_session = ChatSession(
            agent_id=parent_agent_id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"root-{uuid.uuid4().hex[:8]}",
        )
        db.add(root_session)
        await db.flush()

        delegation_task = RuntimeTask(
            task_type="delegation",
            status="completed",
            tenant_id=tenant_id,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            child_agent_name="Worker",
            prompt="Draft the delegated report",
            parent_session_id=str(root_session.id),
            child_session_id=None,  # rebound below once the session exists
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(parent_agent_id), str(child_agent_id)],
            depth=1,
        )
        db.add(delegation_task)
        await db.flush()

        child_session = ChatSession(
            agent_id=child_agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            peer_agent_id=parent_agent_id,
            source_channel="agent",
            session_kind="delegation_run",
            actor_type="agent",
            runtime_source="delegation",
            visibility_scope="agent_owner",
            listed_surface="chat",
            parent_session_id=root_session.id,
            root_session_id=root_session.id,
            runtime_task_id=delegation_task.id,
            title="Delegation: Worker",
            transcript_metadata_json={
                "source": "agent",
                "interaction_type": "delegation",
                "from_agent": str(parent_agent_id),
                "to_agent": str(child_agent_id),
                "runtime_task_id": str(delegation_task.id),
                "delegation_state": "completed",
            },
        )
        db.add(child_session)
        await db.flush()
        delegation_task.child_session_id = str(child_session.id)

        # The ACTIVE child run the message must steer into.  Persisted
        # directly so this regression stays independent of the unrelated
        # runtime_budget_runs migration drift; the real handler still selects
        # it through the canonical ``_find_active_run`` executable-chat query.
        turn_id = f"turn-{uuid.uuid4().hex}"
        active_run = RuntimeTask(
            task_type="a2a_continuation",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=child_agent_id,
            child_agent_id=child_agent_id,
            child_agent_name="Worker",
            prompt="Draft the delegated report",
            parent_session_id=str(child_session.id),
            child_session_id=str(child_session.id),
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(parent_agent_id), str(child_agent_id)],
            depth=1,
            metadata_json={"turn_id": turn_id},
        )
        db.add(active_run)
        await db.commit()

    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "parent_agent_id": parent_agent_id,
        "child_agent_id": child_agent_id,
        "root_session_id": root_session.id,
        "child_session_id": child_session.id,
        "delegation_task_id": delegation_task.id,
        "active_run_id": active_run.id,
        "active_turn_id": turn_id,
    }


async def _seed_writable_web_session(owner_sessionmaker) -> dict[str, uuid.UUID | str]:
    """Seed an ordinary writable web session with an ACTIVE web_chat_turn run."""

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db, prefix="web")
        user_id = await _mk_user(db, tenant_id)
        agent_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Assistant")
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"web-{uuid.uuid4().hex[:8]}",
        )
        db.add(session)
        await db.flush()
        turn_id = f"turn-{uuid.uuid4().hex}"
        active_run = RuntimeTask(
            task_type="web_chat_turn",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=agent_id,
            child_agent_id=agent_id,
            prompt="Hello",
            parent_session_id=str(session.id),
            child_session_id=str(session.id),
            root_user_id=user_id,
            root_session_id=str(session.id),
            metadata_json={"turn_id": turn_id},
        )
        db.add(active_run)
        await db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session.id,
        "active_run_id": active_run.id,
        "active_turn_id": turn_id,
    }


def _bind_db_factories(monkeypatch, sessionmaker) -> None:
    """Point only DB session factories at the Testcontainers engine."""

    def _scoped(tenant_id=None, **kwargs):
        return real_tenant_scoped_session(tenant_id, session_factory=sessionmaker, **kwargs)

    import app.tools.handlers.subagent as handler_mod

    monkeypatch.setattr(handler_mod, "tenant_scoped_session", _scoped)
    monkeypatch.setattr(runtime_task_service, "async_session", sessionmaker)


def _message_request(seeded: dict, message: str, **context_overrides) -> ToolExecutionRequest:
    context = ToolExecutionContext(
        agent_id=seeded["parent_agent_id"],
        user_id=seeded["user_id"],
        tenant_id=str(seeded["tenant_id"]),
        workspace=Path("/tmp"),
        session_id=str(seeded["root_session_id"]),
    )
    for key, value in context_overrides.items():
        setattr(context, key, value)
    return ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(seeded["child_session_id"]), "message": message},
        context=context,
    )


async def _terminalize_run(owner_sessionmaker, run_id: uuid.UUID) -> None:
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "completed"
        await db.commit()


@pytest.mark.asyncio
async def test_parent_agent_message_to_active_delegation_child_is_admitted(owner_sessionmaker, monkeypatch) -> None:
    """A send to an ACTIVE delegation_run child must be admitted, not 409.

    The real ``send_agent_session_message`` handler resolves the session,
    authenticates the parent through the durable delegation RuntimeTask, and
    reaches the canonical active steer path.  On the defective HEAD the call
    raises HTTP 409 ``session_read_only`` from the user-facing read-only
    gate, even though the caller is the authenticated parent Agent bound by
    the durable delegation RuntimeTask.
    """

    import app.tools.handlers.subagent as handler_mod

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_delegation(owner_sessionmaker)

    result = json.loads(
        await handler_mod.send_agent_session_message(_message_request(seeded, "Also cover appendix B."))
    )
    assert result["ok"] is True, result
    assert result["status"] == "queued", result
    assert result["consumer"] == "session_v2_round_input", result

    # The accepted message must live on the canonical Session V2 plane:
    # one command, one steer input, one admitted admission — no side mailbox.
    async with owner_sessionmaker() as db:
        inputs = list(
            (
                await db.execute(
                    select(SessionTurnInput).where(
                        SessionTurnInput.tenant_id == seeded["tenant_id"],
                        SessionTurnInput.session_id == seeded["child_session_id"],
                        SessionTurnInput.intent == "steer_current_turn",
                    )
                )
            ).scalars()
        )
        assert len(inputs) == 1
        row = inputs[0]
        assert row.target_run_id == seeded["active_run_id"]
        assert row.status in {"queued", "bound", "applied"}
        command = await db.get(SessionCommand, row.command_id)
        assert command is not None
        assert command.namespace == "human_input"
        assert command.command_kind == "steer_current_turn"
        admission = (
            await db.execute(
                select(SessionInputAdmission).where(
                    SessionInputAdmission.input_id == row.id,
                    SessionInputAdmission.input_revision == row.revision,
                )
            )
        ).scalar_one()
        assert admission.state == "admitted"


@pytest.mark.asyncio
async def test_owner_and_manager_mutation_of_delegation_run_still_409(owner_sessionmaker) -> None:
    """The product read-only boundary is NOT weakened for user-facing writes."""

    from app.services.session_live_input import submit_live_human_input
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    seeded = await _seed_delegation(owner_sessionmaker)

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seeded["child_agent_id"])
        owner = await db.get(User, seeded["user_id"])
        session = await db.get(ChatSession, seeded["child_session_id"])

        with pytest.raises(HTTPException) as owner_exc:
            await submit_live_human_input(
                db=db,
                agent=agent,
                user=owner,
                session=session,
                content="user tries to steer the delegation run",
                source="web",
            )
        assert owner_exc.value.status_code == 409
        assert owner_exc.value.detail["code"] == "session_read_only"
        await db.rollback()

        manager_id = await _mk_user(db, seeded["tenant_id"], role="org_admin")
        manager = await db.get(User, manager_id)
        with pytest.raises(HTTPException) as manager_exc:
            await resolve_session_mutation_authority(
                db,
                user=manager,
                agent_id=seeded["child_agent_id"],
                session_id=seeded["child_session_id"],
                action="mutate_session_input",
                allow_manager_override=True,
                manager_override_reason="incident inspection",
            )
        assert manager_exc.value.status_code == 409
        assert manager_exc.value.detail["code"] == "session_read_only"
        await db.rollback()


@pytest.mark.asyncio
async def test_wrong_peer_root_session_and_runtime_task_authority_denied(owner_sessionmaker, monkeypatch) -> None:
    """Only the exact authenticated parent Agent may use the peer lane."""

    import app.tools.handlers.subagent as handler_mod
    from app.services.session_live_input import submit_live_human_input

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_delegation(owner_sessionmaker)

    # 1. An unrelated third agent never even resolves the child session.
    async with owner_sessionmaker() as db:
        third_agent_id = await _mk_agent(db, tenant_id=seeded["tenant_id"], user_id=seeded["user_id"], name="Intruder")
        other_root = ChatSession(
            agent_id=seeded["parent_agent_id"],
            user_id=seeded["user_id"],
            tenant_id=seeded["tenant_id"],
            title=f"other-{uuid.uuid4().hex[:8]}",
        )
        db.add(other_root)
        await db.commit()
        other_root_id = other_root.id

    denied = json.loads(
        await handler_mod.send_agent_session_message(_message_request(seeded, "steer", agent_id=third_agent_id))
    )
    assert denied["ok"] is False
    assert "not found" in denied["error"]

    # 2. The right parent agent from the WRONG root session is denied by the
    #    durable RuntimeTask root authority before any continuation happens.
    denied_root = json.loads(
        await handler_mod.send_agent_session_message(_message_request(seeded, "steer", session_id=str(other_root_id)))
    )
    assert denied_root["ok"] is False
    assert denied_root.get("reason") == "root_session_mismatch"

    # 3. Resolver level: a peer that is not the durable delegation parent is
    #    denied with a typed reason even inside the peer lane.
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seeded["child_agent_id"])
        user = await db.get(User, seeded["user_id"])
        session = await db.get(ChatSession, seeded["child_session_id"])
        with pytest.raises(PermissionError, match="a2a_delegation_peer_agent_mismatch"):
            await submit_live_human_input(
                db=db,
                agent=agent,
                user=user,
                session=session,
                content="forged peer steer",
                source="agent_session_mailbox",
                a2a_peer_agent_id=third_agent_id,
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_cross_tenant_peer_and_tampered_runtime_task_denied(owner_sessionmaker) -> None:
    """Tenant and durable RuntimeTask bindings are mechanically revalidated."""

    from app.services.session_v2_persistence import resolve_a2a_delegation_peer_authority

    seeded = await _seed_delegation(owner_sessionmaker)

    # Cross-tenant peer: rebind peer_agent_id to an agent in another tenant.
    async with owner_sessionmaker() as db:
        other_tenant_id = await _mk_tenant(db, prefix="other")
        other_user_id = await _mk_user(db, other_tenant_id)
        foreign_agent_id = await _mk_agent(db, tenant_id=other_tenant_id, user_id=other_user_id, name="Foreign")
        session = await db.get(ChatSession, seeded["child_session_id"])
        session.peer_agent_id = foreign_agent_id
        await db.commit()

        with pytest.raises(PermissionError, match="a2a_delegation_peer_agent_mismatch"):
            await resolve_a2a_delegation_peer_authority(
                db,
                peer_agent_id=foreign_agent_id,
                agent_id=seeded["child_agent_id"],
                session_id=seeded["child_session_id"],
                action="mutate_session_input",
            )
        await db.rollback()

    # Tampered RuntimeTask authority: the durable delegation task no longer
    # names the peer as its parent agent.
    seeded2 = await _seed_delegation(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        third_agent_id = await _mk_agent(
            db, tenant_id=seeded2["tenant_id"], user_id=seeded2["user_id"], name="Intruder"
        )
        task = await db.get(RuntimeTask, seeded2["delegation_task_id"])
        task.parent_agent_id = third_agent_id
        await db.commit()

        with pytest.raises(PermissionError, match="a2a_delegation_peer_runtime_task_mismatch"):
            await resolve_a2a_delegation_peer_authority(
                db,
                peer_agent_id=seeded2["parent_agent_id"],
                agent_id=seeded2["child_agent_id"],
                session_id=seeded2["child_session_id"],
                action="mutate_session_input",
            )
        await db.rollback()


@pytest.mark.asyncio
async def test_terminal_race_rolls_into_exactly_one_a2a_continuation_successor(owner_sessionmaker, monkeypatch) -> None:
    """Terminal target after accept -> one deterministic a2a_continuation FIFO
    successor, rebuilt from durable columns in fresh sessions, replay-safe."""

    import app.tools.handlers.subagent as handler_mod
    from app.services.session_input_dispatch import recover_dispatched_terminal_steers_once
    from app.services.session_v2_persistence import resolve_session_command_authority

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_delegation(owner_sessionmaker)

    accepted = json.loads(
        await handler_mod.send_agent_session_message(_message_request(seeded, "Wrap up the appendix."))
    )
    assert accepted["ok"] is True, accepted
    assert accepted["status"] == "queued", accepted

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(SessionTurnInput).where(
                    SessionTurnInput.session_id == seeded["child_session_id"],
                    SessionTurnInput.intent == "steer_current_turn",
                )
            )
        ).scalar_one()
        input_id = row.id
        command_id = row.command_id
        assert row.status == "queued"
        assert row.target_run_id == seeded["active_run_id"]

    # The selected active run terminalizes AFTER the steer was accepted and
    # mailed — the terminal race this package must settle deterministically.
    await _terminalize_run(owner_sessionmaker, seeded["active_run_id"])

    # Fresh DB session/worker: rebuild authority from durable columns, roll
    # the steer over, and start the deterministic FIFO successor.
    async with owner_sessionmaker() as db:
        counts = await recover_dispatched_terminal_steers_once(
            db, worker_id="test-recovery-1", stale_after=timedelta(seconds=0), tenant_id=seeded["tenant_id"]
        )
        assert counts["dispatched"] == 1, counts

    successor_run_id = uuid.uuid5(input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        assert row.status == "rolled_over"
        assert row.rolled_over_to_turn_id
        successor = await db.get(RuntimeTask, successor_run_id)
        assert successor is not None
        # The successor stays completion-outbox eligible and keeps the
        # durable parent return route (child session bound by tenant +
        # parent_session_id + parent_agent_id; root user/session columns).
        assert successor.task_type == "a2a_continuation"
        assert successor.parent_agent_id == seeded["child_agent_id"]
        assert successor.parent_session_id == str(seeded["child_session_id"])
        assert successor.root_user_id == seeded["user_id"]
        assert successor.root_session_id == str(seeded["root_session_id"])
        child_session = await db.get(ChatSession, seeded["child_session_id"])
        assert child_session.parent_session_id == seeded["root_session_id"]
        assert child_session.peer_agent_id == seeded["parent_agent_id"]

        # The stamp never grants authority by itself: a fresh worker rebuild
        # revalidates the durable binding and reconstructs the peer lane.
        command = await db.get(SessionCommand, command_id)
        context = await resolve_session_command_authority(
            db, command=command, session=child_session, action="mutate_session_input"
        )
        assert context.authority.authority_source == "a2a_delegation_peer"
        assert context.authority.principal_type == "user"
        assert context.authority.principal_id == seeded["user_id"]
        assert context.actor.id == seeded["user_id"]
        assert context.authority.tenant_id == seeded["tenant_id"]

    # Replay the same recovery sweep: no duplicate successor, no duplicate
    # rollover — exactly one a2a_continuation successor exists.
    async with owner_sessionmaker() as db:
        replay = await recover_dispatched_terminal_steers_once(
            db, worker_id="test-recovery-2", stale_after=timedelta(seconds=0), tenant_id=seeded["tenant_id"]
        )
        assert replay["claimed"] == 0, replay
        successors = list(
            (
                await db.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.tenant_id == seeded["tenant_id"],
                        RuntimeTask.parent_session_id == str(seeded["child_session_id"]),
                        RuntimeTask.task_type == "a2a_continuation",
                    )
                )
            ).scalars()
        )
        assert {task.id for task in successors} == {seeded["active_run_id"], successor_run_id}


@pytest.mark.asyncio
async def test_tampered_command_stamp_is_denied_on_recovery_rebuild(owner_sessionmaker, monkeypatch) -> None:
    """The target_json stamp only selects the revalidation lane — a forged
    stamp cannot pass the durable binding revalidation on worker recovery."""

    import app.tools.handlers.subagent as handler_mod
    from app.services.session_v2_persistence import resolve_session_command_authority

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_delegation(owner_sessionmaker)

    accepted = json.loads(
        await handler_mod.send_agent_session_message(_message_request(seeded, "Add the summary table."))
    )
    assert accepted["ok"] is True, accepted

    async with owner_sessionmaker() as db:
        third_agent_id = await _mk_agent(db, tenant_id=seeded["tenant_id"], user_id=seeded["user_id"], name="Intruder")
        row = (
            await db.execute(select(SessionTurnInput).where(SessionTurnInput.session_id == seeded["child_session_id"]))
        ).scalar_one()
        command = await db.get(SessionCommand, row.command_id)
        target = dict(command.target_json or {})
        stamp = dict(target["session_command_authority"])
        stamp["peer_agent_id"] = str(third_agent_id)
        target["session_command_authority"] = stamp
        command.target_json = target
        await db.commit()

        session = await db.get(ChatSession, seeded["child_session_id"])
        with pytest.raises(PermissionError, match="a2a_delegation_peer_agent_mismatch"):
            await resolve_session_command_authority(db, command=command, session=session, action="mutate_session_input")
        await db.rollback()


@pytest.mark.asyncio
async def test_peer_lane_idempotent_replay_makes_one_command_and_input(owner_sessionmaker) -> None:
    """Duplicate submission with the same input id/key replays exactly once."""

    from app.services.session_live_input import submit_live_human_input

    seeded = await _seed_delegation(owner_sessionmaker)
    input_id = uuid.uuid4()

    async def _submit(db) -> dict:
        agent = await db.get(Agent, seeded["child_agent_id"])
        user = await db.get(User, seeded["user_id"])
        session = await db.get(ChatSession, seeded["child_session_id"])
        return await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="Cover appendix B.",
            source="agent_session_mailbox",
            input_id=input_id,
            idempotency_key=f"agent-session-event:{input_id}",
            requested_kind="steer_current_turn",
            expected_turn_id=seeded["active_turn_id"],
            expected_run_id=seeded["active_run_id"],
            terminal_fallback="queue_next_turn",
            a2a_peer_agent_id=seeded["parent_agent_id"],
        )

    async with owner_sessionmaker() as db:
        first = await _submit(db)
    assert first["replayed"] is False

    async with owner_sessionmaker() as db:
        replay = await _submit(db)
    assert replay["replayed"] is True
    assert replay["input_id"] == str(input_id)

    async with owner_sessionmaker() as db:
        commands = list(
            (
                await db.execute(select(SessionCommand).where(SessionCommand.session_id == seeded["child_session_id"]))
            ).scalars()
        )
        assert len(commands) == 1
        inputs = list(
            (
                await db.execute(
                    select(SessionTurnInput).where(SessionTurnInput.session_id == seeded["child_session_id"])
                )
            ).scalars()
        )
        assert len(inputs) == 1


@pytest.mark.asyncio
async def test_writable_web_session_unchanged_and_successor_stays_web_chat_turn(
    owner_sessionmaker, monkeypatch
) -> None:
    """Ordinary writable sessions keep the user authority lane: no stamp, and
    their terminal-fallback FIFO successor remains ``web_chat_turn``."""

    import app.tools.handlers.subagent as handler_mod
    from app.services.session_input_dispatch import recover_dispatched_terminal_steers_once

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_writable_web_session(owner_sessionmaker)

    # Own root session: the handler's root-session branch expects the tool
    # context session to match the session's durable root binding.
    context = ToolExecutionContext(
        agent_id=seeded["agent_id"],
        user_id=seeded["user_id"],
        tenant_id=str(seeded["tenant_id"]),
        workspace=Path("/tmp"),
        session_id=None,
    )
    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(seeded["session_id"]), "message": "One more thing."},
        context=context,
    )
    accepted = json.loads(await handler_mod.send_agent_session_message(request))
    assert accepted["ok"] is True, accepted
    assert accepted["status"] == "queued", accepted

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(select(SessionTurnInput).where(SessionTurnInput.session_id == seeded["session_id"]))
        ).scalar_one()
        command = await db.get(SessionCommand, row.command_id)
        # User authority lane: no A2A peer stamp is minted.
        assert "session_command_authority" not in dict(command.target_json or {})
        assert command.principal_type == "user"
        assert command.principal_id == seeded["user_id"]
        input_id = row.id

    await _terminalize_run(owner_sessionmaker, seeded["active_run_id"])

    async with owner_sessionmaker() as db:
        counts = await recover_dispatched_terminal_steers_once(
            db, worker_id="test-web-recovery", stale_after=timedelta(seconds=0), tenant_id=seeded["tenant_id"]
        )
        assert counts["dispatched"] == 1, counts
        successor = await db.get(RuntimeTask, uuid.uuid5(input_id, "session-v2-successor-run"))
        assert successor is not None
        assert successor.task_type == "web_chat_turn"


async def _seed_nested_delegation(owner_sessionmaker) -> dict[str, uuid.UUID | str]:
    """Seed the production depth-2 nested delegation shape (A→B→C).

    Mirrors ``_ensure_peer_delegation_session`` durable facts for the nested
    child C: ``C.parent_session_id = P`` (the immediate parent delegation
    session) and ``C.root_session_id = P``, while C's durable delegation
    RuntimeTask inherits the chain root: ``parent_session_id = P`` but
    ``root_session_id = R`` (the human root session owned by agent A).  The
    authenticated peer for C is agent B.
    """

    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db, prefix="nest")
        user_id = await _mk_user(db, tenant_id)
        agent_a_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Root Coordinator")
        agent_b_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Middle Worker")
        agent_c_id = await _mk_agent(db, tenant_id=tenant_id, user_id=user_id, name="Leaf Worker")

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
            status="completed",
            tenant_id=tenant_id,
            parent_agent_id=agent_a_id,
            child_agent_id=agent_b_id,
            child_agent_name="Middle Worker",
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

        parent_session = ChatSession(
            agent_id=agent_b_id,
            tenant_id=tenant_id,
            user_id=user_id,
            peer_agent_id=agent_a_id,
            source_channel="agent",
            session_kind="delegation_run",
            actor_type="agent",
            runtime_source="delegation",
            visibility_scope="agent_owner",
            listed_surface="chat",
            parent_session_id=root_session.id,
            root_session_id=root_session.id,
            runtime_task_id=task_ab.id,
            title="Delegation: Middle Worker",
            transcript_metadata_json={"interaction_type": "delegation"},
        )
        db.add(parent_session)
        await db.flush()
        task_ab.child_session_id = str(parent_session.id)

        task_bc = RuntimeTask(
            task_type="delegation",
            status="completed",
            tenant_id=tenant_id,
            parent_agent_id=agent_b_id,
            child_agent_id=agent_c_id,
            child_agent_name="Leaf Worker",
            prompt="Draft the leaf report",
            parent_session_id=str(parent_session.id),
            child_session_id=None,
            root_user_id=user_id,
            # Chain root authority inherited from the execution principal:
            # intentionally NOT the immediate parent session (depth > 1).
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id), str(agent_c_id)],
            depth=2,
        )
        db.add(task_bc)
        await db.flush()

        child_session = ChatSession(
            agent_id=agent_c_id,
            tenant_id=tenant_id,
            user_id=user_id,
            peer_agent_id=agent_b_id,
            source_channel="agent",
            session_kind="delegation_run",
            actor_type="agent",
            runtime_source="delegation",
            visibility_scope="agent_owner",
            listed_surface="chat",
            parent_session_id=parent_session.id,
            # Production behavior: root_session_id = parent_session_id for the
            # delegation child — NOT the human root session.
            root_session_id=parent_session.id,
            runtime_task_id=task_bc.id,
            title="Delegation: Leaf Worker",
            transcript_metadata_json={"interaction_type": "delegation", "depth": 2},
        )
        db.add(child_session)
        await db.flush()
        task_bc.child_session_id = str(child_session.id)

        turn_id = f"turn-{uuid.uuid4().hex}"
        active_run = RuntimeTask(
            task_type="a2a_continuation",
            status="running",
            tenant_id=tenant_id,
            parent_agent_id=agent_c_id,
            child_agent_id=agent_c_id,
            child_agent_name="Leaf Worker",
            prompt="Draft the leaf report",
            parent_session_id=str(child_session.id),
            child_session_id=str(child_session.id),
            root_user_id=user_id,
            root_session_id=str(root_session.id),
            delegation_chain_json=["a2a", str(agent_a_id), str(agent_b_id), str(agent_c_id)],
            depth=2,
            metadata_json={"turn_id": turn_id},
        )
        db.add(active_run)
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
        "delegation_task_id": task_bc.id,
        "active_run_id": active_run.id,
        "active_turn_id": turn_id,
        # Tool context for the authenticated immediate peer (agent B): its
        # execution principal carries the chain root session R.
        "parent_agent_id": agent_b_id,
    }


@pytest.mark.asyncio
async def test_nested_delegation_peer_message_to_active_grandchild_is_admitted(owner_sessionmaker, monkeypatch) -> None:
    """Depth-2 nested delegation: B's send to ACTIVE C must pass, and a fresh
    worker must rebuild the same authority from durable columns.

    Production facts (``_ensure_peer_delegation_session``): the nested child
    session's ``root_session_id`` is the IMMEDIATE parent delegation session
    P, while its durable delegation RuntimeTask's ``root_session_id`` is the
    human root session R.  A validator that equates the two denies a
    legitimate nested parent — this regression locks the separated contract:
    immediate parent route via ``session.parent_session_id`` +
    ``task.parent_session_id``; chain root authority via
    ``task.root_session_id`` (tenant + root user, never peer ownership).
    """

    import app.tools.handlers.subagent as handler_mod
    from app.core.execution_context import ExecutionPrincipal
    from app.services.session_v2_persistence import resolve_session_command_authority

    _bind_db_factories(monkeypatch, owner_sessionmaker)
    seeded = await _seed_nested_delegation(owner_sessionmaker)

    request = ToolExecutionRequest(
        tool_name="send_agent_session_message",
        arguments={"child_session_id": str(seeded["child_session_id"]), "message": "Leaf follow-up."},
        context=ToolExecutionContext(
            agent_id=seeded["agent_b_id"],
            user_id=seeded["user_id"],
            tenant_id=str(seeded["tenant_id"]),
            workspace=Path("/tmp"),
            # Production nested facts: the tool runs INSIDE the immediate
            # parent delegation session P, and the carried execution
            # principal holds the chain root authority (root session R).
            session_id=str(seeded["parent_session_id"]),
            execution_principal=ExecutionPrincipal(
                tenant_id=seeded["tenant_id"],
                source_agent_id=seeded["agent_b_id"],
                requester_user_id=seeded["user_id"],
                root_session_id=str(seeded["root_session_id"]),
                root_runtime_task_id=str(seeded["delegation_task_id"]),
                origin="agent_tool",
                delegation_chain=("a2a", str(seeded["agent_a_id"]), str(seeded["agent_b_id"])),
            ),
        ),
    )
    result = json.loads(await handler_mod.send_agent_session_message(request))
    assert result["ok"] is True, result
    assert result["status"] == "queued", result
    assert result["consumer"] == "session_v2_round_input", result

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(SessionTurnInput).where(
                    SessionTurnInput.session_id == seeded["child_session_id"],
                    SessionTurnInput.intent == "steer_current_turn",
                )
            )
        ).scalar_one()
        assert row.target_run_id == seeded["active_run_id"]
        command = await db.get(SessionCommand, row.command_id)
        assert command is not None

        # Fresh-worker recovery rebuilds the same authority from durable
        # columns — no in-memory object carried from the fast path.
        session = await db.get(ChatSession, seeded["child_session_id"])
        context = await resolve_session_command_authority(
            db, command=command, session=session, action="mutate_session_input"
        )
        assert context.authority.authority_source == "a2a_delegation_peer"
        assert context.authority.principal_type == "user"
        assert context.authority.principal_id == seeded["user_id"]
        assert context.actor.id == seeded["user_id"]
