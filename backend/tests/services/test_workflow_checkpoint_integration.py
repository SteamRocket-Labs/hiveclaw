"""§9 P7 red tests: gate ↔ CoordinationCheckpoint + wait ↔ scheduled resume,
end-to-end on real PG.

The full human-in-the-loop loop: external step suspends behind a checkpoint
(leaf untouched) → approval flips the checkpoint → explicit resume executes
the step. Time suspensions write a resume_at scheduling record; the startup
scan resumes them only once due — and never touches gate suspensions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.coordination import CoordinationRuntime
from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import CheckpointGateDecider, WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _gated_definition() -> dict:
    return {
        "name": "approval-flow",
        "args_schema": {},
        "steps": [
            {
                "id": "draft",
                "type": "agent_step",
                "leaf": {"name": "drafter", "type": "worker"},
                "task": "Draft it",
            },
            {"id": "approve", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send it",
                "effects": "external",
            },
        ],
    }


def _wait_definition(delay_seconds: int) -> dict:
    return {
        "name": "timed-flow",
        "args_schema": {},
        "steps": [
            {"id": "wait", "type": "wait_until_step", "delay_seconds": delay_seconds},
            {
                "id": "after",
                "type": "agent_step",
                "leaf": {"name": "after", "type": "worker"},
                "task": "After the wait",
            },
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-gate", slug=f"wg-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowRuntimeService:
    # A fresh in-process coordination runtime per test keeps checkpoints isolated.
    return WorkflowRuntimeService(
        session_factory=owner_sessionmaker,
        gate_decider=CheckpointGateDecider(CoordinationRuntime()),
    )


def _leaf():
    calls: list[str] = []

    async def executor(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        return LeafOutcome(ok=True, output={"echo": request.task}, tokens_used=1)

    return executor, calls


async def test_external_step_suspends_until_checkpoint_approved(service, tenant_id):
    leaf, calls = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_gated_definition(), args={}, leaf_executor=leaf
    )

    assert handle.outcome.status == "suspended"
    assert calls == ["draft"], "the external leaf must NOT run while the gate is pending"

    # Approval + EXPLICIT resume executes exactly the remaining step.
    assert service.gate_decider.approve(str(handle.run_id), "approve") is True
    leaf2, calls2 = _leaf()
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=leaf2)

    assert outcome.status == "completed"
    assert calls2 == ["send"], "draft replays from journal; only the gated step executes"


async def test_rejected_checkpoint_fails_run(service, tenant_id):
    leaf, _ = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_gated_definition(), args={}, leaf_executor=leaf
    )
    assert handle.outcome.status == "suspended"

    service.gate_decider.reject(str(handle.run_id), "approve")
    leaf2, calls2 = _leaf()
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=leaf2)

    assert outcome.status == "failed"
    assert calls2 == [], "a rejected gate must never execute the protected step"


async def test_gate_suspension_not_picked_by_startup_scan(service, tenant_id):
    leaf, _ = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_gated_definition(), args={}, leaf_executor=leaf
    )
    assert handle.outcome.status == "suspended"

    resumed = await service.resume_pending_runs(leaf_executor=_leaf()[0])
    assert handle.run_id not in [r.run_id for r in resumed], (
        "gate suspensions wait for approval — the time-based startup scan must not touch them"
    )


async def test_wait_records_resume_at_and_startup_scan_honours_due_time(service, tenant_id, owner_sessionmaker):
    leaf, calls = _leaf()
    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_wait_definition(delay_seconds=600), args={}, leaf_executor=leaf
    )
    assert handle.outcome.status == "suspended"
    assert calls == []

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    resume_at_raw = (task.metadata_json or {}).get("resume_at")
    assert resume_at_raw, "the wait must leave an equivalent scheduling record (resume_at)"

    # Not due yet → the scan skips it.
    resumed = await service.resume_pending_runs(leaf_executor=_leaf()[0])
    assert handle.run_id not in [r.run_id for r in resumed]

    # Time-travel the scheduling record into the past → the scan resumes it.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        metadata = dict(task.metadata_json or {})
        metadata["resume_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        task.metadata_json = metadata

    # Also time-travel the JOURNALED wait target (fixed on first suspension —
    # recomputation would push it forever forward, the bug this pins).
    from app.models.workflow import WorkflowStep

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        step_row = (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id, WorkflowStep.step_id == "wait")
            )
        ).scalar_one()
        step_row.input_hash = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    leaf3, calls3 = _leaf()
    resumed = await service.resume_pending_runs(leaf_executor=leaf3)
    statuses = {r.run_id: r.outcome.status for r in resumed}
    assert statuses.get(handle.run_id) == "completed", "the due run must be resumed and land"
    assert calls3 == ["after"], "only the post-wait step executes"
