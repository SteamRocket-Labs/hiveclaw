"""§9 P10 red tests: a fresh worker reclaims a dead worker's unfinished runs.

Worker A dies mid-run (crash leaves the run 'running'); worker B — a brand
new service instance, the cross-worker stand-in — scans and drives the run
to completion off the shared PG journal.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "restartable",
        "args_schema": {},
        "steps": [
            {"id": "one", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "One"},
            {"id": "two", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Two"},
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-restart", slug=f"wr-{tid.hex[:10]}"))
    return tid


async def test_new_worker_resumes_after_crash(tenant_id, owner_sessionmaker):
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
    statuses = {r.outcome.status for r in resumed}

    assert "completed" in statuses
    assert "two" in resume_calls, "the crashed step re-runs on the new worker"
    assert "one" not in resume_calls, "journaled done steps replay, never re-execute"
