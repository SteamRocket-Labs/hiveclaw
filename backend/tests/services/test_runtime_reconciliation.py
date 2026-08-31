from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_ids(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.tenant import Tenant

    first = uuid.uuid4()
    second = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=first, name="reconcile-a", slug=f"ra-{first.hex[:10]}"))
        session.add(Tenant(id=second, name="reconcile-b", slug=f"rb-{second.hex[:10]}"))
    return first, second


@pytest.fixture()
async def operator_authority(owner_sessionmaker, tenant_ids) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.agent import Agent
    from app.models.user import User

    tenant_id, _other_tenant_id = tenant_ids
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"reconcile-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@reconcile.test",
                password_hash="x",
                display_name="Reconciliation Operator",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Reconciliation Agent",
                creator_id=user_id,
            )
        )
    return user_id, agent_id


async def _add_runtime_task(
    session,
    *,
    tenant_id: uuid.UUID,
    task_type: str = "delegation",
    parent_agent_id: uuid.UUID | None = None,
    status: str = "needs_reconciliation",
    metadata: dict | None = None,
) -> RuntimeTask:
    task = RuntimeTask(
        id=uuid.uuid4(),
        task_type=task_type,
        status=status,
        tenant_id=tenant_id,
        parent_agent_id=parent_agent_id or uuid.uuid4(),
        child_agent_id=parent_agent_id or uuid.uuid4(),
        child_agent_name="worker",
        prompt="mutating work",
        result_summary="Restart interrupted a mutating run.",
        metadata_json={
            "needs_reconciliation": status == "needs_reconciliation",
            "reconciliation_reason": "missing_completion_journal",
            "side_effect_risk": "mutating",
            **(metadata or {}),
        },
    )
    session.add(task)
    await session.flush()
    return task


async def _deliver_terminal_projection(session, task: RuntimeTask) -> None:
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_settlement import settle_and_enqueue_runtime_task_terminal

    await settle_and_enqueue_runtime_task_terminal(
        session,
        task,
        terminal_source="test:canonical_terminal_projection",
        root_reason_code="test_terminal_projection",
    )
    boundary = await session.scalar(
        select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.tenant_id == task.tenant_id,
            RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id,
            RuntimeTerminalBoundaryOutbox.terminal_status == task.status,
        )
    )
    assert boundary is not None
    boundary.status = "delivered"
    boundary.delivered_at = datetime.now(timezone.utc)
    boundary.delivery_receipt_json = {"boundary_id": str(boundary.id)}
    await session.flush()


async def _seed_held_trigger_runtime_task(
    session,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    include_response: bool = False,
):
    from app.models.trigger import AgentTrigger
    from app.services.direct_invocation_terminal_boundary_processor import (
        enqueue_direct_terminal_boundary_for_task,
    )
    from app.services.runtime_task_service import _settle_trigger_runtime_task

    task_id = uuid.uuid4()
    session_id = uuid.uuid4() if include_response else None
    final_response = "Mixed trigger batch completed with held workflow evidence."
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        name="Held once trigger",
        type="once",
        config={
            "at": "2026-08-31T00:00:00+00:00",
            "_fire_inflight": {
                "event_key": "once:held-reconciliation",
                "runtime_task_id": str(task_id),
                "started_at": "2026-08-31T00:00:00+00:00",
            },
        },
        reason="Prove operator trigger settlement",
        is_enabled=True,
        fire_count=0,
        cooldown_seconds=0,
    )
    task = RuntimeTask(
        id=task_id,
        task_type="trigger",
        status="needs_reconciliation",
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        child_session_id=str(session_id) if session_id else None,
        root_user_id=user_id,
        root_session_id=str(session_id) if session_id else None,
        result_summary="Trigger effect outcome requires operator reconciliation.",
        completed_at=datetime.now(timezone.utc),
        metadata_json={
            "needs_reconciliation": True,
            "reconciliation_reason": "effect_outcome_unknown",
            "reconciliation_retry_allowed": True,
            "trigger_ids": [str(trigger.id)],
            "trigger_names": [trigger.name],
            "trigger_types": [trigger.type],
            **(
                {
                    "terminal_reason": "turn_stop",
                    "response_complete_payload": {
                        "agent_id": str(agent_id),
                        "session_id": str(session_id),
                        "source": "trigger",
                        "messages": [{"role": "user", "content": "Run the mixed trigger batch."}],
                        "metadata": {
                            "tenant_id": str(tenant_id),
                            "final_response": final_response,
                        },
                    },
                }
                if include_response
                else {}
            ),
        },
    )
    session.add_all((trigger, task))
    await session.flush()
    if include_response:
        assert session_id is not None and user_id is not None
        from app.models.chat_session import ChatSession
        from app.services.chat_transcript import append_session_event

        session.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                source_channel="trigger",
                runtime_source="runtime_task",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                title="Mixed trigger reconciliation",
            )
        )
        await session.flush()
        await append_session_event(
            db=session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=task_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            user_id=user_id,
            content="Run the mixed trigger batch.",
            source="trigger",
            metadata={"turn_id": f"turn-{task_id.hex}"},
            bridge_to_t0=False,
        )
        await append_session_event(
            db=session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=task_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            content=final_response,
            source="trigger",
            metadata={"turn_id": f"turn-{task_id.hex}"},
            bridge_to_t0=False,
        )
    settlement = await _settle_trigger_runtime_task(
        session,
        task,
        status="needs_reconciliation",
    )
    assert settlement is not None
    metadata = dict(task.metadata_json or {})
    metadata["trigger_settlement"] = settlement
    task.metadata_json = metadata
    outbox = await enqueue_direct_terminal_boundary_for_task(session, task)
    assert outbox is not None
    await session.flush()
    return task, trigger, outbox


async def _seed_unresolved_tool_effect(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
    status: str = "failed",
):
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    result_id = uuid.uuid4()
    invocation_id = uuid.uuid4()
    turn_id = f"turn-{run_id.hex}"
    round_id = f"{run_id}:round:1"
    provider_request_id = f"provider-{run_id}"
    session.add(
        ChatSession(
            id=session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_kind="human_chat",
            runtime_source="web_chat",
        )
    )
    task = RuntimeTask(
        id=run_id,
        task_type="web_chat_turn",
        status=status,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        child_agent_id=agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(session_id),
        root_user_id=user_id,
        root_session_id=str(session_id),
        root_runtime_task_id=run_id,
        prompt="write one file",
        result_summary="tool settlement failed after the effect started",
        metadata_json={
            "turn_id": turn_id,
            **(
                {
                    "needs_reconciliation": True,
                    "reconciliation_reason": "tool_lifecycle_persistence",
                    "side_effect_risk": "effect_outcome_unknown",
                    "reconciliation_retry_allowed": False,
                    "session_v2_reconciliation": {"reason": "tool_lifecycle_persistence"},
                }
                if status == "needs_reconciliation"
                else {}
            ),
        },
    )
    session.add(task)
    await session.flush()
    if status == "needs_reconciliation":
        from app.services.runtime_root_ledger import register_runtime_root_item

        await register_runtime_root_item(
            session,
            tenant_id=tenant_id,
            root_runtime_task_id=run_id,
            source_agent_id=agent_id,
            intent_key=f"direct:{run_id}",
            work_type="direct",
            target_ref=str(run_id),
            runtime_task_id=run_id,
            root_user_id=user_id,
            root_session_id=str(session_id),
            state="needs_reconciliation",
            admission_disposition="admitted",
        )
    session.add(
        SessionModelResult(
            id=result_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            round_id=round_id,
            provider_request_id=provider_request_id,
            state="failed",
            model_request_hash="a" * 64,
            model_request_snapshot_json={"messages": []},
            bound_input_ids_json=[],
        )
    )
    await session.flush()
    invocation = SessionToolInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
        round_id=round_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=f"tool-{invocation_id}",
        tool_name="write_file",
        provider_arguments_json={"path": "workspace/probe.md"},
        invocation_item_id=uuid.uuid4(),
        args_hash="b" * 64,
        authority_snapshot_hash="c" * 64,
        effect_idempotency_key=f"tool-effect:{invocation_id}",
        effect_state="effect_started",
        execution_fence_ref=f"tool-effect:{invocation_id}:started",
        recovery_owner="session_tool_runtime:effect_receipt_pending",
    )
    session.add(invocation)
    await session.flush()
    return task, invocation, session_id


