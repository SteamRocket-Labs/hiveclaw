from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_ops import WorkflowOpsConflict, WorkflowOpsService
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "ops-two-step",
        "args_schema": {"target": {"type": "string", "required": True}},
        "default_budget": {"max_total_tokens": 80_000},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan {{args.target}}",
            },
            {
                "id": "write",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Write {{steps.scan.output}}",
            },
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-ops", slug=f"wo-{tid.hex[:10]}"))
    return tid


async def _start_run(owner_sessionmaker, tenant_id: uuid.UUID):
    calls: list[LeafRequest] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request)
        return LeafOutcome(ok=True, output={"step": request.step_id}, tokens_used=123)

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=leaf,
    )
    return handle, calls


async def test_ops_inspect_export_cancel_and_force_suspend(owner_sessionmaker, tenant_id):
    handle, _calls = await _start_run(owner_sessionmaker, tenant_id)
    ops = WorkflowOpsService(session_factory=owner_sessionmaker)

    inspected = await ops.inspect_run(handle.run_id, tenant_id=tenant_id)
    assert inspected["run_id"] == str(handle.run_id)
    assert inspected["status"] == "completed"
    assert {step["step_id"] for step in inspected["steps"]} == {"scan", "write"}
    assert inspected["quota"]["allocated_tokens"] == 80_000

    exported = await ops.export_journal(handle.run_id, tenant_id=tenant_id)
    assert exported["run"]["run_id"] == str(handle.run_id)
    assert {step["step_id"] for step in exported["steps"]} == {"scan", "write"}
    assert exported["leaf_calls"] == [], "plain agent_step runs have step journal rows, not fanout leaf rows"

    cancelled = await ops.cancel_run(handle.run_id, tenant_id=tenant_id, reason="operator requested")
    assert cancelled["status"] == "killed"

    suspended = await ops.force_suspend_run(handle.run_id, tenant_id=tenant_id, reason="manual audit")
    assert suspended["status"] == "suspended"
    assert suspended["reason"] == "manual audit"


async def test_ops_replay_from_step_deletes_target_and_downstream_journal(owner_sessionmaker, tenant_id):
    handle, _calls = await _start_run(owner_sessionmaker, tenant_id)
    ops = WorkflowOpsService(session_factory=owner_sessionmaker)

    replay = await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="write", reason="bad draft")

    assert replay["status"] == "running"
    assert replay["replay_from_step"] == "write"
    inspected = await ops.inspect_run(handle.run_id, tenant_id=tenant_id)
    assert [step["step_id"] for step in inspected["steps"]] == ["scan"]


async def test_ops_replay_requires_quiescent_run(owner_sessionmaker, tenant_id):
    """Replay is destructive journal surgery. It must not run while another
    worker can still be writing step/leaf rows."""
    from sqlalchemy import select

    from app.models.runtime_task import RuntimeTask
    from app.models.workflow import WorkflowStep

    handle, _calls = await _start_run(owner_sessionmaker, tenant_id)
    ops = WorkflowOpsService(session_factory=owner_sessionmaker)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "running"

    with pytest.raises(WorkflowOpsConflict, match="still running"):
        await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="write", reason="unsafe")

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        task.status = "suspended"
        step = (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id, WorkflowStep.step_id == "write")
            )
        ).scalar_one()
        step.status = "running"

    with pytest.raises(WorkflowOpsConflict, match="in-flight journal"):
        await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="write", reason="unsafe")


async def test_force_suspend_mid_run_stops_at_next_step_boundary(owner_sessionmaker, tenant_id):
    """Admin force-suspend during execution must take effect at the NEXT step
    boundary (it cannot interrupt the in-flight leaf): the second step never
    runs, and the run STAYS suspended — not overwritten by completed/failed."""
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    ops = WorkflowOpsService(session_factory=owner_sessionmaker)
    executed: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        executed.append(request.step_id)
        if len(executed) == 1:
            await ops.force_suspend_run(request.run_id, tenant_id=request.tenant_id, reason="manual reconciliation")
        return LeafOutcome(ok=True, output={"step": request.step_id}, tokens_used=10)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={"target": "acme"},
        leaf_executor=leaf,
    )

    assert executed == ["scan"], "the write step must NOT start after a mid-run force-suspend"
    assert handle.outcome.status == "suspended"

    loaded = await service.load_run(handle.run_id, tenant_id=tenant_id)
    assert loaded is not None
    assert loaded.task.status == "suspended", "the operator's suspended state must survive run finalisation"

    # The operator can resume after reconciliation: only the remaining step runs.
    resumed: list[str] = []

    async def resume_leaf(request: LeafRequest) -> LeafOutcome:
        resumed.append(request.step_id)
        return LeafOutcome(ok=True, output={"step": request.step_id}, tokens_used=10)

    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=resume_leaf)
    assert outcome.status == "completed"
    assert resumed == ["write"]


