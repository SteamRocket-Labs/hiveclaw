from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowStep

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="workflow-signal", slug=f"ws-{tid.hex[:10]}"))
    return tid


async def _seed_waiting_run(owner_sessionmaker, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    signal_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="suspended",
                parent_agent_id=agent_id,
                metadata_json={
                    "tenant_id": str(tenant_id),
                    "waiting_for_signal": {"step_id": "wait", "signal_type": "approval_ready"},
                },
            )
        )
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="wait",
                step_type="wait_signal",
                status="suspended",
            )
        )
        session.add(
            CoordinationSignal(
                id=signal_id,
                tenant_id=tenant_id,
                from_agent_id="user",
                to_agent_id=str(agent_id),
                content="approved",
                signal_type="approval_ready",
                thread_id=str(run_id),
            )
        )
    return run_id, agent_id, signal_id


async def test_signal_resume_enumerates_waiting_runs_under_nonowner_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    tenant_id,
):
    from app.runtime.workflow_engine import WorkflowRunOutcome
    from app.services.workflow_signal_consumer import drain_signal_resumes

    run_id, _agent_id, signal_id = await _seed_waiting_run(owner_sessionmaker, tenant_id)
    resumed: list[tuple[uuid.UUID, uuid.UUID]] = []

    class _FakeWorkflowRuntimeService:
        async def resume_run(self, run_id_arg, *, tenant_id, leaf_executor):
            resumed.append((run_id_arg, tenant_id))
            return WorkflowRunOutcome(status="completed")

    async def leaf_executor(_request):
        raise AssertionError("signal consumer should delegate resume to the runtime service")

    result = await drain_signal_resumes(
        leaf_executor=leaf_executor,
        session_factory=app_user_sessionmaker,
        service=_FakeWorkflowRuntimeService(),
    )

    assert [(item.run_id, item.signal_id) for item in result] == [(run_id, signal_id)]
    assert resumed == [(run_id, tenant_id)]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        step = (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalar_one()
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        signal = (await session.execute(select(CoordinationSignal).where(CoordinationSignal.id == signal_id))).scalar_one_or_none()

    assert step.status == "done"
    assert "approved" in (step.result_ref or "")
    assert "waiting_for_signal" not in (task.metadata_json or {})
    assert signal is None