async def _attach_unresolved_tool_effect_to_task(
    session,
    *,
    task: RuntimeTask,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_id: uuid.UUID,
):
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation

    session_id = uuid.uuid4()
    result_id = uuid.uuid4()
    invocation_id = uuid.uuid4()
    turn_id = f"turn-{task.id.hex}"
    round_id = f"{task.id}:round:effect"
    provider_request_id = f"provider-effect-{task.id}"
    session.add(
        ChatSession(
            id=session_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_kind="human_chat",
            runtime_source="web_chat",
        )
    )
    task.parent_session_id = str(session_id)
    session.add(
        SessionModelResult(
            id=result_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=task.id,
            round_id=round_id,
            provider_request_id=provider_request_id,
            state="failed",
            model_request_hash="e" * 64,
            model_request_snapshot_json={"messages": []},
            bound_input_ids_json=[],
        )
    )
    await session.flush()
    invocation = SessionToolInvocation(
        id=invocation_id,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=task.id,
        round_id=round_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=f"tool-{invocation_id}",
        tool_name="write_file",
        provider_arguments_json={"path": "workspace/trigger-probe.md"},
        invocation_item_id=uuid.uuid4(),
        args_hash="f" * 64,
        authority_snapshot_hash="1" * 64,
        effect_idempotency_key=f"tool-effect:{invocation_id}",
        effect_state="effect_started",
        execution_fence_ref=f"tool-effect:{invocation_id}:started",
        recovery_owner="session_tool_runtime:effect_receipt_pending",
    )
    session.add(invocation)
    await session.flush()
    return invocation


async def test_list_runtime_reconciliation_tasks_filters_by_tenant_and_status(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import list_runtime_reconciliation_tasks

    tenant_id, other_tenant_id = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        expected = await _add_runtime_task(session, tenant_id=tenant_id)
        await _add_runtime_task(session, tenant_id=tenant_id, status="running")
        await _add_runtime_task(session, tenant_id=other_tenant_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)

    assert [row["task_id"] for row in rows] == [str(expected.id)]
    assert rows[0]["reason"] == "missing_completion_journal"
    assert rows[0]["retry_allowed"] is False


async def test_runtime_reconciliation_retry_is_fail_closed_without_retry_contract(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict, apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id, parent_agent_id=agent_id)
        await _deliver_terminal_projection(session, task)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="not marked retryable"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="retry",
                reason="try again",
                actor_user_id=actor_user_id,
            )


async def test_runtime_reconciliation_safe_retry_reopens_task(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action, get_runtime_reconciliation_task

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            parent_agent_id=agent_id,
            metadata={"reconciliation_retry_allowed": True, "side_effect_risk": "read_only"},
        )
        await _deliver_terminal_projection(session, task)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="retry",
            reason="safe read-only retry",
            actor_user_id=actor_user_id,
        )

    assert view["status"] == "pending"
    assert view["metadata"]["reconciliation_status"] == "retry_requested"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task.id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "pending"