async def test_admin_ops_write_fail_soft_audit_trail(owner_sessionmaker, tenant_id, monkeypatch):
    """Admin cancel / force-suspend / replay are destructive operator actions
    (replay even deletes journal rows) — they must land on the same audit
    trail as the P9 run lifecycle, fail-soft."""
    import app.services.audit_logger as audit_logger_module

    captured: list[tuple[str, dict]] = []

    async def capturing_audit(action, details=None, agent_id=None, user_id=None):
        captured.append((action, details or {}))

    monkeypatch.setattr(audit_logger_module, "write_audit_log", capturing_audit)

    handle, _calls = await _start_run(owner_sessionmaker, tenant_id)
    ops = WorkflowOpsService(session_factory=owner_sessionmaker)

    await ops.cancel_run(handle.run_id, tenant_id=tenant_id, reason="operator cancel")
    await ops.force_suspend_run(handle.run_id, tenant_id=tenant_id, reason="manual reconciliation")
    await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="write", reason="bad artifact")

    by_action = {action: details for action, details in captured}
    assert "workflow_admin_cancelled" in by_action
    assert "workflow_admin_force_suspended" in by_action
    assert "workflow_admin_replay_from_step" in by_action
    for action in ("workflow_admin_cancelled", "workflow_admin_force_suspended", "workflow_admin_replay_from_step"):
        details = by_action[action]
        assert details["tenant_id"] == str(tenant_id)
        assert details["run_id"] == str(handle.run_id)
        assert details["reason"]
    assert by_action["workflow_admin_replay_from_step"]["replay_from_step"] == "write"


def _fanout_definition() -> dict:
    return {
        "name": "ops-fanout",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "default_budget": {"max_total_tokens": 80_000},
        "steps": [
            {
                "id": "scan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 2,
            },
            {
                "id": "write",
                "type": "agent_step",
                "leaf": {"name": "writer", "type": "worker"},
                "task": "Write {{steps.scan.output}}",
            },
        ],
    }


async def test_replay_from_step_refunds_journaled_leaf_consumption(owner_sessionmaker, tenant_id):
    """Replay deletes leaf journal rows whose token_usage was already settled
    into the quota — without a refund the rerun double-charges and can wedge
    the run on ``quota``. Refund exactly what the deleted rows had metered."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowQuota

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={"step": request.step_id}, tokens_used=100)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fanout_definition(),
        args={"targets": ["a", "b", "c"]},
        leaf_executor=leaf,
    )
    assert handle.outcome.status == "completed"

    async def quota_consumed() -> int:
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            quota = (
                await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))
            ).scalar_one()
            return quota.consumed_tokens

    consumed_before = await quota_consumed()
    assert consumed_before == 400  # 3 fanout leaves + 1 agent step, 100 each

    ops = WorkflowOpsService(session_factory=owner_sessionmaker)
    await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="scan", reason="refund check")

    # The 3 deleted scan leaf rows (300 tokens) are refunded; the write step has
    # no leaf-level metering rows, so its 100 tokens legitimately remain.
    assert await quota_consumed() == 100

    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=leaf)
    assert outcome.status == "completed"


async def test_replay_refuses_to_sweep_unreconciled_external_steps(owner_sessionmaker, tenant_id):
    """P10 invariant carried into admin surgery: a step parked as
    ``unknown_requires_reconciliation`` (external effect, in-flight at crash,
    outcome unknown) must NOT be silently re-executed by replay. Without this
    guard the anchor row is deleted, the gate's persisted checkpoint approval
    still stands, and resume re-fires the external send."""
    from app.agents.coordination import CoordinationRuntime
    from app.models.workflow import WorkflowStep
    from app.services.workflow_runtime_service import CheckpointGateDecider

    service = WorkflowRuntimeService(
        session_factory=owner_sessionmaker,
        gate_decider=CheckpointGateDecider(CoordinationRuntime()),
    )
    sent: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        if request.step_id == "send":
            sent.append(request.task)
        return LeafOutcome(ok=True, output={"ok": True}, tokens_used=1)

    external_def = {
        "name": "ops-external",
        "args_schema": {},
        "steps": [
            {"id": "draft", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Draft"},
            {"id": "approve", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "s", "type": "worker"},
                "task": "Send externally",
                "effects": "external",
            },
        ],
    }
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=external_def, args={}, leaf_executor=leaf
    )
    assert handle.outcome.status == "suspended"
    assert service.gate_decider.approve(str(handle.run_id), "approve") is True

    # Simulate the P10 crash aftermath: gate done, send parked un-reconciled.
    from sqlalchemy import select as _select

    from app.models.runtime_task import RuntimeTask as _RT

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(_select(_RT).where(_RT.id == handle.run_id))).scalar_one()
        task.status = "suspended"
        dh = (task.metadata_json or {}).get("definition_hash")
        approve_row = (
            await session.execute(
                _select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id, WorkflowStep.step_id == "approve")
            )
        ).scalar_one()
        approve_row.status = "done"
        session.add(
            WorkflowStep(
                id=uuid.uuid4(),
                run_id=handle.run_id,
                step_id="send",
                step_type="agent_step",
                status="unknown_requires_reconciliation",
                definition_hash=dh,
            )
        )

    ops = WorkflowOpsService(session_factory=owner_sessionmaker)
    # Both sweep shapes must refuse: replaying the step itself AND a range
    # from upstream that would sweep it.
    with pytest.raises(WorkflowOpsConflict, match="unreconciled"):
        await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="send", reason="redo send")
    with pytest.raises(WorkflowOpsConflict, match="unreconciled"):
        await ops.replay_from_step(handle.run_id, tenant_id=tenant_id, step_id="draft", reason="redo all")

    # The anchor row survives and nothing external re-fired.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        anchor = (
            await session.execute(
                _select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id, WorkflowStep.step_id == "send")
            )
        ).scalar_one()
    assert anchor.status == "unknown_requires_reconciliation"
    assert sent == []
