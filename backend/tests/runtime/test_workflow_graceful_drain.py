"""§9 P10 red tests: graceful drain + the external-in-flight reconciliation rule.

Drain stops NEW leaves at the next step boundary and leaves the run
resumable (journal intact). An external/irreversible step caught RUNNING by
a hard stop is NEVER auto-replayed — it is stamped
``unknown_requires_reconciliation`` and the run waits for a human.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowStep
from app.runtime.workflow_engine import GateDecision, LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-drain", slug=f"wd-{tid.hex[:10]}"))
    return tid


def _two_step_definition() -> dict:
    return {
        "name": "drainable",
        "args_schema": {},
        "steps": [
            {"id": "one", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "One"},
            {"id": "two", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Two"},
        ],
    }


async def test_drain_stops_new_leaves_and_leaves_run_resumable(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    calls: list[str] = []

    async def draining_leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request.step_id)
        service.request_drain()  # drain arrives while step one is executing
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id, definition_data=_two_step_definition(), args={}, leaf_executor=draining_leaf
    )

    assert handle.outcome.status == "suspended"
    assert "drain" in (handle.outcome.reason or "")
    assert calls == ["one"], "the in-flight step finishes; NO new leaf starts"

    # Journal intact and the run is crash-equivalent 'running' → reclaimable.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        steps = (
            (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == handle.run_id))).scalars().all()
        )
    assert task.status == "running"
    assert {s.step_id: s.status for s in steps}["one"] == "done"

    # A fresh (post-restart) worker completes it.
    fresh = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    resume_calls: list[str] = []

    async def ok_leaf(request: LeafRequest) -> LeafOutcome:
        resume_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    resumed = await fresh.resume_pending_runs(leaf_executor=ok_leaf)
    statuses = {r.run_id: r.outcome.status for r in resumed}
    assert statuses.get(handle.run_id) == "completed"
    assert resume_calls == ["two"], "journal survived the drain: only step two runs"


async def test_external_in_flight_step_marks_reconciliation_not_replay(tenant_id, owner_sessionmaker):
    """Hard stop with a gated external step mid-execution: the resume path
    must stamp unknown_requires_reconciliation and refuse to re-run it."""
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)

    definition = {
        "name": "external-flow",
        "args_schema": {},
        "steps": [
            {"id": "gate", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send it",
                "effects": "external",
            },
        ],
    }

    class _Approve:
        async def check(self, run_id, step_id, *, reason):
            return GateDecision(approved=True)

    service._gate_decider = _Approve()

    class _HardStop(RuntimeError):
        pass

    async def dying_external_leaf(request: LeafRequest) -> LeafOutcome:
        raise _HardStop("process killed while the email API call was in flight")

    with pytest.raises(_HardStop):
        await service.start_run(
            tenant_id=tenant_id, definition_data=definition, args={}, leaf_executor=dying_external_leaf
        )

    # The journal now holds send=running (side effect unknown).
    fresh = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    replay_calls: list[str] = []

    async def must_not_run(request: LeafRequest) -> LeafOutcome:
        replay_calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    resumed = await fresh.resume_pending_runs(leaf_executor=must_not_run)
    statuses = {r.run_id: r.outcome for r in resumed}

    assert replay_calls == [], "an external in-flight step must NEVER be auto-replayed"
    assert any("reconciliation" in (o.reason or "") for o in statuses.values())

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (await session.execute(select(WorkflowStep))).scalars().all()
        send_rows = [r for r in rows if r.step_id == "send"]
        tasks = (
            (
                await session.execute(
                    select(RuntimeTask).where(RuntimeTask.task_type == "workflow", RuntimeTask.status == "suspended")
                )
            )
            .scalars()
            .all()
        )
    assert any(r.status == "unknown_requires_reconciliation" for r in send_rows)
    assert any((t.metadata_json or {}).get("needs_reconciliation") == ["send"] for t in tasks)