async def test_trigger_reconciliation_rejects_untyped_generic_resolution(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.trigger import AgentTrigger
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, trigger, _outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="trigger_disposition"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="generic success must not release a held trigger",
                actor_user_id=actor_user_id,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_task = await session.get(RuntimeTask, task.id)
        persisted_trigger = await session.get(AgentTrigger, trigger.id)
        assert persisted_task.status == "needs_reconciliation"
        assert persisted_task.metadata_json["trigger_settlement"]["outcome"] == "hold"
        assert persisted_trigger.config["_fire_inflight"]["hold"] is True


async def test_trigger_reconciliation_view_reports_canonical_readiness_and_typed_evidence(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.direct_invocation_terminal_boundary_processor import (
        build_direct_terminal_boundary_binding,
    )
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        list_runtime_reconciliation_tasks,
    )
    from app.services.runtime_terminal_boundary_outbox import terminal_boundary_binding_sha256
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    session_id = uuid.uuid4()
    completion_outbox_id = uuid.uuid4()
    audit_log_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, _trigger, outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        task.trace_id = "trace-canonical-trigger"
        task.child_session_id = str(session_id)
        metadata = dict(task.metadata_json or {})
        settlement = dict(metadata["trigger_settlement"])
        settlement["audit_log_id"] = str(audit_log_id)
        metadata.update(
            {
                "trigger_settlement": settlement,
                "completion_outbox_id": str(completion_outbox_id),
                "output_artifact": trigger_output_artifact_ref(str(task.id)),
            }
        )
        task.metadata_json = metadata
        canonical_binding = await build_direct_terminal_boundary_binding(session, task)
        outbox.session_id = str(session_id)
        outbox.binding_json = canonical_binding
        outbox.binding_sha256 = terminal_boundary_binding_sha256(canonical_binding)
        legacy = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="trigger",
            parent_agent_id=agent_id,
            metadata={"trigger_settlement_overrides": {str(uuid.uuid4()): "hold"}},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        canonical_row = next(row for row in rows if row["task_id"] == str(task.id))
        legacy_row = next(row for row in rows if row["task_id"] == str(legacy.id))

        assert canonical_row["trigger_disposition_readiness"] == {
            "schema": "runtime_trigger_disposition_readiness.v1",
            "ready": False,
            "blocker": "terminal_projection_pending",
            "terminal_projection_id": str(outbox.id),
        }
        assert canonical_row["child_session_id"] == str(session_id)
        assert canonical_row["trace_id"] == "trace-canonical-trigger"
        assert canonical_row["output_artifact"] == trigger_output_artifact_ref(str(task.id))
        assert canonical_row["completion_outbox_id"] == str(completion_outbox_id)
        assert canonical_row["settlement_audit_ref"] == {
            "kind": "audit_log",
            "id": str(audit_log_id),
        }
        assert legacy_row["trigger_disposition_readiness"]["ready"] is False
        assert legacy_row["trigger_disposition_readiness"]["blocker"] == "canonical_trigger_settlement_missing"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_outbox = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        persisted_outbox.status = "delivered"
        persisted_outbox.delivered_at = datetime.now(timezone.utc)
        persisted_outbox.delivery_receipt_json = {"boundary_id": str(outbox.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        ready_row = next(row for row in rows if row["task_id"] == str(task.id))
        assert ready_row["trigger_disposition_readiness"] == {
            "schema": "runtime_trigger_disposition_readiness.v1",
            "ready": True,
            "blocker": None,
            "terminal_projection_id": str(outbox.id),
        }

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_outbox = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        duplicate = RuntimeTerminalBoundaryOutbox(
            id=uuid.uuid4(),
            tenant_id=persisted_outbox.tenant_id,
            runtime_task_id=persisted_outbox.runtime_task_id,
            agent_id=persisted_outbox.agent_id,
            session_id=persisted_outbox.session_id,
            event_kind="turn_abort",
            terminal_status=persisted_outbox.terminal_status,
            authority_ref=persisted_outbox.authority_ref,
            authority_id=persisted_outbox.authority_id,
            binding_json=dict(persisted_outbox.binding_json),
            binding_sha256=persisted_outbox.binding_sha256,
            idempotency_key="d" * 64,
            status="delivered",
            delivery_receipt_json={"boundary_id": "duplicate"},
            delivered_at=datetime.now(timezone.utc),
        )
        session.add(duplicate)
        await session.flush()
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        duplicate_row = next(row for row in rows if row["task_id"] == str(task.id))
        assert duplicate_row["trigger_disposition_readiness"]["ready"] is False
        assert duplicate_row["trigger_disposition_readiness"]["blocker"] == "terminal_projection_mismatch"
        await session.delete(duplicate)
        persisted_task = await session.get(RuntimeTask, task.id)
        persisted_task.trace_id = "trace-drifted-after-delivery"
        await session.flush()
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        drifted_row = next(row for row in rows if row["task_id"] == str(task.id))
        assert drifted_row["trigger_disposition_readiness"]["ready"] is False
        assert drifted_row["trigger_disposition_readiness"]["blocker"] == "terminal_projection_mismatch"
        persisted_task.trace_id = "trace-canonical-trigger"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_task = await session.get(RuntimeTask, task.id)
        metadata = dict(persisted_task.metadata_json or {})
        settlement = dict(metadata["trigger_settlement"])
        settlement["runtime_task_id"] = str(uuid.uuid4())
        metadata["trigger_settlement"] = settlement
        persisted_task.metadata_json = metadata

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="canonical_trigger_settlement_mismatch"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="The view was ready before canonical evidence drifted.",
                actor_user_id=actor_user_id,
                trigger_disposition="confirmed_success",
            )


async def test_trigger_reconciliation_does_not_invert_task_and_outbox_lock_order(
    monkeypatch,
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services import runtime_reconciliation
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, _trigger, outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    outbox_locked = asyncio.Event()
    task_locked_by_operator = asyncio.Event()
    original_guard = runtime_reconciliation._require_delivered_trigger_terminal_projection

    async def signal_task_lock_then_check(db, locked_task):
        task_locked_by_operator.set()
        await original_guard(db, locked_task)

    monkeypatch.setattr(
        runtime_reconciliation,
        "_require_delivered_trigger_terminal_projection",
        signal_task_lock_then_check,
    )

    async def hold_outbox_then_lock_task():
        async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
            await session.execute(
                select(RuntimeTerminalBoundaryOutbox)
                .where(RuntimeTerminalBoundaryOutbox.id == outbox.id)
                .with_for_update()
            )
            outbox_locked.set()
            await task_locked_by_operator.wait()
            await session.execute(select(RuntimeTask).where(RuntimeTask.id == task.id).with_for_update())

    async def reconcile_while_outbox_is_locked():
        await outbox_locked.wait()
        async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
            with pytest.raises(RuntimeReconciliationConflict, match="terminal_projection_pending"):
                await runtime_reconciliation.apply_runtime_reconciliation_action(
                    session,
                    task_id=task.id,
                    tenant_id=tenant_id,
                    action="mark_resolved",
                    reason="Pending projection must not invert lock order.",
                    actor_user_id=actor_user_id,
                    trigger_disposition="confirmed_success",
                )

    await asyncio.wait_for(
        asyncio.gather(hold_outbox_then_lock_task(), reconcile_while_outbox_is_locked()),
        timeout=2,
    )


async def test_trigger_reconciliation_waits_for_projection_then_settles_hold_atomically_once(
    monkeypatch,
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.audit import AuditLog
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.trigger import AgentTrigger
    from app.services import runtime_terminal_settlement
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, trigger, outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="terminal_projection_pending"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="operator confirmed the exact trigger effect",
                actor_user_id=actor_user_id,
                trigger_disposition="confirmed_success",
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        outbox_row = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        outbox_row.status = "delivered"
        outbox_row.delivered_at = datetime.now(timezone.utc)
        outbox_row.delivery_receipt_json = {"boundary_id": str(outbox.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="new RuntimeTask"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="retry",
                reason="must not replay the ambiguous trigger wrapper",
                actor_user_id=actor_user_id,
                trigger_disposition="release",
            )

    original_settle_terminal = runtime_terminal_settlement.settle_runtime_task_terminal

    async def fail_after_trigger_settlement(*_args, **_kwargs):
        raise RuntimeError("terminal fence unavailable")

    monkeypatch.setattr(
        runtime_terminal_settlement,
        "settle_runtime_task_terminal",
        fail_after_trigger_settlement,
    )
    with pytest.raises(RuntimeError, match="terminal fence unavailable"):
        async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="operator confirmed the exact trigger effect",
                actor_user_id=actor_user_id,
                trigger_disposition="confirmed_success",
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rolled_back_task = await session.get(RuntimeTask, task.id)
        rolled_back_trigger = await session.get(AgentTrigger, trigger.id)
        assert rolled_back_task.status == "needs_reconciliation"
        assert rolled_back_task.metadata_json["trigger_settlement"]["outcome"] == "hold"
        assert rolled_back_trigger.fire_count == 0
        assert rolled_back_trigger.is_enabled is True
        assert rolled_back_trigger.config["_fire_inflight"]["hold"] is True

    monkeypatch.setattr(
        runtime_terminal_settlement,
        "settle_runtime_task_terminal",
        original_settle_terminal,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator confirmed the exact trigger effect",
            actor_user_id=actor_user_id,
            trigger_disposition="confirmed_success",
        )

    assert resolved["status"] == "completed"
    assert resolved["metadata"]["trigger_reconciliation_disposition"] == "confirmed_success"
    assert resolved["metadata"]["trigger_settlement"]["trigger_outcomes"] == {str(trigger.id): "success"}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        settled_task = await session.get(RuntimeTask, task.id)
        settled_trigger = await session.get(AgentTrigger, trigger.id)
        persisted_outbox = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("trigger_fired", "runtime_reconciliation.mark_resolved")),
                    )
                )
            ).scalars()
        )
        assert settled_task.status == "completed"
        assert settled_task.metadata_json["reconciliation_status"] == "trigger_confirmed_success"
        assert settled_trigger.fire_count == 1
        assert settled_trigger.is_enabled is False
        assert "_fire_inflight" not in settled_trigger.config
        assert persisted_outbox.status == "delivered"
        assert persisted_outbox.terminal_status == "needs_reconciliation"
        assert [audit.action for audit in audits].count("trigger_fired") == 1
        assert [audit.action for audit in audits].count("runtime_reconciliation.mark_resolved") == 1
        reconciliation_audit = next(audit for audit in audits if audit.action == "runtime_reconciliation.mark_resolved")
        assert reconciliation_audit.details["trigger_disposition"] == "confirmed_success"

        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="stale duplicate operator action",
                actor_user_id=actor_user_id,
                trigger_disposition="confirmed_success",
            )

        audits_after_duplicate = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("trigger_fired", "runtime_reconciliation.mark_resolved")),
                    )
                )
            ).scalars()
        )
        assert len(audits_after_duplicate) == len(audits)


