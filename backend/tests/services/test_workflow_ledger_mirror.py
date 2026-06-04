"""§9 P9 red tests: step → ledger todo mirror (one-way) on real PG.

§10 invariant ④: journal ≠ ledger. The mirror writes ledger todos as steps
move; mutating the ledger NEVER changes what the engine does next.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import get_settings
from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.agent_work_ledger import load_agent_work_ledger, upsert_agent_work_ledger_todo
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "mirrored",
        "args_schema": {},
        "steps": [
            {"id": "one", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "One"},
            {"id": "two", "type": "agent_step", "leaf": {"name": "w", "type": "worker"}, "task": "Two"},
        ],
    }


@pytest.fixture()
def ledger_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-mirror", slug=f"wm-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
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
                display_name="Mirror Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="mirror-agent", role_description="m", creator_id=uid))
    return aid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowRuntimeService:
    return WorkflowRuntimeService(session_factory=owner_sessionmaker)


def _ok_leaf(calls: list[str] | None = None):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        if calls is not None:
            calls.append(request.step_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    return leaf


async def test_run_creates_ledger_artifact_and_mirrors_steps(ledger_root, service, tenant_id, agent_id):
    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={},
        agent_id=agent_id,
        leaf_executor=_ok_leaf(),
    )
    assert handle.outcome.status == "completed"

    ledger = load_agent_work_ledger(agent_id=agent_id, runtime_task_id=handle.run_id)
    assert ledger is not None, "run start must initialize the ledger artifact"
    todos = {item["title"]: item["status"] for item in ledger.get("todo_items", [])}
    assert todos.get("workflow step: one") == "completed"
    assert todos.get("workflow step: two") == "completed"


async def test_ledger_mutation_never_steers_the_engine(ledger_root, service, tenant_id, agent_id):
    """认知≠治理: flip a ledger todo to 'completed' mid-run — the engine
    still executes the step (it reads only its OWN journal)."""
    first_calls: list[str] = []
    kill = {"armed": True}

    async def killing_leaf(request: LeafRequest) -> LeafOutcome:
        first_calls.append(request.step_id)
        if kill["armed"]:
            kill["armed"] = False
            await service.kill_run(request.run_id, tenant_id=request.tenant_id)
        return LeafOutcome(ok=True, output={}, tokens_used=1)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_definition(),
        args={},
        agent_id=agent_id,
        leaf_executor=killing_leaf,
    )
    assert handle.outcome.status == "killed"
    assert first_calls == ["one"]

    # Tamper with the OBSERVATION surface: mark the unexecuted step done.
    upsert_agent_work_ledger_todo(
        agent_id=agent_id,
        title="workflow step: two",
        status="completed",
        runtime_task_id=handle.run_id,
    )

    resume_calls: list[str] = []
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=_ok_leaf(resume_calls))

    assert outcome.status == "completed"
    assert resume_calls == ["two"], "the ledger lie must NOT stop the engine from running step two"
