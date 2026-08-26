from __future__ import annotations

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
        child_agent_id=uuid.uuid4(),
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
    assert rows_by_type["heartbeat"]["task_id"] == str(heartbeat.id)
    assert rows_by_type["heartbeat"]["reason"] == "direct_core_audit_session_bound"
    assert rows_by_type["heartbeat"]["retry_allowed"] is False

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        resolved = await apply_runtime_reconciliation_action(
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

    assert resolved["status"] == "completed"
    assert resolved["metadata"]["reconciliation_status"] == "resolved"
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
        with pytest.raises(RuntimeReconciliationConflict, match="no longer awaiting reconciliation"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=trigger.id,
                tenant_id=tenant_id,
                action="archive",
                reason="stale second operator action",
                actor_user_id=actor_user_id,
            )

    assert [row.action for row in audit_rows] == [
        "runtime_reconciliation.mark_resolved",
        "runtime_reconciliation.archive",
    ]
    assert audit_rows[0].details["previous_status"] == "needs_reconciliation"
    assert audit_rows[0].details["resulting_status"] == "completed"
    assert audit_rows[0].details["reconciliation_status"] == "resolved"


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
    }
    # Only incomplete projections are claimed; the already-settled row is
    # filtered out in SQL before the limit.
    assert result["examined"] == 5

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
        assert "ambiguous_provider_send_projection_repaired_at" not in (settled_row.metadata_json or {})

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
        _complete_task, _complete_root, _complete_control = await _seed_reconciled_session_run(
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