async def test_confirmed_mixed_trigger_success_enqueues_distinct_turn_stop_projection(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, _trigger, original_outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=actor_user_id,
            include_response=True,
        )
        assert original_outbox.event_kind == "turn_abort"
        original_outbox.status = "delivered"
        original_outbox.delivered_at = datetime.now(timezone.utc)
        original_outbox.delivery_receipt_json = {"boundary_id": str(original_outbox.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="Verified the held workflow child and committed ReAct response.",
            actor_user_id=actor_user_id,
            trigger_disposition="confirmed_success",
        )

    assert resolved["status"] == "completed"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_task = await session.get(RuntimeTask, task.id)
        outboxes = list(
            (
                await session.execute(
                    select(RuntimeTerminalBoundaryOutbox)
                    .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == task.id)
                    .order_by(RuntimeTerminalBoundaryOutbox.event_kind)
                )
            ).scalars()
        )
        assert [(row.event_kind, row.terminal_status, row.status) for row in outboxes] == [
            ("turn_abort", "needs_reconciliation", "delivered"),
            ("turn_stop", "completed", "pending"),
        ]
        assert persisted_task.terminal_boundary_enqueued_at is not None


@pytest.mark.parametrize(
    ("disposition", "action", "expected_status", "expected_outcome"),
    [
        ("confirmed_failure", "mark_resolved", "failed", "failure"),
        ("release", "archive", "killed", "release"),
    ],
)
async def test_trigger_reconciliation_failure_and_release_clear_exact_hold(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
    disposition,
    action,
    expected_status,
    expected_outcome,
):
    from app.models.audit import AuditLog
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.trigger import AgentTrigger
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, trigger, outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        outbox.status = "delivered"
        outbox.delivered_at = datetime.now(timezone.utc)
        outbox.delivery_receipt_json = {"boundary_id": str(outbox.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action=action,
            reason=f"operator chose {disposition}",
            actor_user_id=actor_user_id,
            trigger_disposition=disposition,
        )

    assert resolved["status"] == expected_status
    assert resolved["metadata"]["trigger_settlement"]["trigger_outcomes"] == {str(trigger.id): expected_outcome}
    assert resolved["metadata"]["trigger_settlement"]["reconciliation_disposition"] == disposition

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        settled_trigger = await session.get(AgentTrigger, trigger.id)
        persisted_outbox = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("trigger_fired", f"runtime_reconciliation.{action}")),
                    )
                )
            ).scalars()
        )
        assert "_fire_inflight" not in settled_trigger.config
        assert settled_trigger.fire_count == 0
        assert settled_trigger.is_enabled is True
        assert persisted_outbox.status == "delivered"
        assert [audit.action for audit in audits] == [f"runtime_reconciliation.{action}"]
        if disposition == "confirmed_failure":
            assert settled_trigger.config["failure_count"] == 1
        else:
            assert "failure_count" not in settled_trigger.config


