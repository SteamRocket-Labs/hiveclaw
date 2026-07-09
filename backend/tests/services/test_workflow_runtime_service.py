"""§9 P3 red tests: WorkflowRuntimeService against real PostgreSQL.

kill-resume + journal persistence MUST run on real PG (route principle);
the fake/injected leaf executor is only for isolating control flow.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowQuota, WorkflowStep
from app.runtime.dynamic_workflow import build_dynamic_workflow_run_metadata
from app.runtime.workflow_definition import compute_definition_hash
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService
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
    return tid


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


async def test_startup_resume_picks_up_running_run(service, tenant_id, owner_sessionmaker):
    """A run left in 'running' (e.g. process crash) is picked up by the
    startup scan and driven to completion."""
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
    statuses = {r.run_id: r.outcome.status for r in resumed}
    assert "completed" in statuses.values()


async def test_startup_resume_picks_up_running_run_under_nonowner_rls(
    tenant_id, owner_sessionmaker, app_user_sessionmaker
):
    """The production app role is non-owner. Startup resume must use an audited
    discovery scan, then resume each run inside its tenant scope.
    """
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

    assert [item.outcome.status for item in resumed] == ["completed"]


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

    # promote the first run → provenance must surface in the listing
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
    assert by_id[first.run_id].promoted_definition_id == record.id
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
    from app.services import agent_session_continuation

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

    captured: list[dict] = []

    async def fake_continue_parent_session_with_task_notification(**kwargs):
        captured.append(kwargs)
        return {"ok": True, "status": "queued"}

    monkeypatch.setattr(
        agent_session_continuation,
        "continue_parent_session_with_task_notification",
        fake_continue_parent_session_with_task_notification,
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
    assert len(captured) == 1
    wake = captured[0]
    assert wake["task_id"] == str(handle.run_id)
    assert wake["task_type"] == "workflow"
    assert wake["status"] == "completed"
    assert wake["source"] == "workflow"
    assert wake["session"].id == parent_session_id
    assert "Workflow run" in wake["summary"]
    assert "scan" in wake["summary"]
    assert "report" in wake["summary"]


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
