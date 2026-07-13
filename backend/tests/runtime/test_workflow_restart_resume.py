"""§9 P10 red tests: a fresh worker reclaims a dead worker's unfinished runs.

Worker A dies mid-run (crash leaves the run 'running'); worker B — a brand
new service instance, the cross-worker stand-in — scans and drives the run
to completion off the shared PG journal.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowQuotaReservation, WorkflowStep
from app.runtime.recovery_manifest import (
    RecoveryManifest,
    inspect_recovery_manifest_checkpoint,
    load_recovery_manifest,
    persist_recovery_manifest,
    recovery_manifest_path,
)
from app.runtime.session import SessionContext
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from app.services.runtime_task_fence import current_runtime_task_fence, run_claimed_runtime_task
from app.services.workflow_runtime_service import (
    WorkflowRuntimeService,
    _merge_workflow_recovery_frames,
    _merge_workflow_recovery_targets,
    assess_workflow_recovery_manifest,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(autouse=True)
def _bind_runtime_dependencies_to_testcontainers(monkeypatch, owner_sessionmaker):
    """Keep claim fencing and lifecycle audit on this module's database.

    The production helpers deliberately resolve their own sessions from
    ``app.database.async_session``.  These tests use an injected
    Testcontainers session factory, so leaving that global untouched makes
    lease renewal and audit target a different database from the claimed run.
    Dedicated audit tests cover durable audit writes; this module exercises
    restart and recovery semantics.
    """

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", noop_audit)


def _definition() -> dict:
    return {
        "name": "restartable",
        "args_schema": {},
        "steps": [
            {"id": "one", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "One"},
            {"id": "two", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Two"},
        ],
    }


def test_completed_mutation_evidence_dominates_a_pending_frame_for_retry_policy() -> None:
    assessment = assess_workflow_recovery_manifest(
        RecoveryManifest(
            recent_writes=["workspace/report.md"],
            recent_tool_outcomes=[{"tool": "edit_file", "summary": "updated report"}],
            pending_tool_frames=[
                {
                    "tool_call_id": "call-send",
                    "tool_name": "send_email",
                    "status": "running",
                }
            ],
        ),
        canonical_path_present=True,
    )

    assert assessment.requires_reconciliation is True
    assert assessment.reason == "workflow_completed_mutation_not_journaled"


def test_repeated_reconciliation_replaces_stale_leaf_target_and_frames() -> None:
    old_target = {
        "agent_id": str(uuid.uuid4()),
        "session_id": "workflow-leaf-stable",
        "runtime_task_id": uuid.uuid4().hex,
        "expected_claim_version": 1,
    }
    new_target = {**old_target, "expected_claim_version": 3}
    unrelated_target = {
        "agent_id": str(uuid.uuid4()),
        "session_id": "workflow-leaf-other",
        "runtime_task_id": uuid.uuid4().hex,
    }
    assert _merge_workflow_recovery_targets(
        [old_target, unrelated_target],
        [new_target],
    ) == [new_target, unrelated_target]

    old_frame = {
        "runtime_task_id": old_target["runtime_task_id"],
        "tool_call_id": "old-call",
        "tool_name": "edit_file",
        "workflow_step_id": "edit",
        "workflow_leaf_id": "singleton",
    }
    new_frame = {**old_frame, "tool_call_id": "new-call"}
    unrelated_frame = {
        "runtime_task_id": unrelated_target["runtime_task_id"],
        "tool_call_id": "other-call",
        "tool_name": "send_email",
        "workflow_step_id": "other",
        "workflow_leaf_id": "singleton",
    }
    assert _merge_workflow_recovery_frames(
        [old_frame, unrelated_frame],
        [new_frame],
        replaced_leaf_keys={("edit", "singleton")},
    ) == [unrelated_frame, new_frame]


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-restart", slug=f"wr-{tid.hex[:10]}"))
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


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"restart-{uid.hex[:10]}",
                email=f"restart-{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Restart Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="restart-agent", role_description="w", creator_id=uid))
    return aid


_WORKFLOW_LEAF_RECOVERY_NAMESPACE = uuid.UUID("6f8d4e61-4f22-5d85-9d3e-bc7396fbe2ea")


def _recovery_session_id(run_id: uuid.UUID, step_id: str, leaf_id: str | None = None) -> str:
    leaf_key = leaf_id or "singleton"
    return (
        "workflow-leaf-"
        + uuid.uuid5(
            _WORKFLOW_LEAF_RECOVERY_NAMESPACE,
            f"{run_id.hex}:{step_id}:{leaf_key}",
        ).hex
    )


def _persist_pending_frame(
    *,
    data_root,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_id: str,
    tool_name: str,
) -> None:
    fence = current_runtime_task_fence()
    assert fence is not None
    assert fence.task_id == run_id
    session = SessionContext(
        session_id=_recovery_session_id(run_id, step_id),
        source="subagent",
        channel="internal",
        metadata={
            "runtime_task_id": run_id.hex,
            "tenant_id": str(tenant_id),
            "claim_version": fence.claim_version,
            "claim_worker_id": fence.worker_id,
            "pending_tool_frames": [
                {
                    "tool_call_id": f"call-{tool_name}",
                    "tool_name": tool_name,
                    "arguments": {"path": "workspace/report.md", "content": "updated"},
                    "status": "running",
                }
            ],
        },
    )
    assert persist_recovery_manifest(agent_id, session, data_root=data_root)


def _persist_completed_workspace_mutation(
    *,
    data_root,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_id: str,
) -> None:
    fence = current_runtime_task_fence()
    assert fence is not None
    assert fence.task_id == run_id
    session = SessionContext(
        session_id=_recovery_session_id(run_id, step_id),
        source="subagent",
        channel="internal",
        metadata={
            "runtime_task_id": run_id.hex,
            "tenant_id": str(tenant_id),
            "claim_version": fence.claim_version,
            "claim_worker_id": fence.worker_id,
        },
    )
    session.track_file_write(
        "workspace/report.md",
        snapshot={"exists": True, "size": 7, "mtime_ns": 1},
    )
    session.track_tool_outcome("edit_file", "updated workspace/report.md")
    assert persist_recovery_manifest(agent_id, session, data_root=data_root)


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


async def test_claimed_workflow_invoker_normalizes_db_claim_into_consumable_manifest(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    from app.runtime.invoker import AgentInvocationRequest, _normalize_invocation_session_context

    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    claim = await _enqueue_and_claim_workflow(
        service,
        tenant_id=tenant_id,
        owner_sessionmaker=owner_sessionmaker,
        claim_sessionmaker=app_user_sessionmaker,
        run_id=run_id,
        definition_data={
            "name": "claim-manifest-consumption",
            "args_schema": {},
            "steps": [
                {
                    "id": "read",
                    "type": "agent_step",
                    "leaf": {"name": "reader", "type": "explorer"},
                    "task": "Read the report",
                }
            ],
        },
        args={},
        agent_id=agent_id,
        worker_id="workflow-manifest-consumer",
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed_task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        db_claim_version = claimed_task.claim_version
        db_claim_worker_id = claimed_task.claimed_by
    assert db_claim_version == claim.claim_version
    assert db_claim_worker_id == claim.claimed_by

    async def leaf(request: LeafRequest) -> LeafOutcome:
        identity = _recovery_session_id(run_id, request.step_id)
        context = SessionContext(
            session_id=identity,
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": run_id.hex,
                "tenant_id": str(tenant_id),
            },
        )
        request_context = AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1"),
            messages=[{"role": "user", "content": "Read the report"}],
            agent_name="Workflow reader",
            role_description="Read-only workflow leaf",
            agent_id=agent_id,
            session_context=context,
        )
        _normalize_invocation_session_context(request_context)
        context.track_tool_outcome("read_file", "workspace/report.md read successfully")
        assert context.metadata["claim_version"] == db_claim_version
        assert context.metadata["claim_worker_id"] == db_claim_worker_id
        assert persist_recovery_manifest(agent_id, context, data_root=tmp_path)

        inspection = inspect_recovery_manifest_checkpoint(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=identity,
            runtime_task_id=run_id,
            data_root=tmp_path,
        )
        assert inspection is not None
        assert inspection["state"] == "valid"
        assert inspection["expected_claim_version"] == db_claim_version
        assert inspection["expected_claim_worker_id"] == db_claim_worker_id
        assert inspection["receipt"]["ref"].startswith("runtime_artifacts/recovery_manifests/")
        assert len(inspection["receipt"]["sha256"]) == 64

        manifest = load_recovery_manifest(agent_id, session_context=context, data_root=tmp_path)
        assert manifest is not None
        assert (
            manifest.tenant_id,
            manifest.agent_id,
            manifest.session_id,
            manifest.runtime_task_id,
            manifest.claim_version,
            manifest.claim_worker_id,
        ) == (
            str(tenant_id),
            str(agent_id),
            identity,
            run_id.hex,
            db_claim_version,
            db_claim_worker_id,
        )
        return LeafOutcome(ok=True, output={"read": True})

    outcome = await run_claimed_runtime_task(
        service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=leaf),
        task_id=run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-manifest-consumer",
        lease_seconds=60,
    )
    assert outcome.status == "completed"


async def test_new_worker_quarantines_executor_unknown_after_crash(tenant_id, owner_sessionmaker):
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    class _WorkerDied(RuntimeError):
        pass

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        if request.step_id == "two":
            raise _WorkerDied("power cut")
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    with pytest.raises(_WorkerDied):
        await worker_a.start_run(
            tenant_id=tenant_id, definition_data=_definition(), args={}, leaf_executor=crashing_leaf
        )

    # Worker B: a different service instance scanning the same database.
    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    resume_calls: list[str] = []

    async def ok_leaf(request: LeafRequest) -> LeafOutcome:
        resume_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    resumed = await worker_b.resume_pending_runs(leaf_executor=ok_leaf)

    assert resumed == []
    assert resume_calls == [], "executor-unknown work must not replay before operator reconciliation"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (
            await session.execute(
                select(RuntimeTask).where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "workflow",
                )
            )
        ).scalar_one()
        receipts = list(
            (await session.execute(select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.run_id == task.id)))
            .scalars()
            .all()
        )
    assert task.status == "needs_reconciliation"
    assert any(receipt.state == "needs_reconciliation" for receipt in receipts)


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_restart_quarantines_pending_workspace_mutation_even_when_step_defaults_read_only(
    tool_name,
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    definition = {
        "name": "workspace-write",
        "args_schema": {},
        "steps": [
            {
                "id": "write",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Update the report",
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
        args={},
        agent_id=agent_id,
        worker_id="workflow-workspace-writer",
    )

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        _persist_pending_frame(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=request.step_id,
            tool_name=tool_name,
        )
        raise _WorkerDied("power cut after workspace mutation started")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            worker_a.resume_run(run_id, tenant_id=tenant_id, leaf_executor=crashing_leaf),
            task_id=run_id,
            claim_version=claim.claim_version,
            worker_id=claim.claimed_by or "workflow-workspace-writer",
            lease_seconds=60,
        )

    replay_calls: list[str] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    resumed = await worker_b.resume_pending_runs(leaf_executor=must_not_replay)

    assert replay_calls == []
    # The crashing worker has already durably quarantined the task.  A fresh
    # worker must leave it for the reconciliation consumer instead of claiming
    # it as resumable work or manufacturing a second outcome.
    assert resumed == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        step = (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == run_id, WorkflowStep.step_id == "write")
            )
        ).scalar_one()
    assert task.status == "needs_reconciliation"
    assert step.status == "unknown_requires_reconciliation"
    assert task.metadata_json["reconciliation_retry_allowed"] is True
    targets = task.metadata_json["recovery_resolution_targets"]
    assert len(targets) == 1
    target = targets[0]
    assert {
        "agent_id": target["agent_id"],
        "session_id": target["session_id"],
        "runtime_task_id": target["runtime_task_id"],
        "source": target["source"],
        "workflow_step_id": target["workflow_step_id"],
        "workflow_leaf_id": target["workflow_leaf_id"],
        "expected_checkpoint_seq": target["expected_checkpoint_seq"],
    } == {
        "agent_id": str(agent_id),
        "session_id": _recovery_session_id(run_id, "write"),
        "runtime_task_id": run_id.hex,
        "source": "workflow_leaf",
        "workflow_step_id": "write",
        "workflow_leaf_id": "singleton",
        "expected_checkpoint_seq": 1,
    }
    assert target["expected_manifest_ref"].startswith("runtime_artifacts/recovery_manifests/")
    assert len(target["expected_sha256"]) == 64
    frames = task.metadata_json["recovery_tool_frames"]
    assert len(frames) == 2
    quota_frame = next(frame for frame in frames if frame["event_type"] == "executor_outcome_unknown")
    assert {key: quota_frame[key] for key in ("runtime_task_id", "tool_name", "status", "event_type", "reason")} == {
        "runtime_task_id": run_id.hex,
        "tool_name": "workflow_leaf_execution",
        "status": "needs_reconciliation",
        "event_type": "executor_outcome_unknown",
        "reason": "power cut after workspace mutation started",
    }
    assert quota_frame["tool_call_id"].startswith("workflow-quota:")
    mutation_frame = next(
        frame for frame in frames if frame["event_type"] == "workflow_leaf_recovery_reconciliation_required"
    )
    assert mutation_frame == {
        "runtime_task_id": str(run_id),
        "tool_call_id": f"call-{tool_name}",
        "tool_name": tool_name,
        "status": "needs_reconciliation",
        "event_type": "workflow_leaf_recovery_reconciliation_required",
        "reason": "workflow_pending_tool_not_replay_safe",
        "workflow_step_id": "write",
        "workflow_leaf_id": "singleton",
    }


async def test_restart_quarantines_read_only_executor_unknown_until_operator_review(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    definition = {
        "name": "read-only",
        "args_schema": {},
        "steps": [
            {
                "id": "read",
                "type": "agent_step",
                "leaf": {"name": "reader", "type": "explorer"},
                "task": "Read the report",
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
        args={},
        agent_id=agent_id,
        worker_id="workflow-read-worker",
    )

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        _persist_pending_frame(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=request.step_id,
            tool_name="read_file",
        )
        raise _WorkerDied("power cut during a read")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            worker_a.resume_run(run_id, tenant_id=tenant_id, leaf_executor=crashing_leaf),
            task_id=run_id,
            claim_version=claim.claim_version,
            worker_id=claim.claimed_by or "workflow-read-worker",
            lease_seconds=60,
        )

    replay_calls: list[str] = []

    async def must_not_reenter(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"read": True})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    resumed = await worker_b.resume_pending_runs(leaf_executor=must_not_reenter)

    assert replay_calls == []
    assert resumed == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        receipt = (
            await session.execute(select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.run_id == run_id))
        ).scalar_one()
    assert task.status == "needs_reconciliation"
    assert task.metadata_json["reconciliation_reason"] == "workflow_leaf_executor_outcome_unknown"
    assert task.metadata_json["reconciliation_retry_allowed"] is False
    assert receipt.state == "needs_reconciliation"


async def test_restart_fails_closed_when_canonical_manifest_exists_but_is_corrupt(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    async def crashing_leaf(_request: LeafRequest) -> LeafOutcome:
        path = recovery_manifest_path(
            agent_id,
            session_id=_recovery_session_id(run_id, "inspect"),
            runtime_task_id=run_id,
            data_root=tmp_path,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        raise _WorkerDied("power cut")

    with pytest.raises(_WorkerDied):
        await worker_a.start_run(
            tenant_id=tenant_id,
            definition_data={
                "name": "corrupt-evidence",
                "args_schema": {},
                "steps": [
                    {
                        "id": "inspect",
                        "type": "agent_step",
                        "leaf": {"name": "worker", "type": "worker"},
                        "task": "Inspect",
                    }
                ],
            },
            args={},
            leaf_executor=crashing_leaf,
            agent_id=agent_id,
            run_id=run_id,
        )

    replay_calls: list[str] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    await worker_b.resume_pending_runs(leaf_executor=must_not_replay)

    assert replay_calls == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
    assert task.status == "needs_reconciliation"
    assert task.metadata_json["reconciliation_reason"] == "workflow_recovery_manifest_invalid"


async def test_restart_quarantines_completed_workspace_mutation_before_workflow_journal_commit(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    definition = {
        "name": "completed-write-crash-window",
        "args_schema": {},
        "steps": [
            {
                "id": "write",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Update the report",
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
        args={},
        agent_id=agent_id,
        worker_id="workflow-completed-write-worker",
    )

    async def mutation_finished_then_crashed(request: LeafRequest) -> LeafOutcome:
        _persist_completed_workspace_mutation(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=request.step_id,
        )
        raise _WorkerDied("power cut after write checkpoint but before workflow journal commit")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            worker_a.resume_run(run_id, tenant_id=tenant_id, leaf_executor=mutation_finished_then_crashed),
            task_id=run_id,
            claim_version=claim.claim_version,
            worker_id=claim.claimed_by or "workflow-completed-write-worker",
            lease_seconds=60,
        )

    replay_calls: list[str] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    await worker_b.resume_pending_runs(leaf_executor=must_not_replay)

    assert replay_calls == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
    assert task.status == "needs_reconciliation"
    assert task.metadata_json["reconciliation_reason"] == "workflow_completed_mutation_not_journaled"
    assert task.metadata_json["reconciliation_retry_allowed"] is False


async def test_restart_does_not_replay_executor_unknown_from_an_older_claim_manifest(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    worker_a = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    async def crashed_before_read(_request: LeafRequest) -> LeafOutcome:
        raise _WorkerDied("old worker stopped")

    with pytest.raises(_WorkerDied):
        await worker_a.start_run(
            tenant_id=tenant_id,
            definition_data={
                "name": "claimed-read-recovery",
                "args_schema": {},
                "steps": [
                    {
                        "id": "read",
                        "type": "agent_step",
                        "leaf": {"name": "reader", "type": "explorer"},
                        "task": "Read the report",
                    }
                ],
            },
            args={},
            leaf_executor=crashed_before_read,
            agent_id=agent_id,
            run_id=run_id,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        task.claim_version = 1
        task.claimed_by = "worker-a"

    claimed_session = SessionContext(
        session_id=_recovery_session_id(run_id, "read"),
        source="subagent",
        channel="internal",
        metadata={
            "runtime_task_id": run_id.hex,
            "tenant_id": str(tenant_id),
            "claim_version": 1,
            "claim_worker_id": "worker-a",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-read",
                    "tool_name": "read_file",
                    "arguments": {"path": "workspace/report.md"},
                    "status": "running",
                }
            ],
        },
    )
    assert persist_recovery_manifest(agent_id, claimed_session, data_root=tmp_path)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        task.claim_version = 2
        task.claimed_by = "worker-b"

    replay_calls: list[str] = []

    async def must_not_reenter(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"read": True})

    worker_b = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    outcome = await worker_b.resume_run(run_id, tenant_id=tenant_id, leaf_executor=must_not_reenter)

    assert outcome.status == "suspended"
    assert "operator reconciliation" in (outcome.reason or "")
    assert replay_calls == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
    assert task.status == "needs_reconciliation"


async def test_failed_workflow_repair_quarantines_unsafe_leaf_before_replay(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
):
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    async def failed_leaf_with_pending_mutation(request: LeafRequest) -> LeafOutcome:
        _persist_pending_frame(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=request.step_id,
            tool_name="edit_file",
        )
        return LeafOutcome(ok=False, error="write outcome unknown")

    definition = {
        "name": "failed-write-repair",
        "args_schema": {},
        "steps": [
            {
                "id": "write",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Update the report",
            }
        ],
    }
    claim = await _enqueue_and_claim_workflow(
        service,
        tenant_id=tenant_id,
        owner_sessionmaker=owner_sessionmaker,
        claim_sessionmaker=app_user_sessionmaker,
        run_id=run_id,
        definition_data=definition,
        args={},
        agent_id=agent_id,
        worker_id="workflow-failed-write-worker",
    )
    outcome = await run_claimed_runtime_task(
        service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=failed_leaf_with_pending_mutation),
        task_id=run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-failed-write-worker",
        lease_seconds=60,
    )
    assert outcome.status == "failed"

    replay_calls: list[str] = []

    async def must_not_replay(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={})

    outcome = await service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=must_not_replay)

    assert replay_calls == []
    assert "reconciliation" in (outcome.reason or "")
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        step = (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == run_id, WorkflowStep.step_id == "write")
            )
        ).scalar_one()
    assert task.status == "needs_reconciliation"
    assert step.status == "unknown_requires_reconciliation"


async def test_claimed_worker_quarantines_read_only_executor_unknown_without_new_claim_replay(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    tmp_path,
    monkeypatch,
):
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    definition = {
        "name": "production-claim-recovery",
        "args_schema": {},
        "steps": [
            {
                "id": "read",
                "type": "agent_step",
                "leaf": {"name": "reader", "type": "explorer"},
                "task": "Read the report",
            }
        ],
    }
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=definition,
        args={},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        run_id=run_id,
        enqueue_only=True,
    )
    assert handle.outcome.status == "pending"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed_a = await RuntimeTaskClaimService(
            db=session,
            worker_id="workflow-worker-a",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed_a] == [run_id]
    first_claim = claimed_a[0]

    class _WorkerDied(RuntimeError):
        pass

    async def read_then_crash(request: LeafRequest) -> LeafOutcome:
        fence = current_runtime_task_fence()
        assert fence is not None
        session = SessionContext(
            session_id=_recovery_session_id(run_id, request.step_id),
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": run_id.hex,
                "tenant_id": str(tenant_id),
                "claim_version": fence.claim_version,
                "claim_worker_id": fence.worker_id,
                "pending_tool_frames": [
                    {
                        "tool_call_id": "call-read-production",
                        "tool_name": "read_file",
                        "arguments": {"path": "workspace/report.md"},
                        "status": "running",
                    }
                ],
            },
        )
        assert persist_recovery_manifest(agent_id, session, data_root=tmp_path)
        raise _WorkerDied("worker-a stopped during read")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=read_then_crash),
            task_id=run_id,
            claim_version=first_claim.claim_version,
            worker_id=first_claim.claimed_by or "workflow-worker-a",
            lease_seconds=60,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        task.claim_expires_at = task.started_at

    async def no_op_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_op_wakeup)
    assert await service.requeue_pending_runs() == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        receipt = (
            await session.execute(select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.run_id == run_id))
        ).scalar_one()
    assert task.status == "needs_reconciliation"
    assert task.metadata_json["reconciliation_retry_allowed"] is False
    assert receipt.state == "needs_reconciliation"


@pytest.mark.parametrize("simulate_crash_before_inline_finalize", [False, True])
async def test_live_fanout_reconciliation_waits_for_all_leaf_manifests_before_operator_review(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    tmp_path,
    monkeypatch,
    simulate_crash_before_inline_finalize,
):
    from app.config import get_settings
    from app.runtime.recovery_manifest import persist_recovery_manifest_checkpoint
    from app.runtime.workflow_engine import workflow_leaf_recovery_identity
    from app.services.runtime_reconciliation import (
        get_runtime_reconciliation_task,
        mark_runtime_task_recovery_reconciliation,
    )

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    if simulate_crash_before_inline_finalize:

        async def skip_inline_finalize(**_kwargs):
            return None

        monkeypatch.setattr(service, "_finalize_live_reconciliation_evidence", skip_inline_finalize)
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data={
            "name": "live-fanout-reconciliation-barrier",
            "args_schema": {"targets": {"type": "array", "required": True}},
            "steps": [
                {
                    "id": "fanout",
                    "type": "fanout_step",
                    "leaf": {"name": "sender", "type": "worker"},
                    "items_from": "args.targets",
                    "per_item_task": "Send {{item}}",
                    "max_concurrency": 2,
                }
            ],
        },
        args={"targets": ["a", "b"]},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        run_id=run_id,
        enqueue_only=True,
    )
    assert handle.outcome.status == "pending"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="workflow-live-fanout",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed] == [run_id]
    claim = claimed[0]
    sibling_manifest_ready = asyncio.Event()
    incident_projected = asyncio.Event()

    async def leaf(request: LeafRequest) -> LeafOutcome:
        fence = current_runtime_task_fence()
        assert fence is not None
        identity = workflow_leaf_recovery_identity(run_id, request.step_id, request.leaf_id)
        context = SessionContext(
            session_id=identity.session_id,
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": identity.runtime_task_id,
                "tenant_id": str(tenant_id),
                "claim_version": fence.claim_version,
                "claim_worker_id": fence.worker_id,
            },
        )
        if request.leaf_id == "item-1":
            context.track_file_write(
                "workspace/already-sent.txt",
                snapshot={"exists": True, "size": 1, "mtime_ns": 1},
            )
            context.track_tool_outcome("send_email", "message accepted by provider")
            assert persist_recovery_manifest_checkpoint(agent_id, context, data_root=tmp_path)
            sibling_manifest_ready.set()
            await incident_projected.wait()
            return LeafOutcome(ok=True, output={"sent": "b"})

        await sibling_manifest_ready.wait()
        context.metadata["pending_tool_frames"] = [
            {
                "tool_call_id": "call-send-a",
                "tool_name": "send_email",
                "status": "running",
            }
        ]
        [receipt] = persist_recovery_manifest_checkpoint(agent_id, context, data_root=tmp_path)
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            await mark_runtime_task_recovery_reconciliation(
                session,
                task_id=run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=identity.session_id,
                event={
                    "event_type": "tool_execution_reconciliation_required",
                    "tool_name": "send_email",
                    "tool_call_id": "call-send-a",
                    "reason": "tool_execution_outcome_unknown",
                    "runtime_failure_policy": {
                        "side_effect_risk": "unknown",
                        "requires_reconciliation": True,
                    },
                },
                recovery_manifest_receipt=receipt,
                recovery_authority={
                    "type": "workflow_leaf",
                    "workflow_run_id": str(run_id),
                    "workflow_step_id": request.step_id,
                    "workflow_leaf_id": request.leaf_id,
                },
                expected_status="running",
                expected_claim_version=fence.claim_version,
                expected_claim_worker_id=fence.worker_id,
            )
        incident_projected.set()
        return LeafOutcome(ok=True, output={"sent": "a"})

    outcome = await run_claimed_runtime_task(
        service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=leaf),
        task_id=run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-live-fanout",
        lease_seconds=60,
    )
    assert outcome.status == "suspended"

    if simulate_crash_before_inline_finalize:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            blocked = await get_runtime_reconciliation_task(session, task_id=run_id, tenant_id=tenant_id)
            assert blocked is not None
            assert blocked["recovery_evidence"]["evidence_complete"] is False
            assert "workflow_fanout_evidence_aggregation_pending" in blocked["metadata"].get(
                "recovery_evidence_incomplete_reasons", []
            )
        fresh_worker = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
        repaired = await fresh_worker.repair_pending_live_reconciliation_evidence(limit=10)
        assert repaired == [run_id]

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        view = await get_runtime_reconciliation_task(session, task_id=run_id, tenant_id=tenant_id)
        assert view is not None
        metadata = view["metadata"]
        assert view["recovery_evidence"]["evidence_complete"] is True
        assert "workflow_fanout_evidence_aggregation_pending" not in metadata.get(
            "recovery_evidence_incomplete_reasons", []
        )
        workflow_targets = [
            target for target in metadata["recovery_resolution_targets"] if target.get("source") == "workflow_leaf"
        ]
        assert {target["workflow_leaf_id"] for target in workflow_targets} == {"item-0", "item-1"}
        assert all(target.get("expected_sha256") for target in workflow_targets)
        assert {frame["workflow_leaf_id"] for frame in metadata["recovery_tool_frames"]} == {
            "item-0",
            "item-1",
        }


async def test_operator_retry_reclaims_same_workflow_and_runs_only_unfinished_leaf(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    tmp_path,
    monkeypatch,
):
    from app.config import get_settings
    from app.models.agent import Agent
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)
    calls: list[str] = []
    definition = {
        "name": "operator-retry-workflow",
        "args_schema": {},
        "steps": [
            {
                "id": "done",
                "type": "agent_step",
                "leaf": {"name": "reader", "type": "explorer"},
                "task": "Read inputs",
            },
            {
                "id": "edit",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Edit report",
            },
        ],
    }
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=definition,
        args={},
        leaf_executor=lambda _request: None,
        agent_id=agent_id,
        run_id=run_id,
        enqueue_only=True,
    )
    assert handle.outcome.status == "pending"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed_a = await RuntimeTaskClaimService(
            db=session,
            worker_id="workflow-before-crash",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed_a] == [run_id]
    first_claim = claimed_a[0]

    async def first_worker(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        if request.step_id == "done":
            return LeafOutcome(ok=True, output={"done": True})
        fence = current_runtime_task_fence()
        assert fence is not None
        recovery_session = SessionContext(
            session_id=_recovery_session_id(run_id, request.step_id),
            source="subagent",
            channel="internal",
            metadata={
                "runtime_task_id": run_id.hex,
                "tenant_id": str(tenant_id),
                "claim_version": fence.claim_version,
                "claim_worker_id": fence.worker_id,
                "pending_tool_frames": [
                    {
                        "tool_call_id": "call-edit_file",
                        "tool_name": "edit_file",
                        "arguments": {"path": "workspace/report.md", "content": "updated"},
                        "status": "running",
                    }
                ],
            },
        )
        assert persist_recovery_manifest(agent_id, recovery_session, data_root=tmp_path)
        raise RuntimeError("worker stopped with unknown edit outcome")

    with pytest.raises(RuntimeError, match="unknown edit outcome"):
        await run_claimed_runtime_task(
            service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=first_worker),
            task_id=run_id,
            claim_version=first_claim.claim_version,
            worker_id=first_claim.claimed_by or "workflow-before-crash",
            lease_seconds=60,
        )
    assert calls == ["done", "edit"]

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        task.claim_expires_at = task.started_at

    async def no_op_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_op_wakeup)
    assert await service.requeue_pending_runs() == []

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        view = await get_runtime_reconciliation_task(session, task_id=run_id, tenant_id=tenant_id)
        assert view is not None
        assert view["retry_allowed"] is True
        frames = view["recovery_evidence"]["frames"]
        await apply_runtime_reconciliation_action(
            session,
            task_id=run_id,
            tenant_id=tenant_id,
            action="retry",
            reason="operator verified edit did not commit",
            actor_user_id=agent.creator_id,
            confirmed=True,
            evidence_digest=view["recovery_evidence"]["digest"],
            frame_decisions=[
                {
                    "runtime_task_id": frame["runtime_task_id"],
                    "tool_call_id": frame["tool_call_id"],
                    "tool_name": frame["tool_name"],
                    "decision": "retry",
                }
                for frame in frames
            ],
            operation_id=None,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        claimed = await RuntimeTaskClaimService(
            db=session,
            worker_id="workflow-reconciliation-worker",
            task_types=("workflow",),
            lease_seconds=60,
        ).claim_available(batch_size=1)
    assert [task.id for task in claimed] == [run_id]
    claim = claimed[0]
    resumed_calls: list[str] = []

    async def retry_leaf(request: LeafRequest) -> LeafOutcome:
        resumed_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"edited": True})

    outcome = await run_claimed_runtime_task(
        service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=retry_leaf),
        task_id=run_id,
        claim_version=claim.claim_version,
        worker_id=claim.claimed_by or "workflow-reconciliation-worker",
        lease_seconds=60,
    )

    assert outcome.status == "completed"
    assert resumed_calls == ["edit"], "the journaled done step must not replay after operator retry"


async def test_operator_retry_rejects_manifest_evidence_drift_before_workflow_replay(
    tenant_id,
    agent_id,
    owner_sessionmaker,
    app_user_sessionmaker,
    tmp_path,
    monkeypatch,
):
    from app.config import get_settings
    from app.models.agent import Agent
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    run_id = uuid.uuid4()
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker, recovery_data_root=tmp_path)

    class _WorkerDied(RuntimeError):
        pass

    definition = {
        "name": "operator-retry-drift",
        "args_schema": {},
        "steps": [
            {
                "id": "edit",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Edit report",
            }
        ],
    }
    first_claim = await _enqueue_and_claim_workflow(
        service,
        tenant_id=tenant_id,
        owner_sessionmaker=owner_sessionmaker,
        claim_sessionmaker=app_user_sessionmaker,
        run_id=run_id,
        definition_data=definition,
        args={},
        agent_id=agent_id,
        worker_id="workflow-drift-worker",
    )

    async def unsafe_leaf(request: LeafRequest) -> LeafOutcome:
        _persist_pending_frame(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=request.step_id,
            tool_name="edit_file",
        )
        raise _WorkerDied("unknown edit outcome")

    with pytest.raises(_WorkerDied):
        await run_claimed_runtime_task(
            service.resume_run(run_id, tenant_id=tenant_id, leaf_executor=unsafe_leaf),
            task_id=run_id,
            claim_version=first_claim.claim_version,
            worker_id=first_claim.claimed_by or "workflow-drift-worker",
            lease_seconds=60,
        )
    await service.resume_pending_runs(leaf_executor=lambda _request: None)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        view = await get_runtime_reconciliation_task(session, task_id=run_id, tenant_id=tenant_id)
        assert view is not None
        frames = view["recovery_evidence"]["frames"]

    drifted_session = SessionContext(
        session_id=_recovery_session_id(run_id, "edit"),
        source="subagent",
        channel="internal",
        metadata={
            "runtime_task_id": run_id.hex,
            "tenant_id": str(tenant_id),
            "claim_version": first_claim.claim_version,
            "claim_worker_id": first_claim.claimed_by,
            "recovery_checkpoint_seq": 2,
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-write_file",
                    "tool_name": "write_file",
                    "arguments": {"path": "workspace/report.md", "content": "drifted"},
                    "status": "running",
                }
            ],
        },
    )
    assert persist_recovery_manifest(agent_id, drifted_session, data_root=tmp_path)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="could not be reconciled"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=run_id,
                tenant_id=tenant_id,
                action="retry",
                reason="operator verified original edit did not commit",
                actor_user_id=agent.creator_id,
                confirmed=True,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=[
                    {
                        "runtime_task_id": frame["runtime_task_id"],
                        "tool_call_id": frame["tool_call_id"],
                        "tool_name": frame["tool_name"],
                        "decision": "retry",
                    }
                    for frame in frames
                ],
                operation_id=None,
            )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
    assert task.status == "needs_reconciliation"
    assert "reconciliation_operation" not in task.metadata_json
    assert task.metadata_json["recovery_evidence_status"] == "ready"
    assert task.metadata_json["retired_reconciliation_operations"][-1]["status"] == "evidence_drifted"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        refreshed = await get_runtime_reconciliation_task(session, task_id=run_id, tenant_id=tenant_id)
        assert refreshed is not None
        refreshed_target = refreshed["recovery_evidence"]["targets"][0]
        assert refreshed_target["workflow_step_id"] == "edit"
        assert refreshed_target["workflow_leaf_id"] == "singleton"
        completed_retry = await apply_runtime_reconciliation_action(
            session,
            task_id=run_id,
            tenant_id=tenant_id,
            action="retry",
            reason="operator reviewed refreshed manifest evidence",
            actor_user_id=agent.creator_id,
            confirmed=True,
            evidence_digest=refreshed["recovery_evidence"]["digest"],
            frame_decisions=[
                {
                    "runtime_task_id": frame["runtime_task_id"],
                    "tool_call_id": frame["tool_call_id"],
                    "tool_name": frame["tool_name"],
                    "decision": "retry",
                }
                for frame in refreshed["recovery_evidence"]["frames"]
            ],
            operation_id=None,
        )

    assert completed_retry["status"] == "pending"
    assert completed_retry["metadata"]["reconciliation_status"] == "retry_requested"
    assert completed_retry["reconciliation_operation"]["status"] == "completed"