@pytest.mark.parametrize(
    ("disposition", "expected_status", "expected_outcome"),
    [
        ("confirmed_success", "completed", "success"),
        ("confirmed_failure", "failed", "failure"),
        ("release", "killed", "release"),
    ],
)
async def test_trigger_tool_effect_hold_uses_explicit_atomic_acknowledgement(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
    disposition,
    expected_status,
    expected_outcome,
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.session_v2 import SessionToolInvocation
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        list_runtime_reconciliation_tasks,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, trigger, outbox = await _seed_held_trigger_runtime_task(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        invocation = await _attach_unresolved_tool_effect_to_task(
            session,
            task=task,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=agent_id,
        )
        outbox.status = "delivered"
        outbox.delivered_at = datetime.now(timezone.utc)
        outbox.delivery_receipt_json = {"boundary_id": str(outbox.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        row = next(item for item in rows if item["task_id"] == str(task.id))
        assert row["supported_actions"] == ["acknowledge_tool_effect"]
        assert row["supported_trigger_dispositions"] == [
            "confirmed_success",
            "confirmed_failure",
            "release",
        ]
        assert row["trigger_disposition_readiness"]["ready"] is True
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="acknowledge_tool_effect",
            reason=f"Verified the trigger and unknown tool effect for {disposition}.",
            actor_user_id=actor_user_id,
            trigger_disposition=disposition,
        )

    assert resolved["status"] == expected_status
    assert resolved["tool_effect_reconciliation_required"] is False
    assert resolved["metadata"]["trigger_settlement"]["trigger_outcomes"] == {str(trigger.id): expected_outcome}
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted_invocation = await session.get(SessionToolInvocation, invocation.id)
        persisted_outbox = await session.get(RuntimeTerminalBoundaryOutbox, outbox.id)
        assert persisted_invocation.recovery_owner is None
        assert persisted_invocation.receipt_ref.startswith("session-event://")
        assert persisted_outbox.status == "delivered"


async def test_failed_tool_effect_hold_is_listed_and_can_only_be_acknowledged(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.audit import AuditLog
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        list_runtime_reconciliation_tasks,
    )
    from app.services.session_tool_runtime import (
        ToolEffectReconciliationRequired,
        assert_session_tool_effects_settled,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, invocation, session_id = await _seed_unresolved_tool_effect(
            session,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=agent_id,
            status="failed",
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        row = next(item for item in rows if item["task_id"] == str(task.id))
        assert row["status"] == "failed"
        assert row["reason"] == "tool_effect_outcome_unknown"
        assert row["side_effect_risk"] == "effect_outcome_unknown"
        assert row["retry_allowed"] is False
        assert row["tool_effect_reconciliation_required"] is True
        assert row["unsettled_tool_effect_count"] == 1
        assert row["supported_actions"] == ["acknowledge_tool_effect"]
        with pytest.raises(ToolEffectReconciliationRequired):
            await assert_session_tool_effects_settled(
                session,
                tenant_id=tenant_id,
                session_id=session_id,
            )

        with pytest.raises(ValueError, match="reconciliation evidence reason is required"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="acknowledge_tool_effect",
                reason="   ",
                actor_user_id=actor_user_id,
            )
        with pytest.raises(RuntimeReconciliationConflict, match="acknowledged before task resolution"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="must not manufacture a success",
                actor_user_id=actor_user_id,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        acknowledged = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="acknowledge_tool_effect",
            reason="verified the synthetic file and retained it for evidence; do not replay",
            actor_user_id=actor_user_id,
        )

    assert acknowledged["status"] == "failed"
    assert acknowledged["tool_effect_reconciliation_required"] is False
    assert acknowledged["metadata"]["reconciliation_status"] == "tool_effect_acknowledged"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        invocation_row = await session.get(SessionToolInvocation, invocation.id)
        assert invocation_row is not None
        assert invocation_row.effect_state == "needs_reconciliation"
        assert invocation_row.result_event_id is None
        assert invocation_row.recovery_owner is None
        assert invocation_row.receipt_ref.startswith("session-event://")
        events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.run_id == task.id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert [event.event_type for event in events] == [
            "tool_call.reconciled",
            "recovery_action.reconciled",
        ]
        assert events[0].metadata_json["v2_payload"]["resolution"] == "operator_acknowledged_unknown_effect"
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "runtime_reconciliation.acknowledge_tool_effect",
            )
        )
        assert audit is not None
        assert audit.details["previous_status"] == "failed"
        assert audit.details["resulting_status"] == "failed"
        await assert_session_tool_effects_settled(
            session,
            tenant_id=tenant_id,
            session_id=session_id,
        )

        with pytest.raises(RuntimeReconciliationConflict, match="no unresolved tool effect"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="acknowledge_tool_effect",
                reason="stale duplicate acknowledgement",
                actor_user_id=actor_user_id,
            )


async def test_failed_tool_effect_hold_is_not_starved_by_newer_reconciliation_rows(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.runtime_reconciliation import list_runtime_reconciliation_tasks

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        held, _invocation, _session_id = await _seed_unresolved_tool_effect(
            session,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=agent_id,
            status="failed",
        )
        for _index in range(51):
            await _add_runtime_task(
                session,
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                status="needs_reconciliation",
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(
            session,
            tenant_id=tenant_id,
            limit=50,
        )

    assert len(rows) == 50
    assert rows[0]["task_id"] == str(held.id)
    assert rows[0]["supported_actions"] == ["acknowledge_tool_effect"]


async def test_acknowledging_current_tool_effect_hold_stops_the_run_without_replay(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_reconciliation import apply_runtime_reconciliation_action

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task, _invocation, _session_id = await _seed_unresolved_tool_effect(
            session,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=agent_id,
            status="needs_reconciliation",
        )
        await _deliver_terminal_projection(session, task)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        acknowledged = await apply_runtime_reconciliation_action(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="acknowledge_tool_effect",
            reason="verified effect evidence; abandon the incomplete run",
            actor_user_id=actor_user_id,
        )

    assert acknowledged["status"] == "killed"
    assert acknowledged["metadata"]["reconciliation_status"] == "tool_effect_acknowledged"
    assert acknowledged["metadata"]["reconciliation_retry_allowed"] is False

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        root_item = await session.scalar(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task.id))
        assert root_item is not None
        assert root_item.state == "killed"
        assert root_item.reason_code == "runtime_reconciliation_terminal:acknowledge_tool_effect"
        assert root_item.terminal_at is not None


async def test_workbench_loads_legacy_failed_tool_effect_hold_beyond_normal_task_window(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.services.session_control_plane import _list_runtime_tasks, _runtime_task_payload

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        held, _invocation, session_id = await _seed_unresolved_tool_effect(
            session,
            tenant_id=tenant_id,
            user_id=actor_user_id,
            agent_id=agent_id,
            status="failed",
        )
        future = datetime.now(timezone.utc) + timedelta(days=1)
        for index in range(51):
            session.add(
                RuntimeTask(
                    id=uuid.uuid4(),
                    task_type="web_chat_turn",
                    status="completed",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    child_session_id=str(session_id),
                    prompt=f"later turn {index}",
                    created_at=future + timedelta(seconds=index),
                    completed_at=future + timedelta(seconds=index),
                )
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        tasks = await _list_runtime_tasks(
            session,
            agent_id=agent_id,
            session_id=session_id,
            limit=50,
        )

    held_task = next(task for task in tasks if task.id == held.id)
    payload = _runtime_task_payload(held_task)
    assert len(tasks) == 51
    assert payload["status"] == "failed"
    assert payload["user_blocker"] == {
        "kind": "runtime_reconciliation",
        "status": "blocked",
        "reason_code": "tool_effect_outcome_unknown",
        "title": "工具效果需要管理员核对",
        "reason": "工具可能已经产生效果，但终态回执没有落盘；系统不会自动重放这一轮。",
        "next_action": "平台管理员核对效果证据并停止旧任务后，才能继续或创建分支。",
        "owner": "platform_admin",
        "can_continue_other_work": True,
        "auto_resume": False,
        "retry_available": False,
    }


async def test_session_bound_trigger_and_heartbeat_rows_are_actionable_without_blind_retry(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from sqlalchemy import select

    from app.models.audit import AuditLog
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        list_runtime_reconciliation_tasks,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        trigger = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="trigger",
            parent_agent_id=agent_id,
            metadata={"reconciliation_reason": "session_bound_mutating_trigger"},
        )
        heartbeat = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="heartbeat",
            parent_agent_id=agent_id,
            metadata={"reconciliation_reason": "direct_core_audit_session_bound"},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)

    rows_by_type = {row["task_type"]: row for row in rows}
    assert rows_by_type["trigger"]["task_id"] == str(trigger.id)
    assert rows_by_type["trigger"]["reason"] == "session_bound_mutating_trigger"
    assert rows_by_type["trigger"]["retry_allowed"] is False
    assert rows_by_type["trigger"]["supported_trigger_dispositions"] == [
        "confirmed_success",
        "confirmed_failure",
        "release",
    ]
    assert rows_by_type["heartbeat"]["task_id"] == str(heartbeat.id)
    assert rows_by_type["heartbeat"]["reason"] == "direct_core_audit_session_bound"
    assert rows_by_type["heartbeat"]["retry_allowed"] is False
    assert rows_by_type["heartbeat"]["supported_trigger_dispositions"] == []

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="trigger_disposition"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=trigger.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="operator verified trigger side effects",
                actor_user_id=actor_user_id,
            )
        archived = await apply_runtime_reconciliation_action(
            session,
            task_id=heartbeat.id,
            tenant_id=tenant_id,
            action="archive",
            reason="operator archived interrupted heartbeat",
            actor_user_id=actor_user_id,
        )

    assert archived["status"] == "killed"
    assert archived["metadata"]["reconciliation_status"] == "archived"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(
                            (
                                "runtime_reconciliation.mark_resolved",
                                "runtime_reconciliation.archive",
                            )
                        ),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        trigger_row = await session.get(RuntimeTask, trigger.id)
        assert trigger_row.status == "needs_reconciliation"
        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=heartbeat.id,
                tenant_id=tenant_id,
                action="archive",
                reason="stale second operator action",
                actor_user_id=actor_user_id,
            )

    assert [row.action for row in audit_rows] == ["runtime_reconciliation.archive"]
    assert audit_rows[0].details["previous_status"] == "needs_reconciliation"
    assert audit_rows[0].details["resulting_status"] == "killed"
    assert audit_rows[0].details["reconciliation_status"] == "archived"


async def _seed_reconciled_session_run(
    session,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    root_state: str = "queued",
    accept_cancel: bool = False,
    created_at: datetime | None = None,
) -> tuple[RuntimeTask, object, uuid.UUID | None]:
    """Seed one production-shaped ambiguous row: RuntimeTask settled to
    needs_reconciliation by the canonical fail commit while its RuntimeRootItem
    was never transitioned (the RC-10A drift class)."""

    from app.models.chat_session import ChatSession
    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_root_ledger import register_runtime_root_item

    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    control_id = uuid.uuid4() if accept_cancel else None
    session.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
    task = RuntimeTask(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        child_agent_id=agent_id,
        parent_session_id=str(session_id),
        child_session_id=str(session_id),
        root_runtime_task_id=run_id,
        root_session_id=str(session_id),
        prompt="provider send",
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(task)
    await session.flush()
    await register_runtime_root_item(
        session,
        tenant_id=tenant_id,
        root_runtime_task_id=run_id,
        source_agent_id=agent_id,
        intent_key=f"web-chat-turn:{run_id.hex}",
        work_type="web_chat_turn",
        target_ref=str(run_id),
        runtime_task_id=run_id,
        root_session_id=str(session_id),
        state=root_state,
        admission_disposition="admitted",
    )
    if control_id is not None and user_id is not None:
        from app.models.user import User
        from app.services.session_control_input import accept_cancel_control_input
        from app.services.session_v2_persistence import resolve_session_mutation_authority

        user = await session.get(User, user_id)
        assert user is not None
        authority = await resolve_session_mutation_authority(
            session,
            user=user,
            agent_id=agent_id,
            session_id=session_id,
            action="mutate_session_input",
        )
        receipt = await accept_cancel_control_input(
            session,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:{run_id}",
            expected_run_id=run_id,
        )
        assert receipt.status == "accepted"
    # The old-code canonical fail commit: terminal status without settlement.
    task.status = "needs_reconciliation"
    task.claim_version = 2
    task.completed_at = datetime.now(timezone.utc)
    task.result_summary = "Provider send outcome is ambiguous; operator reconciliation is required."
    task.metadata_json = metadata or {
        "session_v2_reconciliation": {
            "reason": "ambiguous_provider_send",
            "provider_request_id": f"hive:{run_id}:round:1:attempt:1",
            "error_class": "read_error",
            "delivery_state": "unknown",
        },
    }
    await session.flush()
    root_item = (
        await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == run_id))
    ).scalar_one()
    return task, root_item, control_id


async def test_operator_terminal_actions_settle_root_fence_and_controls_once(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    from app.models.audit import AuditLog
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.session_v2 import SessionCommand, SessionControlInput
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolve_task, _resolve_root, resolve_control_id = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=actor_user_id,
            accept_cancel=True,
        )
        archive_task, _archive_root, archive_control_id = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=actor_user_id,
            accept_cancel=True,
        )
        await _deliver_terminal_projection(session, resolve_task)
        await _deliver_terminal_projection(session, archive_task)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=resolve_task.id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator verified provider rows",
            actor_user_id=actor_user_id,
        )
        archived = await apply_runtime_reconciliation_action(
            session,
            task_id=archive_task.id,
            tenant_id=tenant_id,
            action="archive",
            reason="operator archived synthetic provider row",
            actor_user_id=actor_user_id,
        )

    assert resolved["status"] == "completed"
    assert resolved["metadata"]["reconciliation_status"] == "resolved"
    assert archived["status"] == "killed"
    assert archived["metadata"]["reconciliation_status"] == "archived"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolve_row = await session.get(RuntimeTask, resolve_task.id)
        archive_row = await session.get(RuntimeTask, archive_task.id)
        resolve_root = (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == resolve_task.id))
        ).scalar_one()
        archive_root = (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == archive_task.id))
        ).scalar_one()
        resolve_control = await session.get(SessionControlInput, resolve_control_id)
        resolve_command = await session.scalar(
            select(SessionCommand).where(SessionCommand.id == resolve_control.command_id)
        )
        archive_control = await session.get(SessionControlInput, archive_control_id)
        archive_command = await session.scalar(
            select(SessionCommand).where(SessionCommand.id == archive_control.command_id)
        )
        rejected_control_events = list(
            (
                await session.execute(
                    select(ChatTranscriptEvent.event_type).where(
                        ChatTranscriptEvent.item_id.in_((resolve_control_id, archive_control_id)),
                        ChatTranscriptEvent.event_type == "control_input.rejected",
                    )
                )
            ).scalars()
        )
        audit_actions = list(
            (
                await session.execute(
                    select(AuditLog.action)
                    .where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("runtime_reconciliation.mark_resolved", "runtime_reconciliation.archive")),
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars()
        )

        assert resolve_row.status == "completed"
        assert resolve_root.state == "completed"
        assert (
            resolve_root.metadata_json["terminal_execution_fence_ref"]
            == resolve_row.metadata_json["terminal_execution_fence_ref"]
        )
        assert resolve_row.metadata_json["terminal_commit_source"] == "runtime_reconciliation.mark_resolved"
        assert resolve_row.metadata_json["terminal_committed_status"] == "completed"
        assert resolve_row.metadata_json["reconciliation_history"][-1]["action"] == "mark_resolved"

        assert archive_row.status == "killed"
        assert archive_root.state == "killed"
        assert (
            archive_root.metadata_json["terminal_execution_fence_ref"]
            == archive_row.metadata_json["terminal_execution_fence_ref"]
        )
        assert archive_row.metadata_json["terminal_commit_source"] == "runtime_reconciliation.archive"

        # The pending cancel controls settle exactly once with the operator's
        # terminal decision: typed rejection, single settlement ref, one
        # control_input.rejected event per control.
        for control, command in ((resolve_control, resolve_command), (archive_control, archive_command)):
            assert control.status == "rejected"
            assert control.settlement_ref
            assert command.status == "rejected"
            assert command.rejection_json == {"reason_code": "run_terminal_before_cancel_effect"}
            assert command.receipt_ref == control.settlement_ref
        assert rejected_control_events == ["control_input.rejected", "control_input.rejected"]
        assert audit_actions == [
            "runtime_reconciliation.mark_resolved",
            "runtime_reconciliation.archive",
        ]

        # Established admin contract: every stale second action conflicts —
        # the same action replayed and a different action alike.
        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=archive_task.id,
                tenant_id=tenant_id,
                action="archive",
                reason="operator replay",
                actor_user_id=actor_user_id,
            )
        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=archive_task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="conflicting operator action",
                actor_user_id=actor_user_id,
            )

        stale_audit_actions = list(
            (
                await session.execute(
                    select(AuditLog.action)
                    .where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action.in_(("runtime_reconciliation.mark_resolved", "runtime_reconciliation.archive")),
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars()
        )
        history_after_stale = archive_row.metadata_json["reconciliation_history"]

    assert stale_audit_actions == audit_actions
    assert [entry["action"] for entry in history_after_stale] == ["archive"]


async def test_projection_repair_sweep_selects_only_ambiguous_rows_with_missing_projection(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    """RC-10A recovery lane B: repair only the mechanical projection.

    The sweep must preserve status=needs_reconciliation (no semantic
    resolve/archive decision), select exact-code ambiguous_provider_send rows
    whose terminal projection is missing, and leave correctly settled A2A rows
    and unknown-reason rows untouched. Re-running is a no-op.
    """

    from app.models.audit import AuditLog
    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_reconciliation import repair_ambiguous_provider_send_terminal_projections

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        drift_task, _drift_root, _drift_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        a2a_task, _a2a_root, _a2a_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "reconciliation_reason": "a2a_request_snapshot_drift",
                "side_effect_risk": "mutating",
            },
            root_state="needs_reconciliation",
        )
        unknown_task, _unknown_root, _unknown_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "terminal_outcome_commit",
                    "result_id": str(uuid.uuid4()),
                },
            },
            root_state="queued",
        )
        settled_task, _settled_root, _settled_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "ambiguous_provider_send",
                    "provider_request_id": f"hive:{uuid.uuid4()}:round:1:attempt:1",
                    "error_class": "read_error",
                    "delivery_state": "unknown",
                },
                "terminal_commit_source": "session_model_round:ambiguous_provider_send",
                "terminal_committed_status": "needs_reconciliation",
                "terminal_execution_fence_ref": "runtime-task-terminal:settled-seed",
            },
            root_state="needs_reconciliation",
        )
        # Partial drift A: terminal fence missing while the root item already
        # carries the correct state.
        fenceless_task, _fenceless_root, _fenceless_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            root_state="needs_reconciliation",
        )
        # Partial drift B: terminal fence already stamped while the root item
        # is still queued.
        partial_root_task, _partial_root, _partial_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "ambiguous_provider_send",
                    "provider_request_id": f"hive:{uuid.uuid4()}:round:1:attempt:1",
                    "error_class": "read_error",
                    "delivery_state": "unknown",
                },
                "terminal_commit_source": "session_model_round:ambiguous_provider_send",
                "terminal_committed_status": "needs_reconciliation",
                "terminal_execution_fence_ref": "runtime-task-terminal:partial-b-seed",
            },
            root_state="queued",
        )
        # Partial drift C: fence and commit source present, settled root, but
        # terminal_committed_status missing — the fence cannot be proven to
        # belong to this status.
        missing_status_task, _missing_status_root, _missing_status_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "ambiguous_provider_send",
                    "provider_request_id": f"hive:{uuid.uuid4()}:round:1:attempt:1",
                    "error_class": "read_error",
                    "delivery_state": "unknown",
                },
                "terminal_commit_source": "session_model_round:ambiguous_provider_send",
                "terminal_execution_fence_ref": "runtime-task-terminal:missing-status-seed",
            },
            root_state="needs_reconciliation",
        )
        # Partial drift D: fence and committed status present, settled root,
        # but terminal_commit_source missing.
        missing_source_task, _missing_source_root, _missing_source_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "ambiguous_provider_send",
                    "provider_request_id": f"hive:{uuid.uuid4()}:round:1:attempt:1",
                    "error_class": "read_error",
                    "delivery_state": "unknown",
                },
                "terminal_committed_status": "needs_reconciliation",
                "terminal_execution_fence_ref": "runtime-task-terminal:missing-source-seed",
            },
            root_state="needs_reconciliation",
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        result = await repair_ambiguous_provider_send_terminal_projections(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

        assert set(result["repaired_task_ids"]) == {
            str(drift_task.id),
            str(fenceless_task.id),
            str(partial_root_task.id),
            str(missing_status_task.id),
            str(missing_source_task.id),
            str(settled_task.id),
        }
        # A settled root/fence without its required terminal outbox remains an
        # incomplete projection and is repaired in the same lane.
        assert result["examined"] == 6

    async def _root_state(session, task_id):
        return (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == task_id))
        ).scalar_one()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        drift_row = await session.get(RuntimeTask, drift_task.id)
        drift_row_root = await _root_state(session, drift_task.id)
        a2a_row = await session.get(RuntimeTask, a2a_task.id)
        a2a_row_root = await _root_state(session, a2a_task.id)
        unknown_row = await session.get(RuntimeTask, unknown_task.id)
        unknown_row_root = await _root_state(session, unknown_task.id)
        settled_row = await session.get(RuntimeTask, settled_task.id)
        settled_row_root = await _root_state(session, settled_task.id)
        fenceless_row = await session.get(RuntimeTask, fenceless_task.id)
        fenceless_row_root = await _root_state(session, fenceless_task.id)
        partial_root_row = await session.get(RuntimeTask, partial_root_task.id)
        partial_root_row_root = await _root_state(session, partial_root_task.id)
        repair_audit = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant_id,
                        AuditLog.action == "runtime_reconciliation.projection_repair",
                    )
                )
            ).scalars()
        )

        assert drift_row.status == "needs_reconciliation"
        assert drift_row.claim_version == 2
        assert drift_row_root.state == "needs_reconciliation"
        assert (
            drift_row_root.metadata_json["terminal_execution_fence_ref"]
            == drift_row.metadata_json["terminal_execution_fence_ref"]
        )
        assert drift_row.metadata_json["terminal_commit_source"] == (
            "runtime_reconciliation.ambiguous_provider_send_projection_repair"
        )
        assert drift_row.metadata_json["terminal_committed_status"] == "needs_reconciliation"
        assert drift_row.metadata_json["ambiguous_provider_send_projection_repaired_at"]
        assert drift_row.metadata_json["session_v2_reconciliation"]["reason"] == "ambiguous_provider_send"
        assert sorted(row.details["runtime_task_id"] for row in repair_audit) == sorted(result["repaired_task_ids"])
        assert all(row.details["previous_status"] == row.details["resulting_status"] for row in repair_audit)

        assert a2a_row.status == "needs_reconciliation"
        assert a2a_row_root.state == "needs_reconciliation"
        assert "terminal_execution_fence_ref" not in (a2a_row.metadata_json or {})

        assert unknown_row.status == "needs_reconciliation"
        assert unknown_row_root.state == "queued"
        assert "terminal_execution_fence_ref" not in (unknown_row.metadata_json or {})

        assert settled_row.status == "needs_reconciliation"
        assert settled_row_root.state == "needs_reconciliation"
        assert settled_row.metadata_json["terminal_execution_fence_ref"] == "runtime-task-terminal:settled-seed"
        assert settled_row.metadata_json["ambiguous_provider_send_projection_repaired_at"]

        # Partial drift A keeps the already-correct root state and only gains
        # the missing fence.
        assert fenceless_row.status == "needs_reconciliation"
        assert fenceless_row_root.state == "needs_reconciliation"
        assert fenceless_row.metadata_json["terminal_execution_fence_ref"]
        assert fenceless_row.metadata_json["terminal_committed_status"] == "needs_reconciliation"

        # Partial drift B keeps its original fence and terminal provenance —
        # same-status repair preserves them — and only the root item moves.
        assert partial_root_row.status == "needs_reconciliation"
        assert partial_root_row_root.state == "needs_reconciliation"
        assert partial_root_row.metadata_json["terminal_execution_fence_ref"] == "runtime-task-terminal:partial-b-seed"
        assert (
            partial_root_row_root.metadata_json["terminal_execution_fence_ref"]
            == "runtime-task-terminal:partial-b-seed"
        )
        assert partial_root_row.metadata_json["terminal_commit_source"] == (
            "session_model_round:ambiguous_provider_send"
        )
        assert partial_root_row.metadata_json["terminal_committed_status"] == "needs_reconciliation"
        assert partial_root_row.metadata_json["ambiguous_provider_send_projection_repaired_at"]

        missing_status_row = await session.get(RuntimeTask, missing_status_task.id)
        missing_status_row_root = await _root_state(session, missing_status_task.id)
        missing_source_row = await session.get(RuntimeTask, missing_source_task.id)
        missing_source_row_root = await _root_state(session, missing_source_task.id)

        # Partial drift C: a fence without a matching committed status cannot
        # be proven to belong to this lifecycle — the repair generates a new
        # status-matching fence and stamps the committing source.
        assert missing_status_row.status == "needs_reconciliation"
        assert missing_status_row_root.state == "needs_reconciliation"
        assert missing_status_row.metadata_json["terminal_execution_fence_ref"] != (
            "runtime-task-terminal:missing-status-seed"
        )
        assert (
            missing_status_row_root.metadata_json["terminal_execution_fence_ref"]
            == (missing_status_row.metadata_json["terminal_execution_fence_ref"])
        )
        assert missing_status_row.metadata_json["terminal_committed_status"] == "needs_reconciliation"
        assert missing_status_row.metadata_json["terminal_commit_source"] == (
            "runtime_reconciliation.ambiguous_provider_send_projection_repair"
        )

        # Partial drift D: same committed status with a missing source keeps
        # the fence and only stamps the committing source.
        assert missing_source_row.status == "needs_reconciliation"
        assert missing_source_row_root.state == "needs_reconciliation"
        assert missing_source_row.metadata_json["terminal_execution_fence_ref"] == (
            "runtime-task-terminal:missing-source-seed"
        )
        assert missing_source_row_root.metadata_json["terminal_execution_fence_ref"] == (
            "runtime-task-terminal:missing-source-seed"
        )
        assert missing_source_row.metadata_json["terminal_committed_status"] == "needs_reconciliation"
        assert missing_source_row.metadata_json["terminal_commit_source"] == (
            "runtime_reconciliation.ambiguous_provider_send_projection_repair"
        )

        second_pass = await repair_ambiguous_provider_send_terminal_projections(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )

    assert second_pass["repaired_task_ids"] == []
    assert second_pass["examined"] == 0


