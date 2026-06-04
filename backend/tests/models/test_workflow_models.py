"""§9 P1 red tests: workflow ORM models against the real migrated schema.

Inserting through the ORM into the migrated tables is itself the
schema-drift test: if models/workflow.py and add_workflow_tables_0604
disagree on any column, these inserts blow up. All sessions go through
``tenant_scoped_session`` because the tables are FORCEd — a bare session
could neither read nor write them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowDefinitionRecord, WorkflowLeafCall, WorkflowQuota, WorkflowStep


@pytest.fixture()
async def tenant_id(owner_sessionmaker, migrated_pg_url) -> uuid.UUID:
    """Seed a tenant through the ORM so Python-side column defaults apply
    (tenants has several NOT NULL columns without server defaults)."""
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="model-test", slug=f"mt-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def run_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    """A workflow run IS a RuntimeTask(task_type='workflow') — §3.3.

    runtime_tasks has no tenant column (run metadata carries the mirror), so
    a plain session works for it."""
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = RuntimeTask(
            task_type="workflow",
            status="running",
            metadata_json={
                "definition_source": "ephemeral",
                "definition_hash": "hash-x",
                "args_hash": "args-x",
                "tenant_id": str(tenant_id),
            },
        )
        session.add(task)
        await session.flush()
        rid = task.id
    return rid


async def test_definition_version_immutable_unique(owner_sessionmaker, tenant_id):
    """Same (tenant, name, version) twice → IntegrityError: content changes
    MUST mint a new version, never overwrite an existing one."""
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowDefinitionRecord(
                tenant_id=tenant_id,
                name="weekly-report",
                definition_version=1,
                definition_hash="h1",
                definition_json={"steps": []},
                status="active",
            )
        )

    with pytest.raises(IntegrityError):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(
                WorkflowDefinitionRecord(
                    tenant_id=tenant_id,
                    name="weekly-report",
                    definition_version=1,
                    definition_hash="h1-CHANGED",
                    definition_json={"steps": ["x"]},
                )
            )

    # A NEW version of the same name is fine.
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowDefinitionRecord(
                tenant_id=tenant_id,
                name="weekly-report",
                definition_version=2,
                definition_hash="h2",
                definition_json={"steps": ["x"]},
            )
        )


async def test_definition_defaults(owner_sessionmaker, tenant_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        record = WorkflowDefinitionRecord(
            tenant_id=tenant_id,
            name="defaults",
            definition_hash="h",
            definition_json={},
        )
        session.add(record)
        await session.flush()
        assert record.definition_version == 1
        assert record.status == "draft"
        assert record.visibility_scope == "agent"
        assert record.owner_type == "user"


async def test_step_unique_per_run(owner_sessionmaker, tenant_id, run_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(WorkflowStep(tenant_id=tenant_id, run_id=run_id, step_id="s1", status="done"))

    with pytest.raises(IntegrityError):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(WorkflowStep(tenant_id=tenant_id, run_id=run_id, step_id="s1"))


async def test_leaf_call_unique_per_step_and_idempotency(owner_sessionmaker, tenant_id, run_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            WorkflowLeafCall(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="fan1",
                leaf_id="item-0",
                idempotency_key="k-0",
                status="done",
                token_usage={"total": 42},
            )
        )

    with pytest.raises(IntegrityError):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(WorkflowLeafCall(tenant_id=tenant_id, run_id=run_id, step_id="fan1", leaf_id="item-0"))

    with pytest.raises(IntegrityError):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(
                WorkflowLeafCall(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    step_id="fan1",
                    leaf_id="item-1",
                    idempotency_key="k-0",
                )
            )


async def test_quota_single_row_per_run(owner_sessionmaker, tenant_id, run_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(WorkflowQuota(tenant_id=tenant_id, run_id=run_id, allocated_tokens=100_000))

    with pytest.raises(IntegrityError):
        async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
            session.add(WorkflowQuota(tenant_id=tenant_id, run_id=run_id, allocated_tokens=1))


async def test_orm_reads_are_tenant_scoped(app_user_sessionmaker, tenant_id, run_id):
    """tenant_scoped_session + ORM obeys RLS end-to-end on a NON-superuser
    connection (the only kind RLS filters — production must switch to one,
    P15): another tenant's scoped session cannot read this tenant's rows."""
    from sqlalchemy import select

    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        session.add(WorkflowStep(tenant_id=tenant_id, run_id=run_id, step_id="visible", status="done"))

    other_tenant = str(uuid.uuid4())
    async with tenant_scoped_session(other_tenant, session_factory=app_user_sessionmaker) as session:
        rows = (await session.execute(select(WorkflowStep).where(WorkflowStep.step_id == "visible"))).scalars().all()
    assert rows == []

    async with tenant_scoped_session(str(tenant_id), session_factory=app_user_sessionmaker) as session:
        rows = (await session.execute(select(WorkflowStep).where(WorkflowStep.step_id == "visible"))).scalars().all()
    assert len(rows) == 1
