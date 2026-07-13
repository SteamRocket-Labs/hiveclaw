from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import func, select

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User


_LANES = ("heartbeat", "trigger", "subagent", "delegation")


async def _seed_startup_branch_task(owner_sessionmaker, *, lane: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, user_id, agent_id, parent_session_id, child_session_id, task_id = (uuid.uuid4() for _ in range(6))
    metadata = {
        "resume_after_restart": True,
        "side_effect_risk": "mutating",
        "parent_session_id": str(parent_session_id),
        "child_session_id": str(child_session_id),
    }
    if lane == "heartbeat":
        metadata.update(resumable_heartbeat=True, side_effect_risk="internal_governed")
    elif lane == "trigger":
        metadata.update(resumable_trigger=True, trigger_ids=[str(uuid.uuid4())])
    elif lane == "subagent":
        metadata.update(
            resumable_subagent=True,
            subagent_name="writer",
            subagent_type="explorer",
            child_pending_tool_frame={
                "tool_call_id": "call-write",
                "tool_name": "write_file",
                "arguments": {"path": "workspace/a.md", "content": "x"},
                "status": "running",
                "origin_channel": "subagent",
            },
        )
    else:
        metadata.update(
            resumable_delegation=True,
            tool_profile="worker_safe",
            owner_id=str(user_id),
            target_agent_id=str(agent_id),
            conversation_messages=[{"role": "user", "content": "mutate"}],
        )

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="seed startup terminal CAS"):
        db.add(Tenant(id=tenant_id, name=f"Startup {lane}", slug=f"startup-{lane}-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                username=f"startup-{user_id.hex[:8]}",
                email=f"startup-{user_id.hex[:8]}@example.test",
                password_hash="x",
                display_name="Startup Owner",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name=f"Startup {lane}",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await db.flush()
        db.add_all(
            [
                ChatSession(
                    id=parent_session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    title="Startup parent",
                    source_channel="web",
                ),
                ChatSession(
                    id=child_session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    title="Startup child",
                    source_channel="agent",
                    parent_session_id=parent_session_id,
                    root_session_id=parent_session_id,
                ),
            ]
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type=lane,
                status="pending",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                parent_session_id=str(parent_session_id),
                child_session_id=str(child_session_id),
                root_user_id=user_id,
                root_session_id=str(parent_session_id),
                claim_version=0,
                claimed_by=None,
                metadata_json=metadata,
            )
        )
        await db.commit()
    return tenant_id, task_id


async def _run_startup_lane(lane: str) -> list[str]:
    if lane == "heartbeat":
        from app.services.heartbeat import resume_persisted_heartbeat_runs

        return await resume_persisted_heartbeat_runs()
    if lane == "trigger":
        from app.services.trigger_daemon import resume_persisted_trigger_runs

        return await resume_persisted_trigger_runs()
    if lane == "subagent":
        from app.services.subagent_run_service import resume_persisted_subagent_runs

        return await resume_persisted_subagent_runs()
    from app.agents.orchestrator import resume_persisted_async_delegations

    return await resume_persisted_async_delegations()


def _startup_module(lane: str):
    if lane == "heartbeat":
        from app.services import heartbeat

        return heartbeat
    if lane == "trigger":
        from app.services import trigger_daemon

        return trigger_daemon
    if lane == "subagent":
        from app.services import subagent_run_service

        return subagent_run_service
    from app.agents import orchestrator

    return orchestrator


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("lane", _LANES)
async def test_startup_terminal_branch_cas_preserves_interleaved_live_claim(
    owner_sessionmaker,
    monkeypatch,
    lane,
):
    from app.services import runtime_task_service

    tenant_id, task_id = await _seed_startup_branch_task(owner_sessionmaker, lane=lane)
    module = _startup_module(lane)
    real_list = runtime_task_service.list_active_runtime_task_records

    async def stale_snapshot_then_worker_claim(**kwargs):
        records = await real_list(session_factory=owner_sessionmaker, **kwargs)
        async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="interleave live worker claim"):
            task = (
                await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id).with_for_update())
            ).scalar_one()
            task.status = "running"
            task.claim_version = 1
            task.claimed_by = "live-worker"
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await db.commit()
        return records

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    monkeypatch.setattr(module, "list_active_runtime_task_records", stale_snapshot_then_worker_claim)

    assert await _run_startup_lane(lane) == []

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify live worker claim"):
        task = await db.get(RuntimeTask, task_id)
        outbox_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(RuntimeNotificationOutbox.tenant_id == tenant_id)
                )
            ).scalar_one()
        )
    assert task is not None
    assert (task.status, task.claim_version, task.claimed_by) == ("running", 1, "live-worker")
    assert task.claim_expires_at is not None and task.claim_expires_at > datetime.now(timezone.utc)
    assert outbox_count == 0


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.parametrize("lane", _LANES)
async def test_startup_terminal_branch_uncontended_emits_one_reconciliation_intent(
    owner_sessionmaker,
    monkeypatch,
    lane,
):
    from app.services import runtime_task_service

    tenant_id, task_id = await _seed_startup_branch_task(owner_sessionmaker, lane=lane)
    module = _startup_module(lane)
    real_list = runtime_task_service.list_active_runtime_task_records

    async def real_snapshot(**kwargs):
        return await real_list(session_factory=owner_sessionmaker, **kwargs)

    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    monkeypatch.setattr(module, "list_active_runtime_task_records", real_snapshot)

    assert await _run_startup_lane(lane) == []

    async with owner_sessionmaker() as db, enter_rls_bypass(db, reason="verify startup reconciliation"):
        task = await db.get(RuntimeTask, task_id)
        intents = list(
            (
                await db.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.tenant_id == tenant_id,
                        RuntimeNotificationOutbox.source_run_id == str(task_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert task is not None and task.status == "needs_reconciliation"
    assert len(task.metadata_json.get("recovery_tool_frames") or []) == 1
    assert len(intents) == 1
    assert intents[0].terminal_status == "needs_reconciliation"
    if lane == "subagent":
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

        projection_calls: list[uuid.UUID] = []

        async def project_once(item):
            projection_calls.append(item.id)
            return {"status": "projected"}

        outbox = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
        first = await outbox.drain_once(
            worker_id="startup-subagent-projector",
            deliver=project_once,
            item_ids={intents[0].id},
        )
        duplicate = await outbox.drain_once(
            worker_id="startup-subagent-projector-repeat",
            deliver=project_once,
            item_ids={intents[0].id},
        )
        assert first["delivered"] == 1
        assert duplicate["claimed"] == 0
        assert projection_calls == [intents[0].id]