async def test_projection_repair_then_operator_action_generates_new_status_matching_fence(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    """A fence belongs to one terminal lifecycle.

    After the projection repair settles needs_reconciliation, an operator
    resolve/archive is a real status transition: it must generate a NEW fence
    matching the new committed status instead of attaching the repaired
    needs_reconciliation fence. A stale repeated action still conflicts.
    """

    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        repair_ambiguous_provider_send_terminal_projections,
    )

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolve_task, _resolve_root, _resolve_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        archive_task, _archive_root, _archive_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        repaired = await repair_ambiguous_provider_send_terminal_projections(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )
    assert set(repaired["repaired_task_ids"]) == {str(resolve_task.id), str(archive_task.id)}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolve_repaired = await session.get(RuntimeTask, resolve_task.id)
        archive_repaired = await session.get(RuntimeTask, archive_task.id)
        resolve_fence_after_repair = resolve_repaired.metadata_json["terminal_execution_fence_ref"]
        archive_fence_after_repair = archive_repaired.metadata_json["terminal_execution_fence_ref"]
        assert resolve_repaired.status == "needs_reconciliation"
        assert resolve_repaired.metadata_json["terminal_committed_status"] == "needs_reconciliation"
        await _deliver_terminal_projection(session, resolve_repaired)
        await _deliver_terminal_projection(session, archive_repaired)

        resolved = await apply_runtime_reconciliation_action(
            session,
            task_id=resolve_task.id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator verified provider rows",
            actor_user_id=actor_user_id,
        )
        archived = await apply_runtime_reconciliation_action(
            session,
            task_id=archive_task.id,
            tenant_id=tenant_id,
            action="archive",
            reason="operator archived synthetic provider row",
            actor_user_id=actor_user_id,
        )

        assert resolved["status"] == "completed"
        assert archived["status"] == "killed"

        resolve_final = await session.get(RuntimeTask, resolve_task.id)
        archive_final = await session.get(RuntimeTask, archive_task.id)
        resolve_root_final = (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == resolve_task.id))
        ).scalar_one()
        archive_root_final = (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == archive_task.id))
        ).scalar_one()

        # New terminal lifecycle => new fence matching the new status.
        assert resolve_final.metadata_json["terminal_execution_fence_ref"] != resolve_fence_after_repair
        assert resolve_final.metadata_json["terminal_committed_status"] == "completed"
        assert resolve_final.metadata_json["terminal_commit_source"] == "runtime_reconciliation.mark_resolved"
        assert resolve_root_final.state == "completed"
        assert (
            resolve_root_final.metadata_json["terminal_execution_fence_ref"]
            == (resolve_final.metadata_json["terminal_execution_fence_ref"])
        )

        assert archive_final.metadata_json["terminal_execution_fence_ref"] != archive_fence_after_repair
        assert archive_final.metadata_json["terminal_committed_status"] == "killed"
        assert archive_final.metadata_json["terminal_commit_source"] == "runtime_reconciliation.archive"
        assert archive_root_final.state == "killed"
        assert (
            archive_root_final.metadata_json["terminal_execution_fence_ref"]
            == (archive_final.metadata_json["terminal_execution_fence_ref"])
        )

        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=resolve_task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="stale repeat",
                actor_user_id=actor_user_id,
            )
        resolve_after_stale = await session.get(RuntimeTask, resolve_task.id)

    assert (
        resolve_after_stale.metadata_json["terminal_execution_fence_ref"]
        == (resolve_final.metadata_json["terminal_execution_fence_ref"])
    )


