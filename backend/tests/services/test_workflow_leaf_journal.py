"""§9 P5 red tests: leaf-level journal + advisory-lock quota on REAL PG.

THE P5 contract (v1 decision 6): 8-leaf fanout with 7 done resumes exactly
1 — never the whole step. WorkflowLeafCall rows carry
input_hash/idempotency_key/status/token_usage; workflow_quotas is
pre-deducted under a Postgres advisory lock and settled with actual usage.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowLeafCall, WorkflowQuota, WorkflowQuotaReservation, WorkflowStep
from app.runtime.recovery_manifest import persist_recovery_manifest
from app.runtime.session import SessionContext
from app.runtime.workflow_engine import LeafOutcome, LeafRequest, workflow_leaf_recovery_identity
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_task_fence import current_runtime_task_fence, run_claimed_runtime_task
from app.services.workflow_runtime_service import PGQuotaReserver, WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _fan_definition(budget_tokens: int = 200_000) -> dict:
    return {
        "name": "fan-8",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "default_budget": {"max_total_tokens": budget_tokens},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 4,
            },
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-leaf", slug=f"wl-{tid.hex[:10]}"))
    async with tenant_scoped_session(str(tid), session_factory=owner_sessionmaker) as session:
        before_ids = set((await session.execute(select(RuntimeTask.id))).scalars().all())
    try:
        yield tid
    finally:
        async with tenant_scoped_session(str(tid), session_factory=owner_sessionmaker) as session:
            created = list(
                (
                    await session.execute(
                        select(RuntimeTask).where(
                            RuntimeTask.tenant_id == tid,
                            RuntimeTask.id.not_in(before_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for task in created:
                if task.status in {"pending", "resumable", "running", "suspended"}:
                    task.status = "failed"
                    task.claimed_by = None
                    task.claim_expires_at = None
                    task.completed_at = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _bind_runtime_dependencies_to_testcontainers(monkeypatch, owner_sessionmaker):
    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", noop_audit)


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"leaf-{uid.hex[:10]}",
                email=f"leaf-{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Leaf Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="leaf-agent", role_description="w", creator_id=uid))
    return aid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowRuntimeService:
    return WorkflowRuntimeService(session_factory=owner_sessionmaker)


async def _enqueue_and_claim_workflow(
    service: WorkflowRuntimeService,
    *,
    tenant_id: uuid.UUID,
    owner_sessionmaker,
    claim_sessionmaker,
    run_id: uuid.UUID,
    definition_data: dict,
    args: dict,
    agent_id: uuid.UUID,
    worker_id: str,
) -> RuntimeTask:
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=definition_data,
        args=args,
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        run_id=run_id,
        enqueue_only=True,
    )
    assert handle.outcome.status == "pending"
    async with tenant_scoped_session(str(tenant_id), session_factory=claim_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id=worker_id,
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed] == [run_id]
    return claimed[0]


async def test_eight_leaves_seven_done_resume_runs_exactly_one(service, tenant_id, owner_sessionmaker):
    targets = [f"t{i}" for i in range(8)]
    first_calls: list[str] = []

    async def leaf_fails_last(request: LeafRequest) -> LeafOutcome:
        first_calls.append(request.leaf_id)
        if request.leaf_id == "item-7":
            return LeafOutcome(ok=False, error="boom on the 8th")
        return LeafOutcome(ok=True, output={"i": request.leaf_id}, tokens_used=100)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": targets},
        leaf_executor=leaf_fails_last,
    )
    assert handle.outcome.status == "failed"
    assert len(first_calls) == 8

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (
            (await session.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == handle.run_id)))
            .scalars()
            .all()
        )
    by_leaf = {row.leaf_id: row for row in rows}
    assert len(by_leaf) == 8
    assert sum(1 for row in rows if row.status == "done") == 7
    assert by_leaf["item-7"].status == "failed"
    done_row = by_leaf["item-0"]
    assert done_row.input_hash and done_row.definition_hash and done_row.idempotency_key
    assert done_row.token_usage == {"total": 100}

    resume_calls: list[str] = []

    async def ok_leaf(request: LeafRequest) -> LeafOutcome:
        resume_calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={"i": request.leaf_id}, tokens_used=100)

    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=ok_leaf)

    assert outcome.status == "completed"
    assert resume_calls == ["item-7"], "7 done leaves must be replayed from journal, only the 8th runs"


async def test_quota_prededucted_and_settled_on_real_pg(service, tenant_id, owner_sessionmaker):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=250)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": ["a", "b", "c"]},
        leaf_executor=leaf,
    )
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        quota = (await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))).scalar_one()
    assert quota.consumed_tokens == 750, "actual usage settled back (3 leaves × 250)"


async def test_quota_reservation_is_idempotent_across_reserve_to_journal_kill_window(
    service,
    tenant_id,
    owner_sessionmaker,
):
    """A worker may die after quota commit but before journal start.

    The replacement worker must reuse the same stable reservation instead of
    charging the run a second estimate, and settlement replay must also be a
    no-op. This is the real-PG kill window from reserve -> journal.
    """

    estimate = 1_000
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": ["only"]},
        leaf_executor=lambda _request: None,
        enqueue_only=True,
    )
    reservation_key = f"{handle.run_id}:fan:item-0:input-hash"

    worker_a = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=estimate)
    assert await worker_a.reserve(str(handle.run_id), reservation_key=reservation_key) is True

    # Simulated process death: no WorkflowLeafCall/journal start is written.
    worker_b = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=estimate)
    assert await worker_b.reserve(str(handle.run_id), reservation_key=reservation_key) is True

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        quota = (await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))).scalar_one()
        reservations = list(
            (
                await session.execute(
                    select(WorkflowQuotaReservation).where(
                        WorkflowQuotaReservation.run_id == handle.run_id,
                        WorkflowQuotaReservation.logical_key == reservation_key,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert quota.consumed_tokens == estimate
    assert len(reservations) == 1

    await worker_b.settle(str(handle.run_id), 250, reservation_key=reservation_key)
    await worker_a.settle(str(handle.run_id), 250, reservation_key=reservation_key)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        quota = (await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))).scalar_one()
        reservation = (
            await session.execute(
                select(WorkflowQuotaReservation).where(
                    WorkflowQuotaReservation.run_id == handle.run_id,
                    WorkflowQuotaReservation.logical_key == reservation_key,
                )
            )
        ).scalar_one()
    assert quota.consumed_tokens == 250
    assert reservation.attempt == 1
    assert reservation.reservation_key == f"{reservation_key}:attempt-1"
    assert reservation.actual_tokens == 250
    assert reservation.settled_at is not None

    assert await worker_b.reserve(str(handle.run_id), reservation_key=reservation_key) is True
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        quota_after_retry = (
            await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))
        ).scalar_one()
        attempts = list(
            (
                await session.execute(
                    select(WorkflowQuotaReservation)
                    .where(
                        WorkflowQuotaReservation.run_id == handle.run_id,
                        WorkflowQuotaReservation.logical_key == reservation_key,
                    )
                    .order_by(WorkflowQuotaReservation.attempt.asc())
                )
            )
            .scalars()
            .all()
        )
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert attempts[1].state == "reserved"
    assert quota_after_retry.consumed_tokens == 1_250, "retry must charge a new estimate after old actual usage"


async def test_unknown_quota_execution_is_durable_and_operator_settles_conservatively(
    service,
    tenant_id,
    agent_id,
    owner_sessionmaker,
    monkeypatch,
):
    from app.models.agent import Agent
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        operator_user_id = agent.creator_id
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": ["only"]},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        user_id=operator_user_id,
        enqueue_only=True,
    )
    reservation_key = f"{handle.run_id}:fan:item-0:input-hash"
    quota = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=1_000)
    assert await quota.reserve(str(handle.run_id), reservation_key=reservation_key)
    await quota.mark_execution_started(
        str(handle.run_id),
        reservation_key=reservation_key,
        step_id="fan",
        leaf_id="item-0",
        input_hash="input-hash",
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=handle.run_id,
                step_id="fan",
                step_type="fanout_step",
                status="running",
            )
        )
        session.add(
            WorkflowLeafCall(
                tenant_id=tenant_id,
                run_id=handle.run_id,
                step_id="fan",
                leaf_id="item-0",
                input_hash="input-hash",
                idempotency_key="fan:item-0:input-hash",
                status="running",
            )
        )
    await quota.mark_execution_unknown(
        str(handle.run_id),
        reservation_key=reservation_key,
        error="executor connection lost after dispatch",
    )
    await quota.mark_execution_unknown(
        str(handle.run_id),
        reservation_key=reservation_key,
        error="executor connection lost after dispatch",
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        unknown_notification = (
            await session.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "workflow",
                    RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
                    RuntimeNotificationOutbox.terminal_status == "needs_reconciliation",
                )
            )
        ).scalar_one_or_none()
        metadata = dict(task.metadata_json or {})
        metadata["recovery_evidence_incomplete_reasons"] = []
        metadata["recovery_evidence_status"] = "ready"
        task.metadata_json = metadata
    assert unknown_notification is not None, "unknown transition must atomically publish operator attention intent"
    assert unknown_notification.terminal_status == "needs_reconciliation"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        view = await get_runtime_reconciliation_task(session, task_id=handle.run_id, tenant_id=tenant_id)
    assert view is not None and view["recovery_evidence"]["evidence_complete"] is True
    decisions = [
        {
            "runtime_task_id": frame["runtime_task_id"],
            "tool_call_id": frame["tool_call_id"],
            "tool_name": frame["tool_name"],
            "decision": "mark_resolved",
        }
        for frame in view["recovery_evidence"]["frames"]
    ]

    # Test Double rationale: isolate the external recovery-manifest filesystem;
    # DB authority, quota settlement, and operator transaction stay real PG.
    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        lambda **_kwargs: [{"ref": "runtime/reconciled.json", "sha256": "a" * 64}],
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        await apply_runtime_reconciliation_action(
            session,
            task_id=handle.run_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator verified unknown execution outcome",
            actor_user_id=operator_user_id,
            confirmed=True,
            evidence_digest=view["recovery_evidence"]["digest"],
            frame_decisions=decisions,
            operation_id=None,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        reservation = (
            await session.execute(
                select(WorkflowQuotaReservation).where(
                    WorkflowQuotaReservation.run_id == handle.run_id,
                    WorkflowQuotaReservation.logical_key == reservation_key,
                )
            )
        ).scalar_one()
        stored_quota = (
            await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))
        ).scalar_one()
        notification = (
            await session.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_kind == "workflow",
                    RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
                    RuntimeNotificationOutbox.terminal_status == "completed",
                )
            )
        ).scalar_one_or_none()
    assert reservation.state == "settled"
    assert reservation.actual_tokens == 1_000
    assert reservation.reconciliation_operation_id
    assert stored_quota.consumed_tokens == 1_000
    assert notification is not None
    assert notification.metadata_json["reconciliation_operation_id"] == reservation.reconciliation_operation_id


async def test_duplicate_unknown_and_operator_resolution_share_one_lock_order_without_deadlock(
    service,
    tenant_id,
    agent_id,
    owner_sessionmaker,
    monkeypatch,
):
    """Real PG barrier pins the historical reservation→task/task→reservation inversion."""

    from contextlib import asynccontextmanager

    from app.models.agent import Agent
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        operator_user_id = agent.creator_id
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": ["only"]},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        user_id=operator_user_id,
        enqueue_only=True,
    )
    reservation_key = f"{handle.run_id}:fan:item-0:lock-order"
    quota = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=1_000)
    assert await quota.reserve(str(handle.run_id), reservation_key=reservation_key)
    await quota.mark_execution_started(
        str(handle.run_id),
        reservation_key=reservation_key,
        step_id="fan",
        leaf_id="item-0",
        input_hash="lock-order",
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=handle.run_id,
                step_id="fan",
                step_type="fanout_step",
                status="running",
            )
        )
        session.add(
            WorkflowLeafCall(
                tenant_id=tenant_id,
                run_id=handle.run_id,
                step_id="fan",
                leaf_id="item-0",
                input_hash="lock-order",
                idempotency_key="fan:item-0:lock-order",
                status="running",
            )
        )
    await quota.mark_execution_unknown(
        str(handle.run_id),
        reservation_key=reservation_key,
        error="executor connection lost after dispatch",
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        metadata = dict(task.metadata_json or {})
        metadata["recovery_evidence_incomplete_reasons"] = []
        metadata["recovery_evidence_status"] = "ready"
        task.metadata_json = metadata
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        view = await get_runtime_reconciliation_task(session, task_id=handle.run_id, tenant_id=tenant_id)
    assert view is not None
    decisions = [
        {
            "runtime_task_id": frame["runtime_task_id"],
            "tool_call_id": frame["tool_call_id"],
            "tool_name": frame["tool_name"],
            "decision": "mark_resolved",
        }
        for frame in view["recovery_evidence"]["frames"]
    ]
    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        lambda **_kwargs: [{"ref": "runtime/reconciled.json", "sha256": "a" * 64}],
    )

    reservation_locked_before_task = asyncio.Event()
    operator_final_task_locked = asyncio.Event()
    original_quota_session = quota._session

    @asynccontextmanager
    async def controlled_quota_session():
        async with original_quota_session() as session:
            original_execute = session.execute
            task_locked_first = False

            async def controlled_execute(statement, *execute_args, **execute_kwargs):
                nonlocal task_locked_first
                result = await original_execute(statement, *execute_args, **execute_kwargs)
                statement_text = str(statement)
                if "FROM runtime_tasks" in statement_text and "FOR UPDATE" in statement_text:
                    task_locked_first = True
                if (
                    "FROM workflow_quota_reservations" in statement_text
                    and "FOR UPDATE" in statement_text
                    and not task_locked_first
                ):
                    reservation_locked_before_task.set()
                    await asyncio.wait_for(operator_final_task_locked.wait(), timeout=5)
                return result

            session.execute = controlled_execute
            yield session

    quota._session = controlled_quota_session

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as operator_session:
        original_operator_execute = operator_session.execute
        operator_task_group_lock_count = 0

        async def observed_operator_execute(statement, *execute_args, **execute_kwargs):
            nonlocal operator_task_group_lock_count
            result = await original_operator_execute(statement, *execute_args, **execute_kwargs)
            statement_text = str(statement)
            if (
                "FROM runtime_tasks" in statement_text
                and "runtime_tasks.id IN" in statement_text
                and "FOR UPDATE" in statement_text
            ):
                operator_task_group_lock_count += 1
                if operator_task_group_lock_count == 2:
                    operator_final_task_locked.set()
            return result

        operator_session.execute = observed_operator_execute

        duplicate_unknown = asyncio.create_task(
            quota.mark_execution_unknown(
                str(handle.run_id),
                reservation_key=reservation_key,
                error="executor connection lost after dispatch",
            )
        )
        inversion_probe = asyncio.create_task(reservation_locked_before_task.wait())
        done, _pending = await asyncio.wait(
            {duplicate_unknown, inversion_probe},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done
        if duplicate_unknown in done:
            async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
                metadata = dict(task.metadata_json or {})
                metadata["recovery_evidence_incomplete_reasons"] = []
                metadata["recovery_evidence_status"] = "ready"
                task.metadata_json = metadata
            async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
                view = await get_runtime_reconciliation_task(
                    session,
                    task_id=handle.run_id,
                    tenant_id=tenant_id,
                )
            assert view is not None
            decisions = [
                {
                    "runtime_task_id": frame["runtime_task_id"],
                    "tool_call_id": frame["tool_call_id"],
                    "tool_name": frame["tool_name"],
                    "decision": "mark_resolved",
                }
                for frame in view["recovery_evidence"]["frames"]
            ]
        operator_resolution = asyncio.create_task(
            apply_runtime_reconciliation_action(
                operator_session,
                task_id=handle.run_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="operator verified unknown execution outcome",
                actor_user_id=operator_user_id,
                confirmed=True,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=decisions,
                operation_id=None,
            )
        )
        results = await asyncio.gather(duplicate_unknown, operator_resolution, return_exceptions=True)
        inversion_probe.cancel()
    assert not [result for result in results if isinstance(result, BaseException)], results

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        reservation = (
            await session.execute(
                select(WorkflowQuotaReservation).where(
                    WorkflowQuotaReservation.run_id == handle.run_id,
                    WorkflowQuotaReservation.logical_key == reservation_key,
                )
            )
        ).scalar_one()
        stored_quota = (
            await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))
        ).scalar_one()
        completed_notifications = list(
            (
                await session.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "workflow",
                        RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
                        RuntimeNotificationOutbox.terminal_status == "completed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert reservation.state == "settled"
    assert reservation.reconciliation_operation_id
    assert stored_quota.consumed_tokens == 1_000
    assert len(completed_notifications) == 1


async def test_budget_exhaustion_suspends_run_definitely(service, tenant_id, owner_sessionmaker):
    """Budget covers fewer leaves than requested: the run must land in
    'suspended' (a DEFINITE state) and stop spawning."""
    from app.config import get_settings

    estimate = get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE
    calls: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={}, tokens_used=estimate)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(budget_tokens=estimate * 2),
        args={"targets": ["a", "b", "c", "d"]},
        leaf_executor=leaf,
    )

    assert handle.outcome.status == "suspended"
    assert "budget" in (handle.outcome.reason or "")
    assert len(calls) == 2, "no leaf may start past the exhausted budget"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        from app.models.runtime_task import RuntimeTask

        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "suspended"


async def test_fanout_restart_collects_every_mutating_leaf_manifest_before_any_replay(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    both_started = asyncio.Event()
    started = 0

    class _WorkerDied(RuntimeError):
        pass

    definition = {
        "name": "fanout-mutations",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "writer", "type": "worker"},
                "items_from": "args.targets",
                "per_item_task": "Edit {{item}}",
                "max_concurrency": 2,
            }
        ],
    }
    claim = await _enqueue_and_claim_workflow(
        worker_a,
        tenant_id=tenant_id,
        owner_sessionmaker=owner_sessionmaker,
        claim_sessionmaker=app_user_sessionmaker,
        run_id=run_id,
        definition_data=definition,
        args={"targets": ["a", "b"]},
        agent_id=agent_id,
        worker_id="workflow-fanout-crash-worker",
    )

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        nonlocal started
        fence = current_runtime_task_fence()
        assert fence is not None
        assert fence.task_id == run_id
        identity = workflow_leaf_recovery_identity(run_id, request.step_id, request.leaf_id)
        context = SessionContext(
            session_id=identity.session_id,
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": run_id.hex,
                "tenant_id": str(tenant_id),
                "claim_version": fence.claim_version,
                "claim_worker_id": fence.worker_id,
                "pending_tool_frames": [
                    {
                        "tool_call_id": f"call-{request.leaf_id}",
                        "tool_name": "edit_file",
                        "arguments": {"path": f"workspace/{request.leaf_id}.md"},
                        "status": "running",
                    }
                ],
            },
        )
        assert persist_recovery_manifest(agent_id, context, data_root=tmp_path)
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        raise _WorkerDied(f"crashed in {request.leaf_id}")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            worker_a.resume_run(run_id, tenant_id=tenant_id, leaf_executor=crashing_leaf),
            task_id=run_id,
            claim_version=claim.claim_version,
            worker_id=claim.claimed_by or "workflow-fanout-crash-worker",
            lease_seconds=60,
        )

    replay_calls: list[str | None] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    await worker_b.resume_pending_runs(leaf_executor=must_not_replay)

    assert replay_calls == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        leaf_rows = (
            (await session.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == run_id))).scalars().all()
        )
    assert task.status == "needs_reconciliation"
    assert {row.leaf_id: row.status for row in leaf_rows} == {
        "item-0": "needs_reconciliation",
        "item-1": "needs_reconciliation",
    }
    targets = task.metadata_json["recovery_resolution_targets"]
    assert [target["workflow_leaf_id"] for target in targets] == ["item-0", "item-1"]
    assert len({target["session_id"] for target in targets}) == 2


async def test_fanout_reconciliation_preserves_all_51_leaf_targets_and_frames(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    definition = {
        "name": "fanout-51-mutations",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "writer", "type": "worker"},
                "items_from": "args.targets",
                "per_item_task": "Edit {{item}}",
                "max_concurrency": 4,
            }
        ],
    }
    args = {"targets": [f"target-{index}" for index in range(51)]}
    claim = await _enqueue_and_claim_workflow(
        service,
        tenant_id=tenant_id,
        owner_sessionmaker=owner_sessionmaker,
        claim_sessionmaker=app_user_sessionmaker,
        run_id=run_id,
        definition_data=definition,
        args=args,
        agent_id=agent_id,
        worker_id="workflow-fanout-51-worker",
    )

    async def failed_mutating_leaf(request: LeafRequest) -> LeafOutcome:
        fence = current_runtime_task_fence()
        assert fence is not None
        assert fence.task_id == run_id
        identity = workflow_leaf_recovery_identity(run_id, request.step_id, request.leaf_id)
        context = SessionContext(
            session_id=identity.session_id,
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": run_id.hex,
                "tenant_id": str(tenant_id),
                "claim_version": fence.claim_version,
                "claim_worker_id": fence.worker_id,
                "pending_tool_frames": [
                    {
                        "tool_call_id": f"call-{request.leaf_id}",
                        "tool_name": "edit_file",
                        "arguments": {"path": f"workspace/{request.leaf_id}.md"},
                        "status": "running",
                    }
                ],
            },
        )
        assert persist_recovery_manifest(agent_id, context, data_root=tmp_path)
        return LeafOutcome(ok=False, error=f"unknown write outcome for {request.leaf_id}")

    outcome = await run_claimed_runtime_task(
        service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=failed_mutating_leaf),
        task_id=run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-fanout-51-worker",
        lease_seconds=60,
    )
    assert outcome.status == "failed"

    replay_calls: list[str | None] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={})

    outcome = await service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=must_not_replay)
    assert "reconciliation" in (outcome.reason or "")
    assert replay_calls == []

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        from app.services.runtime_reconciliation import (
            _recovery_resolution_targets,
            _validate_resolution_target_authority,
        )

        normalized_targets = _recovery_resolution_targets(task.metadata_json or {})
        authoritative_rows = await _validate_resolution_target_authority(
            session,
            task=task,
            tenant_id=tenant_id,
            targets=normalized_targets,
        )
    targets = task.metadata_json["recovery_resolution_targets"]
    frames = task.metadata_json["recovery_tool_frames"]
    assert len(targets) == 51
    assert len(frames) == 51
    assert {target["workflow_leaf_id"] for target in targets} == {f"item-{index}" for index in range(51)}
    assert {frame["tool_call_id"] for frame in frames} == {f"call-item-{index}" for index in range(51)}
    assert authoritative_rows == {run_id: task}
