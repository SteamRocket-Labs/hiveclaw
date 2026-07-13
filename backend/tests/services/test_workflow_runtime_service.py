"""§9 P3 red tests: WorkflowRuntimeService against real PostgreSQL.

kill-resume + journal persistence MUST run on real PG (route principle);
the fake/injected leaf executor is only for isolating control flow.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowQuota, WorkflowQuotaReservation, WorkflowStep
from app.runtime.dynamic_workflow import build_dynamic_workflow_run_metadata
from app.runtime.workflow_definition import compute_definition_hash
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import PGQuotaReserver, WorkflowRuntimeService
import app.services.workflow_runtime_service as workflow_runtime

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "two-step",
        "args_schema": {"target": {"type": "string", "required": True}},
        "default_budget": {"max_total_tokens": 50_000},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan {{args.target}}",
            },
            {
                "id": "report",
                "type": "agent_step",
                "leaf": {"name": "reporter", "type": "worker"},
                "task": "Report on {{steps.scan.output}}",
            },
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-svc", slug=f"wf-{tid.hex[:10]}"))
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
                    task.completed_at = datetime.now(timezone.utc)


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowRuntimeService:
    return WorkflowRuntimeService(session_factory=owner_sessionmaker)


@pytest.fixture()
async def agent_in_db(owner_sessionmaker, tenant_id) -> uuid.UUID:
    """A real Agent row (+ owning user) so a headless run can bind a ChatSession
    against live FK constraints (agents.id / users.id)."""
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"u-{uid.hex[:10]}",
                email=f"{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="WF Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="wf-agent", role_description="w", creator_id=uid))
    return aid


def _ok_leaf(calls: list[LeafRequest] | None = None):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        if calls is not None:
            calls.append(request)
        return LeafOutcome(ok=True, output={"echo": request.task})

    return leaf


async def test_start_run_completes_and_journals(service, tenant_id, owner_sessionmaker):
    calls: list[LeafRequest] = []
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(calls),
    )

    assert handle.outcome.status == "completed"
    assert [c.step_id for c in calls] == ["scan", "report"]

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        steps = (
            (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id))).scalars().all()
        )
        quota = (await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))).scalar_one()

    assert task.task_type == "workflow"
    assert task.status == "completed"
    assert task.metadata_json["definition_source"] == "ephemeral"
    assert task.metadata_json["tenant_id"] == str(tenant_id)
    assert {s.step_id: s.status for s in steps} == {"scan": "done", "report": "done"}
    assert quota.allocated_tokens == 50_000


async def test_runtime_reconciliation_transition_stops_next_leaf_and_survives_finalizer(
    service,
    tenant_id,
    owner_sessionmaker,
):
    run_id = uuid.uuid4()
    calls: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        if request.step_id == "scan":
            async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
                task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
                task.status = "needs_reconciliation"
                metadata = dict(task.metadata_json or {})
                metadata["needs_reconciliation"] = ["scan"]
                task.metadata_json = metadata
                step = (
                    await session.execute(
                        select(WorkflowStep).where(
                            WorkflowStep.run_id == run_id,
                            WorkflowStep.step_id == "scan",
                        )
                    )
                ).scalar_one()
                step.status = "unknown_requires_reconciliation"
        return LeafOutcome(ok=True, output={"echo": request.task})

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=leaf,
        run_id=run_id,
    )

    assert calls == ["scan"], "a reconciliation transition must stop execution at the next step boundary"
    assert handle.outcome.status == "suspended"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.run_id == run_id,
                    WorkflowStep.step_id == "scan",
                )
            )
        ).scalar_one()
    assert task.status == "needs_reconciliation"
    assert step.status == "unknown_requires_reconciliation"


async def test_reconciliation_between_journal_commit_and_projection_blocks_stale_done_event(
    service,
    tenant_id,
    owner_sessionmaker,
    agent_in_db,
    monkeypatch,
):
    from app.services.workflow_runtime_service import _PGWorkflowJournal

    run_id = uuid.uuid4()
    await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=lambda _request: None,
        agent_id=agent_in_db,
        run_id=run_id,
        enqueue_only=True,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        task.status = "needs_reconciliation"
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="scan",
                step_type="agent_step",
                status="unknown_requires_reconciliation",
            )
        )

    projected: list[str] = []

    async def append_event(**kwargs):
        projected.append(str(kwargs["event_type"]))

    monkeypatch.setattr("app.services.workflow_runtime_service.append_session_event", append_event)
    journal = _PGWorkflowJournal(
        owner_sessionmaker,
        tenant_id,
        agent_id=agent_in_db,
        run_id=run_id,
        parent_session_id=uuid.uuid4(),
    )

    await journal._append_step_event(run_id.hex, "scan", status="done")

    assert projected == []


async def test_workflow_root_reserves_and_settles_background_execution(
    service,
    tenant_id,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService

    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="workflow_test",
            root_run_key=f"workflow-test:{uuid.uuid4()}",
            source="workflow",
            profile="workflow",
            max_background_tasks=2,
            enforcement_mode="enforce",
        )
    )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        budget_run_id=budget_run.id,
        budget_service=budget_service,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        stored_budget = (
            await session.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run.id))
        ).scalar_one()
    assert handle.outcome.status == "completed"
    assert task.budget_admission_status == "settled"
    assert task.budget_reservation_key == f"workflow:{handle.run_id}:start"
    assert stored_budget.reserved_background_tasks == 0
    assert stored_budget.used_background_tasks == 1


async def test_workflow_over_budget_persists_exact_frozen_task_for_approval(
    service,
    tenant_id,
    owner_sessionmaker,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService

    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="workflow_test",
            root_run_key=f"workflow-test:{uuid.uuid4()}",
            source="workflow",
            profile="workflow",
            max_background_tasks=0,
            enforcement_mode="enforce",
            fail_mode="require_confirmation",
        )
    )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        budget_run_id=budget_run.id,
        budget_service=budget_service,
        enqueue_only=True,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        frozen_budget = (
            await session.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run.id))
        ).scalar_one()
    assert handle.outcome.status == "pending"
    assert handle.outcome.reason == "waiting_budget_approval"
    assert task.status == "pending"
    assert task.budget_admission_status == "waiting_budget_approval"
    assert frozen_budget.status == "waiting_budget_approval"

    await budget_service.approve_overrun(
        tenant_id=tenant_id,
        budget_run_id=budget_run.id,
        reason="approve workflow",
        actor_user_id=uuid.uuid4(),
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        resumed_task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert resumed_task.status == "pending"
    assert resumed_task.budget_admission_status == "approved"


async def test_dynamic_workflow_run_updates_decision_entry_with_outcome_and_repair(
    service, tenant_id, owner_sessionmaker
):
    async def flaky_leaf(request: LeafRequest) -> LeafOutcome:
        if request.step_id == "report":
            return LeafOutcome(ok=False, error="report failed")
        return LeafOutcome(ok=True, output={"echo": request.task})

    run_metadata = build_dynamic_workflow_run_metadata(
        proposal_id="proposal-1",
        candidate_id="candidate-1",
        preview_id="preview-1",
        definition_hash=compute_definition_hash(_definition()),
        args_hash=compute_definition_hash({"target": "acme"}),
        candidate={"failure_policy": {"repair_rounds": 1}},
    )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=flaky_leaf,
        definition_source="dynamic_workflow",
        run_metadata=run_metadata,
    )

    assert handle.outcome.status == "failed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()

    dynamic = task.metadata_json["dynamic_workflow"]
    entry = dynamic["workflow_decision_entry"]
    assert entry["schema"] == "hive.ccplus.workflow_decision.v1"
    assert entry["proposal_id"] == "proposal-1"
    assert entry["candidate_id"] == "candidate-1"
    assert entry["preview_id"] == "preview-1"
    assert entry["run_id"] == str(handle.run_id)
    assert entry["outcome"]["status"] == "failed"
    # AgentStep failures journal at STEP level (leaf rows belong to fanout):
    # the report step fails, so the evidence counts one failed step, zero
    # failed leaves, and the repair strategy resumes from the failed step.
    assert entry["outcome"]["steps_failed"] == 1
    assert entry["outcome"]["leaf_failed"] == 0
    assert entry["repair_plan"]["repairable"] is True
    assert entry["repair_plan"]["strategy"] == "resume_from_failed_step"
    assert entry["repair_plan"]["failed_step_count"] == 1
    assert entry["promotion_eligible"] is False


async def test_start_run_projects_workflow_progress_into_parent_session(service, tenant_id, monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    recorded: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded.append(kwargs)
        return None

    monkeypatch.setattr(workflow_runtime, "append_session_event", fake_append_session_event, raising=False)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_id,
        user_id=user_id,
        parent_session_id=session_id,
    )

    assert handle.outcome.status == "completed"
    payloads = [call["metadata"] for call in recorded]
    event_types = [payload["type"] for payload in payloads]
    assert event_types[0] == "workflow_run"
    assert event_types[-1] == "workflow_run"
    assert event_types.count("workflow_step") >= 4
    assert "runtime_action_started" in event_types
    assert "runtime_action_completed" in event_types
    assert event_types.count("runtime_action_progress") >= 4
    assert {call["session_id"] for call in recorded} == {str(session_id)}
    assert payloads[0]["status"] == "running"
    assert payloads[-1]["status"] == "completed"
    assert payloads[-1]["workflow_run_id"] == str(handle.run_id)
    assert payloads[-1]["runtime_task_id"] == str(handle.run_id)
    action_payloads = [payload for payload in payloads if payload["type"].startswith("runtime_action_")]
    assert {payload["action_kind"] for payload in action_payloads} == {"workflow"}
    assert {payload["notification_source"] for payload in action_payloads} == {"workflow"}
    assert all(payload["workflow_run_id"] == str(handle.run_id) for payload in action_payloads)
    step_payloads = [payload for payload in payloads if payload["type"] == "workflow_step"]
    assert {payload["workflow_step_id"] for payload in step_payloads} == {"scan", "report"}
    assert any(payload["status"] == "running" for payload in step_payloads)
    assert any(payload["status"] == "done" for payload in step_payloads)
    assert all(str(call["user_id"]) == str(user_id) for call in recorded)


async def test_kill_then_resume_only_runs_remaining_step(service, tenant_id, owner_sessionmaker):
    """THE P3 contract: two-step sequence, killed after step one — resume
    executes ONLY the second step (real-PG journal proves the skip)."""
    first_calls: list[LeafRequest] = []
    kill_after_first = {"armed": True}

    async def killing_leaf(request: LeafRequest) -> LeafOutcome:
        first_calls.append(request)
        if kill_after_first["armed"]:
            kill_after_first["armed"] = False
            await service.kill_run(request.run_id, tenant_id=request.tenant_id)
        return LeafOutcome(ok=True, output={"echo": request.task})

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=killing_leaf,
    )
    assert handle.outcome.status == "killed"
    assert [c.step_id for c in first_calls] == ["scan"]

    resume_calls: list[LeafRequest] = []
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=_ok_leaf(resume_calls))

    assert outcome.status == "completed"
    assert [c.step_id for c in resume_calls] == ["report"], "scan must NOT re-execute on resume"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "completed"


async def test_killed_run_not_picked_by_startup_resume(service, tenant_id, owner_sessionmaker):
    kill_all = {"armed": True}

    async def killing_leaf(request: LeafRequest) -> LeafOutcome:
        if kill_all["armed"]:
            kill_all["armed"] = False
            await service.kill_run(request.run_id, tenant_id=request.tenant_id)
        return LeafOutcome(ok=True, output={})

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "x"},
        leaf_executor=killing_leaf,
    )
    assert handle.outcome.status == "killed"

    resumed = await service.resume_pending_runs(leaf_executor=_ok_leaf())
    assert handle.run_id not in [r.run_id for r in resumed], "killed runs must never be auto-resumed"


async def test_startup_does_not_replay_executor_unknown_run(service, tenant_id, owner_sessionmaker):
    """A crash after executor dispatch is quarantined, never blindly replayed."""
    crash = {"armed": True}

    class _SimulatedCrash(RuntimeError):
        pass

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        if crash["armed"] and request.step_id == "report":
            crash["armed"] = False
            raise _SimulatedCrash("process died mid-run")
        return LeafOutcome(ok=True, output={"echo": request.task})

    with pytest.raises(_SimulatedCrash):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "x"},
            leaf_executor=crashing_leaf,
        )

    resumed = await service.resume_pending_runs(leaf_executor=_ok_leaf())
    assert resumed == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (
            await session.execute(
                select(RuntimeTask).where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "workflow",
                )
            )
        ).scalar_one()
        reservation = (
            (await session.execute(select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.run_id == task.id)))
            .scalars()
            .all()
        )
    assert task.status == "needs_reconciliation"
    assert any(receipt.state == "needs_reconciliation" for receipt in reservation)


async def test_daemon_requeue_moves_only_expired_workflow_claim_behind_shared_worker(
    service,
    tenant_id,
    owner_sessionmaker,
    monkeypatch,
):
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "x"},
        leaf_executor=_ok_leaf(),
        enqueue_only=True,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"
        task.claimed_by = "dead-worker"
        task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    wakeups: list[tuple[str, uuid.UUID]] = []

    async def record_wakeup(*, reason, runtime_task_id=None):
        wakeups.append((reason, runtime_task_id))

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", record_wakeup)

    requeued = await service.requeue_pending_runs()

    assert requeued == [handle.run_id]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "resumable"
    assert task.claimed_by is None
    assert task.claim_expires_at is None
    assert task.metadata_json["workflow_requeue_reason"] == "workflow_expired_claim_requeue"
    assert wakeups == [("workflow_expired_claim_requeue", handle.run_id)]


async def test_daemon_requeue_does_not_steal_live_workflow_claim(
    service,
    tenant_id,
    owner_sessionmaker,
    monkeypatch,
):
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "x"},
        leaf_executor=_ok_leaf(),
        enqueue_only=True,
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"
        task.claimed_by = "live-worker"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    wakeups: list[object] = []

    async def record_wakeup(**kwargs):
        wakeups.append(kwargs)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", record_wakeup)

    assert await service.requeue_pending_runs() == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "running"
    assert task.claimed_by == "live-worker"
    assert wakeups == []
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "killed"


async def test_startup_quarantines_executor_unknown_run_under_nonowner_rls(
    tenant_id, owner_sessionmaker, app_user_sessionmaker
):
    """The production app role must not discover/replay reconciliation work."""
    owner_service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    app_role_service = WorkflowRuntimeService(session_factory=app_user_sessionmaker)
    crash = {"armed": True}

    class _SimulatedCrash(RuntimeError):
        pass

    async def crashing_leaf(request: LeafRequest) -> LeafOutcome:
        if crash["armed"] and request.step_id == "report":
            crash["armed"] = False
            raise _SimulatedCrash("process died mid-run")
        return LeafOutcome(ok=True, output={"echo": request.task})

    with pytest.raises(_SimulatedCrash):
        await owner_service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "x"},
            leaf_executor=crashing_leaf,
        )

    resumed = await app_role_service.resume_pending_runs(leaf_executor=_ok_leaf())

    assert resumed == []
    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        task = (
            await session.execute(
                select(RuntimeTask).where(
                    RuntimeTask.tenant_id == tenant_id,
                    RuntimeTask.task_type == "workflow",
                )
            )
        ).scalar_one()
    assert task.status == "needs_reconciliation"


async def test_checkpoint_gate_decider_resolves_run_tenant_under_nonowner_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    tenant_id,
):
    from app.services.workflow_runtime_service import CheckpointGateDecider

    run_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="suspended",
                metadata_json={"tenant_id": str(tenant_id)},
            )
        )

    decider = CheckpointGateDecider(session_factory=app_user_sessionmaker)

    assert await decider._tenant_for_run(str(run_id)) == str(tenant_id)


async def test_resume_does_not_reuse_steps_from_other_definition_hash(service, tenant_id, owner_sessionmaker):
    """Journal rows stamped with a DIFFERENT definition_hash are never
    reused — simulates a stale journal from an older definition version."""
    calls: list[LeafRequest] = []
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(calls),
    )

    # Corrupt the journal: pretend 'scan' was done under an older definition.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        steps = (
            (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id))).scalars().all()
        )
        for step in steps:
            if step.step_id == "scan":
                step.definition_hash = "stale-hash-from-v1"

    # Reset the run to running so it can be resumed.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"

    resume_calls: list[LeafRequest] = []
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=_ok_leaf(resume_calls))

    assert outcome.status == "completed"
    assert "scan" in [c.step_id for c in resume_calls], "stale-hash step must re-execute"


async def test_load_run_returns_task_and_steps(service, tenant_id):
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "x"},
        leaf_executor=_ok_leaf(),
    )
    loaded = await service.load_run(handle.run_id, tenant_id=tenant_id)
    assert loaded is not None
    assert loaded.task.id == handle.run_id
    assert {s.step_id for s in loaded.steps} == {"scan", "report"}


async def test_runtime_feature_flag_fails_closed_before_creating_run(
    service, tenant_id, owner_sessionmaker, monkeypatch
):
    from app.config import get_settings
    from app.runtime.workflow_admission import WorkflowAdmissionError

    settings = get_settings()
    monkeypatch.setattr(settings, "WORKFLOW_RUNTIME_ENABLED", False)

    with pytest.raises(WorkflowAdmissionError, match="disabled"):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "x"},
            leaf_executor=_ok_leaf(),
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        count = (
            (
                await session.execute(
                    select(RuntimeTask).where(
                        RuntimeTask.task_type == "workflow",
                        RuntimeTask.metadata_json["tenant_id"].as_string() == str(tenant_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert count == []


async def test_workflow_metrics_record_run_step_leaf_resume_and_quota_denial(service, tenant_id):
    from app.config import get_settings
    from app.services.workflow_metrics import reset_workflow_metrics, snapshot_workflow_metrics

    settings = get_settings()
    reset_workflow_metrics()

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "x"},
        leaf_executor=_ok_leaf(),
    )
    await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=_ok_leaf())

    calls: list[LeafRequest] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        return LeafOutcome(ok=True, output={}, tokens_used=settings.WORKFLOW_LEAF_TOKEN_ESTIMATE)

    budget_starved = dict(_definition())
    budget_starved["default_budget"] = {"max_total_tokens": settings.WORKFLOW_LEAF_TOKEN_ESTIMATE}
    budget_starved["steps"] = [
        budget_starved["steps"][0],
        {
            "id": "second",
            "type": "agent_step",
            "leaf": {"name": "second", "type": "worker"},
            "task": "Second",
        },
    ]
    await service.start_run(
        tenant_id=tenant_id,
        definition_data=budget_starved,
        args={"target": "x"},
        leaf_executor=leaf,
    )

    snapshot = snapshot_workflow_metrics()
    assert snapshot["runs_started_total"] >= 2
    assert snapshot["runs_finished_total"]["completed"] >= 1
    assert snapshot["steps_total"]["done"] >= 2
    assert snapshot["leaf_calls_total"]["done"] >= 1
    assert snapshot["resume_attempts_total"] >= 1
    assert snapshot["quota_denials_total"] >= 1
    assert snapshot["step_duration_seconds"]["count"] >= 1


# ── run history list (asset view, §4 一次性编排归档) ────────────────


async def test_list_runs_for_agent_scopes_counts_and_provenance(service, tenant_id, owner_sessionmaker):
    """list_runs_for_agent returns ONLY the agent's tenant-mirrored runs,
    newest first, with step counts and promote provenance."""
    from app.models.tenant import Tenant
    from app.services.workflow_definitions import WorkflowDefinitionService

    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    first = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "one"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_a,
    )
    second = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "two"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_a,
    )
    # another agent's run — must not leak into A's history
    await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "other"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_b,
    )
    # same parent agent under a FOREIGN tenant — the metadata mirror is the
    # boundary (runtime_tasks has no tenant column)
    foreign_tenant = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=foreign_tenant, name="wf-foreign", slug=f"fx-{foreign_tenant.hex[:10]}"))
    await service.start_run(
        tenant_id=foreign_tenant,
        definition_data=_definition(),
        args={"target": "foreign"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_a,
    )

    # A legacy direct draft is deliberately quarantined and must NOT make the
    # run look promoted. Only an approved immutable proposal surfaces.
    definitions = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    record = await definitions.create_draft(
        tenant_id=tenant_id,
        definition_data=_definition(),
        owner_type="agent",
        owner_id=agent_a,
        promoted_from_run_id=first.run_id,
    )

    summaries = await service.list_runs_for_agent(agent_a, tenant_id=tenant_id)

    assert [s.task.id for s in summaries] == [second.run_id, first.run_id], (
        "newest first, only agent A's tenant-mirrored runs"
    )
    by_id = {s.task.id: s for s in summaries}
    assert record.promotion_proposal_id is None
    assert by_id[first.run_id].promoted_definition_id is None
    assert by_id[second.run_id].promoted_definition_id is None
    assert by_id[first.run_id].step_counts.get("done") == 2
    assert by_id[first.run_id].task.metadata_json["definition_json"]["name"] == "two-step"


# ── §A-2: completion event carries the run's deliverable outputs ──────────


async def test_completion_session_event_projects_run_outputs(service, tenant_id, monkeypatch):
    """The session is the run's truth surface: the COMPLETED workflow_run event
    must carry the per-step deliverable outputs, not just a status row.

    Revert-sensitive: dropping the `outputs=outcome.outputs` wiring (or the
    payload projection) makes the completion event outputs disappear → fail.
    """
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    recorded: list[dict] = []

    async def fake_append_session_event(**kwargs):
        recorded.append(kwargs)
        return None

    monkeypatch.setattr(workflow_runtime, "append_session_event", fake_append_session_event, raising=False)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_id,
        user_id=user_id,
        parent_session_id=session_id,
    )

    assert handle.outcome.status == "completed"
    run_payloads = [call["metadata"] for call in recorded if call["metadata"]["type"] == "workflow_run"]
    completion = run_payloads[-1]
    assert completion["status"] == "completed"
    # The deliverable outputs of every executed step are projected into session.
    assert "outputs" in completion, "completion event must project the run outputs into the session"
    assert set(completion["outputs"]) == {"scan", "report"}
    assert completion["outputs"]["scan"] == {"echo": "Scan acme"}
    assert completion["deliverable_step_ids"] == ["report", "scan"]
    # The 'running' event has no outputs yet — outputs are a completion fact.
    assert "outputs" not in run_payloads[0]


async def test_completed_workflow_wakes_parent_session_with_task_notification(
    service, tenant_id, agent_in_db, owner_sessionmaker, monkeypatch
):
    """A workflow completion is not closed until the parent Agent loop receives
    a CC-style task-notification continuation, not only a session event/signal.
    """

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

    parent_session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Parent workflow session",
                source_channel="web",
            )
        )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_in_db,
        user_id=agent.creator_id,
        parent_session_id=parent_session_id,
    )

    assert handle.outcome.status == "completed"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = list(
            (
                await session.execute(
                    select(RuntimeNotificationOutbox).where(
                        RuntimeNotificationOutbox.source_kind == "workflow",
                        RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    notification = rows[0]
    assert notification.status == "pending"
    assert notification.parent_session_id == parent_session_id
    assert notification.task_type == "workflow"
    assert notification.terminal_status == "completed"
    assert "Workflow run" in notification.summary
    assert "scan" in notification.summary
    assert "report" in notification.summary


async def test_terminal_truth_repair_restores_workflow_notification_and_channel_outboxes_once(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    """A worker crash after terminal RuntimeTask commit cannot lose either consumer."""

    from sqlalchemy import delete, func

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

    parent_session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Workflow crash repair",
                source_channel="telegram",
                delivery_target_json={"channel": "telegram", "chat_id": "workflow-crash"},
            )
        )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "crash-window"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_in_db,
        user_id=agent.creator_id,
        parent_session_id=parent_session_id,
        delivery_target={"channel": "telegram", "chat_id": "workflow-crash"},
    )
    assert handle.outcome.status == "completed"

    # Reconstruct the exact crash state from immutable execution truth: terminal
    # RuntimeTask committed while both consumer intents are absent.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        await session.execute(
            delete(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_kind == "workflow",
                RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
            )
        )
        await session.execute(
            delete(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == handle.run_id)
        )

    notifications = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    channels = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    assert await notifications.reconcile_terminal_tasks_once(limit=100, task_ids={handle.run_id}) == 1
    assert await channels.reconcile_workflow_terminal_runs_once(limit=100, task_ids={handle.run_id}) == 1
    assert await notifications.reconcile_terminal_tasks_once(limit=100, task_ids={handle.run_id}) == 0
    assert await channels.reconcile_workflow_terminal_runs_once(limit=100, task_ids={handle.run_id}) == 0

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        notification_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.source_kind == "workflow",
                        RuntimeNotificationOutbox.source_run_id == str(handle.run_id),
                    )
                )
            ).scalar_one()
        )
        channel_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChannelDeliveryOutbox)
                    .where(ChannelDeliveryOutbox.runtime_task_id == handle.run_id)
                )
            ).scalar_one()
        )
    assert notification_count == 1
    assert channel_count == 1


async def test_channel_repair_filters_invalid_session_poison_before_limit_and_advances_oldest(
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    from sqlalchemy import delete, func

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    valid_session_id = uuid.uuid4()
    valid_task_id = uuid.uuid4()
    poison_task_ids = [uuid.uuid4() for _ in range(500)]
    oldest = datetime.now(timezone.utc) - timedelta(hours=2)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=valid_session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Valid oldest channel repair target",
                source_channel="telegram",
                delivery_target_json={"channel": "telegram", "chat_id": "valid-oldest"},
            )
        )
        session.add(
            RuntimeTask(
                id=valid_task_id,
                tenant_id=tenant_id,
                task_type="workflow",
                status="completed",
                parent_agent_id=agent_in_db,
                parent_session_id=str(valid_session_id),
                root_user_id=agent.creator_id,
                created_at=oldest,
                completed_at=oldest,
                result_summary="valid oldest workflow result",
                metadata_json={
                    "user_id": str(agent.creator_id),
                    "parent_session_id": str(valid_session_id),
                    "delivery_target_json": {"channel": "telegram", "chat_id": "valid-oldest"},
                },
            )
        )
        session.add_all(
            [
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(uuid.uuid4()),
                    root_user_id=agent.creator_id,
                    created_at=oldest + timedelta(seconds=index + 1),
                    completed_at=oldest + timedelta(seconds=index + 1),
                    result_summary=f"invalid channel repair poison {index}",
                    metadata_json={
                        "user_id": str(agent.creator_id),
                        "delivery_target_json": {"channel": "telegram", "chat_id": f"poison-{index}"},
                    },
                )
                for index, task_id in enumerate(poison_task_ids)
            ]
        )

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    created_task_ids = [valid_task_id, *poison_task_ids]
    try:
        assert await service.reconcile_workflow_terminal_runs_once(limit=1) == 1

        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            repaired = int(
                (
                    await session.execute(
                        select(func.count(ChannelDeliveryOutbox.id)).where(
                            ChannelDeliveryOutbox.runtime_task_id == valid_task_id,
                        )
                    )
                ).scalar_one()
            )
        assert repaired == 1
    finally:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            await session.execute(
                delete(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id.in_(created_task_ids))
            )
            await session.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(created_task_ids)))
            await session.execute(delete(ChatSession).where(ChatSession.id == valid_session_id))


async def test_channel_repair_uses_session_user_authority_and_does_not_starve_later_valid_task(
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    from sqlalchemy import delete, func

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    session_id = uuid.uuid4()
    poison_task_id = uuid.uuid4()
    valid_task_id = uuid.uuid4()
    oldest = datetime.now(timezone.utc) - timedelta(hours=2)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Canonical channel owner authority",
                source_channel="telegram",
                delivery_target_json={"channel": "telegram", "chat_id": "canonical-owner"},
            )
        )
        session.add_all(
            [
                RuntimeTask(
                    id=poison_task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(session_id),
                    root_user_id=agent.creator_id,
                    created_at=oldest,
                    completed_at=oldest,
                    result_summary="oldest task with stale user metadata",
                    metadata_json={
                        "user_id": "invalid-user-uuid",
                        "parent_session_id": str(session_id),
                        "delivery_target_json": {"channel": "telegram", "chat_id": "oldest-poison"},
                    },
                ),
                RuntimeTask(
                    id=valid_task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(session_id),
                    root_user_id=agent.creator_id,
                    created_at=oldest + timedelta(seconds=1),
                    completed_at=oldest + timedelta(seconds=1),
                    result_summary="later valid workflow delivery",
                    metadata_json={
                        "parent_session_id": str(session_id),
                        "delivery_target_json": {"channel": "telegram", "chat_id": "later-valid"},
                    },
                ),
            ]
        )

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    task_ids = {poison_task_id, valid_task_id}
    try:
        assert await service.reconcile_workflow_terminal_runs_once(limit=1, task_ids=task_ids) == 1
        assert await service.reconcile_workflow_terminal_runs_once(limit=1, task_ids=task_ids) == 1
        assert await service.reconcile_workflow_terminal_runs_once(limit=1, task_ids=task_ids) == 0

        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            rows = list(
                (
                    await session.execute(
                        select(ChannelDeliveryOutbox)
                        .where(ChannelDeliveryOutbox.runtime_task_id.in_(task_ids))
                        .order_by(ChannelDeliveryOutbox.runtime_task_id)
                    )
                )
                .scalars()
                .all()
            )
            count = int(
                (
                    await session.execute(
                        select(func.count(ChannelDeliveryOutbox.id)).where(
                            ChannelDeliveryOutbox.runtime_task_id.in_(task_ids)
                        )
                    )
                ).scalar_one()
            )
        assert count == 2
        assert {row.user_id for row in rows} == {agent.creator_id}
    finally:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            await session.execute(
                delete(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id.in_(task_ids))
            )
            await session.execute(delete(RuntimeTask).where(RuntimeTask.id.in_(task_ids)))
            await session.execute(delete(ChatSession).where(ChatSession.id == session_id))


async def test_channel_repair_rejects_metadata_session_authority_spoof(
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    from sqlalchemy import delete

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.user import User
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    canonical_session_id = uuid.uuid4()
    forged_session_id = uuid.uuid4()
    forged_user_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            User(
                id=forged_user_id,
                username=f"forged-{forged_user_id.hex[:10]}",
                email=f"forged-{forged_user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Forged session owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add_all(
            [
                ChatSession(
                    id=canonical_session_id,
                    agent_id=agent_in_db,
                    tenant_id=tenant_id,
                    user_id=agent.creator_id,
                    title="Canonical Workflow delivery session",
                    source_channel="telegram",
                ),
                ChatSession(
                    id=forged_session_id,
                    agent_id=agent_in_db,
                    tenant_id=tenant_id,
                    user_id=forged_user_id,
                    title="Metadata-forged Workflow delivery session",
                    source_channel="telegram",
                ),
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(canonical_session_id),
                    root_user_id=agent.creator_id,
                    result_summary="canonical authority result",
                    metadata_json={
                        "user_id": str(forged_user_id),
                        "parent_session_id": str(forged_session_id),
                        "delivery_target_json": {"channel": "telegram", "chat_id": "canonical-authority"},
                    },
                ),
            ]
        )

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    try:
        assert await service.reconcile_workflow_terminal_runs_once(limit=1, task_ids={task_id}) == 1
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task_id)
                )
            ).scalar_one()
        assert row.session_id == canonical_session_id
        assert row.user_id == agent.creator_id
        assert row.metadata_json["owner_user_authority_source"] == "chat_session"
        assert row.metadata_json["recorded_runtime_parent_session_id"] == str(forged_session_id)
        assert row.metadata_json["recorded_runtime_user_id"] == str(forged_user_id)
    finally:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            await session.execute(delete(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task_id))
            await session.execute(delete(RuntimeTask).where(RuntimeTask.id == task_id))
            await session.execute(
                delete(ChatSession).where(ChatSession.id.in_((canonical_session_id, forged_session_id)))
            )
            await session.execute(delete(User).where(User.id == forged_user_id))


async def test_channel_repair_ignores_missing_metadata_session_and_uses_canonical_runtime_session(
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    from sqlalchemy import delete

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    canonical_session_id = uuid.uuid4()
    missing_metadata_session_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add_all(
            [
                ChatSession(
                    id=canonical_session_id,
                    agent_id=agent_in_db,
                    tenant_id=tenant_id,
                    user_id=agent.creator_id,
                    title="Canonical repair session",
                    source_channel="telegram",
                ),
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant_id,
                    task_type="workflow",
                    status="completed",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(canonical_session_id),
                    root_user_id=agent.creator_id,
                    result_summary="metadata must not block repair",
                    metadata_json={
                        "user_id": "invalid-user-evidence",
                        "parent_session_id": str(missing_metadata_session_id),
                        "delivery_target_json": {"channel": "telegram", "chat_id": "canonical-repair"},
                    },
                ),
            ]
        )

    service = ChannelDeliveryOutboxService(session_factory=owner_sessionmaker)
    try:
        assert await service.reconcile_workflow_terminal_runs_once(limit=1, task_ids={task_id}) == 1
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            row = (
                await session.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task_id)
                )
            ).scalar_one()
        assert row.session_id == canonical_session_id
        assert row.user_id == agent.creator_id
        assert row.metadata_json["recorded_runtime_parent_session_id"] == str(missing_metadata_session_id)
        assert row.metadata_json["recorded_runtime_user_id"] == "invalid-user-evidence"
    finally:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            await session.execute(delete(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.runtime_task_id == task_id))
            await session.execute(delete(RuntimeTask).where(RuntimeTask.id == task_id))
            await session.execute(delete(ChatSession).where(ChatSession.id == canonical_session_id))


async def test_terminal_commit_atomically_contains_notification_and_channel_intents_before_crash(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
    monkeypatch,
):
    from sqlalchemy import func

    from app.models.agent import Agent
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

    parent_session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Atomic terminal delivery",
                source_channel="telegram",
                delivery_target_json={"channel": "telegram", "chat_id": "atomic-terminal"},
            )
        )

    class WorkerCrashed(RuntimeError):
        pass

    async def crash_immediately_after_terminal_commit(action, **_kwargs):
        if action == "workflow_run_completed":
            raise WorkerCrashed("process exited after terminal transaction commit")

    monkeypatch.setattr(service, "_audit", crash_immediately_after_terminal_commit)
    run_id = uuid.uuid4()
    with pytest.raises(WorkerCrashed):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "atomic-crash"},
            leaf_executor=_ok_leaf(),
            agent_id=agent_in_db,
            user_id=agent.creator_id,
            parent_session_id=parent_session_id,
            delivery_target={"channel": "telegram", "chat_id": "atomic-terminal"},
            run_id=run_id,
        )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        notification_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.source_kind == "workflow",
                        RuntimeNotificationOutbox.source_run_id == str(run_id),
                    )
                )
            ).scalar_one()
        )
        channel_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChannelDeliveryOutbox)
                    .where(ChannelDeliveryOutbox.runtime_task_id == run_id)
                )
            ).scalar_one()
        )
    assert task.status == "completed"
    assert notification_count == 1
    assert channel_count == 1


async def test_live_fanout_evidence_repair_filters_pending_marker_before_limit(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    """An older ordinary reconciliation row must not starve fanout evidence."""

    ordinary_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    definition = _definition()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=ordinary_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="needs_reconciliation",
                parent_agent_id=agent_in_db,
                created_at=datetime(1900, 1, 1, tzinfo=timezone.utc),
                metadata_json={
                    "tenant_id": str(tenant_id),
                    "recovery_evidence_status": "ready",
                },
            )
        )
        session.add(
            RuntimeTask(
                id=pending_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="needs_reconciliation",
                parent_agent_id=agent_in_db,
                created_at=datetime(1900, 1, 2, tzinfo=timezone.utc),
                metadata_json={
                    "tenant_id": str(tenant_id),
                    "definition_json": definition,
                    "definition_hash": compute_definition_hash(definition),
                    "recovery_evidence_status": "incomplete",
                    "recovery_evidence_incomplete_reasons": ["workflow_fanout_evidence_aggregation_pending"],
                },
            )
        )
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=pending_id,
                step_id="scan",
                step_type="agent_step",
                status="running",
                definition_hash=compute_definition_hash(definition),
            )
        )

    repaired = await service.repair_pending_live_reconciliation_evidence(limit=1)

    assert repaired == [pending_id]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pending = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == pending_id))).scalar_one()
    reasons = set((pending.metadata_json or {}).get("recovery_evidence_incomplete_reasons") or [])
    assert "workflow_fanout_evidence_aggregation_pending" not in reasons


async def test_live_fanout_evidence_repair_dead_letters_missing_definition_without_starving_next(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    marker = "workflow_fanout_evidence_aggregation_pending"
    poisoned_id = uuid.uuid4()
    repairable_id = uuid.uuid4()
    poisoned_session_id = uuid.uuid4()
    definition = _definition()
    definition_hash = compute_definition_hash(definition)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        from app.models.agent import Agent

        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        session.add(
            ChatSession(
                id=poisoned_session_id,
                agent_id=agent_in_db,
                tenant_id=tenant_id,
                user_id=agent.creator_id,
                title="Poisoned Workflow evidence",
                source_channel="web",
            )
        )
        session.add_all(
            [
                RuntimeTask(
                    id=poisoned_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="needs_reconciliation",
                    parent_agent_id=agent_in_db,
                    parent_session_id=str(poisoned_session_id),
                    root_session_id=str(poisoned_session_id),
                    created_at=datetime(1900, 1, 1, tzinfo=timezone.utc),
                    metadata_json={
                        "tenant_id": str(tenant_id),
                        "recovery_evidence_status": "incomplete",
                        "recovery_evidence_incomplete_reasons": [marker],
                    },
                ),
                RuntimeTask(
                    id=repairable_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="needs_reconciliation",
                    parent_agent_id=agent_in_db,
                    created_at=datetime(1900, 1, 2, tzinfo=timezone.utc),
                    metadata_json={
                        "tenant_id": str(tenant_id),
                        "definition_json": definition,
                        "definition_hash": definition_hash,
                        "recovery_evidence_status": "incomplete",
                        "recovery_evidence_incomplete_reasons": [marker],
                    },
                ),
                WorkflowStep(
                    tenant_id=tenant_id,
                    run_id=repairable_id,
                    step_id="scan",
                    step_type="agent_step",
                    status="running",
                    definition_hash=definition_hash,
                ),
            ]
        )

    repaired = await service.repair_pending_live_reconciliation_evidence(limit=1)

    assert repaired == [repairable_id]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        poisoned = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == poisoned_id))).scalar_one()
    poisoned_metadata = dict(poisoned.metadata_json or {})
    assert marker not in set(poisoned_metadata.get("recovery_evidence_incomplete_reasons") or [])
    assert poisoned_metadata["workflow_fanout_evidence_dead_letter"]["reason"] == "archived_definition_missing"
    assert poisoned_metadata["recovery_evidence_status"] == "ready"


async def test_live_fanout_evidence_repair_rotates_lease_busy_prefix(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    marker = "workflow_fanout_evidence_aggregation_pending"
    busy_id = uuid.uuid4()
    repairable_id = uuid.uuid4()
    definition = _definition()
    definition_hash = compute_definition_hash(definition)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        for run_id, created_at in (
            (busy_id, datetime(1900, 1, 1, tzinfo=timezone.utc)),
            (repairable_id, datetime(1900, 1, 2, tzinfo=timezone.utc)),
        ):
            session.add(
                RuntimeTask(
                    id=run_id,
                    task_type="workflow",
                    tenant_id=tenant_id,
                    status="needs_reconciliation",
                    parent_agent_id=agent_in_db,
                    created_at=created_at,
                    metadata_json={
                        "tenant_id": str(tenant_id),
                        "definition_json": definition,
                        "definition_hash": definition_hash,
                        "recovery_evidence_status": "incomplete",
                        "recovery_evidence_incomplete_reasons": [marker],
                    },
                )
            )
            session.add(
                WorkflowStep(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    step_id="scan",
                    step_type="agent_step",
                    status="running",
                    definition_hash=definition_hash,
                )
            )

    held_lease = await service._lease_manager.try_acquire(busy_id)
    assert held_lease is not None
    try:
        repaired = await service.repair_pending_live_reconciliation_evidence(limit=1)
    finally:
        await held_lease.release()

    assert repaired == [repairable_id]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        busy = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == busy_id))).scalar_one()
    assert (busy.metadata_json or {}).get("workflow_evidence_repair_deferred_at")


async def test_unsettled_workflow_quota_repair_releases_reserved_and_quarantines_executing(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    reserved_run = uuid.uuid4()
    executing_run = uuid.uuid4()
    for run_id in (reserved_run, executing_run):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "acme"},
            leaf_executor=lambda _request: None,
            agent_id=agent_in_db,
            run_id=run_id,
            enqueue_only=True,
        )

    quota = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=1_000)
    reserved_key = f"{reserved_run}:scan:singleton:reserved-input"
    executing_key = f"{executing_run}:scan:singleton:executing-input"
    assert await quota.reserve(str(reserved_run), reservation_key=reserved_key)
    assert await quota.reserve(str(executing_run), reservation_key=executing_key)
    await quota.mark_execution_started(
        str(executing_run),
        reservation_key=executing_key,
        step_id="scan",
        leaf_id=None,
        input_hash="executing-input",
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=executing_run,
                step_id="scan",
                step_type="agent_step",
                status="running",
            )
        )

    empty_summary = await service.repair_unsettled_quota_reservations_once(limit=10, task_ids=set())
    assert empty_summary == {"settled_reserved": 0, "quarantined_executing": 0}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        untouched_states = {
            row.run_id: row.state
            for row in (
                (
                    await session.execute(
                        select(WorkflowQuotaReservation).where(
                            WorkflowQuotaReservation.run_id.in_({reserved_run, executing_run})
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
    assert untouched_states == {reserved_run: "reserved", executing_run: "executing"}

    summary = await service.repair_unsettled_quota_reservations_once(
        limit=10,
        task_ids={reserved_run, executing_run},
    )

    assert summary == {"settled_reserved": 1, "quarantined_executing": 1}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        reservations = list(
            (
                await session.execute(
                    select(WorkflowQuotaReservation).where(
                        WorkflowQuotaReservation.run_id.in_({reserved_run, executing_run})
                    )
                )
            )
            .scalars()
            .all()
        )
        tasks = {
            task.id: task
            for task in (
                (await session.execute(select(RuntimeTask).where(RuntimeTask.id.in_({reserved_run, executing_run}))))
                .scalars()
                .all()
            )
        }
    by_run = {row.run_id: row for row in reservations}
    assert by_run[reserved_run].state == "settled"
    assert by_run[reserved_run].actual_tokens == 0
    assert by_run[executing_run].state == "needs_reconciliation"
    assert tasks[executing_run].status == "needs_reconciliation"


async def test_unsettled_quota_repair_durably_rotates_live_lease_prefix(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
):
    live_run = uuid.uuid4()
    stale_run = uuid.uuid4()
    for run_id in (live_run, stale_run):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data=_definition(),
            args={"target": "acme"},
            leaf_executor=lambda _request: None,
            agent_id=agent_in_db,
            run_id=run_id,
            enqueue_only=True,
        )
    quota = PGQuotaReserver(owner_sessionmaker, tenant_id, estimate=1_000)
    live_key = f"{live_run}:scan:singleton:live"
    stale_key = f"{stale_run}:scan:singleton:stale"
    assert await quota.reserve(str(live_run), reservation_key=live_key)
    assert await quota.reserve(str(stale_run), reservation_key=stale_key)

    held_lease = await service._lease_manager.try_acquire(live_run)
    assert held_lease is not None
    try:
        first = await service.repair_unsettled_quota_reservations_once(
            limit=1,
            task_ids={live_run, stale_run},
        )
        second = await service.repair_unsettled_quota_reservations_once(
            limit=1,
            task_ids={live_run, stale_run},
        )
    finally:
        await held_lease.release()

    assert first == {"settled_reserved": 0, "quarantined_executing": 0}
    assert second == {"settled_reserved": 1, "quarantined_executing": 0}
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        live_receipt = (
            await session.execute(
                select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.logical_key == live_key)
            )
        ).scalar_one()
        stale_receipt = (
            await session.execute(
                select(WorkflowQuotaReservation).where(WorkflowQuotaReservation.logical_key == stale_key)
            )
        ).scalar_one()
    assert live_receipt.state == "reserved"
    assert live_receipt.repair_deferred_at is not None
    assert stale_receipt.state == "settled"


async def test_real_pg_crash_quarantine_preserves_durable_root_user_authority(
    service,
    tenant_id,
    agent_in_db,
    owner_sessionmaker,
    monkeypatch,
):
    """Resume must act as the original user, never as ``agent.id``.

    This executes the first attempt and the post-crash attempt through the real
    Workflow PG ledger while injecting only the subagent process boundary.
    """

    from app.agents.subagent import SubagentSpawnContext
    from app.models.agent import Agent
    from app.services import workflow_launch
    from app.services.workflow_launch import (
        build_resumable_workflow_leaf_executor,
        build_subagent_leaf_executor,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        agent = (await session.execute(select(Agent).where(Agent.id == agent_in_db))).scalar_one()
        original_user_id = agent.creator_id

    observed_users: list[uuid.UUID] = []

    class WorkerDied(RuntimeError):
        pass

    async def crashing_spawn(ctx, _spec, _task, *, budget):
        observed_users.append(ctx.parent_user_id)
        raise WorkerDied("worker died after durable step start")

    initial_executor = build_subagent_leaf_executor(
        SubagentSpawnContext(
            parent_agent_id=agent_in_db,
            parent_user_id=original_user_id,
            model=object(),
            tenant_id=tenant_id,
        ),
        spawn=crashing_spawn,
    )
    run_id = uuid.uuid4()
    with pytest.raises(WorkerDied):
        await service.start_run(
            tenant_id=tenant_id,
            definition_data={
                "name": "authority-resume",
                "args_schema": {},
                "steps": [
                    {
                        "id": "work",
                        "type": "agent_step",
                        "leaf": {"name": "worker", "type": "worker"},
                        "task": "Continue as the original requester",
                    }
                ],
            },
            args={},
            leaf_executor=initial_executor,
            agent_id=agent_in_db,
            user_id=original_user_id,
            run_id=run_id,
        )

    async def fake_resolve_agent_runtime(agent_id, *, tenant_id=None, session_factory=None):
        assert agent_id == agent_in_db
        assert uuid.UUID(str(tenant_id)) == tenant_id_fixture
        return agent, object()

    tenant_id_fixture = tenant_id
    monkeypatch.setattr(workflow_launch, "resolve_agent_runtime", fake_resolve_agent_runtime)

    async def resumed_spawn(ctx, _spec, _task, *, budget):
        observed_users.append(ctx.parent_user_id)
        return SimpleNamespace(
            result=SimpleNamespace(
                ok=True,
                status="completed",
                content="done",
                sources=[],
                tokens_used=1,
                error=None,
            )
        )

    outcome = await service.resume_run(
        run_id,
        tenant_id=tenant_id,
        leaf_executor=build_resumable_workflow_leaf_executor(
            session_factory=owner_sessionmaker,
            spawn=resumed_spawn,
        ),
    )

    assert outcome.status == "suspended"
    assert "operator reconciliation" in (outcome.reason or "")
    assert observed_users == [original_user_id], "unknown execution must not replay before operator review"
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
    assert task.root_user_id == original_user_id
    assert task.status == "needs_reconciliation"
    assert (task.metadata_json or {}).get("workflow_quota_reconciliation_pending")


# ── §A-6: a headless run (no parent session) becomes session-visible ──────


async def test_headless_run_binds_a_chat_session(service, tenant_id, agent_in_db, owner_sessionmaker):
    """A workflow started WITHOUT a parent session (standalone / scheduled /
    admin / heartbeat) must create + bind a ChatSession so the run is visible
    on a session timeline instead of being a silent no-op.

    Revert-sensitive: removing `_ensure_run_session` (or its wiring) leaves the
    run with no parent session → no ChatSession row → assertion fails.
    """
    from app.models.chat_session import ChatSession

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_in_db,
        # NB: no parent_session_id / user_id — the headless case.
    )
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        bound_sessions = (
            (await session.execute(select(ChatSession).where(ChatSession.runtime_task_id == handle.run_id)))
            .scalars()
            .all()
        )
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()

    assert len(bound_sessions) == 1, "headless run must create exactly one bound ChatSession"
    chat = bound_sessions[0]
    assert chat.agent_id == agent_in_db
    assert chat.tenant_id == tenant_id
    assert chat.user_id is not None, "session must resolve a valid owning user (FK)"
    assert chat.session_kind == "workflow"
    # The run is now session-bound, not a silent no-op.
    assert str(task.parent_session_id) == str(chat.id)
    assert task.metadata_json["session_bound"] is True
    assert task.metadata_json["headless_session_created"] is True


async def test_run_with_parent_session_does_not_create_a_new_session(
    service, tenant_id, agent_in_db, owner_sessionmaker
):
    """The existing 'has a parent session' path is preserved: a run started WITH
    a parent session must NOT fabricate a second bound session."""
    from app.models.chat_session import ChatSession

    parent_session_id = uuid.uuid4()
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=_ok_leaf(),
        agent_id=agent_in_db,
        parent_session_id=parent_session_id,
    )
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        bound_sessions = (
            (await session.execute(select(ChatSession).where(ChatSession.runtime_task_id == handle.run_id)))
            .scalars()
            .all()
        )
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()

    assert bound_sessions == [], "a run with a parent session must not create a new bound session"
    assert str(task.parent_session_id) == str(parent_session_id)
    assert task.metadata_json.get("headless_session_created") is not True