async def test_projection_repair_limit_does_not_starve_incomplete_rows_behind_complete_ones(
    owner_sessionmaker,
    tenant_ids,
    operator_authority,
):
    """The candidate query filters incomplete projections in SQL.

    With limit=1, an older already-complete row must not consume the slot and
    starve a newer drift; ``examined`` counts the claimed candidates only.
    """

    from app.models.runtime_root_item import RuntimeRootItem
    from app.services.runtime_reconciliation import repair_ambiguous_provider_send_terminal_projections

    tenant_id, _other = tenant_ids
    actor_user_id, agent_id = operator_authority
    now = datetime.now(timezone.utc)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        complete_task, _complete_root, _complete_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            metadata={
                "session_v2_reconciliation": {
                    "reason": "ambiguous_provider_send",
                    "provider_request_id": f"hive:{uuid.uuid4()}:round:1:attempt:1",
                    "error_class": "read_error",
                    "delivery_state": "unknown",
                },
                "terminal_commit_source": "session_model_round:ambiguous_provider_send",
                "terminal_committed_status": "needs_reconciliation",
                "terminal_execution_fence_ref": "runtime-task-terminal:complete-seed",
            },
            root_state="needs_reconciliation",
            created_at=now - timedelta(days=1),
        )
        await _deliver_terminal_projection(session, complete_task)
        drift_task, _drift_root, _drift_control = await _seed_reconciled_session_run(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            created_at=now,
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        result = await repair_ambiguous_provider_send_terminal_projections(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            limit=1,
        )

    assert result == {"examined": 1, "repaired_task_ids": [str(drift_task.id)]}

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        drift_root = (
            await session.execute(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == drift_task.id))
        ).scalar_one()

    assert drift_root.state == "needs_reconciliation"
